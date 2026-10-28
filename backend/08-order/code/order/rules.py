"""订单纯规则：状态机、单号、金额分摊、可退金额、商品类型汇总（无数据库访问，便于单测）。"""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from luzback import StateMachine

# ---------------------------------------------------------------- 主状态

PENDING = "pending_payment"
PAID = "paid"
FULFILLING = "fulfilling"
FULFILLED = "fulfilled"
COMPLETED = "completed"
CANCELLED = "cancelled"     # 用户 / 后台取消（未支付）
CLOSED = "closed"           # 支付超时关闭
REFUNDED = "refunded"       # 全部实付已退（终态）
SHIPPED = FULFILLING        # v1 兼容名

STATUSES = (PENDING, PAID, FULFILLING, FULFILLED, COMPLETED, CANCELLED, CLOSED, REFUNDED)
ACTIVE_AFTER_PAY = (PAID, FULFILLING, FULFILLED, COMPLETED)

ORDER_FLOW = StateMachine(
    "order",
    {
        PENDING: {PAID, CANCELLED, CLOSED},
        PAID: {FULFILLING, FULFILLED, COMPLETED, REFUNDED},
        FULFILLING: {FULFILLED, COMPLETED, REFUNDED},
        FULFILLED: {COMPLETED, REFUNDED},
        COMPLETED: {REFUNDED},
    },
    terminal=frozenset({CANCELLED, CLOSED, REFUNDED}),
)

# ---------------------------------------------------------------- 退款维度（与主状态独立）

REFUND_NONE = "none"
REFUNDING = "refunding"
PARTIALLY_REFUNDED = "partially_refunded"
REFUND_DONE = "refunded"
REFUND_STATUSES = (REFUND_NONE, REFUNDING, PARTIALLY_REFUNDED, REFUND_DONE)

REFUND_FLOW = StateMachine(
    "order_refund",
    {
        REFUND_NONE: {REFUNDING},
        REFUNDING: {REFUND_NONE, PARTIALLY_REFUNDED, REFUND_DONE},
        PARTIALLY_REFUNDED: {REFUNDING},
    },
    terminal=frozenset({REFUND_DONE}),
)

# ---------------------------------------------------------------- 状态页签

TABS: dict[str, tuple[str, ...]] = {
    "all": STATUSES,
    "unpaid": (PENDING,),
    "to_fulfil": (PAID, FULFILLING),       # 待发货 / 待服务
    "to_receive": (FULFILLED,),            # 待确认收货
    "completed": (COMPLETED,),
    "closed": (CANCELLED, CLOSED, REFUNDED),
}

# ---------------------------------------------------------------- 支付方式 / 取消类型 / 商品类型

PAY_WECHAT = "wechat"
PAY_BALANCE = "balance"
PAY_FREE = "free"
PAY_METHODS = (PAY_WECHAT, PAY_BALANCE)

CANCEL_USER = "user"
CANCEL_TIMEOUT = "timeout"
CANCEL_ADMIN = "admin"

GOODS_PHYSICAL = "physical"
GOODS_OFFLINE_SERVICE = "offline_service"
GOODS_VIRTUAL_CONTENT = "virtual_content"
GOODS_KINDS = (GOODS_PHYSICAL, GOODS_OFFLINE_SERVICE, GOODS_VIRTUAL_CONTENT)


def aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def order_goods_kind(kinds: list[str]) -> str:
    """订单整体商品类型（决定支付渠道规则）：含虚拟内容 → virtual_content；含实物 → physical；否则线下服务。"""
    if GOODS_VIRTUAL_CONTENT in kinds:
        return GOODS_VIRTUAL_CONTENT
    if GOODS_PHYSICAL in kinds:
        return GOODS_PHYSICAL
    return GOODS_OFFLINE_SERVICE


def new_order_no(now: datetime, tz: str = "Asia/Shanghai", prefix: str = "") -> str:
    """时间有序且唯一：前缀(≤4) + 本地 yyMMddHHmmss + 毫秒 3 位 + 6 位随机数字；≤ 25 位，满足微信 out_trade_no 字符集。

    唯一约束是最后一道防线（冲突时调用方换号重试）。
    """
    local = now.astimezone(ZoneInfo(tz))
    return f"{prefix}{local.strftime('%y%m%d%H%M%S')}{local.microsecond // 1000:03d}{secrets.randbelow(10**6):06d}"


def request_hash(data: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def max_refund_for_qty(paid_cents: int, qty: int, reserved_qty: int, request_qty: int) -> int:
    """按数量退款的上限（累计公式）：paid × (已占 + 本次) // 数量 − paid × 已占 // 数量。

    无论分几次退，全部退完时累计金额严格等于该行实付（17-campaign `pricing.refund_amount` 同一口径）。
    """
    if qty <= 0 or request_qty < 0 or reserved_qty < 0 or reserved_qty + request_qty > qty:
        raise ValueError("invalid refund quantity")
    return paid_cents * (reserved_qty + request_qty) // qty - paid_cents * reserved_qty // qty


def refund_status_after(paid_cents: int, refunded_cents: int, reserved_open_cents: int) -> str:
    """订单退款维度：有处理中占用 → refunding；全退 → refunded；部分 → partially_refunded；否则 none。"""
    if reserved_open_cents > 0:
        return REFUNDING
    if paid_cents > 0 and refunded_cents >= paid_cents:
        return REFUND_DONE
    if refunded_cents > 0:
        return PARTIALLY_REFUNDED
    return REFUND_NONE
