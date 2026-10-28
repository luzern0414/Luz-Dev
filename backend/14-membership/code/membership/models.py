"""membership 数据模型：会员等级、会员、积分账户、积分台账、付费会员订单、对账差异。

- 余额只通过条件 UPDATE 变动（见 service.py）；`points_ledger.ref_key` 唯一是幂等的最后一道防线。
- `remaining` 记录每个入账批次尚未用掉的积分，用于 FIFO 过期；全部入账行的 Σremaining == balance。
- `debt`（欠分）：退款追回时余额不足的差额，后续入账先抵销。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from luzback import Base, BigIdMixin, SoftDeleteMixin, TimestampMixin, utcnow

THRESHOLD_FREE = "free"
THRESHOLD_SPEND = "spend"
THRESHOLD_POINTS = "points"
THRESHOLD_PAID = "paid"
THRESHOLD_TYPES = {THRESHOLD_FREE, THRESHOLD_SPEND, THRESHOLD_POINTS, THRESHOLD_PAID}

MEMBER_ACTIVE = "active"
MEMBER_EXPIRED = "expired"

KIND_EARN = "earn"
KIND_REDEEM = "redeem"
KIND_REVERT = "revert"
KIND_CLAWBACK = "clawback"
KIND_DEBT = "debt"
KIND_EXPIRE = "expire"
KIND_ADJUST = "adjust"
KIND_GIFT = "gift"
KIND_LOTTERY = "lottery"
KIND_SIGNIN = "signin"
LEDGER_KINDS = {KIND_EARN, KIND_REDEEM, KIND_REVERT, KIND_CLAWBACK, KIND_DEBT, KIND_EXPIRE, KIND_ADJUST, KIND_GIFT,
                KIND_LOTTERY, KIND_SIGNIN}

ORDER_PENDING = "pending"
ORDER_PAID = "paid"
ORDER_CLOSED = "closed"


class MemberLevel(Base, BigIdMixin, TimestampMixin, SoftDeleteMixin):
    """等级配置。store_id 为空表示全局；v1 只使用全局等级。"""

    __tablename__ = "member_levels"
    __table_args__ = (Index("ix_member_levels_store_level", "store_id", "level"),)

    store_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    level: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(32))
    threshold_type: Mapped[str] = mapped_column(String(16), default=THRESHOLD_FREE)  # free|spend|points|paid
    threshold_value: Mapped[int] = mapped_column(Integer, default=0)  # spend: 累计实付分；points: 当前积分
    discount_bp: Mapped[int] = mapped_column(Integer, default=10000)  # 9000 = 九折
    points_multiplier_bp: Mapped[int] = mapped_column(Integer, default=10000)
    free_delivery: Mapped[bool] = mapped_column(Boolean, default=False)
    birthday_coupon_template_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    price_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 付费等级
    valid_days: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 付费等级
    seq: Mapped[int] = mapped_column(Integer, default=0)


class Member(Base, BigIdMixin, TimestampMixin):
    __tablename__ = "members"
    __table_args__ = (
        Index("ix_members_user_id", "user_id", unique=True),
        Index("ix_members_paid_expires", "paid_level", "expires_at"),
    )

    user_id: Mapped[int] = mapped_column(Integer)
    level: Mapped[int] = mapped_column(Integer, default=1)  # 当前生效等级（只升不降；付费到期除外）
    paid_level: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 付费开通的等级
    growth_cents: Mapped[int] = mapped_column(Integer, default=0)  # 累计实付
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # 付费会员到期
    expiring_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    status: Mapped[str] = mapped_column(String(16), default=MEMBER_ACTIVE)  # active | expired（付费已到期）
    birthday_gift_year: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 最近一次发生日礼的年份
    version: Mapped[int] = mapped_column(Integer, default=0)


class PointsAccount(Base, BigIdMixin, TimestampMixin):
    __tablename__ = "points_accounts"
    __table_args__ = (Index("ix_points_accounts_user_id", "user_id", unique=True),)

    user_id: Mapped[int] = mapped_column(Integer)
    balance: Mapped[int] = mapped_column(Integer, default=0)
    debt: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=0)


class PointsLedger(Base, BigIdMixin):
    __tablename__ = "points_ledger"
    __table_args__ = (
        Index("ix_points_ledger_user_id", "user_id", "id"),
        Index("ix_points_ledger_remaining_expires", "remaining", "expires_at"),
    )

    user_id: Mapped[int] = mapped_column(Integer)
    delta: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))
    ref_key: Mapped[str] = mapped_column(String(96), unique=True)  # 业务幂等键
    balance_after: Mapped[int] = mapped_column(Integer, default=0)
    remaining: Mapped[int] = mapped_column(Integer, default=0)  # 入账批次尚未使用的积分（FIFO 过期）
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    operator_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # 到期提醒已发
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MembershipOrder(Base, BigIdMixin, TimestampMixin):
    """付费会员开通 / 续费单；支付单在 09（business_type=membership, business_id=本表 id）。"""

    __tablename__ = "membership_orders"
    __table_args__ = (Index("ix_membership_orders_user_status", "user_id", "status"),)

    user_id: Mapped[int] = mapped_column(Integer)
    level: Mapped[int] = mapped_column(Integer)
    price_cents: Mapped[int] = mapped_column(Integer)
    valid_days: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default=ORDER_PENDING)  # pending | paid | closed
    payment_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    out_trade_no: Mapped[str | None] = mapped_column(String(40), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # 支付截止


class PointsMismatch(Base, BigIdMixin):
    """每日对账：balance != Σledger.delta 的用户。"""

    __tablename__ = "points_mismatches"
    __table_args__ = (Index("ix_points_mismatches_day_user", "checked_on", "user_id", unique=True),)

    user_id: Mapped[int] = mapped_column(Integer)
    balance: Mapped[int] = mapped_column(Integer)
    ledger_sum: Mapped[int] = mapped_column(Integer)
    checked_on: Mapped[str] = mapped_column(String(10))  # YYYY-MM-DD
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
