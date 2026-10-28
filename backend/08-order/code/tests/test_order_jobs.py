"""超时关单：先查单；已付不取消；查询失败或关单失败不取消；已取消的不重复回补库存。"""
from datetime import datetime, timedelta, timezone

import pytest

import inventory
import order
from inventory import Sku
from luzback.testing import memory_session
from order import CANCELLED, PAID, PENDING, Order, create_order
from order.jobs import close_expired_orders

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def db():
    session = memory_session(inventory, order)
    session.add(Sku(id=1, name="月卡", price_cents=29900, stock=10))
    session.commit()
    yield session
    session.close()


def make_order(db, key, minutes_ago):
    created = create_order(db, 7, [(1, 1)], idempotency_key=key)
    db.commit()
    row = db.get(Order, created["order_id"])
    row.created_at = NOW - timedelta(minutes=minutes_ago)
    db.commit()
    return row.id


def stock(db):
    db.expire_all()
    return db.get(Sku, 1).stock


def run(db, states, closed=None, fail_close=False):
    closed = closed if closed is not None else []

    def query(o):
        value = states[o.id]
        if isinstance(value, Exception):
            raise value
        return value

    def close(o):
        if fail_close:
            raise RuntimeError("channel down")
        closed.append(o.id)

    return close_expired_orders(db, now=NOW, timeout_minutes=15, query_payment=query, close_channel=close)


def test_unpaid_orders_are_closed_and_stock_restored(db):
    expired, fresh = make_order(db, "a", 20), make_order(db, "b", 5)
    closed = []
    report = run(db, {expired: "unpaid", fresh: "unpaid"}, closed)
    assert report["cancelled"] == [expired] and closed == [expired]
    assert db.get(Order, expired).status == CANCELLED and db.get(Order, fresh).status == PENDING
    assert stock(db) == 9


def test_paid_on_query_is_marked_paid_not_cancelled(db):
    oid = make_order(db, "c", 30)
    report = run(db, {oid: "paid"})
    assert report["paid"] == [oid] and db.get(Order, oid).status == PAID and stock(db) == 9


def test_query_failure_or_unknown_keeps_order_pending(db):
    a, b = make_order(db, "d", 30), make_order(db, "e", 30)
    report = run(db, {a: RuntimeError("timeout"), b: "unknown"})
    assert sorted(report["retry"]) == sorted([a, b])
    assert {db.get(Order, a).status, db.get(Order, b).status} == {PENDING}


def test_channel_close_failure_does_not_cancel(db):
    oid = make_order(db, "f", 30)
    report = run(db, {oid: "unpaid"}, fail_close=True)
    assert report["retry"] == [oid] and db.get(Order, oid).status == PENDING and stock(db) == 9


def test_rerun_is_idempotent(db):
    oid = make_order(db, "g", 30)
    run(db, {oid: "closed"})
    report = run(db, {oid: "closed"})
    assert report["cancelled"] == [] and stock(db) == 10
