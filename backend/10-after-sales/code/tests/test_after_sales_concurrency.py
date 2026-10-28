"""并发（文件 SQLite + 50 线程）：同一订单行的售后申请合计永远不超过实付。"""
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

import after_sales
import order
from luzback import AppError, utcnow
from luzback.db import get_session_factory
from order.testing import add_sku, order_db


@pytest.fixture()
def paid(tmp_path):
    order.reset_config()
    after_sales.reset_config()
    db = order_db("after_sales", url=f"sqlite:///{tmp_path / 'as-race.db'}")
    add_sku(db, 1, 333, stock=100)
    placed = order.create_order(db, 7, [(1, 50)], idempotency_key="as-race-1")
    order.mark_paid(db, placed["order_id"])
    db.commit()
    item = order.service._items(db, placed["order_id"])[0]
    yield db, placed["order_id"], item.id
    db.close()


def run(n, fn):
    def wrapped(i):
        for _ in range(30):
            session = get_session_factory()()
            try:
                result = fn(session, i)
                session.commit()
                return result
            except OperationalError:
                session.rollback()
            except AppError as exc:
                session.rollback()
                return exc
            finally:
                session.close()
        return "exhausted"

    with ThreadPoolExecutor(max_workers=50) as pool:
        return list(pool.map(wrapped, range(n)))


def test_concurrent_full_line_applications_only_one_wins(paid):
    db, order_id, item_id = paid
    body = {"order_id": order_id, "type": "refund_only", "reason_code": "quality", "lines": [{"item_id": item_id, "qty": 50}]}
    results = run(50, lambda s, i: after_sales.apply(s, 7, body))
    wins = [r for r in results if isinstance(r, dict)]
    assert len(wins) == 1 and wins[0]["amount_cents"] == 16650 and "exhausted" not in results
    assert all(r.code in (4090, 4220) for r in results if isinstance(r, AppError))
    db.expire_all()
    assert db.get(order.OrderItem, item_id).refund_reserved_cents == 16650


def test_concurrent_partial_applications_never_exceed_paid(paid):
    db, order_id, item_id = paid
    body = {"order_id": order_id, "type": "refund_only", "reason_code": "quality", "lines": [{"item_id": item_id, "qty": 1}]}
    results = run(50, lambda s, i: after_sales.apply(s, 7, body))
    assert "exhausted" not in results
    wins = [r for r in results if isinstance(r, dict)]
    db.expire_all()
    item = db.get(order.OrderItem, item_id)
    total = db.scalar(select(func.coalesce(func.sum(after_sales.AfterSale.amount_cents), 0)))
    assert item.refund_reserved_cents == total == sum(w["amount_cents"] for w in wins) <= item.paid_cents
    assert item.refund_reserved_qty == len(wins) <= item.qty
