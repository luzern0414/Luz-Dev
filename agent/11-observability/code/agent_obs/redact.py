"""脱敏事件日志：只保留事件类型、文本长度、工具名、错误码、trace_id，不保存对话正文（SPEC §13 隐私）。"""
from __future__ import annotations

import threading
from typing import Any

_KEEP_KEYS = {"tool", "ok", "latency_ms", "code", "retryable", "level", "intent", "confidence",
              "trace_id", "degraded", "request_id", "action", "amount_cents", "expires_at"}
_TEXT_KEYS = {"text", "summary", "message", "reason", "question"}


def redact_event(event: str, data: dict[str, Any] | None, *, turn_id: int | None = None,
                 trace_id: str | None = None, seq: int | None = None) -> dict[str, Any]:
    """返回可入日志的脱敏记录：文本类字段只记长度。"""
    record: dict[str, Any] = {"event": event}
    if turn_id is not None:
        record["turn_id"] = turn_id
    if trace_id:
        record["trace_id"] = trace_id
    if seq is not None:
        record["seq"] = seq
    for key, value in (data or {}).items():
        if key in _TEXT_KEYS and isinstance(value, str):
            record[f"{key}_len"] = len(value)
        elif key in _KEEP_KEYS and isinstance(value, (str, int, float, bool, list)):
            record[key] = value
        elif key in ("cards", "quick_replies", "citations", "items", "chunks") and isinstance(value, list):
            record[f"{key}_count"] = len(value)
    return record


class EventLog:
    """进程内日志缓冲；生产替换为结构化日志输出。"""

    def __init__(self, max_items: int = 5000) -> None:
        self.max_items = max_items
        self._items: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def log(self, event: str, data: dict[str, Any] | None = None, **meta: Any) -> dict[str, Any]:
        record = redact_event(event, data, **meta)
        with self._lock:
            self._items.append(record)
            if len(self._items) > self.max_items:
                del self._items[: len(self._items) - self.max_items]
        return record

    def items(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._items)
