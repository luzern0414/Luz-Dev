"""结构化日志（references/config-logging.md §3，OBS-001、OBS-005~008）：一行一条 JSON 写 stdout，经队列异步输出。

字段：`ts`（UTC，毫秒，结尾 Z）、`level`、`logger`、`service`、`env`、`version`、`instance`、`config_version`、`event`、
`message`、`trace_id`、`span_id`、`request_id`、`user_ref`、`store_id`、`outcome`、`reason`、`error.type` / `error.message` /
`error.stack`，以及 `log_event` 传入或 `bind_log_fields` 绑定的业务字段（`order_no`、`job`、`run_id`……）。
安全事件（`security.*`）自动带 `category=security`，且不受日志级别配置影响（合规日志不能被整体关闭）。

- 脱敏在格式化前统一做：字段名黑名单（password、token、secret、authorization、cookie、sign、cvv、card_no / bank_card、
  api_key、private_key、session_id / session_key、sms_code……）整值打码；其余字符串再过正则（手机号、Bearer 令牌、JWT、
  证件号、`password=…` 一类键值）。会员卡编号（member_card_id）等业务编号不是银行卡号，不打码。
- 防注入：只输出 JSON；控制字符、回车换行与行分隔符转成可见转义；字符串字段截断到 512 字，堆栈截断到 2000 字。
- 不阻塞：QueueHandler + QueueListener，队列有上限；积压到九成后丢 DEBUG / INFO 并计 `luz_log_dropped_total`；
  WARNING 以上与安全事件不丢（队列满时在调用线程直接写出）。日志调用本身的异常吞掉并计 `luz_log_errors_total`；
  生产设 `logging.raiseExceptions = False`。
- 库代码只 `getLogger(__name__)` 并调用 `log_event`；处理器只由进程入口的 `configure_logging` 安装。
"""
from __future__ import annotations

import atexit
import copy
import json
import logging
import queue
import re
import sys
import threading
import time
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from logging.handlers import QueueHandler, QueueListener
from typing import Any

from .log_events import LOG_FORMAT_FAILED, is_security

trace_id_var: ContextVar[str | None] = ContextVar("trace_id", default=None)
span_id_var: ContextVar[str | None] = ContextVar("span_id", default=None)
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
user_id_var: ContextVar[str | None] = ContextVar("user_id", default=None)  # 输出为 user_ref：用户内部编号，不放手机号或 openid
user_ref_var = user_id_var
store_id_var: ContextVar[str | None] = ContextVar("store_id", default=None)
config_version_var: ContextVar[str | None] = ContextVar("config_version", default=None)
fields_var: ContextVar[dict[str, Any] | None] = ContextVar("log_fields", default=None)

MAX_FIELD_CHARS = 512
MAX_STACK_CHARS = 2000
MAX_DEPTH = 4
MAX_ITEMS = 50
MASK = "***"
SOFT_LIMIT = 0.9  # 队列积压到九成后只收 WARNING 以上与安全事件

# 调用方传入也不能覆盖的上下文字段（防止伪造 trace、版本、分类）
RESERVED = frozenset({"ts", "level", "logger", "service", "env", "version", "instance", "config_version", "trace_id",
                      "span_id", "request_id", "category"})

_static: dict[str, str | None] = {"service": None, "env": None, "version": None, "instance": None, "config_version": None}
_counter_lock = threading.Lock()
_counters = {"dropped": 0, "errors": 0}


def _count(name: str) -> None:
    with _counter_lock:
        _counters[name] += 1


def dropped_total() -> int:
    return _counters["dropped"]


def errors_total() -> int:
    return _counters["errors"]


def metrics_lines() -> list[str]:
    return ["# TYPE luz_log_dropped_total counter", f"luz_log_dropped_total {dropped_total()}",
            "# TYPE luz_log_errors_total counter", f"luz_log_errors_total {errors_total()}"]


def set_static(**values: str | None) -> None:
    """进程级字段：service、env、version、instance、config_version（配置加载后更新）。"""
    for key, value in values.items():
        if key not in _static:
            raise KeyError(key)
        _static[key] = None if value is None else str(value)


def static_fields() -> dict[str, str | None]:
    return dict(_static)


# ---------- 脱敏 ----------

_PHONE = re.compile(r"(?<!\d)(1[3-9]\d)(\d{4})(\d{4})(?!\d)")
_BEARER = re.compile(r"(Bearer\s+)[A-Za-z0-9\-_.~+/=]+", re.IGNORECASE)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}")
_ID_CARD = re.compile(r"(?<!\d)(\d{6})\d{8}(\d{3}[\dXx])(?!\d)")
_KV_SECRET = re.compile(
    r"(?i)\b(password|passwd|pwd|token|access_token|refresh_token|secret|client_secret|api_?key|sign|signature|"
    r"session_key|sms_code|verify_code)(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s&,;\"']+)")

_SENSITIVE_WORDS = frozenset({
    "password", "passwd", "pwd", "passphrase", "passcode", "token", "secret", "authorization", "cookie", "cookies",
    "sign", "signature", "cvv", "cvc", "cvn", "otp", "captcha", "credential", "credentials", "apikey", "privatekey",
    "sessionid", "sessionkey", "cardno", "pan",
})
_SENSITIVE_PAIRS = frozenset({
    "api_key", "private_key", "secret_key", "access_key", "session_id", "session_key", "sms_code", "verify_code",
    "card_no", "card_number", "bank_card", "bank_account", "id_card", "id_number", "set_cookie",
})
_COUNT_WORDS = frozenset({"count", "total", "ttl", "len", "length", "size", "limit", "max", "min", "expires", "used"})


def _segments(name: str) -> list[str]:
    snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(name)).lower()
    return [part for part in re.split(r"[^a-z0-9]+", snake) if part]


def is_sensitive_key(name: str, value: Any = None) -> bool:
    """字段名是否在黑名单里（按词切分，不按子串：design、assignee 不算 sign）。

    数值型的计数与时长（token_count、token_ttl_s）不是秘密，照常输出。
    """
    parts = _segments(name)
    if not parts:
        return False
    hit = any(p in _SENSITIVE_WORDS for p in parts) or any(f"{a}_{b}" in _SENSITIVE_PAIRS for a, b in zip(parts, parts[1:]))
    if not hit:
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool) and _COUNT_WORDS & set(parts):
        return False
    return True


def redact(text: str) -> str:
    text = _BEARER.sub(r"\1***", text)
    text = _JWT.sub(MASK, text)
    text = _KV_SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", text)
    text = _PHONE.sub(r"\1****\3", text)
    text = _ID_CARD.sub(r"\1********\2", text)
    return text


_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_ESCAPES = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _escape_controls(text: str) -> str:
    return _CONTROL.sub(lambda m: _ESCAPES.get(m.group(), f"\\u{ord(m.group()):04x}"), text)


def clean_text(text: str, limit: int = MAX_FIELD_CHARS) -> str:
    """脱敏 → 转义控制字符 → 截断（用户输入可能出现在任何字符串字段里）。"""
    text = _escape_controls(redact(text))
    return text if len(text) <= limit else text[:limit] + "…"


def sanitize(value: Any, depth: int = 0) -> Any:
    """把任意值变成可安全写进日志的 JSON 值：字符串清洗、按字段名打码、限制嵌套深度与元素个数。"""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    if depth >= MAX_DEPTH:
        return "<nested>"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_ITEMS:
                out["…"] = f"+{len(value) - MAX_ITEMS} more"
                break
            name = clean_text(str(key), 64)
            out[name] = MASK if is_sensitive_key(name, item) else sanitize(item, depth + 1)
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        out_list = [sanitize(item, depth + 1) for item in items[:MAX_ITEMS]]
        if len(items) > MAX_ITEMS:
            out_list.append(f"+{len(items) - MAX_ITEMS} more")
        return out_list
    return clean_text(str(value))


# ---------- 上下文 ----------

def capture_context() -> dict[str, Any]:
    return {"trace_id": trace_id_var.get(), "span_id": span_id_var.get(), "request_id": request_id_var.get(),
            "user_ref": user_id_var.get(), "store_id": store_id_var.get(), "config_version": config_version_var.get(),
            "fields": fields_var.get()}


@contextmanager
def bind_log_fields(**fields: Any) -> Iterator[None]:
    """在一段代码里的每条日志都带上这些字段（job、run_id、topic、event_id……）。"""
    current = fields_var.get() or {}
    token = fields_var.set({**current, **fields} if fields else current)
    try:
        yield
    finally:
        fields_var.reset(token)


def _error(exc_info: Any) -> dict[str, str] | None:
    if exc_info is True:
        exc_info = sys.exc_info()
    elif isinstance(exc_info, BaseException):
        exc_info = (type(exc_info), exc_info, exc_info.__traceback__)
    if not exc_info or exc_info[0] is None:
        return None
    exc_type, exc, tb = exc_info
    stack = "".join(traceback.format_exception(exc_type, exc, tb))
    return {"error.type": exc_type.__name__, "error.message": clean_text(str(exc)),
            "error.stack": redact(stack)[-MAX_STACK_CHARS:]}


def _timestamp(created: float, msecs: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(created)) + f".{min(int(msecs), 999):03d}Z"


class JsonFormatter(logging.Formatter):
    """一行一条 JSON。格式化失败时输出一条最小的错误行并计数，不向调用方抛出。"""

    def format(self, record: logging.LogRecord) -> str:
        try:
            return self._format(record)
        except Exception:  # noqa: BLE001
            _count("errors")
            return json.dumps({"ts": _timestamp(record.created, record.msecs), "level": "error",
                               "logger": "luzback.logging", "event": LOG_FORMAT_FAILED,
                               "message": "log record could not be formatted", "source_logger": str(record.name)[:100]})

    def _format(self, record: logging.LogRecord) -> str:
        ctx = getattr(record, "luz_ctx", None) or capture_context()
        fields: dict[str, Any] = dict(ctx.get("fields") or {})
        fields.update(getattr(record, "fields", None) or {})
        event = fields.pop("event", None)
        message = record.message if hasattr(record, "luz_ctx") else record.getMessage()
        payload: dict[str, Any] = {
            "ts": _timestamp(record.created, record.msecs),
            "level": record.levelname.lower(),
            "logger": record.name,
            "service": _static["service"], "env": _static["env"], "version": _static["version"],
            "instance": _static["instance"],
            "config_version": ctx.get("config_version") or _static["config_version"],
            "event": clean_text(str(event), 64) if event else None,
            "message": clean_text(str(message)),
            "trace_id": ctx.get("trace_id"), "span_id": ctx.get("span_id"), "request_id": ctx.get("request_id"),
            "user_ref": ctx.get("user_ref"), "store_id": ctx.get("store_id"),
        }
        if event and is_security(str(event)):
            payload["category"] = "security"
        for key, value in fields.items():
            name = str(key)
            if name in RESERVED:
                continue
            payload[name] = MASK if is_sensitive_key(name, value) else sanitize(value)
        error = getattr(record, "luz_error", None) or (_error(record.exc_info) if record.exc_info else None)
        if error:
            payload.update(error)
        return json.dumps({k: v for k, v in payload.items() if v is not None}, ensure_ascii=False, default=str)


def _keep(record: logging.LogRecord) -> bool:
    if record.levelno >= logging.WARNING:
        return True
    return is_security((getattr(record, "fields", None) or {}).get("event"))


class _SafeStreamHandler(logging.StreamHandler):
    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802 - 写出失败不打印 traceback，只计数
        _count("errors")


class _BoundedQueueHandler(QueueHandler):
    """在调用线程取上下文与格式化消息（监听线程没有 contextvars），JSON 序列化与写出在监听线程。"""

    def __init__(self, q: queue.Queue, target: logging.Handler, capacity: int):
        super().__init__(q)
        self.target = target
        self.soft_limit = max(1, int(capacity * SOFT_LIMIT))

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        record = copy.copy(record)
        record.luz_ctx = capture_context()
        record.message = record.getMessage()
        record.msg = record.message
        record.args = None
        if record.exc_info:
            record.luz_error = _error(record.exc_info)
        record.exc_info = None
        record.exc_text = None
        return record

    def emit(self, record: logging.LogRecord) -> None:
        try:
            keep = _keep(record)
            if not keep and self.queue.qsize() >= self.soft_limit:
                _count("dropped")
                return
            prepared = self.prepare(record)
            try:
                self.queue.put_nowait(prepared)
            except queue.Full:
                if keep:
                    self.target.handle(prepared)  # WARNING 以上与安全事件不丢：在调用线程直接写出
                else:
                    _count("dropped")
        except Exception:  # noqa: BLE001 - 日志失败不影响业务
            _count("errors")


_listener: QueueListener | None = None
_queue: queue.Queue | None = None
_applied_modules: set[str] = set()
_atexit_registered = False


def _level(name: str | int) -> int:
    if isinstance(name, int):
        return name
    return getattr(logging, str(name).upper(), logging.INFO)


def set_levels(level: str | int, modules: dict[str, str] | None = None) -> None:
    """根级别 + 按 logger 覆盖（运行配置 [logging] 热加载时调用）；去掉的覆盖恢复继承。"""
    logging.getLogger().setLevel(_level(level))
    modules = dict(modules or {})
    for name in _applied_modules - set(modules):
        logging.getLogger(name).setLevel(logging.NOTSET)
    for name, value in modules.items():
        logging.getLogger(name).setLevel(_level(value))
    _applied_modules.clear()
    _applied_modules.update(modules)


def configure_logging(level: str = "info", *, service: str | None = None, env: str | None = None,
                      version: str | None = None, instance: str | None = None, queue_capacity: int = 10_000,
                      stream: Any = None, use_queue: bool = True) -> None:
    """进程入口调用一次（api、worker、scheduler、命令行）；再次调用会先停掉旧的监听线程。

    service / env / version / instance 未传时取 Settings（LUZ_SERVICE_NAME、LUZ_ENV、APP_VERSION、LUZ_INSTANCE_ID 或主机名）。
    """
    global _listener, _queue, _atexit_registered
    shutdown_logging()
    if None in (service, env, version, instance):
        from .config import get_settings  # noqa: PLC0415

        settings = get_settings()
        service = service or settings.service_name
        env = env or settings.env
        version = version or settings.app_version
        instance = instance or settings.instance_id
    set_static(service=service, env=env, version=version, instance=instance)
    if env == "prod":
        logging.raiseExceptions = False
    target = _SafeStreamHandler(stream or sys.stdout)
    target.setFormatter(JsonFormatter())
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    if use_queue:
        _queue = queue.Queue(maxsize=max(1, int(queue_capacity)))
        handler: logging.Handler = _BoundedQueueHandler(_queue, target, int(queue_capacity))
        _listener = QueueListener(_queue, target, respect_handler_level=False)
        _listener.start()
    else:
        handler = target
    root.addHandler(handler)
    set_levels(level, None)
    if not _atexit_registered:
        atexit.register(shutdown_logging)
        _atexit_registered = True


def flush_logging(timeout: float = 5.0) -> None:
    """等队列写完（测试与优雅退出用）。"""
    q = _queue
    if q is None or _listener is None:
        return
    deadline = time.monotonic() + timeout
    while q.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(0.005)


def shutdown_logging() -> None:
    """停止监听线程并写完队列里剩余的日志。"""
    global _listener, _queue
    listener, _listener = _listener, None
    if listener is not None:
        try:
            listener.stop()
        except Exception:  # noqa: BLE001
            _count("errors")
    _queue = None


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO, *, message: str | None = None,
              exc_info: Any = None, **fields: Any) -> None:
    """记录一个登记过的事件（luzback.log_events）：结构化字段走 fields，不拼进消息；日志失败不抛出。

        log_event(log, ORDER_CREATED, order_no=order.order_no, amount_cents=order.total_cents)
        log_event(log, HTTP_REQUEST, **{"http.route": "/orders/{id}", "http.status": 200}, duration_ms=12)

    安全事件不受日志级别配置影响，总会写出。
    """
    try:
        extra = {"fields": {"event": event, **fields}}
        if logger.isEnabledFor(level):
            logger.log(level, message or event, exc_info=exc_info, extra=extra)
        elif is_security(event) and not logger.disabled:
            exc = sys.exc_info() if exc_info is True else exc_info
            if isinstance(exc, BaseException):
                exc = (type(exc), exc, exc.__traceback__)
            record = logger.makeRecord(logger.name, level, "(security)", 0, message or event, None, exc or None,
                                       extra=extra)
            logger.handle(record)
    except Exception:  # noqa: BLE001
        _count("errors")


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
