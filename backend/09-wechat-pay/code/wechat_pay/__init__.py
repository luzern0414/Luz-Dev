"""支付模块：支付单、回调验签、回调幂等、金额校验。

真实接入（微信支付 v3 为例）时：
- verify_notify_signature 替换为平台证书 RSA-SHA256 验签（见 backend/payment/MODULE.md §4），本文件给出的 HMAC 实现仅用于本地联调。
- 回调处理必须“先验签、再验金额、再流转订单”，任何一步失败都不改变订单状态。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import datetime, timezone

from sqlalchemy import Integer, String, select, update
from sqlalchemy.orm import Mapped, Session, mapped_column

from luzback import AppError, Base, ErrorCode

PAY_PENDING = "pending"
PAY_SUCCESS = "success"
PAY_FAILED = "failed"


class PaymentRecord(Base):
    __tablename__ = "payment_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(Integer, index=True)
    out_trade_no: Mapped[str] = mapped_column(String(40), unique=True)
    amount_cents: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default=PAY_PENDING)
    channel_txn_id: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    paid_at: Mapped[datetime | None] = mapped_column(nullable=True)


def create_payment(db: Session, order_id: int, amount_cents: int) -> PaymentRecord:
    """创建支付单（调用方 commit）。out_trade_no 全局唯一，既是对外单号也是幂等依据。"""
    if amount_cents <= 0:
        raise AppError(ErrorCode.BAD_REQUEST, "amount must be positive")
    record = PaymentRecord(
        order_id=order_id,
        out_trade_no=f"P{datetime.now(timezone.utc):%Y%m%d%H%M%S}{secrets.token_hex(4)}",
        amount_cents=amount_cents,
    )
    db.add(record)
    db.flush()
    return record


def sign_payload(body: bytes, secret: str) -> str:
    """HMAC-SHA256 签名，仅用于本地联调与测试。"""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_notify_signature(body: bytes, signature: str, secret: str) -> None:
    if not hmac.compare_digest(sign_payload(body, secret), signature or ""):
        raise AppError(ErrorCode.UNAUTHORIZED, "invalid notify signature")


def handle_notify(db: Session, body: bytes, signature: str, secret: str, on_paid) -> str:
    """处理支付平台回调。on_paid(db, order_id) 在同一事务内流转订单。

    返回 "processed" 或 "duplicate"。支付平台会重复回调，必须幂等。
    """
    verify_notify_signature(body, signature, secret)
    payload = json.loads(body)
    out_trade_no = payload["out_trade_no"]
    txn_id = payload["transaction_id"]
    amount = int(payload["amount_cents"])

    record = db.execute(
        select(PaymentRecord).where(PaymentRecord.out_trade_no == out_trade_no)
    ).scalar_one_or_none()
    if record is None:
        raise AppError(ErrorCode.NOT_FOUND, "payment not found")
    if record.status == PAY_SUCCESS:
        return "duplicate"
    if amount != record.amount_cents:
        # 金额不一致：拒绝，不改订单；需要人工对账
        raise AppError(ErrorCode.BUSINESS_RULE, "amount mismatch", data={"out_trade_no": out_trade_no})

    # 条件更新：只有 pending 才能变成 success，并发回调只有一个能成功
    result = db.execute(
        update(PaymentRecord)
        .where(PaymentRecord.id == record.id, PaymentRecord.status == PAY_PENDING)
        .values(status=PAY_SUCCESS, channel_txn_id=txn_id, paid_at=datetime.now(timezone.utc))
    )
    if result.rowcount != 1:
        db.rollback()
        return "duplicate"
    on_paid(db, record.order_id)
    db.commit()
    return "processed"
