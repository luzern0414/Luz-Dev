"""协议错误码（SPEC §8.3）与用户文案。网关把 AgentException 转为 `error` 事件或 HTTP 响应。"""
from __future__ import annotations

from enum import StrEnum

from .models import AgentError


class ErrorCode(StrEnum):
    AGENT_BUSY = "AGENT_BUSY"
    RATE_LIMITED = "RATE_LIMITED"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    TOOL_TIMEOUT = "TOOL_TIMEOUT"
    CONFIRM_EXPIRED = "CONFIRM_EXPIRED"
    CONFIRM_MISMATCH = "CONFIRM_MISMATCH"
    STEP_LIMIT = "STEP_LIMIT"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    DISABLED = "DISABLED"
    INTERNAL = "INTERNAL"


RETRYABLE: frozenset[ErrorCode] = frozenset({
    ErrorCode.AGENT_BUSY, ErrorCode.RATE_LIMITED, ErrorCode.MODEL_UNAVAILABLE,
    ErrorCode.TOOL_TIMEOUT, ErrorCode.INTERNAL,
})

_HTTP = {
    ErrorCode.AGENT_BUSY: 409,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.MODEL_UNAVAILABLE: 503,
    ErrorCode.TOOL_TIMEOUT: 504,
    ErrorCode.CONFIRM_EXPIRED: 410,
    ErrorCode.CONFIRM_MISMATCH: 409,
    ErrorCode.STEP_LIMIT: 422,
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.DISABLED: 503,
    ErrorCode.INTERNAL: 500,
}

USER_MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.AGENT_BUSY: "上一条还在处理，请稍等。",
    ErrorCode.RATE_LIMITED: "操作太频繁了，请稍后再试。",
    ErrorCode.MODEL_UNAVAILABLE: "助手现在有点忙，请稍后再试。",
    ErrorCode.TOOL_TIMEOUT: "查询超时了，请稍后再试，或直接到页面查看。",
    ErrorCode.CONFIRM_EXPIRED: "确认已过期，请重新发起。",
    ErrorCode.CONFIRM_MISMATCH: "确认请求已失效，请刷新会话。",
    ErrorCode.STEP_LIMIT: "这次操作步骤较多，我先停在这里。请换个说法或分步告诉我。",
    ErrorCode.UNAUTHORIZED: "登录已过期，请重新登录。",
    ErrorCode.FORBIDDEN: "你没有这项操作的权限。",
    ErrorCode.DISABLED: "助手维护中，请稍后再来。",
    ErrorCode.INTERNAL: "出了点问题，请稍后再试。",
}


def error_to_http_status(code: ErrorCode | str) -> int:
    return _HTTP.get(ErrorCode(code), 500)


class AgentException(Exception):
    """协议级错误。message 面向用户；网关据 code 决定 HTTP 状态或 error 事件。"""

    def __init__(self, code: ErrorCode | str, message: str | None = None,
                 retry_after: int | None = None, trace_id: str = ""):
        self.code = ErrorCode(code)
        self.message = message or USER_MESSAGES[self.code]
        self.retry_after = retry_after
        self.trace_id = trace_id
        super().__init__(self.message)

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE

    @property
    def status(self) -> int:
        return error_to_http_status(self.code)

    def to_error(self, trace_id: str | None = None) -> AgentError:
        return AgentError(code=self.code.value, message=self.message, retryable=self.retryable,
                          retry_after=self.retry_after, trace_id=trace_id or self.trace_id)


def make_agent_error(code: ErrorCode | str, trace_id: str = "", message: str | None = None,
                     retry_after: int | None = None) -> AgentError:
    return AgentException(code, message, retry_after, trace_id).to_error()
