"""订单接口（FastAPI 路由）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field
from sqlalchemy import select

from identity.api import current_user
from luzback import AppError, ErrorCode, get_db, ok
from order import Order, OrderItem, cancel_order, create_order

router = APIRouter(prefix="/api/v1/app/orders", tags=["orders"])


class LineIn(BaseModel):
    sku_id: int = Field(ge=1)
    qty: int = Field(ge=1, le=99)


class CreateOrderIn(BaseModel):
    items: list[LineIn] = Field(min_length=1, max_length=50)


def _owned_order(db, user_id: int, order_id: int) -> Order:
    order = db.get(Order, order_id)
    if order is None or order.user_id != user_id:
        # 不区分“不存在”和“不属于你”，避免枚举
        raise AppError(ErrorCode.NOT_FOUND, "order not found")
    return order


@router.post("")
def create(
    body: CreateOrderIn,
    user_id: int = Depends(current_user),
    db=Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    return ok(create_order(db, user_id, [(i.sku_id, i.qty) for i in body.items], idempotency_key))


@router.get("/{order_id}")
def detail(order_id: int, user_id: int = Depends(current_user), db=Depends(get_db)):
    order = _owned_order(db, user_id, order_id)
    items = db.execute(select(OrderItem).where(OrderItem.order_id == order_id)).scalars().all()
    return ok({
        "order_id": order.id,
        "order_no": order.order_no,
        "status": order.status,
        "total_cents": order.total_cents,
        "items": [{"sku_id": i.sku_id, "qty": i.qty, "unit_price_cents": i.unit_price_cents} for i in items],
    })


@router.post("/{order_id}/cancel")
def cancel(order_id: int, user_id: int = Depends(current_user), db=Depends(get_db)):
    _owned_order(db, user_id, order_id)
    order = cancel_order(db, order_id)
    return ok({"order_id": order.id, "status": order.status})
