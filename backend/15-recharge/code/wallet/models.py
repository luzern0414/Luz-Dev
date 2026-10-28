"""储值钱包表结构。

- 账户：本金 / 赠送金分账，另有退款预扣（冻结）两列；余额只经条件 UPDATE 变动。
- 批次（充值单）：每笔入账一条，记录该批剩余本金 / 赠送金，消费按 FIFO 归属到批次，退款按批次计算。
- 流水：只追加；(ref, kind) 唯一保证幂等；带变动后余额快照。
- 分配：一条消费 / 扣减流水落到哪些批次、各多少，用于冲回与退款核算。
金额单位为分。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from luzback import Base, BigId, BigIdMixin, SoftDeleteMixin, TimestampMixin, utcnow

# 账户状态
ACCOUNT_ACTIVE = "active"
ACCOUNT_CLOSED = "closed"

# 批次（充值单）状态
BATCH_PENDING = "pending"          # 待支付
BATCH_PAID = "paid"                # 已入账（含线下、调整、赠送入账）
BATCH_CLOSED = "closed"            # 超时关闭 / 取消（未入账）
BATCH_REFUNDING = "refunding"      # 退款处理中（剩余已预扣）
BATCH_REFUNDED = "refunded"        # 已退款（终态）
BATCH_EXHAUSTED = "exhausted"      # 已用完（剩余均为 0，可因冲回恢复为 paid）

# 批次来源
SOURCE_PAYMENT = "payment"     # 线上支付充值（可原路退）
SOURCE_OFFLINE = "offline"     # 门店线下收款充值（线下退）
SOURCE_ADJUST = "adjust"       # 后台调整加款
SOURCE_GIFT = "gift"           # 活动赠送（只有赠送金）
SOURCE_DIRECT = "direct"       # 其他模块直接入账（旧接口 recharge()）

# 退款单状态
REFUND_PENDING = "pending_approval"
REFUND_PROCESSING = "processing"
REFUND_SUCCEEDED = "succeeded"
REFUND_FAILED = "failed"
REFUND_REJECTED = "rejected"

# 流水类型
L_RECHARGE = "recharge"
L_CONSUME = "consume"
L_REVERSE = "reverse"
L_ADJUST = "adjust"
L_GIFT = "gift"
L_REFUND_HOLD = "refund_hold"
L_REFUND_RELEASE = "refund_release"
L_REFUND_DONE = "refund_done"
L_BONUS_EXPIRE = "bonus_expire"
L_CARD_REFUND = "card_refund"  # 旧接口：整户退卡


class WalletAccount(Base, BigIdMixin, TimestampMixin):
    __tablename__ = "wallet_accounts"
    __table_args__ = (
        CheckConstraint("principal_cents >= 0", name="principal_nonneg"),
        CheckConstraint("bonus_cents >= 0", name="bonus_nonneg"),
        CheckConstraint("frozen_principal_cents >= 0", name="frozen_p_nonneg"),
        CheckConstraint("frozen_bonus_cents >= 0", name="frozen_b_nonneg"),
    )

    user_id: Mapped[int] = mapped_column(BigId, unique=True)
    principal_cents: Mapped[int] = mapped_column(Integer, default=0)
    bonus_cents: Mapped[int] = mapped_column(Integer, default=0)
    frozen_principal_cents: Mapped[int] = mapped_column(Integer, default=0)
    frozen_bonus_cents: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default=ACCOUNT_ACTIVE)
    version: Mapped[int] = mapped_column(Integer, default=0)


class RechargePlan(Base, BigIdMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "wallet_recharge_plans"

    store_id: Mapped[int | None] = mapped_column(BigId, nullable=True, index=True)  # NULL = 全部门店
    name: Mapped[str] = mapped_column(String(64))
    amount_cents: Mapped[int] = mapped_column(Integer)
    bonus_cents: Mapped[int] = mapped_column(Integer, default=0)
    bonus_valid_days: Mapped[int | None] = mapped_column(Integer, nullable=True)  # NULL = 赠送金不过期
    status: Mapped[str] = mapped_column(String(8), default="off")  # on | off
    sort: Mapped[int] = mapped_column(Integer, default=0)
    sale_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sale_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    per_user_daily_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    terms_text: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[int | None] = mapped_column(BigId, nullable=True)
    updated_by: Mapped[int | None] = mapped_column(BigId, nullable=True)


class RechargeBatch(Base, BigIdMixin, TimestampMixin):
    """充值单 = 入账批次。"""

    __tablename__ = "wallet_recharge_orders"
    __table_args__ = (
        CheckConstraint("principal_remaining_cents >= 0", name="p_remaining_nonneg"),
        CheckConstraint("bonus_remaining_cents >= 0", name="b_remaining_nonneg"),
        Index("ix_wallet_recharge_orders_user_status", "user_id", "status"),
    )

    order_no: Mapped[str] = mapped_column(String(32), unique=True)
    user_id: Mapped[int] = mapped_column(BigId)
    store_id: Mapped[int | None] = mapped_column(BigId, nullable=True)
    plan_id: Mapped[int | None] = mapped_column(BigId, nullable=True)
    source: Mapped[str] = mapped_column(String(16), default=SOURCE_PAYMENT)
    source_ref: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)  # 直接入账的幂等键
    amount_cents: Mapped[int] = mapped_column(Integer)  # 实付本金
    bonus_cents: Mapped[int] = mapped_column(Integer, default=0)
    principal_remaining_cents: Mapped[int] = mapped_column(Integer, default=0)
    bonus_remaining_cents: Mapped[int] = mapped_column(Integer, default=0)
    refunded_cents: Mapped[int] = mapped_column(Integer, default=0)
    bonus_forfeited_cents: Mapped[int] = mapped_column(Integer, default=0)
    bonus_expired_cents: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default=BATCH_PENDING, index=True)
    goods_kind: Mapped[str] = mapped_column(String(20), default="offline_service")
    payment_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    out_trade_no: Mapped[str | None] = mapped_column(String(64), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # 待支付超时
    bonus_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    needs_manual: Mapped[bool] = mapped_column(default=False)
    lpr_bp: Mapped[int] = mapped_column(Integer, default=0)  # 合同成立时一年期 LPR（万分比）
    terms_snapshot: Mapped[str] = mapped_column(Text, default="")
    # 发票相关（只记录，不开票）：预付卡销售和充值，不征税，编码 601，不得开专票
    invoice_tax_code: Mapped[str] = mapped_column(String(8), default="601")
    invoice_note: Mapped[str] = mapped_column(String(64), default="预付卡销售和充值 不征税")
    operator_id: Mapped[int | None] = mapped_column(BigId, nullable=True)
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)


class WalletLedger(Base, BigIdMixin):
    __tablename__ = "wallet_ledger"
    __table_args__ = (
        UniqueConstraint("ref", "kind", name="uq_wallet_ledger_ref_kind"),
        Index("ix_wallet_ledger_account_id", "account_id", "id"),
    )

    account_id: Mapped[int] = mapped_column(BigId)
    user_id: Mapped[int] = mapped_column(BigId, index=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)
    principal_delta: Mapped[int] = mapped_column(Integer, default=0)
    bonus_delta: Mapped[int] = mapped_column(Integer, default=0)
    frozen_principal_delta: Mapped[int] = mapped_column(Integer, default=0)
    frozen_bonus_delta: Mapped[int] = mapped_column(Integer, default=0)
    principal_after: Mapped[int] = mapped_column(Integer, default=0)
    bonus_after: Mapped[int] = mapped_column(Integer, default=0)
    ref: Mapped[str] = mapped_column(String(64))
    batch_id: Mapped[int | None] = mapped_column(BigId, nullable=True, index=True)
    store_id: Mapped[int | None] = mapped_column(BigId, nullable=True)
    operator_id: Mapped[int | None] = mapped_column(BigId, nullable=True)
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # 冲回：reverse 流水指向被冲回的 consume 流水；consume 流水上累计已冲回金额（条件 UPDATE 封顶，部分退款可多次）
    origin_ledger_id: Mapped[int | None] = mapped_column(BigId, nullable=True, index=True)
    reversed_cents: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class BatchAllocation(Base, BigIdMixin):
    """一条扣减流水在各批次上的分配（只追加）。"""

    __tablename__ = "wallet_batch_allocations"

    ledger_id: Mapped[int] = mapped_column(BigId, index=True)
    batch_id: Mapped[int] = mapped_column(BigId, index=True)
    principal_cents: Mapped[int] = mapped_column(Integer, default=0)  # 正数 = 从批次扣走
    bonus_cents: Mapped[int] = mapped_column(Integer, default=0)


class WalletRefund(Base, BigIdMixin, TimestampMixin):
    __tablename__ = "wallet_refunds"

    refund_no: Mapped[str] = mapped_column(String(32), unique=True)
    user_id: Mapped[int] = mapped_column(BigId, index=True)
    batch_id: Mapped[int] = mapped_column(BigId, index=True)
    cause: Mapped[str] = mapped_column(String(16))  # customer | merchant
    rule: Mapped[str] = mapped_column(String(24))   # cooling_off_full | customer_original_price | merchant_discounted
    hold_principal_cents: Mapped[int] = mapped_column(Integer, default=0)
    hold_bonus_cents: Mapped[int] = mapped_column(Integer, default=0)
    refund_cents: Mapped[int] = mapped_column(Integer, default=0)       # 原路退回的本金
    interest_cents: Mapped[int] = mapped_column(Integer, default=0)     # 经营者原因的 LPR 利息（另行支付）
    fee_cents: Mapped[int] = mapped_column(Integer, default=0)
    bonus_forfeit_cents: Mapped[int] = mapped_column(Integer, default=0)
    retained_cents: Mapped[int] = mapped_column(Integer, default=0)     # 剩余本金中不退的部分（抵扣已用赠送金）
    route: Mapped[str] = mapped_column(String(16), default="original")  # original | offline
    status: Mapped[str] = mapped_column(String(20), default=REFUND_PENDING, index=True)
    breakdown_json: Mapped[str] = mapped_column(Text, default="{}")
    reason: Mapped[str] = mapped_column(String(200), default="")
    requested_by: Mapped[str] = mapped_column(String(32), default="")  # user:1 / staff:2
    approved_by: Mapped[int | None] = mapped_column(BigId, nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fail_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    overdue_notified: Mapped[bool] = mapped_column(default=False)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # 已提交支付模块
    channel_refund_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    submit_error: Mapped[str | None] = mapped_column(String(200), nullable=True)


class WalletMismatch(Base, BigIdMixin):
    __tablename__ = "wallet_mismatches"
    __table_args__ = (UniqueConstraint("account_id", "check_date", name="uq_wallet_mismatch_account_date"),)

    account_id: Mapped[int] = mapped_column(BigId)
    user_id: Mapped[int] = mapped_column(BigId, index=True)
    check_date: Mapped[str] = mapped_column(String(10))
    detail_json: Mapped[str] = mapped_column(Text)
    resolved: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
