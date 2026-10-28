"""会员卡表结构：卡种、购卡单、卡、使用记录、流水（只追加）、冻结、转让、退款、到期提醒去重。

计量单位：次卡按“次”，期限卡 / 时段卡按“天”。paid_units = 付费部分，gift_units = 赠送部分，total_units = 二者之和
（后台调整增加的次数 / 天数计入赠送部分）。金额单位为分。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from luzback import Base, BigId, BigIdMixin, SoftDeleteMixin, TimestampMixin, utcnow

KIND_COUNT = "count"
KIND_PERIOD = "period"
KIND_TIME_SLOT = "time_slot"
KINDS = (KIND_COUNT, KIND_PERIOD, KIND_TIME_SLOT)

ACT_ON_PURCHASE = "on_purchase"
ACT_ON_FIRST_USE = "on_first_use"
ACT_ON_DATE = "on_date"
ACTIVATIONS = (ACT_ON_PURCHASE, ACT_ON_FIRST_USE, ACT_ON_DATE)

SCOPE_SINGLE = "single"
SCOPE_MULTI = "multi"
SCOPE_ALL = "all"

# 卡状态
S_INACTIVE = "inactive"     # 未激活（首次使用激活）
S_ACTIVE = "active"
S_FROZEN = "frozen"         # 请假冻结
S_LOST = "lost"             # 挂失
S_USED_UP = "used_up"       # 次数用完
S_EXPIRED = "expired"
S_REFUNDING = "refunding"
S_REFUNDED = "refunded"     # 终态
S_DISABLED = "disabled"     # 后台停用
CARD_STATUSES = (S_INACTIVE, S_ACTIVE, S_FROZEN, S_LOST, S_USED_UP, S_EXPIRED, S_REFUNDING, S_REFUNDED, S_DISABLED)

# 发卡来源
SRC_PURCHASE = "purchase"
SRC_MANUAL = "manual"
SRC_GIFT = "gift"

# 购卡单
O_PENDING = "pending"
O_PAID = "paid"
O_CLOSED = "closed"
ORDER_PURCHASE = "purchase"
ORDER_TRANSFER_FEE = "transfer_fee"

# 退款单
R_PENDING = "pending_approval"
R_PROCESSING = "processing"
R_SUCCEEDED = "succeeded"
R_FAILED = "failed"
R_REJECTED = "rejected"


class CardProduct(Base, BigIdMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "member_card_products"

    store_id: Mapped[int | None] = mapped_column(BigId, nullable=True, index=True)  # 归属门店，NULL = 品牌级
    name: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16))
    price_cents: Mapped[int] = mapped_column(Integer)                 # 实付价
    original_price_cents: Mapped[int] = mapped_column(Integer)        # 打折前价格（消费者原因退款按此计价）
    sessions: Mapped[int] = mapped_column(Integer, default=0)         # 次卡付费次数
    gift_sessions: Mapped[int] = mapped_column(Integer, default=0)
    period_days: Mapped[int] = mapped_column(Integer, default=0)      # 期限卡 / 时段卡付费天数
    gift_days: Mapped[int] = mapped_column(Integer, default=0)
    valid_days: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 次卡有效期，NULL = 不限
    slot_windows_json: Mapped[str] = mapped_column(Text, default="[]")     # 时段卡：[{"weekdays":[1..7],"start":"06:00","end":"10:00"}]
    activation_mode: Mapped[str] = mapped_column(String(16), default=ACT_ON_PURCHASE)
    activation_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    activation_deadline_days: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 首次使用激活：N 天未用自动激活
    renew_stack: Mapped[bool] = mapped_column(default=True)           # 续购接在同卡种最晚到期之后
    store_scope: Mapped[str] = mapped_column(String(8), default=SCOPE_SINGLE)
    store_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    store_ids_key: Mapped[str] = mapped_column(String(500), default="")  # ",1,5," 便于 LIKE 过滤适用门店
    per_member_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sale_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sale_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(8), default="off")     # on | off
    goods_kind: Mapped[str] = mapped_column(String(20), default="offline_service")
    transferable: Mapped[bool] = mapped_column(default=True)
    transfer_fee_cents: Mapped[int] = mapped_column(Integer, default=0)
    max_transfers: Mapped[int] = mapped_column(Integer, default=1)
    transfer_cooling_days: Mapped[int] = mapped_column(Integer, default=0)
    freezable: Mapped[bool] = mapped_column(default=True)
    freeze_min_days: Mapped[int] = mapped_column(Integer, default=7)
    freeze_max_days: Mapped[int] = mapped_column(Integer, default=30)
    freeze_max_times: Mapped[int] = mapped_column(Integer, default=2)
    freeze_total_max_days: Mapped[int] = mapped_column(Integer, default=60)
    refund_fee_bp: Mapped[int] = mapped_column(Integer, default=0)    # 仅消费者原因
    entry_cooldown_minutes: Mapped[int] = mapped_column(Integer, default=0)  # 期限卡同卡两次使用最小间隔（防借卡）
    terms_text: Mapped[str] = mapped_column(Text, default="")
    sort: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[int | None] = mapped_column(BigId, nullable=True)
    updated_by: Mapped[int | None] = mapped_column(BigId, nullable=True)


class CardOrder(Base, BigIdMixin, TimestampMixin):
    __tablename__ = "member_card_orders"
    __table_args__ = (Index("ix_member_card_orders_user_product", "user_id", "product_id", "status"),)

    order_no: Mapped[str] = mapped_column(String(32), unique=True)
    kind: Mapped[str] = mapped_column(String(16), default=ORDER_PURCHASE)
    user_id: Mapped[int] = mapped_column(BigId)
    store_id: Mapped[int | None] = mapped_column(BigId, nullable=True)
    product_id: Mapped[int] = mapped_column(BigId)
    card_id: Mapped[int | None] = mapped_column(BigId, nullable=True)          # 转让手续费单：被转让的卡
    target_user_id: Mapped[int | None] = mapped_column(BigId, nullable=True)   # 转让手续费单：受让人
    amount_cents: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(8), default=O_PENDING, index=True)
    payment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    out_trade_no: Mapped[str | None] = mapped_column(String(64), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    needs_manual: Mapped[bool] = mapped_column(default=False)
    product_snapshot_json: Mapped[str] = mapped_column(Text, default="{}")
    goods_kind: Mapped[str] = mapped_column(String(20), default="offline_service")
    invoice_tax_code: Mapped[str | None] = mapped_column(String(8), nullable=True)
    invoice_note: Mapped[str] = mapped_column(String(64), default="")


class MemberCard(Base, BigIdMixin, TimestampMixin):
    __tablename__ = "member_card_cards"
    __table_args__ = (
        CheckConstraint("sessions_remaining >= 0", name="sessions_nonneg"),
        CheckConstraint("sessions_used >= 0", name="used_nonneg"),
        Index("ix_member_card_cards_user_status", "user_id", "status"),
        Index("ix_member_card_cards_status_ends", "status", "ends_at"),
    )

    card_no: Mapped[str] = mapped_column(String(20), unique=True)
    user_id: Mapped[int] = mapped_column(BigId)
    product_id: Mapped[int] = mapped_column(BigId, index=True)
    store_id: Mapped[int | None] = mapped_column(BigId, nullable=True, index=True)  # 开卡门店
    kind: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default=S_ACTIVE)
    prev_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    source: Mapped[str] = mapped_column(String(16), default=SRC_PURCHASE)
    issue_ref: Mapped[str] = mapped_column(String(64), unique=True)
    order_id: Mapped[int | None] = mapped_column(BigId, nullable=True)
    payment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    paid_cents: Mapped[int] = mapped_column(Integer, default=0)
    original_price_cents: Mapped[int] = mapped_column(Integer, default=0)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lpr_bp: Mapped[int] = mapped_column(Integer, default=0)
    paid_units: Mapped[int] = mapped_column(Integer, default=0)
    gift_units: Mapped[int] = mapped_column(Integer, default=0)
    total_units: Mapped[int] = mapped_column(Integer, default=0)
    sessions_remaining: Mapped[int] = mapped_column(Integer, default=0)
    sessions_used: Mapped[int] = mapped_column(Integer, default=0)
    use_count: Mapped[int] = mapped_column(Integer, default=0)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    activation_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    store_scope: Mapped[str] = mapped_column(String(8), default=SCOPE_SINGLE)
    store_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    slot_windows_json: Mapped[str] = mapped_column(Text, default="[]")
    entry_cooldown_minutes: Mapped[int] = mapped_column(Integer, default=0)
    freeze_count: Mapped[int] = mapped_column(Integer, default=0)
    frozen_days_total: Mapped[int] = mapped_column(Integer, default=0)
    transfer_count: Mapped[int] = mapped_column(Integer, default=0)
    last_transfer_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_store_id: Mapped[int | None] = mapped_column(BigId, nullable=True)
    holder_name: Mapped[str] = mapped_column(String(64), default="")
    holder_phone_masked: Mapped[str] = mapped_column(String(20), default="")
    holder_phone_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    product_snapshot_json: Mapped[str] = mapped_column(Text, default="{}")
    terms_snapshot: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[int] = mapped_column(Integer, default=0)


class CardUsage(Base, BigIdMixin):
    __tablename__ = "member_card_usages"

    card_id: Mapped[int] = mapped_column(BigId, index=True)
    user_id: Mapped[int] = mapped_column(BigId, index=True)
    store_id: Mapped[int | None] = mapped_column(BigId, nullable=True)
    ref: Mapped[str] = mapped_column(String(64), unique=True)
    units: Mapped[int] = mapped_column(Integer, default=0)            # 次卡扣次数；期限卡 0
    sessions_after: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(10), default="done")   # done | reversed
    operator: Mapped[str] = mapped_column(String(32), default="")
    used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reverse_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)


class CardLedger(Base, BigIdMixin):
    """卡流水：只追加，不更新不删除。"""

    __tablename__ = "member_card_ledger"
    __table_args__ = (UniqueConstraint("ref", "kind", name="uq_member_card_ledger_ref_kind"),
                      Index("ix_member_card_ledger_card", "card_id", "id"))

    card_id: Mapped[int] = mapped_column(BigId)
    user_id: Mapped[int] = mapped_column(BigId, index=True)
    kind: Mapped[str] = mapped_column(String(20), index=True)
    delta_sessions: Mapped[int] = mapped_column(Integer, default=0)
    delta_days: Mapped[int] = mapped_column(Integer, default=0)
    sessions_after: Mapped[int] = mapped_column(Integer, default=0)
    ends_at_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status_after: Mapped[str] = mapped_column(String(16), default="")
    ref: Mapped[str] = mapped_column(String(64))
    store_id: Mapped[int | None] = mapped_column(BigId, nullable=True)
    operator: Mapped[str] = mapped_column(String(32), default="system")
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class CardFreeze(Base, BigIdMixin, TimestampMixin):
    __tablename__ = "member_card_freezes"

    card_id: Mapped[int] = mapped_column(BigId, index=True)
    user_id: Mapped[int] = mapped_column(BigId)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    planned_end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    planned_days: Mapped[int] = mapped_column(Integer)
    actual_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    actual_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(10), default="active", index=True)  # active | ended
    reason: Mapped[str] = mapped_column(String(200), default="")
    operator: Mapped[str] = mapped_column(String(32), default="")


class CardTransfer(Base, BigIdMixin):
    __tablename__ = "member_card_transfers"

    card_id: Mapped[int] = mapped_column(BigId, index=True)
    from_user_id: Mapped[int] = mapped_column(BigId, index=True)
    to_user_id: Mapped[int] = mapped_column(BigId, index=True)
    fee_cents: Mapped[int] = mapped_column(Integer, default=0)
    fee_order_id: Mapped[int | None] = mapped_column(BigId, nullable=True)
    fee_collected: Mapped[str] = mapped_column(String(10), default="none")  # none | online | offline
    sessions_remaining: Mapped[int] = mapped_column(Integer, default=0)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    operator: Mapped[str] = mapped_column(String(32), default="")
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CardRefund(Base, BigIdMixin, TimestampMixin):
    __tablename__ = "member_card_refunds"

    refund_no: Mapped[str] = mapped_column(String(32), unique=True)
    card_id: Mapped[int] = mapped_column(BigId, index=True)
    user_id: Mapped[int] = mapped_column(BigId, index=True)
    cause: Mapped[str] = mapped_column(String(16))
    rule: Mapped[str] = mapped_column(String(32))
    refund_cents: Mapped[int] = mapped_column(Integer, default=0)
    interest_cents: Mapped[int] = mapped_column(Integer, default=0)
    fee_cents: Mapped[int] = mapped_column(Integer, default=0)
    route: Mapped[str] = mapped_column(String(10), default="original")
    status: Mapped[str] = mapped_column(String(20), default=R_PENDING, index=True)
    breakdown_json: Mapped[str] = mapped_column(Text, default="{}")
    reason: Mapped[str] = mapped_column(String(200), default="")
    requested_by: Mapped[str] = mapped_column(String(32), default="")
    approved_by: Mapped[int | None] = mapped_column(BigId, nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fail_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    overdue_notified: Mapped[bool] = mapped_column(default=False)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # 已提交支付模块
    channel_refund_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    submit_error: Mapped[str | None] = mapped_column(String(200), nullable=True)


class CardReminder(Base, BigIdMixin):
    __tablename__ = "member_card_reminders"
    __table_args__ = (UniqueConstraint("card_id", "days_before", "ends_at_key", name="uq_member_card_reminder"),)

    card_id: Mapped[int] = mapped_column(BigId)
    days_before: Mapped[int] = mapped_column(Integer)
    ends_at_key: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
