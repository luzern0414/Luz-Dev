"""链路上下文：W3C Trace Context（references/config-logging.md §3.6，OBS-006）。

- 请求入口：合法的 `traceparent` 沿用其 trace 并生成新的 span；不合法或缺失时兼容旧的 `X-Trace-Id`
  （1–64 位字母、数字、横线、下划线，例如智能体的 `t_` 编号），再不行就新建。外部请求的采样标志不采用：
  本服务对每个请求都记日志，向下游一律写 `01`。
- trace_id 不是 32 位小写十六进制时（旧 X-Trace-Id），写 traceparent 取它的 SHA-256 前 32 位，并同时写 X-Trace-Id；
  下游收到两者且摘要一致时继续用原编号，各服务的日志仍按同一个 trace_id 串起来。
- 发件箱事件与消息带上 traceparent（和旧编号），消费端用 `continue_trace` 接上；定时任务每次运行 `new_trace`。
- 传播头只放随机编号，不放手机号、用户名、令牌等任何个人信息或凭据。

    with new_trace(job="order.timeout_close", run_id="42"):      # 定时任务
        ...
    with continue_trace(event.traceparent, event.trace_id, topic=event.topic, event_id=event.event_id):
        ...
    httpx.Client(event_hooks={"request": [httpx_request_hook]})  # 对外 HTTP 自动带 traceparent
"""
from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Iterator, MutableMapping
from contextlib import contextmanager
from typing import Any

from .logging import bind_log_fields, span_id_var, trace_id_var

_TRACEPARENT = re.compile(r"^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})(-.*)?$")
_LEGACY = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_ZERO_TRACE = "0" * 32
_ZERO_SPAN = "0" * 16
SAMPLED = "01"


def new_trace_id() -> str:
    while True:
        value = secrets.token_hex(16)
        if value != _ZERO_TRACE:
            return value


def new_span_id() -> str:
    while True:
        value = secrets.token_hex(8)
        if value != _ZERO_SPAN:
            return value


def parse_traceparent(value: str | None) -> tuple[str, str] | None:
    """返回 (trace_id, parent_span_id)；不合法返回 None（W3C：只认小写十六进制，ff 版本与全零编号无效）。"""
    if not value:
        return None
    match = _TRACEPARENT.match(value.strip())
    if match is None:
        return None
    version, trace_id, parent_id, _flags, rest = match.groups()
    if version == "ff" or (version == "00" and rest):
        return None
    if trace_id == _ZERO_TRACE or parent_id == _ZERO_SPAN:
        return None
    return trace_id, parent_id


def valid_legacy_trace_id(value: str | None) -> str | None:
    return value if value and _LEGACY.match(value) else None


def w3c_trace_id(trace_id: str) -> str:
    """日志里的 trace_id → traceparent 里的 32 位编号（旧编号取摘要，保证同一编号总得到同一结果）。"""
    if _HEX32.match(trace_id) and trace_id != _ZERO_TRACE:
        return trace_id
    return hashlib.sha256(trace_id.encode()).hexdigest()[:32]


def format_traceparent(trace_id: str, span_id: str) -> str:
    return f"00-{w3c_trace_id(trace_id)}-{span_id}-{SAMPLED}"


def resolve_incoming(traceparent: str | None, legacy_trace_id: str | None = None) -> tuple[str, str | None]:
    """入口或消费端：决定本段沿用的 trace_id 与上游 span（没有则 None）。"""
    parsed = parse_traceparent(traceparent)
    legacy = valid_legacy_trace_id(legacy_trace_id)
    if parsed is not None:
        trace_id, parent = parsed
        if legacy and w3c_trace_id(legacy) == trace_id:
            return legacy, parent  # 上游用的是旧编号：继续用原编号
        return trace_id, parent
    if legacy:
        return legacy, None
    return new_trace_id(), None


def current_traceparent() -> str | None:
    trace_id = trace_id_var.get()
    if not trace_id:
        return None
    return format_traceparent(trace_id, span_id_var.get() or new_span_id())


def outgoing_headers() -> dict[str, str]:
    """调用下游 HTTP、写消息时带的头；当前没有链路上下文时返回空。"""
    trace_id = trace_id_var.get()
    if not trace_id:
        return {}
    headers = {"traceparent": format_traceparent(trace_id, span_id_var.get() or new_span_id())}
    if w3c_trace_id(trace_id) != trace_id:
        headers["X-Trace-Id"] = trace_id
    return headers


def inject(headers: MutableMapping[str, str]) -> MutableMapping[str, str]:
    """把链路头写进已有的头字典（调用方已设置的不覆盖）。"""
    for name, value in outgoing_headers().items():
        if name not in headers:
            headers[name] = value
    return headers


def httpx_request_hook(request: Any) -> None:
    """httpx 事件钩子：`httpx.Client(event_hooks={"request": [httpx_request_hook]})`。"""
    for name, value in outgoing_headers().items():
        if name not in request.headers:
            request.headers[name] = value


@contextmanager
def trace_scope(trace_id: str | None = None, **fields: Any) -> Iterator[str]:
    """进入新的一段 span（trace_id 为空则新建 trace）；fields 绑定到这段里的每条日志（job、run_id、topic……）。"""
    trace_token = trace_id_var.set(trace_id or new_trace_id())
    span_token = span_id_var.set(new_span_id())
    try:
        with bind_log_fields(**fields):
            yield trace_id_var.get()
    finally:
        span_id_var.reset(span_token)
        trace_id_var.reset(trace_token)


def new_trace(**fields: Any):
    """定时任务、启动任务等没有上游的工作：新建 trace。"""
    return trace_scope(None, **fields)


def continue_trace(traceparent: str | None, legacy_trace_id: str | None = None, **fields: Any):
    """消费发件箱事件或消息：接上生产者的 trace（头不合法或缺失时新建）。"""
    trace_id, _parent = resolve_incoming(traceparent, legacy_trace_id)
    return trace_scope(trace_id, **fields)
