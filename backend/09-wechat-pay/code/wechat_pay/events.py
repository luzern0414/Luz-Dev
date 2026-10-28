"""wechat_pay 消费的事件（25-jobs worker 加载 SUBSCRIPTIONS；处理器幂等、不提交、不调用外部接口）。

| 主题 | 处理 |
|---|---|
| order.cancelled | 该订单未完成的支付单登记“待关单”并立即唤醒补偿任务（任务里先查单再关单；已支付则落库支付事实并按回调规则处理） |
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import Session

from luzback import utcnow

from .models import PaymentRecord
from .rules import PAY_OPEN


def on_order_cancelled(db: Session, payload: dict[str, Any], event_id: str) -> int:
    """幂等：只对未完成支付单设置 close_requested_at（已设置的保留原值）与 next_query_at=now。"""
    order_id = payload.get("order_id")
    if order_id is None:
        return 0
    now = utcnow()
    base = (PaymentRecord.business_type == "order", PaymentRecord.business_id == int(order_id),
            PaymentRecord.status.in_(PAY_OPEN))
    db.execute(update(PaymentRecord).where(*base, PaymentRecord.close_requested_at.is_(None))
               .values(close_requested_at=now).execution_options(synchronize_session=False))
    return db.execute(update(PaymentRecord).where(*base).values(next_query_at=now)
                      .execution_options(synchronize_session=False)).rowcount


SUBSCRIPTIONS = {"order.cancelled": on_order_cancelled}
