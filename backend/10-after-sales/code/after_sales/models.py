"""after_sales 数据模型。

- `after_sales`：售后单（仅退款 / 退货退款），金额整数分；退款额度占用在订单行上（08 reserve_refund 条件 UPDATE）。
- `after_sale_lines`：本单涉及的订单行、数量、金额、是否回库。
- `after_sale_logs`：状态变化日志（只追加）。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from luzback import Base, BigId, BigIdMixin, TimestampMixin, utcnow

from .rules import PENDING_REVIEW


class AfterSale(Base, BigIdMixin, TimestampMixin):
    __tablename__ = "after_sales"
    __table_args__ = (
        Index("ix_after_sales_order", "order_id"),
        Index("ix_after_sales_user_status", "user_id", "status"),
        Index("ix_after_sales_status_due", "status", "due_at"),
        Index("ix_after_sales_store_created", "store_id", "created_at"),
    )

    after_sale_no: Mapped[str] = mapped_column(String(32), unique=True)
    order_id: Mapped[int] = mapped_column(BigId)
    order_no: Mapped[str] = mapped_column(String(32))
    user_id: Mapped[int] = mapped_column(BigId)
    store_id: Mapped[int | None] = mapped_column(BigId, nullable=True)
    type: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default=PENDING_REVIEW)
    reason_code: Mapped[str] = mapped_column(String(24))
    reason_text: Mapped[str] = mapped_column(String(200), default="")
    evidence_json: Mapped[str] = mapped_column(String(500), default="[]")     # 21-media 的 media id 列表
    amount_cents: Mapped[int] = mapped_column(Integer)
    route: Mapped[str] = mapped_column(String(16))                           # wechat | balance（原路）
    applied_by: Mapped[str] = mapped_column(String(8), default="user")      # user | staff
    applicant_staff_id: Mapped[int | None] = mapped_column(BigId, nullable=True)
    reviewed_by: Mapped[int | None] = mapped_column(BigId, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reject_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # 自动同意 / 寄回截止 / 下次提交退款
    return_company: Mapped[str | None] = mapped_column(String(64), nullable=True)
    return_tracking_no: Mapped[str | None] = mapped_column(String(64), nullable=True)
    return_shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    return_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    received_by: Mapped[int | None] = mapped_column(BigId, nullable=True)
    refund_id: Mapped[int | None] = mapped_column(BigId, nullable=True)          # 09 refund_records.id
    out_refund_no: Mapped[str | None] = mapped_column(String(64), nullable=True)
    refund_attempts: Mapped[int] = mapped_column(Integer, default=0)
    refund_error: Mapped[str | None] = mapped_column(String(200), nullable=True)
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    settle_note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=0)


class AfterSaleLine(Base, BigIdMixin):
    __tablename__ = "after_sale_lines"
    __table_args__ = (Index("ix_after_sale_lines_case", "after_sale_id"), Index("ix_after_sale_lines_item", "order_item_id"))

    after_sale_id: Mapped[int] = mapped_column(BigId)
    order_item_id: Mapped[int] = mapped_column(BigId)
    kind: Mapped[str] = mapped_column(String(24))
    name: Mapped[str] = mapped_column(String(128))
    qty: Mapped[int] = mapped_column(Integer)
    amount_cents: Mapped[int] = mapped_column(Integer)
    restock: Mapped[bool] = mapped_column(Boolean, default=False)
    fulfil_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)


class AfterSaleLog(Base, BigIdMixin):
    __tablename__ = "after_sale_logs"
    __table_args__ = (Index("ix_after_sale_logs_case", "after_sale_id", "id"),)

    after_sale_id: Mapped[int] = mapped_column(BigId)
    from_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    to_status: Mapped[str] = mapped_column(String(20))
    actor_type: Mapped[str] = mapped_column(String(16))
    actor_id: Mapped[str] = mapped_column(String(64), default="")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
