"""结构化日志：JSON 一行一条，自动带 trace_id / request_id / user_id，并脱敏手机号。

字段：ts, level, logger, event, trace_id, request_id, user_id, store_id, duration_ms, code, msg 及调用方附加字段。
禁止把请求正文、令牌、支付参数写入日志（SPEC §13）。
"""
from __future__ import annotations

import json
import logging
import re
import sys
import time
from contextvars import ContextVar
from typing import Any

trace_id_var: ContextVar[str | None] = ContextVar("trace_id", default=None)
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
user_id_var: ContextVar[str | None] = ContextVar("user_id", default=None)
store_id_var: ContextVar[str | None] = ContextVar("store_id", default=None)

_PHONE = re.compile(r"(?<!\d)(1[3-9]\d)(\d{4})(\d{4})(?!\d)")
_TOKEN = re.compile(r"(Bearer\s+)[A-Za-z0-9\-_.]+", re.IGNORECASE)
_ID_CARD = re.compile(r"(?<!\d)(\d{6})\d{8}(\d{3}[\dXx])(?!\d)")


def redact(text: str) -> str:
    text = _PHONE.sub(r"\1****\3", text)
    text = _TOKEN.sub(r"\1***", text)
    text = _ID_CARD.sub(r"\1********\2", text)
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": redact(record.getMessage()),
            "trace_id": trace_id_var.get(),
            "request_id": request_id_var.get(),
            "user_id": user_id_var.get(),
            "store_id": store_id_var.get(),
        }
        for key, value in getattr(record, "fields", {}).items():
            payload[key] = redact(value) if isinstance(value, str) else value
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))[-2000:]
        return json.dumps({k: v for k, v in payload.items() if v is not None}, ensure_ascii=False, default=str)


def configure_logging(level: str = "info") -> None:
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO, **fields: Any) -> None:
    logger.log(level, event, extra={"fields": {"event": event, **fields}})


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
