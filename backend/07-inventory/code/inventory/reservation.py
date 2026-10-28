"""库存预占：下单预占 → 支付确认 / 取消或超时释放，并记录不可变的库存流水。

做法（references/patterns/inventory.md）：
- 预占即扣减可售库存：沿用 deduct() 的条件 UPDATE（stock >= qty），由数据库裁决，不先读后写。
- 每个业务单（ref，如 "order:123"）对每个 SKU 只有一条预占记录（唯一约束），重复请求返回已有结果。
- 预占状态 reserved → confirmed（支付成功）| released（取消、超时、退款回库）；用条件 UPDATE 推进，并发只有一个成功。
- 每次库存变化写一条流水（唯一约束 ref + sku + reason），可由流水重算库存并做对账。
- 多 SKU 按 sku_id 升序加锁，避免订单之间死锁；任一 SKU 不足时调用方回滚整个事务。
本模块不 commit。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import DateTime, Integer, String, UniqueConstraint, func, select, update
from sqlalchemy.orm import Mapped, Session, mapped_column

from luzback import AppError, Base, ErrorCode, StateMachine

from . import Sku, deduct

RESERVED = "reserved"
CONFIRMED = "confirmed"
RELEASED = "released"

RESERVATION_FLOW = StateMachine(
    "stock_reservation",
    {RESERVED: {CONFIRMED, RELEASED}, CONFIRMED: {RELEASED}},
    terminal=frozenset({RELEASED}),
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    # SQLite 返回无时区时间；统一按 UTC 解释
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class StockReservation(Base):
    __tablename__ = "stock_reservations"
    __table_args__ = (UniqueConstraint("ref", "sku_id", name="uq_stock_reservation_ref_sku"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ref: Mapped[str] = mapped_column(String(64), index=True)
    sku_id: Mapped[int] = mapped_column(Integer, index=True)
    qty: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default=RESERVED, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class StockLedger(Base):
    """只追加。delta 为可售库存变化量（预占为负、释放为正、调整可正可负）。"""

    __tablename__ = "stock_ledger"
    __table_args__ = (UniqueConstraint("ref", "sku_id", "reason", name="uq_stock_ledger_ref_sku_reason"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    sku_id: Mapped[int] = mapped_column(Integer, index=True)
    delta: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(24))  # reserve | release | adjust
    ref: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


def _rows(db: Session, ref: str) -> list[StockReservation]:
    return list(db.execute(select(StockReservation).where(StockReservation.ref == ref)
                           .order_by(StockReservation.sku_id)).scalars())


def reserve(db: Session, ref: str, items: dict[int, int], ttl_seconds: int, now: datetime | None = None) -> list[StockReservation]:
    """为业务单 ref 预占 items={sku_id: qty}。幂等：同一 ref 重复调用返回已有预占。

    库存不足抛 OUT_OF_STOCK；ref 已被释放后再次预占抛 CONFLICT（应生成新的业务单）。
    """
    if not items or any(qty <= 0 for qty in items.values()):
        raise AppError(ErrorCode.BAD_REQUEST, "items must contain positive quantities")
    existing = _rows(db, ref)
    if existing:
        if {row.sku_id: row.qty for row in existing} != dict(items):
            raise AppError(ErrorCode.IDEMPOTENCY_MISMATCH, "reservation ref reused with different items", data={"ref": ref})
        if any(row.status == RELEASED for row in existing):
            raise AppError(ErrorCode.CONFLICT, "reservation already released", data={"ref": ref})
        return existing
    expires_at = (now or _utcnow()) + timedelta(seconds=ttl_seconds)
    rows = []
    for sku_id in sorted(items):  # 固定加锁顺序，避免死锁
        deduct(db, sku_id, items[sku_id])
        row = StockReservation(ref=ref, sku_id=sku_id, qty=items[sku_id], status=RESERVED, expires_at=expires_at)
        db.add(row)
        db.add(StockLedger(sku_id=sku_id, delta=-items[sku_id], reason="reserve", ref=ref))
        rows.append(row)
    db.flush()
    return rows


def confirm(db: Session, ref: str) -> list[StockReservation]:
    """支付成功后确认预占。幂等；预占已释放（超时后才支付成功）抛 CONFLICT，调用方需重新扣减或发起退款。"""
    rows = _rows(db, ref)
    if not rows:
        raise AppError(ErrorCode.NOT_FOUND, "reservation not found", data={"ref": ref})
    for row in rows:
        if row.status == CONFIRMED:
            continue
        if row.status == RELEASED:
            raise AppError(ErrorCode.CONFLICT, "reservation released before confirmation", data={"ref": ref, "sku_id": row.sku_id})
        RESERVATION_FLOW.advance(db, StockReservation, row.id, RESERVED, CONFIRMED)
    db.expire_all()
    return _rows(db, ref)


def release(db: Session, ref: str, *, allow_confirmed: bool = False) -> int:
    """释放预占并回补库存，返回回补的件数。幂等：已释放的不再回补。

    allow_confirmed=True 用于已支付订单退款后回库；默认只释放未确认的预占（取消、超时）。
    """
    restored = 0
    for row in _rows(db, ref):
        if row.status == RELEASED:
            continue
        if row.status == CONFIRMED and not allow_confirmed:
            raise AppError(ErrorCode.CONFLICT, "confirmed reservation needs allow_confirmed", data={"ref": ref})
        try:
            RESERVATION_FLOW.advance(db, StockReservation, row.id, row.status, RELEASED)
        except AppError:
            db.refresh(row)
            if row.status == CONFIRMED and not allow_confirmed:
                # 关单与支付成功并发，支付先完成：不能释放，交给调用方按“已支付”处理
                raise AppError(ErrorCode.CONFLICT, "reservation confirmed concurrently", data={"ref": ref}) from None
            continue  # 另一个请求已释放，本次不重复回补
        db.execute(update(Sku).where(Sku.id == row.sku_id).values(stock=Sku.stock + row.qty, version=Sku.version + 1))
        db.add(StockLedger(sku_id=row.sku_id, delta=row.qty, reason="release", ref=ref))
        restored += row.qty
    db.flush()
    return restored


def expired_refs(db: Session, now: datetime | None = None, limit: int = 200) -> list[str]:
    """到期仍未确认的预占所属业务单（供关单任务处理）。"""
    moment = now or _utcnow()
    rows = db.execute(
        select(StockReservation.ref, StockReservation.expires_at)
        .where(StockReservation.status == RESERVED)
        .order_by(StockReservation.expires_at)
        .limit(limit * 4)
    ).all()
    refs: list[str] = []
    for ref, expires_at in rows:
        if _aware(expires_at) <= moment and ref not in refs:
            refs.append(ref)
        if len(refs) >= limit:
            break
    return refs


def ledger_balance(db: Session, sku_id: int) -> int:
    """流水净变化量；对账：初始库存 + 调整 + 流水净变化 应等于当前可售库存。"""
    return int(db.execute(select(func.coalesce(func.sum(StockLedger.delta), 0)).where(StockLedger.sku_id == sku_id)).scalar_one())
