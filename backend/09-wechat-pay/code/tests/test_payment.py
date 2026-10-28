"""支付回调测试：验签、金额校验、重复回调幂等。"""
import json

import pytest

import inventory
import order
import wechat_pay
from inventory import Sku
from luzback import AppError, ErrorCode
from luzback.testing import memory_session
from order import PAID, Order, create_order
from wechat_pay import create_payment, handle_notify, sign_payload

SECRET = "notify-secret"


@pytest.fixture()
def setup():
    db = memory_session(inventory, order, wechat_pay)
    db.add(Sku(id=1, name="鱼头", price_cents=8800, stock=5))
    db.commit()
    created = create_order(db, 7, [(1, 1)], idempotency_key="pay-test")
    record = create_payment(db, created["order_id"], created["total_cents"])
    db.commit()
    yield db, created, record
    db.close()


def notify_body(record, txn="WX-1", amount=None):
    return json.dumps({
        "out_trade_no": record.out_trade_no,
        "transaction_id": txn,
        "amount_cents": record.amount_cents if amount is None else amount,
    }).encode()


def on_paid(db, order_id):
    from order import mark_paid
    mark_paid(db, order_id)


def test_valid_notify_marks_order_paid(setup):
    db, created, record = setup
    body = notify_body(record)
    assert handle_notify(db, body, sign_payload(body, SECRET), SECRET, on_paid) == "processed"
    assert db.get(Order, created["order_id"]).status == PAID


def test_duplicate_notify_is_ignored(setup):
    db, created, record = setup
    body = notify_body(record)
    sig = sign_payload(body, SECRET)
    assert handle_notify(db, body, sig, SECRET, on_paid) == "processed"
    assert handle_notify(db, body, sig, SECRET, on_paid) == "duplicate"


def test_bad_signature_rejected_and_order_untouched(setup):
    db, created, record = setup
    body = notify_body(record)
    with pytest.raises(AppError) as exc:
        handle_notify(db, body, "forged", SECRET, on_paid)
    assert exc.value.code == ErrorCode.UNAUTHORIZED
    assert db.get(Order, created["order_id"]).status == "pending_payment"


def test_amount_mismatch_rejected(setup):
    db, created, record = setup
    body = notify_body(record, amount=1)
    with pytest.raises(AppError) as exc:
        handle_notify(db, body, sign_payload(body, SECRET), SECRET, on_paid)
    assert exc.value.code == ErrorCode.BUSINESS_RULE
    assert db.get(Order, created["order_id"]).status == "pending_payment"
