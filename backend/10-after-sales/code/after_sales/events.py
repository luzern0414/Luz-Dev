"""after_sales 消费的事件（25-jobs worker 加载 SUBSCRIPTIONS；处理器幂等、不提交、不调用外部接口）。

| 主题 | 处理 |
|---|---|
| refund.succeeded | business_ref = after_sales:{id} → 售后完成：订单计入已退、回库存、按券策略退券、after_sale.completed |
| refund.failed | → refund_failed（人工：重试 / 线下结清 / 关闭）；09 已释放该笔退款额度 |
| refund.abnormal | → refund_abnormal（09 后台发起异常退款后再收到 refund.succeeded） |

幂等：`mark_processed(db, "after_sales.refund_result", event_id)` + 售后单状态机条件 UPDATE。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from luzback.outbox import mark_processed

from .service import on_refund_result


def _handler(status: str):
    def handle(db: Session, payload: dict[str, Any], event_id: str) -> str:
        if not str(payload.get("business_ref") or "").startswith("after_sales:"):
            return "ignored"
        if event_id and not mark_processed(db, "after_sales.refund_result", event_id):
            return "duplicate"
        return on_refund_result(db, payload, status)

    handle.__name__ = f"on_refund_{status}"
    return handle


on_refund_succeeded = _handler("succeeded")
on_refund_failed = _handler("failed")
on_refund_abnormal = _handler("abnormal")

SUBSCRIPTIONS = {
    "refund.succeeded": on_refund_succeeded,
    "refund.failed": on_refund_failed,
    "refund.abnormal": on_refund_abnormal,
}
