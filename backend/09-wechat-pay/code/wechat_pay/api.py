"""支付接口（FastAPI 路由）：创建支付单、支付回调。"""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy import select

from identity.api import current_user
from luzback import AppError, ErrorCode, get_db, ok, run_idempotent
from order import Order, PENDING, mark_paid
from wechat_pay import PaymentRecord, create_payment, handle_notify

router = APIRouter(tags=["payment"])

NOTIFY_SECRET = os.getenv("LUZ_PAY_NOTIFY_SECRET", "dev-notify-secret")  # 生产从密钥服务注入


@router.post("/api/v1/app/orders/{order_id}/pay")
def pay(
    order_id: int,
    user_id: int = Depends(current_user),
    db=Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    order = db.get(Order, order_id)
    if order is None or order.user_id != user_id:
        raise AppError(ErrorCode.NOT_FOUND, "order not found")
    if order.status != PENDING:
        raise AppError(ErrorCode.CONFLICT, "order is not awaiting payment")

    def work():
        record = create_payment(db, order.id, order.total_cents)
        return {"out_trade_no": record.out_trade_no, "amount_cents": record.amount_cents}

    return ok(run_idempotent(db, "payment.create", idempotency_key, work))


@router.post("/api/v1/payments/notify/wechat")
async def notify(request: Request, db=Depends(get_db),
                 signature: str = Header(default="", alias="Wechatpay-Signature")):
    body = await request.body()
    result = handle_notify(db, body, signature, NOTIFY_SECRET, on_paid=mark_paid)
    return {"code": "SUCCESS", "message": result}


@router.get("/api/v1/app/payments/{out_trade_no}")
def status(out_trade_no: str, user_id: int = Depends(current_user), db=Depends(get_db)):
    record = db.execute(select(PaymentRecord).where(PaymentRecord.out_trade_no == out_trade_no)).scalar_one_or_none()
    if record is None:
        raise AppError(ErrorCode.NOT_FOUND, "payment not found")
    order = db.get(Order, record.order_id)
    if order is None or order.user_id != user_id:
        raise AppError(ErrorCode.NOT_FOUND, "payment not found")
    return ok({"out_trade_no": out_trade_no, "status": record.status, "order_status": order.status})
