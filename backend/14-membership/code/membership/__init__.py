"""会员与积分模块：积分余额原子扣减、流水台账、按业务单号幂等入账。

- 余额 UserPoints.balance 通过条件 UPDATE 扣减（balance >= amount），不会出现负数。
- 每一笔变动写 PointsLedger，ref_key 唯一：同一业务事件（如订单 A1 的返积分）只会入账一次。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Integer, String, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from luzback import AppError, Base, ErrorCode


class UserPoints(Base):
    __tablename__ = "user_points"

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    balance: Mapped[int] = mapped_column(Integer, default=0)


class PointsLedger(Base):
    __tablename__ = "points_ledger"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    delta: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(32))
    ref_key: Mapped[str] = mapped_column(String(64), unique=True)  # 业务幂等键
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(timezone.utc))


def _ensure_account(db: Session, user_id: int) -> None:
    if db.get(UserPoints, user_id) is None:
        db.add(UserPoints(user_id=user_id, balance=0))
        try:
            db.flush()
        except IntegrityError:
            db.rollback()  # 并发创建，另一方已成功


def _already_applied(db: Session, ref_key: str) -> bool:
    return db.execute(select(PointsLedger.id).where(PointsLedger.ref_key == ref_key)).first() is not None


def earn(db: Session, user_id: int, amount: int, reason: str, ref_key: str) -> bool:
    """入账积分。返回 False 表示该 ref_key 已入账过（幂等跳过）。调用方 commit。"""
    if amount <= 0:
        raise AppError(ErrorCode.BAD_REQUEST, "amount must be positive")
    if _already_applied(db, ref_key):
        return False
    _ensure_account(db, user_id)
    db.execute(update(UserPoints).where(UserPoints.user_id == user_id).values(balance=UserPoints.balance + amount))
    db.add(PointsLedger(user_id=user_id, delta=amount, reason=reason, ref_key=ref_key))
    db.flush()
    return True


def spend(db: Session, user_id: int, amount: int, reason: str, ref_key: str) -> bool:
    """扣减积分（如抵扣订单）。余额不足抛 BUSINESS_RULE；重复 ref_key 返回 False。调用方 commit。"""
    if amount <= 0:
        raise AppError(ErrorCode.BAD_REQUEST, "amount must be positive")
    if _already_applied(db, ref_key):
        return False
    _ensure_account(db, user_id)
    result = db.execute(
        update(UserPoints)
        .where(UserPoints.user_id == user_id, UserPoints.balance >= amount)
        .values(balance=UserPoints.balance - amount)
    )
    if result.rowcount != 1:
        raise AppError(ErrorCode.BUSINESS_RULE, "insufficient points")
    db.add(PointsLedger(user_id=user_id, delta=-amount, reason=reason, ref_key=ref_key))
    db.flush()
    return True


def balance(db: Session, user_id: int) -> int:
    row = db.get(UserPoints, user_id)
    return row.balance if row else 0
