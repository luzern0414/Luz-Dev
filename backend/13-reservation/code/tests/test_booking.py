"""预约名额：并发不超约、一人一约、时间冲突、候补递补、取消截止、签到窗口、爽约任务。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import booking as booking_module
from booking import (
    BOOKED, CANCELLED, CHECKED_IN, NO_SHOW, WAITLISTED, Booking, Slot, book, cancel, check_in, mark_no_shows,
    waitlist_position,
)
from luzback import AppError, Base, ErrorCode, OutboxEvent
from luzback.testing import memory_session

NOW = datetime(2026, 10, 26, 8, 0, tzinfo=timezone.utc)
CLASS_START = NOW + timedelta(hours=10)


@pytest.fixture()
def db():
    session = memory_session(booking_module, "luzback.outbox")
    session.add_all([
        Slot(id=1, resource="团课:动感单车", starts_at=CLASS_START, ends_at=CLASS_START + timedelta(hours=1), capacity=2),
        Slot(id=2, resource="私教:王教练", starts_at=CLASS_START + timedelta(minutes=30), ends_at=CLASS_START + timedelta(hours=2), capacity=1),
    ])
    session.commit()
    yield session
    session.close()


def seats(db, slot_id=1):
    db.expire_all()
    return db.get(Slot, slot_id).booked


def test_capacity_and_duplicate_submission(db):
    first = book(db, 1, 101, NOW)
    again = book(db, 1, 101, NOW)
    assert first.id == again.id and seats(db) == 1
    book(db, 1, 102, NOW)
    with pytest.raises(AppError) as error:
        book(db, 1, 103, NOW)
    assert error.value.code == ErrorCode.LIMIT_REACHED
    db.commit()
    assert seats(db) == 2


def test_overlapping_bookings_are_rejected(db):
    book(db, 1, 101, NOW)
    with pytest.raises(AppError) as error:
        book(db, 2, 101, NOW)
    assert error.value.code == ErrorCode.BUSINESS_RULE


def test_cancel_promotes_first_waitlisted_user(db):
    a = book(db, 1, 101, NOW)
    book(db, 1, 102, NOW)
    w1 = book(db, 1, 103, NOW + timedelta(seconds=1), allow_waitlist=True)
    w2 = book(db, 1, 104, NOW + timedelta(seconds=2), allow_waitlist=True)
    assert w1.status == WAITLISTED and waitlist_position(db, w2.id) == 2
    cancel(db, a.id, 101, NOW)
    db.commit()
    db.expire_all()
    assert db.get(Booking, w1.id).status == BOOKED and db.get(Booking, w2.id).status == WAITLISTED
    assert seats(db) == 2 and waitlist_position(db, w2.id) == 1
    assert db.execute(select(OutboxEvent.topic)).scalars().all() == ["booking.promoted"]


def test_cancel_cutoff_and_staff_override(db):
    b = book(db, 1, 101, NOW)
    late = CLASS_START - timedelta(minutes=30)
    with pytest.raises(AppError):
        cancel(db, b.id, 101, late, cutoff_minutes=120)
    assert cancel(db, b.id, 0, late, by_staff=True).status == CANCELLED
    rebooked = book(db, 1, 101, NOW)  # 取消后可再约，复用记录
    assert rebooked.id == b.id and rebooked.status == BOOKED


def test_other_users_cannot_cancel(db):
    b = book(db, 1, 101, NOW)
    with pytest.raises(AppError) as error:
        cancel(db, b.id, 999, NOW)
    assert error.value.code == ErrorCode.NOT_FOUND


def test_check_in_window_and_no_show_job(db):
    a, b = book(db, 1, 101, NOW), book(db, 1, 102, NOW)
    with pytest.raises(AppError):
        check_in(db, a.id, CLASS_START - timedelta(hours=1))
    assert check_in(db, a.id, CLASS_START - timedelta(minutes=10)).status == CHECKED_IN
    assert mark_no_shows(db, CLASS_START + timedelta(hours=2)) == [b.id]
    assert mark_no_shows(db, CLASS_START + timedelta(hours=3)) == []
    assert db.get(Booking, b.id).status == NO_SHOW


def test_concurrent_booking_never_exceeds_capacity(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'slots.db'}", future=True, connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as s:
        s.add(Slot(id=1, resource="热门课", starts_at=CLASS_START, ends_at=CLASS_START + timedelta(hours=1), capacity=8))
        s.commit()

    def attempt(user_id: int) -> str:
        with factory() as session:
            try:
                book(session, 1, user_id, NOW)
                session.commit()
                return "ok"
            except AppError as exc:
                session.rollback()
                return exc.code.name

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(attempt, range(1000, 1040)))
    assert results.count("ok") == 8
    with factory() as s:
        assert s.get(Slot, 1).booked == 8
        assert len(s.execute(select(Booking)).scalars().all()) == 8
    engine.dispose()
