"""lottery 表结构。金额整数分；概率万分比整数；时间 UTC；状态小写蛇形字符串。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from luzback import Base, BigId, BigIdMixin, SoftDeleteMixin, TimestampMixin, utcnow

# ---- 活动 ----
KIND_WHEEL, KIND_GRID, KIND_SCRATCH = "wheel", "grid", "scratch"
ACTIVITY_KINDS = (KIND_WHEEL, KIND_GRID, KIND_SCRATCH)  # 仅展示形态不同，抽奖引擎相同

A_DRAFT, A_PUBLISHED, A_RUNNING, A_ENDED = "draft", "published", "running", "ended"
ACTIVITY_STATUSES = (A_DRAFT, A_PUBLISHED, A_RUNNING, A_ENDED)

OOS_THANKS = "thanks"               # 抽中已无库存的奖品 → 按谢谢参与（默认，不重抽）
OOS_EXCLUDE = "exclude_disclosed"   # 无库存奖品移出奖池、按剩余权重重新归一（必须在公示规则中写明，发布后不可改）
OOS_POLICIES = (OOS_THANKS, OOS_EXCLUDE)

# ---- 奖品 ----
P_COUPON, P_POINTS, P_CARD_DAYS, P_PHYSICAL, P_THANKS = "coupon", "points", "member_card_days", "physical", "thanks"
PRIZE_KINDS = (P_COUPON, P_POINTS, P_CARD_DAYS, P_PHYSICAL, P_THANKS)
VIRTUAL_KINDS = (P_COUPON, P_POINTS, P_CARD_DAYS)

# ---- 次数来源 ----
SRC_FREE, SRC_EARNED = "free", "earned"
G_PAYMENT, G_CHECKIN, G_ADMIN, G_EVENT = "payment", "checkin", "admin", "event"
GRANT_SOURCES = (G_PAYMENT, G_CHECKIN, G_ADMIN, G_EVENT)
GRANT_ACTIVE, GRANT_REVOKED = "active", "revoked"

# ---- 发奖状态 ----
D_NONE = "none"                      # 谢谢参与
D_PENDING = "pending"                # 虚拟奖：已发事件，等待消费方回执
D_DELIVERED = "delivered"            # 已到账 / 实物已签收
D_FAILED = "failed"                  # 重试耗尽，等待人工重试
D_AWAITING = "awaiting_claim"        # 实物奖：等待中奖人填写收货信息
D_CLAIMED = "claimed"                # 实物奖：已填写，待发货
D_SHIPPED = "shipped"                # 实物奖：已发货
D_EXPIRED = "expired"                # 实物奖：逾期未领
DELIVERY_STATUSES = (D_NONE, D_PENDING, D_DELIVERED, D_FAILED, D_AWAITING, D_CLAIMED, D_SHIPPED, D_EXPIRED)

FB_OUT_OF_STOCK, FB_MEMBER_LIMIT, FB_BUDGET = "out_of_stock", "member_limit", "budget"


class LotteryActivity(Base, BigIdMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "lottery_activities"
    __table_args__ = (Index("ix_lottery_activities_status_start", "status", "start_at"),)

    store_id: Mapped[int | None] = mapped_column(BigId, nullable=True, index=True)  # None = 全部门店
    name: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16), default=KIND_WHEEL)
    status: Mapped[str] = mapped_column(String(16), default=A_DRAFT)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    rules_text: Mapped[str] = mapped_column(Text, default="")
    audience: Mapped[str] = mapped_column(String(64), default="all")  # 交给 audience 钩子解释，例如 all / members / level:2
    # 次数规则（0 = 不限 / 关闭）
    free_daily_chances: Mapped[int] = mapped_column(Integer, default=0)
    limit_daily: Mapped[int] = mapped_column(Integer, default=0)
    limit_monthly: Mapped[int] = mapped_column(Integer, default=0)
    limit_total: Mapped[int] = mapped_column(Integer, default=0)
    earn_payment_min_cents: Mapped[int] = mapped_column(Integer, default=0)
    earn_payment_chances: Mapped[int] = mapped_column(Integer, default=0)
    earn_checkin_chances: Mapped[int] = mapped_column(Integer, default=0)
    # 奖池规则
    out_of_stock_policy: Mapped[str] = mapped_column(String(24), default=OOS_THANKS)
    budget_cents: Mapped[int] = mapped_column(Integer, default=0)
    spent_cents: Mapped[int] = mapped_column(Integer, default=0)             # 全部中奖价值（统计）
    reserved_cents: Mapped[int] = mapped_column(Integer, default=0)          # 有限库存奖品 Σ价值×库存（发布时预留）
    unlimited_spent_cents: Mapped[int] = mapped_column(Integer, default=0)   # 不限量奖品已用（只能用预算 − 预留）
    claim_days: Mapped[int] = mapped_column(Integer, default=7)
    early_end_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    publicity_threshold_cents: Mapped[int] = mapped_column(Integer, default=50_000)
    # 发布冻结
    odds_json: Mapped[str | None] = mapped_column(Text, nullable=True)   # 发布时的奖池快照（抽奖唯一依据）
    rules_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[int | None] = mapped_column(BigId, nullable=True)
    updated_by: Mapped[int | None] = mapped_column(BigId, nullable=True)


class LotteryPrize(Base, BigIdMixin, TimestampMixin):
    __tablename__ = "lottery_prizes"
    __table_args__ = (UniqueConstraint("activity_id", "seq", name="uq_lottery_prizes_activity_seq"),)

    activity_id: Mapped[int] = mapped_column(BigId, index=True)
    seq: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(24))
    ref_id: Mapped[int | None] = mapped_column(BigId, nullable=True)  # 券模板 id / 会员卡 id
    quantity: Mapped[int] = mapped_column(Integer, default=1)          # 券张数 / 积分数 / 天数 / 件数
    value_cents: Mapped[int] = mapped_column(Integer, default=0)       # 同期市场价（第十八条），用于封顶、预算、公示
    weight_bp: Mapped[int] = mapped_column(Integer)
    stock_total: Mapped[int | None] = mapped_column(Integer, nullable=True)  # None = 不限
    stock_left: Mapped[int | None] = mapped_column(Integer, nullable=True)
    per_member_limit: Mapped[int] = mapped_column(Integer, default=0)       # 0 = 不限
    win_count: Mapped[int] = mapped_column(Integer, default=0)
    image_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str] = mapped_column(String(255), default="")      # 券 / 积分的兑换规则、使用范围、有效期（第十四条）


class LotteryChanceAccount(Base, BigIdMixin, TimestampMixin):
    """会员在活动内的次数账户：granted 为获得的（非每日免费）次数，used 为已用，draws 为累计抽奖次数（总限额）。"""

    __tablename__ = "lottery_chance_accounts"
    __table_args__ = (UniqueConstraint("activity_id", "member_id", name="uq_lottery_chance_accounts_member"),)

    activity_id: Mapped[int] = mapped_column(BigId)
    member_id: Mapped[int] = mapped_column(BigId, index=True)
    granted: Mapped[int] = mapped_column(Integer, default=0)
    used: Mapped[int] = mapped_column(Integer, default=0)
    draws: Mapped[int] = mapped_column(Integer, default=0)


class LotteryChancePeriod(Base, BigIdMixin):
    """日 / 月计数行：period = 'YYYY-MM-DD'（日）或 'YYYY-MM'（月），按门店时区。"""

    __tablename__ = "lottery_chance_periods"
    __table_args__ = (UniqueConstraint("activity_id", "member_id", "period", name="uq_lottery_chance_periods_key"),)

    activity_id: Mapped[int] = mapped_column(BigId)
    member_id: Mapped[int] = mapped_column(BigId, index=True)
    period: Mapped[str] = mapped_column(String(10))
    draws: Mapped[int] = mapped_column(Integer, default=0)
    free_used: Mapped[int] = mapped_column(Integer, default=0)


class LotteryChanceGrant(Base, BigIdMixin, TimestampMixin):
    """领次数单：(activity, source, source_ref) 唯一 → 同一笔支付 / 同一次签到 / 同一个后台请求只发一次。"""

    __tablename__ = "lottery_chance_grants"
    __table_args__ = (UniqueConstraint("activity_id", "source", "source_ref", name="uq_lottery_chance_grants_ref"),)

    activity_id: Mapped[int] = mapped_column(BigId)
    member_id: Mapped[int] = mapped_column(BigId, index=True)
    source: Mapped[str] = mapped_column(String(16))
    source_ref: Mapped[str] = mapped_column(String(128))
    chances: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default=GRANT_ACTIVE)
    operator_id: Mapped[int | None] = mapped_column(BigId, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)


class LotteryMemberPrizeWin(Base, BigIdMixin):
    __tablename__ = "lottery_member_prize_wins"
    __table_args__ = (UniqueConstraint("prize_id", "member_id", name="uq_lottery_member_prize_wins_key"),)

    activity_id: Mapped[int] = mapped_column(BigId, index=True)
    prize_id: Mapped[int] = mapped_column(BigId)
    member_id: Mapped[int] = mapped_column(BigId)
    wins: Mapped[int] = mapped_column(Integer, default=0)


class LotteryDraw(Base, BigIdMixin, TimestampMixin):
    """抽奖记录（含未中奖），可审计复现：random_value + random_range + odds_json → 奖品。"""

    __tablename__ = "lottery_draws"
    __table_args__ = (
        UniqueConstraint("activity_id", "member_id", "request_key", name="uq_lottery_draws_request"),
        Index("ix_lottery_draws_activity_created", "activity_id", "created_at"),
        Index("ix_lottery_draws_delivery", "delivery_status", "delivery_due_at"),
    )

    activity_id: Mapped[int] = mapped_column(BigId)
    member_id: Mapped[int] = mapped_column(BigId, index=True)
    store_id: Mapped[int | None] = mapped_column(BigId, nullable=True, index=True)
    request_key: Mapped[str] = mapped_column(String(128))
    # 结果
    prize_id: Mapped[int] = mapped_column(BigId)
    prize_seq: Mapped[int] = mapped_column(Integer)
    prize_kind: Mapped[str] = mapped_column(String(24))
    prize_name: Mapped[str] = mapped_column(String(64))
    ref_id: Mapped[int | None] = mapped_column(BigId, nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    value_cents: Mapped[int] = mapped_column(Integer, default=0)
    won: Mapped[bool] = mapped_column(Boolean, default=False)
    hit_prize_id: Mapped[int] = mapped_column(BigId)                 # 随机数命中的奖品（退化前）
    fallback_reason: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # 审计
    random_value: Mapped[int] = mapped_column(Integer)
    random_range: Mapped[int] = mapped_column(Integer)
    rng: Mapped[str] = mapped_column(String(16))
    odds_json: Mapped[str] = mapped_column(Text)
    rules_hash: Mapped[str] = mapped_column(String(64))
    chance_source: Mapped[str] = mapped_column(String(8))
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 发奖
    delivery_status: Mapped[str] = mapped_column(String(16), default=D_NONE)
    delivery_attempts: Mapped[int] = mapped_column(Integer, default=0)
    delivery_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivery_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    delivery_error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 实物
    claim_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claim_name_enc: Mapped[str | None] = mapped_column(String(255), nullable=True)
    claim_phone_enc: Mapped[str | None] = mapped_column(String(255), nullable=True)
    claim_address_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    carrier: Mapped[str | None] = mapped_column(String(32), nullable=True)
    tracking_no: Mapped[str | None] = mapped_column(String(64), nullable=True)
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LotteryAmendment(Base, BigIdMixin):
    """发布后的变更（只允许有利于消费者的），在活动详情中公示。"""

    __tablename__ = "lottery_amendments"

    activity_id: Mapped[int] = mapped_column(BigId, index=True)
    field: Mapped[str] = mapped_column(String(32))
    before: Mapped[str | None] = mapped_column(String(255), nullable=True)
    after: Mapped[str | None] = mapped_column(String(255), nullable=True)
    staff_id: Mapped[int | None] = mapped_column(BigId, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
