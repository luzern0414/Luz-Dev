"""超卖防护测试：单线程语义 + 多线程并发（文件库）。"""
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import inventory
from inventory import Sku, deduct, restore
from luzback import AppError, Base, ErrorCode
from luzback.testing import memory_session


@pytest.fixture()
def db():
    session = memory_session(inventory)
    session.add(Sku(id=1, name="剁椒鱼头", price_cents=8800, stock=3))
    session.commit()
    yield session
    session.close()


def test_deduct_within_stock(db):
    deduct(db, 1, 2)
    db.commit()
    assert db.get(Sku, 1).stock == 1


def test_deduct_rejects_when_insufficient(db):
    with pytest.raises(AppError) as exc:
        deduct(db, 1, 4)
    assert exc.value.code == ErrorCode.OUT_OF_STOCK
    db.rollback()
    assert db.get(Sku, 1).stock == 3


def test_deduct_rejects_off_sale(db):
    db.get(Sku, 1).status = "off_sale"
    db.commit()
    with pytest.raises(AppError):
        deduct(db, 1, 1)


def test_restore_returns_stock(db):
    deduct(db, 1, 3)
    restore(db, 1, 3)
    db.commit()
    assert db.get(Sku, 1).stock == 3


def test_concurrent_buyers_never_oversell(tmp_path):
    """50 个并发买家抢 10 件库存，成功数必须恰好为 10，且库存不为负。"""
    url = f"sqlite:///{tmp_path / 'race.db'}"
    engine = create_engine(url, future=True, connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as s:
        s.add(Sku(id=1, name="限量款", price_cents=100, stock=10))
        s.commit()

    def buy(_: int) -> str:
        session = factory()
        try:
            deduct(session, 1, 1)
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
        assert s.execute(select(Sku.stock).where(Sku.id == 1)).scalar_one() == 0
    engine.dispose()


# ---------------------------------------------------------------- 按业务引用去重的回补 / 扣减

def test_restore_with_ref_is_idempotent(db):
    assert restore(db, 1, 2, ref="after_sales:9") is True
    assert restore(db, 1, 2, ref="after_sales:9") is False  # 重放不再回补
    assert restore(db, 1, 1, ref="after_sales:10") is True  # 另一张售后单照常回补
    db.commit()
    assert db.get(Sku, 1).stock == 6 and inventory.restocked(db, "after_sales:9", 1)
    assert not inventory.restocked(db, "after_sales:11", 1)
    rows = db.execute(select(inventory.StockLedger).where(inventory.StockLedger.reason == "restore")).scalars().all()
    assert sorted(r.delta for r in rows) == [1, 2]


def test_restore_unknown_sku_and_deduct_with_ref(db):
    with pytest.raises(AppError) as exc:
        restore(db, 999, 1, ref="x:1")
    assert exc.value.code == ErrorCode.NOT_FOUND
    db.rollback()
    assert deduct(db, 1, 1, ref="pos:1") is True and deduct(db, 1, 1, ref="pos:1") is False
    db.commit()
    assert db.get(Sku, 1).stock == 2


def test_restore_with_same_ref_under_race(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'restock.db'}", connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as s:
        s.add(Sku(id=1, name="退货款", price_cents=100, stock=0))
        s.commit()

    def worker(_):
        with factory() as s:
            try:
                done = restore(s, 1, 3, ref="after_sales:race")
                s.commit()
                return done
            except Exception:  # noqa: BLE001 - SQLite 写锁冲突视为未执行
                s.rollback()
                return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(worker, range(16)))
    with factory() as s:
        assert s.get(Sku, 1).stock == 3 and results.count(True) == 1
    engine.dispose()
