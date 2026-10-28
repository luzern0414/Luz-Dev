"""储值钱包：本金与赠送金分账、不可变流水、幂等、条件扣减（references/patterns/accounts.md）。

- 余额只通过本模块变动；每次变动写一条流水（唯一约束 ref + kind），重复请求不会重复记账。
- 扣减用条件 UPDATE（principal >= p AND bonus >= b），并发下不会出现负余额。
- 消费拆分策略：proportional（按本金 / 赠送金余额比例，默认，退卡时双方都容易核对）或 principal_first。
- 冲回（订单退款）按原消费流水的拆分原路退回。
- 退卡只退剩余本金，赠送金作废；冷静期等规则由调用方按方案判断后调用。
金额单位为分；本模块不 commit。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, UniqueConstraint, select, update
from sqlalchemy.orm import Mapped, Session, mapped_column

from luzback import AppError, Base, ErrorCode

PROPORTIONAL = "proportional"
PRINCIPAL_FIRST = "principal_first"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class WalletAccount(Base):
    __tablename__ = "wallet_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, unique=True)
    principal_cents: Mapped[int] = mapped_column(Integer, default=0)
    bonus_cents: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | closed


class WalletLedger(Base):
    __tablename__ = "wallet_ledger"
    __table_args__ = (UniqueConstraint("ref", "kind", name="uq_wallet_ledger_ref_kind"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(16))  # recharge | consume | reverse | card_refund
    principal_delta: Mapped[int] = mapped_column(Integer)
    bonus_delta: Mapped[int] = mapped_column(Integer)
    ref: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


def _account(db: Session, user_id: int, create: bool = False) -> WalletAccount:
    account = db.execute(select(WalletAccount).where(WalletAccount.user_id == user_id)).scalar_one_or_none()
    if account is None:
        if not create:
            raise AppError(ErrorCode.NOT_FOUND, "wallet not found")
        account = WalletAccount(user_id=user_id)
        db.add(account)
        db.flush()
    if account.status != "active":
        raise AppError(ErrorCode.BUSINESS_RULE, "wallet closed")
    return account


def _existing(db: Session, ref: str, kind: str) -> WalletLedger | None:
    return db.execute(select(WalletLedger).where(WalletLedger.ref == ref, WalletLedger.kind == kind)).scalar_one_or_none()


def _apply(db: Session, account: WalletAccount, kind: str, ref: str, principal: int, bonus: int) -> WalletLedger:
    """principal / bonus 为变化量；扣减时由条件保证不为负。"""
    result = db.execute(
        update(WalletAccount)
        .where(WalletAccount.id == account.id, WalletAccount.status == "active",
               WalletAccount.principal_cents + principal >= 0, WalletAccount.bonus_cents + bonus >= 0)
        .values(principal_cents=WalletAccount.principal_cents + principal, bonus_cents=WalletAccount.bonus_cents + bonus)
    )
    if result.rowcount != 1:
        raise AppError(ErrorCode.INSUFFICIENT_BALANCE, "insufficient balance")
    entry = WalletLedger(account_id=account.id, kind=kind, principal_delta=principal, bonus_delta=bonus, ref=ref)
    db.add(entry)
    db.flush()
    db.refresh(account)
    return entry


def recharge(db: Session, user_id: int, ref: str, principal_cents: int, bonus_cents: int = 0) -> WalletLedger:
    """充值（支付成功后调用，ref 用支付单号）。幂等。"""
    if principal_cents <= 0 or bonus_cents < 0:
        raise AppError(ErrorCode.BAD_REQUEST, "invalid recharge amount")
    found = _existing(db, ref, "recharge")
    if found is not None:
        return found
    return _apply(db, _account(db, user_id, create=True), "recharge", ref, principal_cents, bonus_cents)


def split(amount: int, principal: int, bonus: int, policy: str = PROPORTIONAL) -> tuple[int, int]:
    """把消费金额拆成 (本金, 赠送金)。"""
    if amount <= 0:
        raise AppError(ErrorCode.BAD_REQUEST, "amount must be positive")
    if amount > principal + bonus:
        raise AppError(ErrorCode.INSUFFICIENT_BALANCE, "insufficient balance")
    if policy == PRINCIPAL_FIRST:
        from_principal = min(amount, principal)
        return from_principal, amount - from_principal
    if policy != PROPORTIONAL:
        raise ValueError(f"unknown policy {policy}")
    from_bonus = amount * bonus // (principal + bonus)  # 赠送金向下取整，本金多扣不超过 1 分
    from_principal = amount - from_bonus
    if from_principal > principal:  # 极端取整时回退到赠送金
        from_bonus += from_principal - principal
        from_principal = principal
    return from_principal, from_bonus


def consume(db: Session, user_id: int, ref: str, amount_cents: int, policy: str = PROPORTIONAL) -> WalletLedger:
    """余额支付。幂等：同一 ref 重复调用返回原流水。"""
    found = _existing(db, ref, "consume")
    if found is not None:
        return found
    account = _account(db, user_id)
    principal, bonus = split(amount_cents, account.principal_cents, account.bonus_cents, policy)
    return _apply(db, account, "consume", ref, -principal, -bonus)


def reverse(db: Session, user_id: int, ref: str) -> WalletLedger:
    """订单退款：按原消费拆分原路退回钱包。幂等。"""
    found = _existing(db, ref, "reverse")
    if found is not None:
        return found
    original = _existing(db, ref, "consume")
    if original is None:
        raise AppError(ErrorCode.NOT_FOUND, "consume record not found")
    return _apply(db, _account(db, user_id), "reverse", ref, -original.principal_delta, -original.bonus_delta)


def refund_card(db: Session, user_id: int, ref: str) -> int:
    """退卡：退还剩余本金（返回金额，调用方原路退款），赠送金作废，账户关闭。幂等。"""
    found = _existing(db, ref, "card_refund")
    if found is not None:
        return -found.principal_delta
    account = _account(db, user_id)
    principal, bonus = account.principal_cents, account.bonus_cents
    _apply(db, account, "card_refund", ref, -principal, -bonus)
    db.execute(update(WalletAccount).where(WalletAccount.id == account.id).values(status="closed"))
    return principal


def ledger_matches_balance(db: Session, user_id: int) -> bool:
    """对账：流水累加必须等于账户余额。"""
    account = db.execute(select(WalletAccount).where(WalletAccount.user_id == user_id)).scalar_one()
    rows = db.execute(select(WalletLedger.principal_delta, WalletLedger.bonus_delta)
                      .where(WalletLedger.account_id == account.id)).all()
    return (sum(r[0] for r in rows), sum(r[1] for r in rows)) == (account.principal_cents, account.bonus_cents)
