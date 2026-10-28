"""优惠券测试：库存售罄、每人限领、门槛抵扣、核销防重。"""
from datetime import datetime, timedelta, timezone

import pytest

from luzback import AppError, ErrorCode
from luzback.testing import memory_session
from coupon import CouponTemplate, UserCoupon, claim, discount_for, redeem


@pytest.fixture()
def db():
    session = memory_session()
    session.add(CouponTemplate(id=1, name="满 68 减 10", face_cents=1000, threshold_cents=6800,
                               remaining_stock=2, per_user_limit=1))
    session.commit()
    yield session
    session.close()


def test_claim_decrements_stock(db):
    claim(db, 1, 1)
    assert db.get(CouponTemplate, 1).remaining_stock == 1


def test_per_user_limit(db):
    claim(db, 1, 1)
    with pytest.raises(AppError) as exc:
        claim(db, 1, 1)
    assert exc.value.code == ErrorCode.BUSINESS_RULE


def test_sold_out(db):
    claim(db, 1, 1)
    claim(db, 2, 1)
    with pytest.raises(AppError) as exc:
        claim(db, 3, 1)
    assert "sold out" in exc.value.message


def test_expired_activity_rejected(db):
    db.get(CouponTemplate, 1).valid_until = datetime.now(timezone.utc) - timedelta(days=1)
    db.commit()
    with pytest.raises(AppError):
        claim(db, 1, 1)


def test_threshold_discount():
    t = CouponTemplate(face_cents=1000, threshold_cents=6800)
    assert discount_for(t, 6799) == 0
    assert discount_for(t, 6800) == 1000
    assert discount_for(CouponTemplate(face_cents=1000, threshold_cents=0), 500) == 500


def test_coupon_can_be_redeemed_once(db):
    coupon = claim(db, 1, 1)
    redeem(db, coupon.id, order_id=100)
    db.commit()
    with pytest.raises(AppError) as exc:
        redeem(db, coupon.id, order_id=101)
    assert exc.value.code == ErrorCode.CONFLICT
    assert db.get(UserCoupon, coupon.id).order_id == 100
