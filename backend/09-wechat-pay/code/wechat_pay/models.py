"""wechat_pay 数据模型：支付单、退款单、回调日志。

- 支付单按业务对象（business_type + business_id）归属；同一业务同一时刻只允许一个 pending（部分唯一索引）。
- 退款单用 refunded_cents（支付单上的累计已申请退款额，含处理中）做条件 UPDATE 防止超退。
- 回调日志只追加，记录原始序列号与拒绝原因，用于排查。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from luzback import Base, BigId, BigIdMixin, TimestampMixin

PAY_PENDING = "pending"
PAY_SUCCESS = "success"
PAY_FAILED = "failed"
PAY_CLOSED = "closed"

REFUND_PENDING = "refund_pending"
REFUND_SUCCESS = "refund_success"
REFUND_FAILED = "refund_failed"
REFUND_CLOSED = "closed"

BUSINESS_TYPES = ("order", "recharge", "membership", "reservation")


class PaymentRecord(Base, BigIdMixin, TimestampMixin):
    __tablename__ = "payment_records"
    __table_args__ = (
        Index("ix_payment_records_business", "business_type", "business_id"),
        Index(
            "uq_payment_records_pending", "business_type", "business_id", unique=True,
            sqlite_where=text("status = 'pending'"), postgresql_where=text("status = 'pending'"),
        ),
        Index("ix_payment_records_status_created", "status", "created_at"),
    )

    business_type: Mapped[str] = mapped_column(String(16))
    business_id: Mapped[int] = mapped_column(BigId)
    user_id: Mapped[int | None] = mapped_column(BigId, nullable=True, index=True)
    store_id: Mapped[int | None] = mapped_column(BigId, nullable=True)
    out_trade_no: Mapped[str] = mapped_column(String(32), unique=True)
    amount_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="CNY")
    status: Mapped[str] = mapped_column(String(16), default=PAY_PENDING)
    channel: Mapped[str] = mapped_column(String(16), default="wechat_jsapi")
    description: Mapped[str] = mapped_column(String(127), default="")
    prepay_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    channel_txn_id: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    payer_openid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    channel_closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    close_attempts: Mapped[int] = mapped_column(Integer, default=0)
    refunded_cents: Mapped[int] = mapped_column(Integer, default=0)  # 累计已申请退款（含处理中），用于条件 UPDATE 防超退
    needs_manual: Mapped[bool] = mapped_column(Boolean, default=False)
    manual_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    business_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def order_id(self) -> int:
        """兼容 v1：旧代码把支付单当作订单支付单使用。"""
        return self.business_id


class RefundRecord(Base, BigIdMixin, TimestampMixin):
    __tablename__ = "refund_records"
    __table_args__ = (
        Index("ix_refund_records_payment", "payment_id"),
        Index("ix_refund_records_status", "status"),
    )

    payment_id: Mapped[int] = mapped_column(BigId)
    business_ref: Mapped[str] = mapped_column(String(64), default="")  # 约定 "{module}:{id}"，如 after_sales:12
    out_refund_no: Mapped[str] = mapped_column(String(32), unique=True)
    amount_cents: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(80), default="")
    status: Mapped[str] = mapped_column(String(16), default=REFUND_PENDING)
    channel_refund_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(200), nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # 首次成功提交渠道的时间；NULL 表示尚未提交
    succeeded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    needs_manual: Mapped[bool] = mapped_column(Boolean, default=False)


class PayNotifyLog(Base, BigIdMixin):
    __tablename__ = "pay_notify_logs"
    __table_args__ = (Index("ix_pay_notify_logs_no", "out_trade_no"),)

    kind: Mapped[str] = mapped_column(String(8))  # pay | refund
    out_trade_no: Mapped[str | None] = mapped_column(String(32), nullable=True)
    channel_txn_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    serial: Mapped[str | None] = mapped_column(String(64), nullable=True)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    result: Mapped[str] = mapped_column(String(16))  # processed | duplicate | rejected
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="notify")  # notify | query | legacy
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
