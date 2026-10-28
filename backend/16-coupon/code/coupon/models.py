"""coupon 数据模型：券模板、用户券、发放日志、发放批次、兑换码、兑换失败计数。

- 库存在模板上：`issued_count` 条件 UPDATE（`total_stock IS NULL OR issued_count < total_stock`），CHECK 兜底。
- 每人限领：用户券 (template_id, user_id, slot) 唯一，slot = 已有张数 + 1；每日限领：(template_id, user_id, claim_day, day_slot) 唯一。
  并发多领由唯一索引裁决，不依赖“先查后插”。
- 发放日志 ref_key 唯一：同一业务键（批次 + 会员、事件、抽奖记录、兑换码）只发一次。
- 用户券保存领取时的规则快照（rules_json + template_version），模板之后的修改不影响已发出的券。
- 兑换码只存 HMAC 哈希（查询、唯一）与密文（导出），不存明文。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from luzback import Base, BigIdMixin, SoftDeleteMixin, TimestampMixin, utcnow

# ---------------------------------------------------------------- 模板

TYPE_AMOUNT_OFF = "amount_off"      # 满减 / 无门槛（threshold_cents = 0）
TYPE_PERCENT_OFF = "percent_off"    # 折扣（rate_bp = 实付比例，8500 = 八五折），可设封顶
TYPE_EXCHANGE = "exchange"          # 兑换券：范围内商品 exchange_qty 件免费（取最便宜的件）
TEMPLATE_TYPES = (TYPE_AMOUNT_OFF, TYPE_PERCENT_OFF, TYPE_EXCHANGE)

SCOPE_ALL = "all"
SCOPE_CATEGORIES = "categories"
SCOPE_PRODUCTS = "products"
SCOPE_PRODUCT_KINDS = "product_kinds"   # 例如 member_card / course / goods
SCOPE_TYPES = (SCOPE_ALL, SCOPE_CATEGORIES, SCOPE_PRODUCTS, SCOPE_PRODUCT_KINDS)

VALID_FIXED = "fixed"         # 固定区间 use_start_at ~ use_end_at
VALID_RELATIVE = "relative"   # 领取后 start_delay_days 天生效，有效 valid_days 天
VALID_TYPES = (VALID_FIXED, VALID_RELATIVE)

CHANNEL_CLAIM_CENTER = "claim_center"   # 会员在领券中心领取
CHANNEL_ADMIN = "admin"                 # 后台定向发放
CHANNEL_EVENT = "event"                 # 事件触发（注册礼等）
CHANNEL_LOTTERY = "lottery"             # 抽奖奖品
CHANNEL_COMPENSATION = "compensation"   # 客诉补偿
CHANNEL_CODE = "code"                   # 兑换码
CHANNELS = (CHANNEL_CLAIM_CENTER, CHANNEL_ADMIN, CHANNEL_EVENT, CHANNEL_LOTTERY, CHANNEL_COMPENSATION, CHANNEL_CODE)
SELF_SERVICE_CHANNELS = (CHANNEL_CLAIM_CENTER, CHANNEL_CODE)  # 受领取期与每日限领约束

AUDIENCE_ALL = "all"
AUDIENCE_NEW_MEMBERS = "new_members"
AUDIENCE_LEVELS = "levels"
AUDIENCE_TAGS = "tags"
AUDIENCE_TYPES = (AUDIENCE_ALL, AUDIENCE_NEW_MEMBERS, AUDIENCE_LEVELS, AUDIENCE_TAGS)

TEMPLATE_DRAFT = "draft"
TEMPLATE_ACTIVE = "active"
TEMPLATE_PAUSED = "paused"
TEMPLATE_ENDED = "ended"
TEMPLATE_STATUSES = (TEMPLATE_DRAFT, TEMPLATE_ACTIVE, TEMPLATE_PAUSED, TEMPLATE_ENDED)

# ---------------------------------------------------------------- 用户券

STATUS_UNUSED = "unused"
STATUS_LOCKED = "locked"
STATUS_USED = "used"
STATUS_EXPIRED = "expired"
STATUS_REVOKED = "revoked"
COUPON_STATUSES = (STATUS_UNUSED, STATUS_LOCKED, STATUS_USED, STATUS_EXPIRED, STATUS_REVOKED)


class CouponTemplate(Base, BigIdMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "coupon_templates"
    __table_args__ = (
        CheckConstraint("issued_count >= 0", name="issued_nonneg"),
        CheckConstraint("used_count >= 0", name="used_nonneg"),
        CheckConstraint("total_stock IS NULL OR issued_count <= total_stock", name="stock_cap"),
        Index("ix_coupon_templates_status", "status"),
        Index("ix_coupon_templates_trigger", "trigger_topic"),
    )

    store_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 空 = 全部门店
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(String(500), default="")
    type: Mapped[str] = mapped_column(String(16))
    amount_cents: Mapped[int] = mapped_column(Integer, default=0)
    rate_bp: Mapped[int] = mapped_column(Integer, default=10000)
    max_discount_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exchange_qty: Mapped[int] = mapped_column(Integer, default=1)
    threshold_cents: Mapped[int] = mapped_column(Integer, default=0)
    scope_type: Mapped[str] = mapped_column(String(16), default=SCOPE_ALL)
    scope_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    exclude_product_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    stack_group: Mapped[str] = mapped_column(String(32), default="")  # 空 = 不与任何券叠加
    stack_with_campaign: Mapped[bool] = mapped_column(Boolean, default=True)
    stack_with_member: Mapped[bool] = mapped_column(Boolean, default=True)
    valid_type: Mapped[str] = mapped_column(String(16), default=VALID_RELATIVE)
    use_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    use_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    start_delay_days: Mapped[int] = mapped_column(Integer, default=0)
    claim_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claim_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    total_stock: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 空 = 不限
    issued_count: Mapped[int] = mapped_column(Integer, default=0)
    used_count: Mapped[int] = mapped_column(Integer, default=0)
    per_member_limit: Mapped[int] = mapped_column(Integer, default=1)        # 0 = 不限
    per_member_daily_limit: Mapped[int] = mapped_column(Integer, default=0)  # 0 = 不限
    audience_json: Mapped[str] = mapped_column(Text, default='{"type": "all"}')
    channels_json: Mapped[str] = mapped_column(Text, default='["claim_center"]')
    trigger_topic: Mapped[str | None] = mapped_column(String(64), nullable=True)  # 事件发放：member.registered 等
    status: Mapped[str] = mapped_column(String(16), default=TEMPLATE_DRAFT)
    version: Mapped[int] = mapped_column(Integer, default=1)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_by: Mapped[int | None] = mapped_column(Integer, nullable=True)


class UserCoupon(Base, BigIdMixin, TimestampMixin):
    __tablename__ = "user_coupons"
    __table_args__ = (
        UniqueConstraint("template_id", "user_id", "slot", name="uq_user_coupons_slot"),
        UniqueConstraint("template_id", "user_id", "claim_day", "day_slot", name="uq_user_coupons_day_slot"),
        Index("ix_user_coupons_user_status", "user_id", "status"),
        Index("ix_user_coupons_order", "order_id"),
        Index("ix_user_coupons_status_valid_to", "status", "valid_to"),
    )

    user_id: Mapped[int] = mapped_column(Integer)
    template_id: Mapped[int] = mapped_column(Integer)
    template_version: Mapped[int] = mapped_column(Integer, default=1)
    name: Mapped[str] = mapped_column(String(64))
    rules_json: Mapped[str] = mapped_column(Text)      # 领取时的规则快照
    slot: Mapped[int] = mapped_column(Integer)
    claim_day: Mapped[str] = mapped_column(String(10))  # 门店时区的领取日 YYYY-MM-DD
    day_slot: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default=STATUS_UNUSED)
    channel: Mapped[str] = mapped_column(String(16))
    source_ref: Mapped[str | None] = mapped_column(String(96), nullable=True)
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    order_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 锁定 / 核销它的订单
    discount_cents: Mapped[int] = mapped_column(Integer, default=0)       # 锁定时计算的抵扣额
    alloc_json: Mapped[str | None] = mapped_column(Text, nullable=True)   # 抵扣在订单行上的分摊
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    returned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoke_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CouponIssueLog(Base, BigIdMixin):
    """按业务键发放的记录：ref_key 唯一保证同一业务键只发一次；source_ref 用于来源退款时收回。"""

    __tablename__ = "coupon_issue_logs"
    __table_args__ = (Index("ix_coupon_issue_logs_source_ref", "source_ref"),)

    ref_key: Mapped[str] = mapped_column(String(128), unique=True)
    template_id: Mapped[int] = mapped_column(Integer)
    user_id: Mapped[int] = mapped_column(Integer)
    channel: Mapped[str] = mapped_column(String(16))
    source_ref: Mapped[str | None] = mapped_column(String(96), nullable=True)
    user_coupon_id: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


BATCH_PENDING = "pending"
BATCH_DONE = "done"
ITEM_PENDING = "pending"
ITEM_ISSUED = "issued"
ITEM_FAILED = "failed"


class CouponIssueBatch(Base, BigIdMixin, TimestampMixin):
    """后台批量发放：异步任务逐个会员发放，(batch_id, user_id) 唯一。"""

    __tablename__ = "coupon_issue_batches"
    __table_args__ = (Index("ix_coupon_issue_batches_status", "status"),)

    template_id: Mapped[int] = mapped_column(Integer, index=True)
    channel: Mapped[str] = mapped_column(String(16), default=CHANNEL_ADMIN)
    reason: Mapped[str] = mapped_column(String(200))
    ignore_limit: Mapped[bool] = mapped_column(Boolean, default=False)
    total: Mapped[int] = mapped_column(Integer, default=0)
    issued: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default=BATCH_PENDING)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CouponIssueBatchItem(Base, BigIdMixin):
    __tablename__ = "coupon_issue_batch_items"
    __table_args__ = (
        UniqueConstraint("batch_id", "user_id", name="uq_coupon_issue_batch_items_member"),
        Index("ix_coupon_issue_batch_items_status", "status", "id"),
    )

    batch_id: Mapped[int] = mapped_column(Integer)
    user_id: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default=ITEM_PENDING)
    error: Mapped[str | None] = mapped_column(String(200), nullable=True)
    user_coupon_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


CODE_UNUSED = "unused"
CODE_USED = "used"
CODE_VOID = "void"


class CouponCodeBatch(Base, BigIdMixin, TimestampMixin):
    __tablename__ = "coupon_code_batches"

    template_id: Mapped[int] = mapped_column(Integer, index=True)
    name: Mapped[str] = mapped_column(String(64))
    count: Mapped[int] = mapped_column(Integer)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CouponCode(Base, BigIdMixin):
    """兑换码：code_hash 唯一（HMAC，查询用），code_enc 密文（导出用）；一码一次（条件 UPDATE unused → used）。"""

    __tablename__ = "coupon_codes"
    __table_args__ = (Index("ix_coupon_codes_batch", "batch_id", "id"),)

    batch_id: Mapped[int] = mapped_column(Integer)
    template_id: Mapped[int] = mapped_column(Integer)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    code_enc: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default=CODE_UNUSED)
    used_by_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    user_coupon_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CouponCodeAttempt(Base, BigIdMixin):
    """兑换码失败计数（防暴力枚举）：(user_id, window) 唯一，条件 UPDATE 累加。"""

    __tablename__ = "coupon_code_attempts"
    __table_args__ = (UniqueConstraint("user_id", "window", name="uq_coupon_code_attempts_user_window"),)

    user_id: Mapped[int] = mapped_column(Integer)
    window: Mapped[int] = mapped_column(Integer)  # epoch 秒 // 窗口长度
    failures: Mapped[int] = mapped_column(Integer, default=0)
