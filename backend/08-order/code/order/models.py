"""order 数据模型。

- `orders`：订单头。金额整数分；主状态 `status` 与退款维度 `refund_status` 分开（芋道 trade 的做法，R7 §B-2）。
- `order_items`：行快照（名称 / 规格 / 单价 / 数量）+ 优惠分摊（活动、券）+ 行实付 `paid_cents`；
  退款额度计数 `refund_reserved_*`（申请占用）与 `refunded_*`（已退）只由本模块的条件 UPDATE 修改。
- `order_status_logs`：每次状态变化一行（只追加）。
- `order_quotes`：试算结果（quote_token，过期时间，一次性使用）。
- `order_purchase_counters`：每人限购计数（唯一 (user_id, product_key)，条件自增）。
- `order_notes`：后台备注（只对员工可见）。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from luzback import Base, BigId, BigIdMixin, TimestampMixin, utcnow

from .rules import PENDING, REFUND_NONE


class Order(Base, BigIdMixin, TimestampMixin):
    __tablename__ = "orders"
    __table_args__ = (
        Index("ix_orders_user_status", "user_id", "status"),
        Index("ix_orders_status_deadline", "status", "pay_deadline_at"),
        Index("ix_orders_store_created", "store_id", "created_at"),
    )

    order_no: Mapped[str] = mapped_column(String(32), unique=True)
    user_id: Mapped[int] = mapped_column(BigId)
    store_id: Mapped[int | None] = mapped_column(BigId, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default=PENDING)
    refund_status: Mapped[str] = mapped_column(String(24), default=REFUND_NONE)
    kind: Mapped[str] = mapped_column(String(24), default="goods")      # 行种类；多种混合为 mixed
    goods_kind: Mapped[str] = mapped_column(String(20), default="physical")
    items_cents: Mapped[int] = mapped_column(Integer, default=0)
    campaign_discount_cents: Mapped[int] = mapped_column(Integer, default=0)
    coupon_cents: Mapped[int] = mapped_column(Integer, default=0)
    total_cents: Mapped[int] = mapped_column(Integer)
    paid_cents: Mapped[int] = mapped_column(Integer, default=0)
    refunded_cents: Mapped[int] = mapped_column(Integer, default=0)
    refund_reserved_cents: Mapped[int] = mapped_column(Integer, default=0)  # 已占用（含已退）；处理中 = 占用 − 已退
    pay_method: Mapped[str | None] = mapped_column(String(16), nullable=True)   # wechat | balance | free
    payment_id: Mapped[int | None] = mapped_column(BigId, nullable=True)
    out_trade_no: Mapped[str | None] = mapped_column(String(64), nullable=True)
    coupon_ids_json: Mapped[str] = mapped_column(String(200), default="[]")
    pay_deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fulfilling_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fulfilled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_type: Mapped[str | None] = mapped_column(String(16), nullable=True)  # user | timeout | admin
    cancel_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    express_company: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tracking_no: Mapped[str | None] = mapped_column(String(64), nullable=True)
    shipping_uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    remark: Mapped[str] = mapped_column(String(200), default="")
    address_snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    buyer_phone_masked: Mapped[str | None] = mapped_column(String(24), nullable=True)
    buyer_phone_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    terms_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    terms_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    quote_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    needs_manual: Mapped[bool] = mapped_column(Boolean, default=False)
    manual_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=0)


class OrderItem(Base, BigIdMixin):
    __tablename__ = "order_items"
    __table_args__ = (Index("ix_order_items_order", "order_id"),)

    order_id: Mapped[int] = mapped_column(BigId)
    user_id: Mapped[int] = mapped_column(BigId)
    kind: Mapped[str] = mapped_column(String(24))                      # goods | service | member_card | …
    goods_kind: Mapped[str] = mapped_column(String(20))                # physical | offline_service | virtual_content
    product_id: Mapped[int] = mapped_column(BigId)
    sku_id: Mapped[int | None] = mapped_column(BigId, nullable=True)
    name: Mapped[str] = mapped_column(String(128))
    spec: Mapped[str] = mapped_column(String(128), default="")
    image: Mapped[str] = mapped_column(String(500), default="")
    unit_price_cents: Mapped[int] = mapped_column(Integer)
    qty: Mapped[int] = mapped_column(Integer)
    gross_cents: Mapped[int] = mapped_column(Integer)
    campaign_alloc_cents: Mapped[int] = mapped_column(Integer, default=0)
    coupon_alloc_cents: Mapped[int] = mapped_column(Integer, default=0)
    paid_cents: Mapped[int] = mapped_column(Integer)                   # 行实付分摊 = gross − 活动 − 券
    returnable: Mapped[bool] = mapped_column(Boolean, default=True)    # 是否适用七日无理由
    snapshot_json: Mapped[str] = mapped_column(Text, default="{}")
    fulfil_status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | fulfilled
    fulfil_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)  # 例：member_card:12
    fulfilled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    pay_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)     # 余额支付流水 ref（按行）
    refund_reserved_qty: Mapped[int] = mapped_column(Integer, default=0)
    refund_reserved_cents: Mapped[int] = mapped_column(Integer, default=0)
    refunded_qty: Mapped[int] = mapped_column(Integer, default=0)
    refunded_cents: Mapped[int] = mapped_column(Integer, default=0)


class OrderStatusLog(Base, BigIdMixin):
    __tablename__ = "order_status_logs"
    __table_args__ = (Index("ix_order_status_logs_order", "order_id", "id"),)

    order_id: Mapped[int] = mapped_column(BigId)
    from_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    to_status: Mapped[str] = mapped_column(String(24))
    actor_type: Mapped[str] = mapped_column(String(16))  # user | staff | system | payment
    actor_id: Mapped[str] = mapped_column(String(64), default="")
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OrderQuote(Base, BigIdMixin):
    __tablename__ = "order_quotes"
    __table_args__ = (Index("ix_order_quotes_user", "user_id", "id"),)

    token: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[int] = mapped_column(BigId)
    store_id: Mapped[int | None] = mapped_column(BigId, nullable=True)
    request_json: Mapped[str] = mapped_column(Text)
    request_hash: Mapped[str] = mapped_column(String(64))
    result_json: Mapped[str] = mapped_column(Text)
    total_cents: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    order_id: Mapped[int | None] = mapped_column(BigId, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PurchaseCounter(Base, BigIdMixin):
    __tablename__ = "order_purchase_counters"
    __table_args__ = (UniqueConstraint("user_id", "product_key", name="uq_order_purchase_counters_user_product"),)

    user_id: Mapped[int] = mapped_column(BigId)
    product_key: Mapped[str] = mapped_column(String(64))   # kind:product_id
    qty: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class OrderNote(Base, BigIdMixin):
    __tablename__ = "order_notes"
    __table_args__ = (Index("ix_order_notes_order", "order_id"),)

    order_id: Mapped[int] = mapped_column(BigId)
    staff_id: Mapped[int] = mapped_column(BigId)
    text: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
