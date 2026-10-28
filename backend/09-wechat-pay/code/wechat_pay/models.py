"""wechat_pay 数据模型：支付单、退款单、回调日志、对账批次与差异、发货信息上传。

- 支付单按业务对象归属（business_type + business_id）；同一业务同一时刻最多一个未完成支付单（部分唯一索引）。
- 支付单上的 refunded_cents / refund_count / last_refund_at 是退款额度与次数的计数器，只用条件 UPDATE 修改。
- 订单、支付、流水不可删除（SPEC §3.5），本模块表都没有 deleted_at。
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from luzback import Base, BigId, BigIdMixin, TimestampMixin, utcnow

from .rules import GOODS_PHYSICAL, PAY_CREATED, REFUND_PENDING

_OPEN = text("status IN ('created', 'paying')")


class PaymentRecord(Base, BigIdMixin, TimestampMixin):
    __tablename__ = "payment_records"
    __table_args__ = (
        Index("ix_payment_records_business", "business_type", "business_id"),
        Index("uq_payment_records_open", "business_type", "business_id", unique=True,
              sqlite_where=_OPEN, postgresql_where=_OPEN),
        Index("ix_payment_records_status_query", "status", "next_query_at"),
    )

    business_type: Mapped[str] = mapped_column(String(32))
    business_id: Mapped[int] = mapped_column(BigId)
    business_no: Mapped[str] = mapped_column(String(64), default="")
    user_id: Mapped[int | None] = mapped_column(BigId, nullable=True)
    store_id: Mapped[int | None] = mapped_column(BigId, nullable=True)
    goods_kind: Mapped[str] = mapped_column(String(16), default=GOODS_PHYSICAL)
    out_trade_no: Mapped[str] = mapped_column(String(32), unique=True)
    amount_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="CNY")
    status: Mapped[str] = mapped_column(String(16), default=PAY_CREATED)
    channel: Mapped[str] = mapped_column(String(16), default="wechat_jsapi")
    description: Mapped[str] = mapped_column(String(127), default="")
    payer_openid: Mapped[str | None] = mapped_column(String(128), nullable=True)
    request_json: Mapped[str] = mapped_column(Text, default="")  # 首次下单报文：prepay 过期后原样重放
    prepay_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prepay_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    trade_state: Mapped[str | None] = mapped_column(String(16), nullable=True)  # 最近一次渠道事实
    channel_txn_id: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    close_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    close_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fail_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    query_count: Mapped[int] = mapped_column(Integer, default=0)
    next_query_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_queried_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    refunded_cents: Mapped[int] = mapped_column(Integer, default=0)       # 已占用（处理中 + 成功）
    refund_succeeded_cents: Mapped[int] = mapped_column(Integer, default=0)
    refund_count: Mapped[int] = mapped_column(Integer, default=0)         # 已使用的退款单号数（含失败）
    last_refund_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    needs_manual: Mapped[bool] = mapped_column(Boolean, default=False)
    manual_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)

    @property
    def order_id(self) -> int:
        """兼容 v1：旧代码把支付单当作订单支付单使用。"""
        return self.business_id


class RefundRecord(Base, BigIdMixin, TimestampMixin):
    __tablename__ = "refund_records"
    __table_args__ = (
        Index("ix_refund_records_payment", "payment_id"),
        Index("ix_refund_records_status_due", "status", "next_query_at"),
    )

    payment_id: Mapped[int] = mapped_column(BigId)
    business_ref: Mapped[str] = mapped_column(String(64), default="")  # 约定 "{module}:{id}"，如 after_sales:12
    out_refund_no: Mapped[str] = mapped_column(String(64), unique=True)
    channel_refund_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    amount_cents: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(80), default="")
    status: Mapped[str] = mapped_column(String(16), default=REFUND_PENDING)
    channel_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    refund_channel: Mapped[str | None] = mapped_column(String(32), nullable=True)
    user_received_account: Mapped[str | None] = mapped_column(String(64), nullable=True)
    requested_by: Mapped[str] = mapped_column(String(64), default="system")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    query_count: Mapped[int] = mapped_column(Integer, default=0)
    next_query_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    succeeded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    needs_manual: Mapped[bool] = mapped_column(Boolean, default=False)


class PayNotifyLog(Base, BigIdMixin):
    """回调与主动查单的处理留痕（只追加）：不记明文资源、不记 openid。"""

    __tablename__ = "pay_notify_logs"
    __table_args__ = (Index("ix_pay_notify_logs_no", "out_trade_no"), Index("ix_pay_notify_logs_received", "received_at"))

    kind: Mapped[str] = mapped_column(String(8))  # pay | refund
    notify_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    out_trade_no: Mapped[str | None] = mapped_column(String(32), nullable=True)
    out_refund_no: Mapped[str | None] = mapped_column(String(64), nullable=True)
    channel_txn_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    serial: Mapped[str | None] = mapped_column(String(64), nullable=True)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    result: Mapped[str] = mapped_column(String(16))  # processed | duplicate | rejected | ignored
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="notify")  # notify | query
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReconBatch(Base, BigIdMixin, TimestampMixin):
    """一天一批对账（交易账单 ALL）。"""

    __tablename__ = "pay_recon_batches"

    bill_date: Mapped[date] = mapped_column(Date, unique=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | creating | done | no_statement | failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    hash_value: Mapped[str | None] = mapped_column(String(64), nullable=True)
    remote_payments: Mapped[int] = mapped_column(Integer, default=0)
    remote_refunds: Mapped[int] = mapped_column(Integer, default=0)
    local_payments: Mapped[int] = mapped_column(Integer, default=0)
    local_refunds: Mapped[int] = mapped_column(Integer, default=0)
    diff_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(200), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ReconDiff(Base, BigIdMixin, TimestampMixin):
    __tablename__ = "pay_recon_diffs"
    __table_args__ = (
        UniqueConstraint("batch_id", "ref_type", "ref_no", "diff_type", name="uq_pay_recon_diffs_key"),
        Index("ix_pay_recon_diffs_date", "bill_date"),
    )

    batch_id: Mapped[int] = mapped_column(BigId)
    bill_date: Mapped[date] = mapped_column(Date)
    diff_type: Mapped[str] = mapped_column(String(24))  # missing_local | missing_remote | amount_mismatch | status_mismatch
    ref_type: Mapped[str] = mapped_column(String(8))    # payment | refund
    ref_no: Mapped[str] = mapped_column(String(64))     # out_trade_no 或 out_refund_no
    out_trade_no: Mapped[str] = mapped_column(String(32), default="")
    local_amount_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    remote_amount_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    local_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    remote_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    resolved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)


class ShippingUpload(Base, BigIdMixin, TimestampMixin):
    """小程序发货信息录入（upload_shipping_info）：每笔支付一条，状态可追踪、任务重试。"""

    __tablename__ = "pay_shipping_uploads"
    __table_args__ = (Index("ix_pay_shipping_status_due", "status", "next_attempt_at"),)

    payment_id: Mapped[int] = mapped_column(BigId, unique=True)
    out_trade_no: Mapped[str] = mapped_column(String(32))
    logistics_type: Mapped[int] = mapped_column(Integer, default=3)
    payload_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | uploaded | failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_errcode: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(200), nullable=True)
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
