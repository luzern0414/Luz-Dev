"""订单测试：创建、幂等、整单回滚、支付流转、取消回补、非法流转。"""
import pytest
from sqlalchemy import select

import inventory
import order
from inventory import Sku
from luzback import AppError, ErrorCode, OutboxEvent
from luzback.testing import memory_session
from order import CANCELLED, PAID, PENDING, Order, cancel_order, create_order, mark_paid, transition


@pytest.fixture()
def db():
    session = memory_session(inventory, order)
    session.add_all([
        Sku(id=1, name="剁椒鱼头", price_cents=8800, stock=5),
        Sku(id=2, name="米饭", price_cents=300, stock=2),
    ])
    session.commit()
    yield session
    session.close()


def stock(db, sku_id):
    db.expire_all()
    return db.get(Sku, sku_id).stock


def test_create_order_deducts_stock_and_totals(db):
    result = create_order(db, user_id=7, lines=[(1, 2), (2, 1)], idempotency_key="k1")
    assert result["status"] == PENDING
    assert result["total_cents"] == 8800 * 2 + 300
    assert stock(db, 1) == 3 and stock(db, 2) == 1


def test_create_order_is_idempotent(db):
    first = create_order(db, 7, [(1, 1)], idempotency_key="same")
    second = create_order(db, 7, [(1, 1)], idempotency_key="same")
    assert first == second
    assert stock(db, 1) == 4  # 只扣一次
    assert len(db.execute(select(Order)).scalars().all()) == 1


def test_create_order_rolls_back_all_lines_when_one_is_short(db):
    with pytest.raises(AppError) as exc:
        create_order(db, 7, [(1, 1), (2, 5)], idempotency_key="k2")  # 米饭只有 2 份
    assert exc.value.code == ErrorCode.OUT_OF_STOCK
    # 第一行（鱼头）的扣减必须一起回滚
    assert stock(db, 1) == 5
    assert db.execute(select(Order)).scalars().all() == []


def test_create_order_writes_outbox_event(db):
    create_order(db, 7, [(1, 1)], idempotency_key="k3")
    topics = [e.topic for e in db.execute(select(OutboxEvent)).scalars().all()]
    assert topics == ["order.created"]


def test_pay_then_cancel_is_illegal(db):
    created = create_order(db, 7, [(1, 1)], idempotency_key="k4")
    mark_paid(db, created["order_id"])
    db.commit()
    with pytest.raises(AppError) as exc:
        cancel_order(db, created["order_id"])
    assert exc.value.code == ErrorCode.CONFLICT


def test_cancel_unpaid_order_restores_stock(db):
    created = create_order(db, 7, [(1, 2)], idempotency_key="k5")
    cancel_order(db, created["order_id"])
    db.commit()
    assert stock(db, 1) == 5
    assert db.get(Order, created["order_id"]).status == CANCELLED


def test_mark_paid_twice_is_rejected(db):
    created = create_order(db, 7, [(1, 1)], idempotency_key="k6")
    mark_paid(db, created["order_id"])
    db.commit()
    with pytest.raises(AppError):
        transition(db, created["order_id"], PAID)  # 已是 paid，不允许再次 paid
