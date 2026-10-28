"""统一错误码（SPEC §4）。HTTP 状态只表达大类，业务含义以 code 为准。

服务层只抛 AppError；应用工厂把它转换为响应信封 {code, message, data}。
未预期的异常由应用工厂转为 5000，并带 trace_id 记录日志。
"""
from __future__ import annotations

from enum import IntEnum
from typing import Any


class ErrorCode(IntEnum):
    OK = 0
    BAD_REQUEST = 4001            # 参数错误（含缺幂等键、请求体过大）
    UNAUTHORIZED = 4010           # 未登录 / 令牌无效或过期
    MFA_REQUIRED = 4011           # 需要二次验证
    FORBIDDEN = 4030              # 无权限
    SCOPE_FORBIDDEN = 4031        # 不在门店范围
    NOT_FOUND = 4040              # 不存在（含他人资源）
    CONFLICT = 4090               # 状态冲突 / 版本冲突
    OUT_OF_STOCK = 4091           # 库存不足
    DUPLICATE_REQUEST = 4092      # 同幂等键处理中
    IDEMPOTENCY_MISMATCH = 4093   # 幂等键被不同请求体复用
    BUSINESS_RULE = 4220          # 业务规则不满足
    STORE_CLOSED = 4221           # 门店休息 / 不在可售时段
    OUT_OF_RANGE = 4222           # 超出配送范围
    QUOTE_EXPIRED = 4223          # 报价已过期或价格变化
    INSUFFICIENT_BALANCE = 4224   # 余额 / 积分不足
    LIMIT_REACHED = 4225          # 次数 / 数量上限（领券、抽奖、地址数）
    RATE_LIMITED = 4290           # 限流
    INTERNAL = 5000               # 内部错误
    UPSTREAM_FAILED = 5020        # 上游失败
    SERVICE_UNAVAILABLE = 5030    # 熔断打开 / 维护


_HTTP_STATUS = {
    ErrorCode.BAD_REQUEST: 400,
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.MFA_REQUIRED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.SCOPE_FORBIDDEN: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.CONFLICT: 409,
    ErrorCode.OUT_OF_STOCK: 409,
    ErrorCode.DUPLICATE_REQUEST: 409,
    ErrorCode.IDEMPOTENCY_MISMATCH: 422,  # draft-ietf-httpapi-idempotency-key-header：同键不同请求体为 422
    ErrorCode.BUSINESS_RULE: 422,
    ErrorCode.STORE_CLOSED: 422,
    ErrorCode.OUT_OF_RANGE: 422,
    ErrorCode.QUOTE_EXPIRED: 422,
    ErrorCode.INSUFFICIENT_BALANCE: 422,
    ErrorCode.LIMIT_REACHED: 422,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.INTERNAL: 500,
    ErrorCode.UPSTREAM_FAILED: 502,
    ErrorCode.SERVICE_UNAVAILABLE: 503,
}

# 哪些错误可以由客户端重试（写进响应，前端据此决定是否自动重试）
RETRYABLE = {ErrorCode.DUPLICATE_REQUEST, ErrorCode.RATE_LIMITED, ErrorCode.UPSTREAM_FAILED, ErrorCode.SERVICE_UNAVAILABLE}


class AppError(Exception):
    """业务异常。message 面向用户（不含内部细节），data 为结构化补充。"""

    def __init__(self, code: ErrorCode, message: str = "", data: Any = None, retry_after: int | None = None):
        super().__init__(message or code.name)
        self.code = code
        self.message = message or code.name.replace("_", " ").lower()
        self.data = data
        self.retry_after = retry_after

    @property
    def status(self) -> int:
        return _HTTP_STATUS.get(self.code, 400)

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE


def not_found(what: str = "resource") -> AppError:
    return AppError(ErrorCode.NOT_FOUND, f"{what} not found")


def conflict(message: str = "state changed, refresh and retry") -> AppError:
    return AppError(ErrorCode.CONFLICT, message)


def rule(message: str, data: Any = None) -> AppError:
    return AppError(ErrorCode.BUSINESS_RULE, message, data)
