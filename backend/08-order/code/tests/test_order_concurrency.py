"""并发（文件 SQLite + 线程）：库存不超卖、同一报价只下一单、限购不超、退款额度占用不超过实付。"""
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

import order
from luzback import AppError, utcnow
from luzback.db import get_session_factory
from order.testing import add_sku, order_db, sku_stock


@pytest.fixture()
def filedb(tmp_path):
    order.reset_config()
    db = order_db(url=f"sqlite:///{tmp_path / 'order-race.db'}")
    yield db
    db.close()
    order.set_service_catalog(None)


def run(n, fn):
    def wrapped(i):
        for _ in range(20):
            session = get_session_factory()()
            try:
                result = fn(session, i)
                session.commit()
                return "ok" if result is None else result
            except OperationalError:  # SQLite 写锁超时：重试（Postgres 下不会出现）
                session.rollback()
                continue
            except AppError as exc:
                session.rollback()
                return exc
            finally:
                session.close()
        return "exhausted"

    with ThreadPoolExecutor(max_workers=50) as pool:
        return list(pool.map(wrapped, range(n)))


def test_concurrent_submits_never_oversell(filedb):
    add_sku(filedb, 1, 100, stock=5)
    now = utcnow()
    tokens = []
    for i in range(30):
        tokens.append(order.preview(filedb, 1000 + i, {"items": [{"kind": "goods", "sku_id": 1, "qty": 1}]}, now)["quote_token"])
        filedb.commit()
    results = run(30, lambda s, i: order.submit(s, 1000 + i, {"quote_token": tokens[i], "terms_accepted": True}, now))
    ok = [r for r in results if isinstance(r, dict)]
    assert len(ok) == 5 and sku_stock(filedb, 1) == 0
    assert all(r.code == 4091 for r in results if isinstance(r, AppError))


def test_same_quote_only_one_order(filedb):
    add_sku(filedb, 1, 100, stock=50)
    now = utcnow()
    token = order.preview(filedb, 7, {"items": [{"kind": "goods", "sku_id": 1, "qty": 1}]}, now)["quote_token"]
    filedb.commit()
    results = run(20, lambda s, i: order.submit(s, 7, {"quote_token": token, "terms_accepted": True}, now))
    assert len([r for r in results if isinstance(r, dict)]) == 1
    filedb.expire_all()
    assert filedb.scalar(select(func.count()).select_from(order.Order)) == 1 and sku_stock(filedb, 1) == 49


def test_purchase_limit_under_concurrency(filedb):
    order.set_service_catalog(lambda db, pid, store: {"name": "限购课", "price_cents": 100, "limit_per_user": 3})
    now = utcnow()
    tokens = []
    for _ in range(12):
        tokens.append(order.preview(filedb, 7, {"items": [{"kind": "service", "product_id": 1, "qty": 1}]}, now)["quote_token"])
        filedb.commit()
    results = run(12, lambda s, i: order.submit(s, 7, {"quote_token": tokens[i], "terms_accepted": True}, now))
    assert len([r for r in results if isinstance(r, dict)]) == 3
    filedb.expire_all()
    assert filedb.scalar(select(order.PurchaseCounter.qty)) == 3


def test_refund_reservation_never_exceeds_paid(filedb):
    add_sku(filedb, 1, 100, stock=100)
    placed = order.create_order(filedb, 7, [(1, 50)], idempotency_key="race-refund-1")
    order.mark_paid(filedb, placed["order_id"])
    filedb.commit()
    item = order.service._items(filedb, placed["order_id"])[0]
    results = run(60, lambda s, i: order.reserve_refund(s, placed["order_id"], [(item.id, 1, 100)]))
    assert results.count("ok") == 50 and "exhausted" not in results
    filedb.expire_all()
    item = filedb.get(order.OrderItem, item.id)
    o = filedb.get(order.Order, placed["order_id"])
    assert item.refund_reserved_qty == 50 and item.refund_reserved_cents == 5000 == o.refund_reserved_cents == o.paid_cents
