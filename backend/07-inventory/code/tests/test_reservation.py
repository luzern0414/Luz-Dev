"""库存预占：幂等、全有或全无、确认 / 释放、超时、并发不超卖、流水对账（references/patterns/inventory.md）。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import inventory
from inventory import Sku
from inventory.reservation import (
    CONFIRMED, RELEASED, StockReservation, confirm, expired_refs, ledger_balance, release, reserve,
)
from luzback import AppError, Base, ErrorCode
from luzback.testing import memory_session

NOW = datetime(2026, 10, 26, 10, 0, tzinfo=timezone.utc)


@pytest.fixture()
def db():
    session = memory_session(inventory, "inventory.reservation")
    session.add_all([Sku(id=1, name="年卡", price_cents=199900, stock=5), Sku(id=2, name="毛巾", price_cents=1500, stock=1)])
    session.commit()
    yield session
    session.close()


def stock(db, sku_id):
    db.expire_all()
    return db.get(Sku, sku_id).stock


def test_reserve_deducts_and_is_idempotent(db):
    reserve(db, "order:1", {1: 2}, ttl_seconds=900, now=NOW)
    db.commit()
    again = reserve(db, "order:1", {1: 2}, ttl_seconds=900, now=NOW)
    db.commit()
    assert len(again) == 1 and stock(db, 1) == 3
    with pytest.raises(AppError) as error:
        reserve(db, "order:1", {1: 3}, ttl_seconds=900, now=NOW)
    assert error.value.code == ErrorCode.IDEMPOTENCY_MISMATCH


def test_multi_sku_reservation_is_all_or_nothing(db):
    with pytest.raises(AppError) as error:
        reserve(db, "order:2", {1: 1, 2: 2}, ttl_seconds=900, now=NOW)
    assert error.value.code == ErrorCode.OUT_OF_STOCK
    db.rollback()
    assert stock(db, 1) == 5 and stock(db, 2) == 1
    assert db.execute(select(StockReservation)).first() is None


def test_confirm_then_release_requires_flag(db):
    reserve(db, "order:3", {1: 1}, ttl_seconds=900, now=NOW)
    confirm(db, "order:3")
    confirm(db, "order:3")  # 幂等
    db.commit()
    with pytest.raises(AppError):
        release(db, "order:3")
    assert release(db, "order:3", allow_confirmed=True) == 1  # 退款回库
    assert release(db, "order:3", allow_confirmed=True) == 0  # 幂等，不重复回补
    db.commit()
    assert stock(db, 1) == 5


def test_release_is_idempotent_and_blocks_later_confirm(db):
    reserve(db, "order:4", {1: 2, 2: 1}, ttl_seconds=900, now=NOW)
    assert release(db, "order:4") == 3
    assert release(db, "order:4") == 0
    db.commit()
    assert stock(db, 1) == 5 and stock(db, 2) == 1
    with pytest.raises(AppError) as error:
        confirm(db, "order:4")  # 超时释放后才支付成功：调用方需重新扣减或退款
    assert error.value.code == ErrorCode.CONFLICT
    with pytest.raises(AppError):
        reserve(db, "order:4", {1: 2, 2: 1}, ttl_seconds=900, now=NOW)


def test_expired_refs_only_returns_unconfirmed_past_deadline(db):
    reserve(db, "order:5", {1: 1}, ttl_seconds=60, now=NOW)
    reserve(db, "order:6", {1: 1}, ttl_seconds=3600, now=NOW)
    reserve(db, "order:7", {1: 1}, ttl_seconds=60, now=NOW)
    confirm(db, "order:7")
    db.commit()
    assert expired_refs(db, NOW + timedelta(minutes=5)) == ["order:5"]
    for ref in expired_refs(db, NOW + timedelta(minutes=5)):
        release(db, ref)
    db.commit()
    assert db.execute(select(StockReservation.status).where(StockReservation.ref == "order:5")).scalar_one() == RELEASED


def test_ledger_reconciles_with_stock(db):
    reserve(db, "order:8", {1: 2}, ttl_seconds=900, now=NOW)
    reserve(db, "order:9", {1: 1}, ttl_seconds=900, now=NOW)
    release(db, "order:8")
    db.commit()
    assert 5 + ledger_balance(db, 1) == stock(db, 1) == 4


def test_concurrent_reservations_never_oversell(tmp_path):
    url = f"sqlite:///{tmp_path / 'reserve.db'}"
    engine = create_engine(url, future=True, connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as s:
        s.add(Sku(id=1, name="限量私教课", price_cents=100, stock=10))
        s.commit()

    def buy(i: int) -> str:
        session = factory()
        try:
            reserve(session, f"order:{i}", {1: 1}, ttl_seconds=900, now=NOW)
            session.commit()
            return "ok"
        except AppError as exc:
            session.rollback()
            return exc.code.name
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(buy, range(50)))
    assert results.count("ok") == 10
    assert results.count("OUT_OF_STOCK") == 40
    with factory() as s:
        assert s.get(Sku, 1).stock == 0
        assert len(s.execute(select(StockReservation)).scalars().all()) == 10
        assert 10 + ledger_balance(s, 1) == 0
    engine.dispose()


def test_release_racing_with_confirm_reports_paid(tmp_path, monkeypatch):
    """关单任务读到 reserved 后，支付回调先确认：释放必须失败并报告，不得回补库存。"""
    from inventory import reservation

    engine = create_engine(f"sqlite:///{tmp_path / 'race.db'}", future=True, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as s:
        s.add(Sku(id=1, name="团课", price_cents=100, stock=5))
        s.commit()
        reserve(s, "order:10", {1: 1}, ttl_seconds=60, now=NOW)
        s.commit()

    original = reservation._rows

    def rows_then_payment_lands(session, ref):
        rows = original(session, ref)
        monkeypatch.setattr(reservation, "_rows", original)  # 只拦截关单任务的第一次读取
        with factory() as payment:
            confirm(payment, ref)
            payment.commit()
        return rows

    monkeypatch.setattr(reservation, "_rows", rows_then_payment_lands)
    with factory() as job:
        with pytest.raises(AppError) as error:
            release(job, "order:10")
        assert error.value.code == ErrorCode.CONFLICT
        job.rollback()
    monkeypatch.setattr(reservation, "_rows", original)
    with factory() as s:
        assert s.get(Sku, 1).stock == 4
        assert s.execute(select(StockReservation.status)).scalar_one() == CONFIRMED
    engine.dispose()
