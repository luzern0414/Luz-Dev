"""积分：余额不为负、重复业务单号只入账一次、扣减与流水一致、FIFO 过期、追回与欠分、赠送、对账、画像、
订单返积分 / 退款追回事件、抽奖积分奖品与回执、并发扣减。"""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import identity  # noqa: F401 - 注册 users 表（画像读注册时间）
import membership
from luzback import AppError, Base, ErrorCode, OutboxEvent
from luzback.testing import memory_session
from membership import (
    Member, PointsAccount, PointsLedger, PointsMismatch, balance, clawback, earn, expire_points, gift_points,
    member_profile, reconcile, revert_spend, spend,
)
from membership.events import SUBSCRIPTIONS

T0 = datetime(2026, 10, 1, 2, 0, tzinfo=timezone.utc)


@pytest.fixture()
def db():
    session = memory_session(membership, identity)
    yield session
    session.close()


def events(db, topic):
    db.flush()
    return [json.loads(e.payload_json) for e in db.execute(select(OutboxEvent).where(OutboxEvent.topic == topic)).scalars()]


def invariant(db, user_id):
    acct = db.execute(select(PointsAccount).where(PointsAccount.user_id == user_id)).scalar_one()
    rows = db.execute(select(PointsLedger).where(PointsLedger.user_id == user_id)).scalars().all()
    assert acct.balance == sum(r.delta for r in rows) == sum(r.remaining for r in rows) >= 0
    return acct


def test_earn_then_spend(db):
    assert earn(db, 1, 100, "order", "order:A1") is True
    assert spend(db, 1, 30, "redeem", "redeem:R1") is True
    db.commit()
    assert balance(db, 1) == 70
    invariant(db, 1)


def test_spend_more_than_balance_is_rejected(db):
    earn(db, 1, 10, "order", "order:A1")
    db.commit()
    with pytest.raises(AppError) as exc:
        spend(db, 1, 11, "redeem", "redeem:R2")
    assert exc.value.code == ErrorCode.BUSINESS_RULE
    db.rollback()
    assert balance(db, 1) == 10
    with pytest.raises(AppError) as exc:
        earn(db, 1, 0, "order", "order:zero")
    assert exc.value.code == ErrorCode.BAD_REQUEST


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


def test_fifo_spend_and_expiry(db):
    earn(db, 1, 100, "order", "e1", expires_at=T0 + timedelta(days=10))
    earn(db, 1, 100, "order", "e2", expires_at=T0 + timedelta(days=5))  # 先到期先用
    spend(db, 1, 120, "redeem", "s1")
    db.commit()
    assert expire_points(db, at=T0 + timedelta(days=6)) == 0  # e2 已用完
    assert expire_points(db, at=T0 + timedelta(days=11)) == 1  # e1 剩 80 过期
    assert expire_points(db, at=T0 + timedelta(days=11)) == 0  # 可重跑
    db.commit()
    assert balance(db, 1) == 0
    invariant(db, 1)
    assert events(db, "points.expired")[0]["points"] == 80


def test_clawback_debt_then_offset_by_next_earn(db):
    earn(db, 1, 30, "order", "e1")
    spend(db, 1, 20, "redeem", "s1")
    assert clawback(db, 1, 30, "claw:1") == 20  # 余额 10 扣到 0，欠 20
    assert clawback(db, 1, 30, "claw:1") == 0  # 幂等
    earn(db, 1, 50, "order", "e2")  # 先抵销欠分 20
    db.commit()
    acct = invariant(db, 1)
    assert acct.balance == 30 and acct.debt == 0


def test_gift_and_revert_spend(db):
    assert gift_points(db, 3, 88, "birthday:2026:3", "生日礼") is True
    assert gift_points(db, 3, 88, "birthday:2026:3", "生日礼") is False
    spend(db, 3, 50, "redeem", "order:7:redeem")
    assert revert_spend(db, 3, "order:7:redeem") is True
    assert revert_spend(db, 3, "order:7:redeem") is False
    with pytest.raises(AppError):
        revert_spend(db, 3, "nope")
    db.commit()
    assert balance(db, 3) == 88
    invariant(db, 3)


def test_reconcile_flags_drift(db):
    earn(db, 1, 10, "order", "e1")
    db.commit()
    assert reconcile(db, at=T0) == 0
    db.execute(PointsAccount.__table__.update().values(balance=999))
    assert reconcile(db, at=T0) == 1
    assert reconcile(db, at=T0) == 1  # 同一天不重复写
    db.commit()
    assert len(db.execute(select(PointsMismatch)).scalars().all()) == 1


def test_member_profile_for_coupon_audience(db):
    assert member_profile(db, 404) is None
    db.add(Member(user_id=5, level=3, joined_at=T0))
    db.commit()
    profile = member_profile(db, 5)
    assert profile.level == 3 and profile.tags == frozenset() and profile.registered_at == T0
    from coupon import MemberProfile

    assert isinstance(profile, MemberProfile)


def test_order_completed_and_refunded_events(db):
    done = {"order_id": 9, "order_no": "O9", "user_id": 1, "store_id": 1, "status": "completed", "total_cents": 12345,
            "by": "user"}
    assert SUBSCRIPTIONS["order.completed"](db, done, "oc-1") == "earned"
    assert SUBSCRIPTIONS["order.completed"](db, done, "oc-1") == "duplicate"
    assert balance(db, 1) == 123
    refunded = {"order_id": 9, "order_no": "O9", "user_id": 1, "store_id": 1, "status": "completed", "total_cents": 12345,
                "amount_cents": 4000, "refunded_cents": 4000, "full": False, "ref": "after_sales:1"}
    assert SUBSCRIPTIONS["order.refunded"](db, refunded, "or-1") == "clawed_back"
    assert balance(db, 1) == 123 - 40  # 向上取整 123 × 4000 / 12345 = 40
    full = dict(refunded, amount_cents=8345, refunded_cents=12345, full=True, ref="after_sales:2")
    assert SUBSCRIPTIONS["order.refunded"](db, full, "or-2") == "clawed_back"
    assert SUBSCRIPTIONS["order.refunded"](db, full, "or-2") == "duplicate"
    db.commit()
    assert balance(db, 1) == 0
    invariant(db, 1)


def test_refund_before_completion_reduces_earn(db):
    refunded = {"order_id": 10, "user_id": 2, "total_cents": 10000, "amount_cents": 3000, "refunded_cents": 3000,
                "full": False, "ref": "after_sales:5"}
    assert SUBSCRIPTIONS["order.refunded"](db, refunded, "or-5") == "marked"
    done = {"order_id": 10, "user_id": 2, "total_cents": 10000, "by": "system"}
    assert SUBSCRIPTIONS["order.completed"](db, done, "oc-5") == "earned"
    db.commit()
    assert balance(db, 2) == 70


def test_lottery_points_prize_granted_once_and_acked(db, monkeypatch):
    import sys
    import types

    monkeypatch.setitem(sys.modules, "lottery", types.SimpleNamespace())  # 无 ack_delivery → 事件回执
    prize = {"draw_id": 21, "activity_id": 3, "member_id": 7, "store_id": 1, "prize_id": 4, "prize_kind": "points",
             "prize_name": "100 积分", "ref_id": None, "quantity": 100, "value_cents": 100,
             "delivery_key": "lottery:draw:21", "attempt": 1, "won_at": T0.isoformat()}
    assert SUBSCRIPTIONS["lottery.prize_won"](db, prize, "lp-1") == "granted"
    assert SUBSCRIPTIONS["lottery.prize_won"](db, dict(prize, attempt=2), "lp-2") == "duplicate"  # 重发奖
    assert SUBSCRIPTIONS["lottery.prize_won"](db, dict(prize, prize_kind="coupon"), "lp-3") == "ignored"
    db.commit()
    assert balance(db, 7) == 100
    acks = events(db, "lottery.delivery_result")
    assert len(acks) == 2 and all(a["ok"] and a["draw_id"] == 21 and a["delivery_key"] == "lottery:draw:21" for a in acks)
    bad = dict(prize, draw_id=22, delivery_key="lottery:draw:22", quantity=0)
    assert SUBSCRIPTIONS["lottery.prize_won"](db, bad, "lp-4") == "invalid"
    assert events(db, "lottery.delivery_result")[-1]["ok"] is False


def test_lottery_ack_via_ack_delivery(db, monkeypatch):
    import sys
    import types

    calls = []
    monkeypatch.setitem(sys.modules, "lottery", types.SimpleNamespace(ack_delivery=lambda db, *a: calls.append(a)))
    prize = {"draw_id": 30, "member_id": 1, "prize_kind": "points", "quantity": 5, "delivery_key": "lottery:draw:30"}
    assert membership.on_prize_won(db, prize, "lp-9") == "granted"
    assert calls and calls[0][0] == 30 and calls[0][1] is True


def test_concurrent_spend_never_negative(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'p.db'}", future=True,
                           connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as s:
        earn(s, 1, 500, "order", "e1")
        s.commit()

    def go(i):
        with factory() as session:
            try:
                spend(session, 1, 100, "redeem", f"s:{i}")
                session.commit()
                return "ok"
            except AppError as exc:
                session.rollback()
                return exc.code.name

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(go, range(20)))
    assert results.count("ok") == 5
    with factory() as s:
        assert balance(s, 1) == 0
        invariant(s, 1)
