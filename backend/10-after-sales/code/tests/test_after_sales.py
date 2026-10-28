"""售后：可退数量、金额精确、随机申请/驳回不超退、状态流转、退款事件与重试、售后期、非本人订单。"""
import random
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

import after_sales
from after_sales import (
    REFUND_FAILED, REFUND_ONLY, REFUNDED, REFUNDING, RETURN_PENDING, RETURN_REFUND, RefundableLine,
    apply, approve, cancel, confirm_returned, on_refund_result, reject, retry_refund,
)
from luzback import AppError, ErrorCode, OutboxEvent
from luzback.testing import memory_session

NOW = datetime(2026, 10, 26, 9, 0, tzinfo=timezone.utc)


@pytest.fixture()
def db():
    session = memory_session(after_sales, "luzback.outbox")
    session.add_all([
        RefundableLine(id=11, order_id=1, user_id=7, qty=3, paid_cents=100, paid_at=NOW),
        RefundableLine(id=12, order_id=1, user_id=7, qty=1, paid_cents=2999, paid_at=NOW),
    ])
    session.commit()
    yield session
    session.close()


def test_refund_only_flow_emits_refund_event_and_is_idempotent(db):
    case = apply(db, 7, 1, {12: 1}, REFUND_ONLY, "不想要了", NOW)
    assert case.amount_cents == 2999
    assert approve(db, case.id).status == REFUNDING
    topics = db.execute(select(OutboxEvent.topic)).scalars().all()
    assert topics == ["after_sale.applied", "after_sale.refund_requested"]
    assert on_refund_result(db, case.id, True).status == REFUNDED
    assert on_refund_result(db, case.id, True).status == REFUNDED  # 重复通知


def test_return_refund_waits_for_goods_and_failed_refund_can_retry(db):
    case = apply(db, 7, 1, {11: 1}, RETURN_REFUND, "尺码不合适", NOW)
    assert approve(db, case.id).status == RETURN_PENDING
    assert confirm_returned(db, case.id).status == REFUNDING
    assert on_refund_result(db, case.id, False).status == REFUND_FAILED
    assert retry_refund(db, case.id).status == REFUNDING


def test_quantity_limit_and_reject_releases_quota(db):
    first = apply(db, 7, 1, {11: 2}, REFUND_ONLY, "质量问题", NOW)
    with pytest.raises(AppError) as error:
        apply(db, 7, 1, {11: 2}, REFUND_ONLY, "再申请", NOW)
    assert error.value.code == ErrorCode.CONFLICT
    reject(db, first.id, "已使用，不符合退款条件")
    assert apply(db, 7, 1, {11: 3}, REFUND_ONLY, "全部退", NOW).amount_cents == 100


def test_random_apply_and_reject_never_over_refund():
    rng = random.Random(9)
    for round_ in range(60):
        session = memory_session(after_sales, "luzback.outbox")
        qty, paid = rng.randint(1, 6), rng.randint(0, 500)
        session.add(RefundableLine(id=1, order_id=1, user_id=7, qty=qty, paid_cents=paid, paid_at=NOW))
        session.commit()
        active: list[tuple[int, int]] = []
        for _ in range(12):
            line = session.get(RefundableLine, 1)
            remaining = qty - line.reserved_qty
            if remaining and rng.random() < 0.7:
                case = apply(session, 7, 1, {1: rng.randint(1, remaining)}, REFUND_ONLY, "r", NOW)
                active.append((case.id, case.amount_cents))
            elif active:
                case_id, _ = active.pop(rng.randrange(len(active)))
                reject(session, case_id, "驳回")
            session.commit()
        line = session.get(RefundableLine, 1)
        total = sum(amount for _, amount in active)
        assert total == line.reserved_cents <= paid, round_
        if line.reserved_qty < qty:
            final = apply(session, 7, 1, {1: qty - line.reserved_qty}, REFUND_ONLY, "rest", NOW)
            total += final.amount_cents
        assert total == paid, round_
        session.close()


def test_window_ownership_and_cancel(db):
    with pytest.raises(AppError) as error:
        apply(db, 7, 1, {11: 1}, REFUND_ONLY, "过期", NOW + timedelta(days=8))
    assert error.value.code == ErrorCode.BUSINESS_RULE
    with pytest.raises(AppError) as error:
        apply(db, 8, 1, {11: 1}, REFUND_ONLY, "别人的订单", NOW)
    assert error.value.code == ErrorCode.NOT_FOUND
    case = apply(db, 7, 1, {11: 1}, REFUND_ONLY, "误操作", NOW)
    with pytest.raises(AppError):
        cancel(db, case.id, 8)
    cancel(db, case.id, 7)
    db.commit()
    assert db.get(RefundableLine, 11).reserved_qty == 0
    with pytest.raises(AppError):
        approve(db, case.id)  # 已撤销不能再审核
