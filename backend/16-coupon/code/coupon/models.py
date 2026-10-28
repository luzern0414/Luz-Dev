"""coupon 数据模型：券模板、用户券、发放日志、兑换码。

- 库存在模板上（remaining），领取 / 发放用条件 UPDATE 扣减。
- 用户券按 (template_id, user_id, slot) 唯一，slot 从 1 到 per_user_limit，并发多领由唯一索引裁决。
- 发放日志 ref_key 唯一：同一业务键（定向批次、新人、抽奖）只发一次。
- 兑换码一码一次：码行 status 条件 UPDATE unused → used。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from luzback import Base, BigIdMixin, SoftDeleteMixin, TimestampMixin

# 模板类型
TYPE_AMOUNT = "amount"
TYPE_PERCENT = "percent"
TYPE_FREE_DELIVERY = "free_delivery"
TYPE_EXCHANGE = "exchange"
TYPES = {TYPE_AMOUNT, TYPE_PERCENT, TYPE_FREE_DELIVERY, TYPE_EXCHANGE}

# 适用范围
SCOPE_ALL = "all"
SCOPE_CATEGORY = "category"
SCOPE_PRODUCT = "product"
SCOPES = {SCOPE_ALL, SCOPE_CATEGORY, SCOPE_PRODUCT}

# 有效期类型
VALID_FIXED = "fixed"
VALID_RELATIVE = "relative"
VALID_TYPES = {VALID_FIXED, VALID_RELATIVE}

# 领取方式
MODE_PUBLIC = "public"
MODE_TARGETED = "targeted"
MODE_NEW_USER = "new_user"
MODE_ACTIVITY = "activity"
MODE_CODE = "code"
MODES = {MODE_PUBLIC, MODE_TARGETED, MODE_NEW_USER, MODE_ACTIVITY, MODE_CODE}

# 模板状态
TEMPLATE_DRAFT = "draft"
TEMPLATE_ACTIVE = "active"
TEMPLATE_PAUSED = "paused"
TEMPLATE_ENDED = "ended"
TEMPLATE_STATUSES = {TEMPLATE_DRAFT, TEMPLATE_ACTIVE, TEMPLATE_PAUSED, TEMPLATE_ENDED}

# 用户券状态
STATUS_UNUSED = "unused"
STATUS_LOCKED = "locked"
STATUS_USED = "used"
STATUS_EXPIRED = "expired"
STATUS_REVOKED = "revoked"
COUPON_STATUSES = {STATUS_UNUSED, STATUS_LOCKED, STATUS_USED, STATUS_EXPIRED, STATUS_REVOKED}

# 用户券来源
SOURCE_CLAIM = "claim"
SOURCE_ISSUE = "issue"
SOURCE_NEW_USER = "new_user"
SOURCE_LOTTERY = "lottery"
SOURCE_CAMPAIGN = "campaign"
SOURCE_CODE = "code"

CHANNELS = {"dine_in", "delivery", "pickup"}


class CouponTemplate(Base, BigIdMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "coupon_templates"
    __table_args__ = (Index("ix_coupon_templates_store_status", "store_id", "status"),)

    store_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 空为全店通用
    name: Mapped[str] = mapped_column(String(64))
    type: Mapped[str] = mapped_column(String(16), default=TYPE_AMOUNT)
    value: Mapped[int] = mapped_column(Integer, default=0)  # amount: 面额分；percent: 优惠比例万分比（2000 = 优惠 20%）
    max_discount_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    threshold_cents: Mapped[int] = mapped_column(Integer, default=0)
    scope: Mapped[str] = mapped_column(String(16), default=SCOPE_ALL)
    scope_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    channels_json: Mapped[str] = mapped_column(Text, default="[]")  # 空为全部渠道
    valid_type: Mapped[str] = mapped_column(String(16), default=VALID_FIXED)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # 领取期开始 / fixed 券有效期开始
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)    # 领取期结束 / fixed 券有效期结束
    valid_days: Mapped[int | None] = mapped_column(Integer, nullable=True)                       # relative：领取后 N 天
    per_user_limit: Mapped[int] = mapped_column(Integer, default=1)
    total: Mapped[int] = mapped_column(Integer, default=0)
    remaining: Mapped[int] = mapped_column(Integer, default=0)
    claim_mode: Mapped[str] = mapped_column(String(16), default=MODE_PUBLIC)
    stack_with_member: Mapped[bool] = mapped_column(Boolean, default=True)
    stack_with_campaign: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(16), default=TEMPLATE_DRAFT)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_by: Mapped[int | None] = mapped_column(Integer, nullable=True)


class UserCoupon(Base, BigIdMixin, TimestampMixin):
    __tablename__ = "user_coupons"
    __table_args__ = (
        UniqueConstraint("template_id", "user_id", "slot", name="uq_user_coupon_slot"),
        Index("ix_user_coupons_user_status", "user_id", "status"),
        Index("ix_user_coupons_locked_order", "locked_order_id"),
        Index("ix_user_coupons_used_order", "used_order_id"),
    )

    user_id: Mapped[int] = mapped_column(Integer)
    template_id: Mapped[int] = mapped_column(Integer)
    slot: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default=STATUS_UNUSED)
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    locked_order_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    used_order_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source: Mapped[str] = mapped_column(String(16), default=SOURCE_CLAIM)


class CouponIssueLog(Base, BigIdMixin):
    __tablename__ = "coupon_issue_logs"
    __table_args__ = (Index("ix_coupon_issue_logs_template_user", "template_id", "user_id"),)

    template_id: Mapped[int] = mapped_column(Integer)
    user_id: Mapped[int] = mapped_column(Integer)
    ref_key: Mapped[str] = mapped_column(String(96), unique=True)
    kind: Mapped[str] = mapped_column(String(16))
    user_coupon_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CouponCode(Base, BigIdMixin):
    __tablename__ = "coupon_codes"

    template_id: Mapped[int] = mapped_column(Integer, index=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    status: Mapped[str] = mapped_column(String(16), default=STATUS_UNUSED)  # unused | used
    used_by_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    user_coupon_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    batch: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
