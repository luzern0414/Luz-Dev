"""订单模块：创建订单（幂等 + 原子扣库存 + 发件箱）、支付成功流转、取消回补库存。

状态机见 backend/order/MODULE.md：
    pending_payment --pay--> paid --ship--> shipped --complete--> completed
    pending_payment --cancel--> cancelled
    paid --refund--> refunded
所有状态变更都是 “UPDATE ... WHERE status = 旧状态”，返回 rowcount 判断是否成功，防止重复流转。
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone

from sqlalchemy import Integer, String, select, update
from sqlalchemy.orm import Mapped, Session, mapped_column

from inventory import Sku, deduct, restore
from luzback import AppError, Base, ErrorCode, enqueue_event, run_idempotent

PENDING = "pending_payment"
PAID = "paid"
SHIPPED = "shipped"
COMPLETED = "completed"
CANCELLED = "cancelled"
REFUNDED = "refunded"

TRANSITIONS = {
    (PENDING, PAID),
    (PAID, SHIPPED),
    (SHIPPED, COMPLETED),
    (PENDING, CANCELLED),
    (PAID, REFUNDED),
}


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_no: Mapped[str] = mapped_column(String(32), unique=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    status: Mapped[str] = mapped_column(String(24), default=PENDING, index=True)
    total_cents: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(timezone.utc))
    paid_at: Mapped[datetime | None] = mapped_column(nullable=True)


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(Integer, index=True)
    sku_id: Mapped[int] = mapped_column(Integer)
    qty: Mapped[int] = mapped_column(Integer)
    unit_price_cents: Mapped[int] = mapped_column(Integer)


def _new_order_no() -> str:
    # 时间戳 + 随机后缀；生产环境可改为号段或雪花算法
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + secrets.token_hex(4)


def create_order(db: Session, user_id: int, lines: list[tuple[int, int]], idempotency_key: str | None) -> dict:
    """创建订单。lines = [(sku_id, qty), ...]。

    同一事务内完成：幂等记录、逐项原子扣库存、写订单与明细、写发件箱事件。
    任一 SKU 库存不足 → 整个事务回滚（已扣的库存一起回滚），返回 OUT_OF_STOCK。
    """
    if not lines:
        raise AppError(ErrorCode.BAD_REQUEST, "empty order")

    def work() -> dict:
        # 按 sku_id 排序加锁顺序，避免多 SKU 订单之间的死锁
        merged: dict[int, int] = {}
        for sku_id, qty in lines:
            merged[sku_id] = merged.get(sku_id, 0) + qty
        total = 0
        priced: list[tuple[int, int, int]] = []
        for sku_id in sorted(merged):
            sku = db.get(Sku, sku_id)
            if sku is None or sku.status != "on_sale":
                raise AppError(ErrorCode.NOT_FOUND, f"sku {sku_id} not on sale")
            deduct(db, sku_id, merged[sku_id])
            priced.append((sku_id, merged[sku_id], sku.price_cents))
            total += sku.price_cents * merged[sku_id]

        order = Order(order_no=_new_order_no(), user_id=user_id, status=PENDING, total_cents=total)
        db.add(order)
        db.flush()
        for sku_id, qty, price in priced:
            db.add(OrderItem(order_id=order.id, sku_id=sku_id, qty=qty, unit_price_cents=price))
        enqueue_event(db, "order.created", {"order_id": order.id, "order_no": order.order_no})
        return {"order_id": order.id, "order_no": order.order_no, "total_cents": total, "status": PENDING}

    return run_idempotent(db, "order.create", idempotency_key, work)


def transition(db: Session, order_id: int, to_status: str) -> Order:
    """按状态机流转。调用方负责 commit。"""
    order = db.get(Order, order_id)
    if order is None:
        raise AppError(ErrorCode.NOT_FOUND, "order not found")
    if (order.status, to_status) not in TRANSITIONS:
        raise AppError(ErrorCode.CONFLICT, f"illegal transition {order.status} -> {to_status}")
    values = {"status": to_status}
    if to_status == PAID:
        values["paid_at"] = datetime.now(timezone.utc)
    result = db.execute(
        update(Order).where(Order.id == order_id, Order.status == order.status).values(**values)
    )
    if result.rowcount != 1:
        # 并发下另一个请求先完成了流转
        raise AppError(ErrorCode.CONFLICT, "order status changed concurrently")
    db.refresh(order)
    return order


def mark_paid(db: Session, order_id: int) -> Order:
    """支付成功：状态 pending_payment -> paid，并写发件箱。由支付回调调用。"""
    order = transition(db, order_id, PAID)
    enqueue_event(db, "order.paid", {"order_id": order_id, "order_no": order.order_no})
    return order


def cancel_order(db: Session, order_id: int) -> Order:
    """取消未支付订单并回补库存。已支付订单需走退款流程，这里拒绝。"""
    order = db.get(Order, order_id)
    if order is None:
        raise AppError(ErrorCode.NOT_FOUND, "order not found")
    order = transition(db, order_id, CANCELLED)
    items = db.execute(select(OrderItem).where(OrderItem.order_id == order_id)).scalars().all()
    for item in items:
        restore(db, item.sku_id, item.qty)
    enqueue_event(db, "order.cancelled", {"order_id": order_id})
    return order
