"""默认业务适配（兜底）：业务模块未自行注册时，为 v1 订单模块提供支付解析器与支付成功钩子。

正式做法（CONTRACTS「回调注册」）：各业务模块在自己的 __init__ 末尾调用
`register_payable(...)` / `register_on_paid(...)` / `register_on_refunded(...)`。
这里只在对方没有注册、且对方导出了 v1 接口（Order、PENDING、mark_paid）时才登记，不覆盖对方的注册。
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from luzback import AppError, ErrorCode
from luzback.contracts import PaymentInfo

from . import service
from .rules import GOODS_PHYSICAL, aware


def install_defaults() -> None:
    try:
        import order as order_module  # 导入即触发订单模块自己的注册（若有）
    except Exception:  # noqa: BLE001 - 未启用订单模块
        return
    order_cls = getattr(order_module, "Order", None)
    pending = getattr(order_module, "PENDING", None)
    if order_cls is None or pending is None:
        return

    if not service.has_payable("order"):
        def resolve_order(db: Session, user_id: int, order_id: int) -> service.Payable:
            order = db.get(order_cls, order_id)
            if order is None or getattr(order, "user_id", None) != user_id:
                raise AppError(ErrorCode.NOT_FOUND, "order not found")
            if order.status != pending:
                raise AppError(ErrorCode.CONFLICT, "order is not awaiting payment", data={"status": order.status})
            deadline = getattr(order, "pay_deadline_at", None)
            if deadline is None:
                # 旧订单没有支付截止时间：按订单模块自己的支付时限推算（与下单时写入 pay_deadline_at 的是同一个值）
                timeout = getattr(getattr(order_module, "get_config", lambda: None)(), "pay_timeout_minutes", 15)
                deadline = aware(order.created_at) + timedelta(minutes=timeout)
            amount = int(getattr(order, "total_cents")) - int(getattr(order, "paid_cents", 0) or 0)
            return service.Payable(amount_cents=amount, description=f"订单 {order.order_no}", expires_at=aware(deadline),
                                   store_id=getattr(order, "store_id", None), business_no=str(order.order_no),
                                   goods_kind=getattr(order, "goods_kind", GOODS_PHYSICAL) or GOODS_PHYSICAL)

        service.register_payable("order", resolve_order)

    mark_paid = getattr(order_module, "mark_paid", None)
    if not service.has_on_paid("order") and mark_paid is not None:
        def on_order_paid(db: Session, info: PaymentInfo) -> None:
            mark_paid(db, info.business_id)

        service.register_on_paid("order", on_order_paid)
