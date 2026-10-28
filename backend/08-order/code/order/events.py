"""order 消费的事件（25-jobs worker 加载 SUBSCRIPTIONS；处理器幂等、不提交、不调用外部接口）。

| 主题 | 处理 |
|---|---|
| payment.succeeded | 兜底：business_type=order、未标人工、订单仍待支付时按支付事实成交（正常情况下支付钩子已在同一事务完成，此处为空操作） |
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from luzback.outbox import mark_processed

from .models import Order
from .rules import PAY_WECHAT, PENDING
from .service import _settle_paid


def on_payment_succeeded(db: Session, payload: dict[str, Any], event_id: str) -> str:
    if payload.get("business_type") != "order" or payload.get("needs_manual"):
        return "ignored"
    order = db.get(Order, int(payload["business_id"]))
    if order is None or order.status != PENDING:
        return "noop"
    if event_id and not mark_processed(db, "order.payment_succeeded", event_id):
        return "duplicate"
    paid_at = payload.get("paid_at")
    _settle_paid(db, order, method=PAY_WECHAT, amount_cents=int(payload["amount_cents"]),
                 paid_at=datetime.fromisoformat(paid_at) if paid_at else None,
                 actor=("payment", payload.get("out_trade_no")), payment_id=payload.get("payment_id"),
                 out_trade_no=payload.get("out_trade_no"))
    return "settled"


SUBSCRIPTIONS = {"payment.succeeded": on_payment_succeeded}
