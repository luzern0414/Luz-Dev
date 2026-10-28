"""订单超时关单任务（references/patterns/order.md §超时关单）。

顺序与微信支付开发指引一致：先查单，再决定。
    paid    → 按支付成功处理（不能取消已付款的订单）
    unknown → 本轮跳过，下轮再查（查询失败不等于未支付）
    unpaid / closed → 关闭渠道订单（unpaid 时）→ 取消本地订单并回补库存

每个订单独立提交：一个订单失败不影响其他订单；任务可重复执行（状态机条件更新保证幂等）。
定时扫描是默认方案；订单量大时改用延迟队列触发同一个函数，扫描保留为兜底。
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from luzback import AppError

from . import PENDING, Order, cancel_order, mark_paid

PAID_STATE = "paid"
UNPAID_STATE = "unpaid"
CLOSED_STATE = "closed"
UNKNOWN_STATE = "unknown"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def close_expired_orders(
    db: Session,
    *,
    now: datetime,
    timeout_minutes: int,
    query_payment: Callable[[Order], str],
    close_channel: Callable[[Order], None],
    limit: int = 100,
) -> dict[str, list[int]]:
    """处理超时未支付订单，返回各结果对应的订单 ID。"""
    deadline = now - timedelta(minutes=timeout_minutes)
    candidates = db.execute(
        select(Order).where(Order.status == PENDING).order_by(Order.created_at).limit(limit)
    ).scalars().all()
    report: dict[str, list[int]] = {"paid": [], "cancelled": [], "retry": [], "skipped": []}
    for order in candidates:
        if _aware(order.created_at) > deadline:
            continue
        try:
            state = query_payment(order)
        except Exception:  # noqa: BLE001 - 渠道查询失败：保持待支付，下轮重试
            report["retry"].append(order.id)
            continue
        try:
            if state == PAID_STATE:
                mark_paid(db, order.id)
                report["paid"].append(order.id)
            elif state in (UNPAID_STATE, CLOSED_STATE):
                if state == UNPAID_STATE:
                    close_channel(order)
                cancel_order(db, order.id)
                report["cancelled"].append(order.id)
            else:
                report["retry"].append(order.id)
                continue
            db.commit()
        except AppError:
            # 状态已被并发请求改变（例如回调刚好把订单标记为已支付）：放弃本单，不覆盖
            db.rollback()
            report["skipped"].append(order.id)
        except Exception:  # noqa: BLE001 - 关单接口失败：不取消本地订单，下轮重试
            db.rollback()
            report["retry"].append(order.id)
    return report
