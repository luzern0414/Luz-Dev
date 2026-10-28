"""优惠券锁定：一券不能同时用于两笔订单、取消解锁、只核销本订单锁定的券、退款退回。"""
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import coupon
from coupon import LOCKED, UNUSED, USED, CouponTemplate, UserCoupon, confirm_use, lock, return_coupon, unlock
from luzback import AppError, Base, ErrorCode
from luzback.testing import memory_session


@pytest.fixture()
def db():
    session = memory_session(coupon)
    session.add(CouponTemplate(id=1, name="满100减20", face_cents=2000, threshold_cents=10000, remaining_stock=10))
    session.add(UserCoupon(id=5, template_id=1, user_id=7, slot=1))
    session.commit()
    yield session
    session.close()


def status(db):
    db.expire_all()
    return db.get(UserCoupon, 5).status


def test_lock_confirm_and_idempotency(db):
    lock(db, 5, 7, 100)
    assert lock(db, 5, 7, 100).status == LOCKED  # 同一订单重复提交
    with pytest.raises(AppError) as error:
        lock(db, 5, 7, 101)
    assert error.value.code == ErrorCode.CONFLICT
    with pytest.raises(AppError):
        confirm_use(db, 5, 101)  # 不是该订单锁定的券
    confirm_use(db, 5, 100)
    confirm_use(db, 5, 100)
    assert status(db) == USED


def test_unlock_on_cancel_and_return_after_refund(db):
    lock(db, 5, 7, 100)
    unlock(db, 5, 999)  # 其他订单不影响
    assert status(db) == LOCKED
    unlock(db, 5, 100)
    unlock(db, 5, 100)
    assert status(db) == UNUSED
    lock(db, 5, 7, 200)
    confirm_use(db, 5, 200)
    return_coupon(db, 5, 200)
    assert status(db) == UNUSED


def test_other_users_cannot_lock(db):
    with pytest.raises(AppError) as error:
        lock(db, 5, 8, 100)
    assert error.value.code == ErrorCode.NOT_FOUND


def test_concurrent_orders_cannot_share_one_coupon(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'c.db'}", future=True, connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as s:
        s.add(CouponTemplate(id=1, name="券", face_cents=500, remaining_stock=1))
        s.add(UserCoupon(id=5, template_id=1, user_id=7, slot=1))
        s.commit()

    def use(order_id: int) -> str:
        with factory() as session:
            try:
                lock(session, 5, 7, order_id)
                session.commit()
                return "ok"
            except AppError as exc:
                session.rollback()
                return exc.code.name

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(use, range(100, 120)))
    assert results.count("ok") == 1
    engine.dispose()
