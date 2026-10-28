"""预约名额：课程 / 教练 / 场地时段（references/patterns/reservation.md）。

名额就是库存：booked 用条件 UPDATE（booked < capacity）原子占位，不先读后写。
- 同一用户在同一时段只能有一条预约（唯一约束）；取消后再约复用该记录。
- 同一用户的有效预约时间不能重叠。
- 满员时可进入候补；有人取消后，按排队顺序自动递补第一位（同一事务内）。
- 开课前 cutoff 分钟内不能自助取消（迟取消按商户规则处理，如计为爽约）。
- 签到窗口：开课前 checkin_before 分钟至下课；下课后仍未签到的预约由任务标记为爽约。
本模块不 commit；递补等副作用写入发件箱。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import DateTime, Integer, String, UniqueConstraint, func, select, update
from sqlalchemy.orm import Mapped, Session, mapped_column

from luzback import AppError, Base, ErrorCode, StateMachine, enqueue_event

OPEN = "open"
BOOKED = "booked"
WAITLISTED = "waitlisted"
CANCELLED = "cancelled"
CHECKED_IN = "checked_in"
NO_SHOW = "no_show"

BOOKING_FLOW = StateMachine(
    "booking",
    {
        BOOKED: {CANCELLED, CHECKED_IN, NO_SHOW},
        WAITLISTED: {BOOKED, CANCELLED},
        CANCELLED: {BOOKED, WAITLISTED},
    },
    terminal=frozenset({CHECKED_IN, NO_SHOW}),
)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class Slot(Base):
    __tablename__ = "booking_slots"

    id: Mapped[int] = mapped_column(primary_key=True)
    resource: Mapped[str] = mapped_column(String(64), index=True)  # 课程 / 教练 / 场地标识
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    capacity: Mapped[int] = mapped_column(Integer)
    booked: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default=OPEN)


class Booking(Base):
    __tablename__ = "bookings"
    __table_args__ = (UniqueConstraint("slot_id", "user_id", name="uq_booking_slot_user"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    slot_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


def _take_seat(db: Session, slot_id: int) -> bool:
    result = db.execute(
        update(Slot).where(Slot.id == slot_id, Slot.status == OPEN, Slot.booked < Slot.capacity)
        .values(booked=Slot.booked + 1)
    )
    return result.rowcount == 1


def _free_seat(db: Session, slot_id: int) -> None:
    db.execute(update(Slot).where(Slot.id == slot_id, Slot.booked > 0).values(booked=Slot.booked - 1))


def _check_overlap(db: Session, slot: Slot, user_id: int) -> None:
    clash = db.execute(
        select(Booking.id).join(Slot, Slot.id == Booking.slot_id)
        .where(Booking.user_id == user_id, Booking.status == BOOKED, Booking.slot_id != slot.id,
               Slot.starts_at < slot.ends_at, Slot.ends_at > slot.starts_at)
    ).first()
    if clash:
        raise AppError(ErrorCode.BUSINESS_RULE, "time conflicts with another booking", data={"booking_id": clash[0]})


def book(db: Session, slot_id: int, user_id: int, now: datetime, *, allow_waitlist: bool = False) -> Booking:
    slot = db.get(Slot, slot_id)
    if slot is None or slot.status != OPEN:
        raise AppError(ErrorCode.NOT_FOUND, "slot not available")
    if _aware(slot.starts_at) <= now:
        raise AppError(ErrorCode.BUSINESS_RULE, "slot already started")
    _check_overlap(db, slot, user_id)
    existing = db.execute(select(Booking).where(Booking.slot_id == slot_id, Booking.user_id == user_id)).scalar_one_or_none()
    if existing is not None and existing.status in (BOOKED, WAITLISTED, CHECKED_IN):
        return existing  # 重复提交：返回已有预约
    if _take_seat(db, slot_id):
        target = BOOKED
    elif allow_waitlist:
        target = WAITLISTED
    else:
        raise AppError(ErrorCode.LIMIT_REACHED, "slot is full", data={"slot_id": slot_id})
    if existing is None:
        existing = Booking(slot_id=slot_id, user_id=user_id, status=target, queued_at=now, updated_at=now)
        db.add(existing)
        db.flush()
    else:
        BOOKING_FLOW.advance(db, Booking, existing.id, existing.status, target, queued_at=now, updated_at=now)
        db.refresh(existing)
    return existing


def _promote_waitlist(db: Session, slot_id: int, now: datetime) -> Booking | None:
    candidate = db.execute(
        select(Booking).where(Booking.slot_id == slot_id, Booking.status == WAITLISTED)
        .order_by(Booking.queued_at, Booking.id).limit(1)
    ).scalar_one_or_none()
    if candidate is None or not _take_seat(db, slot_id):
        return None
    BOOKING_FLOW.advance(db, Booking, candidate.id, WAITLISTED, BOOKED, updated_at=now)
    enqueue_event(db, "booking.promoted", {"booking_id": candidate.id, "user_id": candidate.user_id, "slot_id": slot_id})
    db.refresh(candidate)
    return candidate


def cancel(db: Session, booking_id: int, user_id: int, now: datetime, *, cutoff_minutes: int = 120, by_staff: bool = False) -> Booking:
    booking = db.get(Booking, booking_id)
    if booking is None or (booking.user_id != user_id and not by_staff):
        raise AppError(ErrorCode.NOT_FOUND, "booking not found")
    slot = db.get(Slot, booking.slot_id)
    if booking.status == BOOKED and not by_staff and now > _aware(slot.starts_at) - timedelta(minutes=cutoff_minutes):
        raise AppError(ErrorCode.BUSINESS_RULE, "too late to cancel", data={"cutoff_minutes": cutoff_minutes})
    previous = booking.status
    BOOKING_FLOW.advance(db, Booking, booking.id, previous, CANCELLED, updated_at=now)
    if previous == BOOKED:
        _free_seat(db, slot.id)
        _promote_waitlist(db, slot.id, now)
    db.refresh(booking)
    return booking


def check_in(db: Session, booking_id: int, now: datetime, *, checkin_before_minutes: int = 30) -> Booking:
    booking = db.get(Booking, booking_id)
    if booking is None:
        raise AppError(ErrorCode.NOT_FOUND, "booking not found")
    slot = db.get(Slot, booking.slot_id)
    if not (_aware(slot.starts_at) - timedelta(minutes=checkin_before_minutes) <= now <= _aware(slot.ends_at)):
        raise AppError(ErrorCode.BUSINESS_RULE, "outside check-in window")
    BOOKING_FLOW.advance(db, Booking, booking.id, BOOKED, CHECKED_IN, updated_at=now)
    db.refresh(booking)
    return booking


def mark_no_shows(db: Session, now: datetime) -> list[int]:
    """下课后仍为 booked 的预约标记为爽约（可重复执行）。"""
    rows = db.execute(
        select(Booking.id, Slot.ends_at).join(Slot, Slot.id == Booking.slot_id).where(Booking.status == BOOKED)
    ).all()
    marked = []
    for booking_id, ends_at in rows:
        if _aware(ends_at) < now:
            BOOKING_FLOW.advance(db, Booking, booking_id, BOOKED, NO_SHOW, updated_at=now)
            marked.append(booking_id)
    return marked


def waitlist_position(db: Session, booking_id: int) -> int:
    booking = db.get(Booking, booking_id)
    if booking is None or booking.status != WAITLISTED:
        return 0
    ahead = db.execute(
        select(func.count()).select_from(Booking).where(
            Booking.slot_id == booking.slot_id, Booking.status == WAITLISTED,
            (Booking.queued_at < booking.queued_at) | ((Booking.queued_at == booking.queued_at) & (Booking.id < booking.id)),
        )
    ).scalar_one()
    return int(ahead) + 1
