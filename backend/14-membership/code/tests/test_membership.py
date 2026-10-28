"""积分测试：余额不为负、重复业务单号只入账一次、扣减与流水一致。"""
import pytest

from luzback import AppError, ErrorCode
from luzback.testing import memory_session
from membership import PointsLedger, balance, earn, spend


@pytest.fixture()
def db():
    session = memory_session()
    yield session
    session.close()


def test_earn_then_spend(db):
    assert earn(db, 1, 100, "order", "order:A1") is True
    assert spend(db, 1, 30, "redeem", "redeem:R1") is True
    db.commit()
    assert balance(db, 1) == 70


def test_spend_more_than_balance_is_rejected(db):
    earn(db, 1, 10, "order", "order:A1")
    db.commit()
    with pytest.raises(AppError) as exc:
        spend(db, 1, 11, "redeem", "redeem:R2")
    assert exc.value.code == ErrorCode.BUSINESS_RULE
    db.rollback()
    assert balance(db, 1) == 10


def test_same_ref_key_earns_once(db):
    assert earn(db, 1, 50, "order", "order:A9") is True
    assert earn(db, 1, 50, "order", "order:A9") is False
    db.commit()
    assert balance(db, 1) == 50
    assert len(db.query(PointsLedger).all()) == 1


def test_ledger_matches_balance(db):
    earn(db, 2, 40, "order", "order:B1")
    spend(db, 2, 15, "redeem", "redeem:B1")
    db.commit()
    total = sum(e.delta for e in db.query(PointsLedger).filter_by(user_id=2).all())
    assert total == balance(db, 2) == 25
