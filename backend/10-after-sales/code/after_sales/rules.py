"""售后纯规则：状态机、类型、原因、窗口判断。"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from luzback import StateMachine

REFUND_ONLY = "refund_only"
RETURN_AND_REFUND = "return_and_refund"
TYPES = (REFUND_ONLY, RETURN_AND_REFUND)

PENDING_REVIEW = "pending_review"
AWAITING_RETURN = "awaiting_return"      # 已同意，等待买家寄回
RETURN_SHIPPED = "return_shipped"        # 买家已寄回（快递单号）
REFUNDING = "refunding"                  # 退款处理中（渠道受理 / 待提交）
REFUND_FAILED = "refund_failed"          # 渠道失败 / 关闭：人工处理
REFUND_ABNORMAL = "refund_abnormal"      # 渠道异常（如银行卡失效）：09 后台发起异常退款
COMPLETED = "completed"
REJECTED = "rejected"
CANCELLED = "cancelled"                  # 买家撤销
CLOSED = "closed"                        # 超时未寄回 / 人工关闭

STATUSES = (PENDING_REVIEW, AWAITING_RETURN, RETURN_SHIPPED, REFUNDING, REFUND_FAILED, REFUND_ABNORMAL, COMPLETED,
            REJECTED, CANCELLED, CLOSED)
OPEN = (PENDING_REVIEW, AWAITING_RETURN, RETURN_SHIPPED, REFUNDING, REFUND_FAILED, REFUND_ABNORMAL)
RELEASES_QUOTA = (REJECTED, CANCELLED, CLOSED)

AFTER_SALE_FLOW = StateMachine(
    "after_sale",
    {
        PENDING_REVIEW: {AWAITING_RETURN, REFUNDING, REJECTED, CANCELLED},
        AWAITING_RETURN: {RETURN_SHIPPED, CANCELLED, CLOSED},
        RETURN_SHIPPED: {REFUNDING, REJECTED},
        REFUNDING: {COMPLETED, REFUND_FAILED, REFUND_ABNORMAL},
        REFUND_FAILED: {REFUNDING, COMPLETED, CLOSED},
        REFUND_ABNORMAL: {COMPLETED, REFUND_FAILED},
    },
    terminal=frozenset({COMPLETED, REJECTED, CANCELLED, CLOSED}),
)

TABS = {
    "all": STATUSES,
    "processing": OPEN,
    "done": (COMPLETED, REJECTED, CANCELLED, CLOSED),
}

REASON_NO_REASON = "no_reason"           # 七日无理由（仅适用商品）
REASONS = (REASON_NO_REASON, "quality", "wrong_item", "missing", "not_received", "not_as_described", "service_issue",
           "other")

ROUTE_WECHAT = "wechat"
ROUTE_BALANCE = "balance"


def aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def window_deadline(fulfilled_at: datetime, days: int, tz: str = "Asia/Shanghai") -> datetime:
    """签收（交付）次日起算 days 天，截止到第 days 天当地 24:00（七日办法第十条）。"""
    local = aware(fulfilled_at).astimezone(ZoneInfo(tz))
    return datetime.combine(local.date() + timedelta(days=days + 1), time.min, tzinfo=ZoneInfo(tz))


def window_open(fulfilled_at: datetime | None, now: datetime, days: int, tz: str = "Asia/Shanghai") -> bool:
    """未履约（未签收）的订单随时可申请；已履约的看截止时间。"""
    if fulfilled_at is None:
        return True
    return now < window_deadline(fulfilled_at, days, tz)
