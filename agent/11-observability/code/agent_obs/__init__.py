"""观测：trace_id、每轮指标计数器、脱敏事件日志（SPEC §14）。

日志不存完整对话：只记事件类型、长度、工具名、错误码、trace_id。
"""
from .metrics import Metrics, TurnTrace
from .redact import EventLog, redact_event
from .trace import new_request_id, new_trace_id

__all__ = ["Metrics", "TurnTrace", "EventLog", "redact_event", "new_request_id", "new_trace_id"]
