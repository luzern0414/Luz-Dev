"""退款：额度 / 次数 / 间隔 / 365 天、并发防超退、同单号重试、查询节奏、退款回调、异常退款。"""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import sessionmaker

import wechat_pay.models
from luzback import AppError, AuditLog, Base, ErrorCode, OutboxEvent
from luzback.testing import memory_session
from wechat_pay import jobs, service
from wechat_pay.config import set_config
from wechat_pay.models import PaymentRecord, RefundRecord
from wechat_pay.testing import wechat_pay_kit

BT = "test_order"


def paid_payment(kit, db=None, amount=10000, business_id=1):
    db = db or kit.db
    result = service.create_payment(db, BT, business_id, 7, 1, amount, kit.clock.now + timedelta(minutes=15), "退款测试",
                                    now=kit.clock.now)
    kit.fake.pay(result["out_trade_no"])
    headers, body = kit.fake.payment_notification(result["out_trade_no"])
    assert service.handle_pay_notify(db, headers, body, now=kit.clock.now).result == "processed"
    return result["payment_id"]


@pytest.fixture()
def kit():
    with wechat_pay_kit() as k:
        k.db = memory_session(wechat_pay.models)
        k.refunded = []
        service.register_on_paid(BT, lambda db, info: None)
        service.register_on_refunded("after_sales", lambda db, info: k.refunded.append((info.business_ref, info.status)))
        k.payment_id = paid_payment(k)
        yield k
        k.db.close()


def refund(kit, amount=1000, ref="after_sales:1", **kwargs):
    return service.create_refund(kit.db, kit.payment_id, amount, "质量问题", ref, now=kit.clock.now, **kwargs)


def reload(db, model, row_id):
    return db.get(model, row_id, populate_existing=True)


def topics(db):
    return [e.topic for e in db.scalars(select(OutboxEvent).order_by(OutboxEvent.id))]


def settle(kit, out_refund_no, status="SUCCESS"):
    kit.fake.settle_refund(out_refund_no, status)
    headers, body = kit.fake.refund_notification(out_refund_no)
    return service.handle_refund_notify(kit.db, headers, body, now=kit.clock.now)


# ---------------------------------------------------------------- 申请与回调


def test_refund_accepted_then_success_notification(kit):
    info = refund(kit)
    assert info.status == "processing"
    record = reload(kit.db, RefundRecord, info.refund_id)
    p = reload(kit.db, PaymentRecord, kit.payment_id)
    assert record.out_refund_no == f"R{p.out_trade_no}01" and record.channel_refund_id
    body = kit.fake.refunds[record.out_refund_no]
    assert body["amount"] == 1000 and body["total"] == 10000
    assert p.refunded_cents == 1000 and p.refund_count == 1
    result = settle(kit, record.out_refund_no)
    assert (result.http_status, result.result) == (204, "processed")
    record = reload(kit.db, RefundRecord, info.refund_id)
    assert record.status == "succeeded" and record.succeeded_at
    assert reload(kit.db, PaymentRecord, kit.payment_id).refund_succeeded_cents == 1000
    assert "refund.succeeded" in topics(kit.db) and kit.refunded == [("after_sales:1", "succeeded")]
    assert settle(kit, record.out_refund_no).result == "duplicate"
    assert topics(kit.db).count("refund.succeeded") == 1


def test_refund_notification_tampered_or_mismatch(kit):
    info = refund(kit)
    record = reload(kit.db, RefundRecord, info.refund_id)
    kit.fake.settle_refund(record.out_refund_no)
    headers, body = kit.fake.refund_notification(record.out_refund_no, tamper=True)
    assert service.handle_refund_notify(kit.db, headers, body, now=kit.clock.now).http_status == 401
    kit.fake.refunds[record.out_refund_no]["amount"] = 999
    headers, body = kit.fake.refund_notification(record.out_refund_no)
    result = service.handle_refund_notify(kit.db, headers, body, now=kit.clock.now)
    assert result.http_status == 400 and result.reason == "amount_mismatch"
    assert reload(kit.db, RefundRecord, info.refund_id).needs_manual


def test_refund_closed_releases_amount_and_allows_new_refund(kit):
    info = refund(kit, amount=10000)
    record = reload(kit.db, RefundRecord, info.refund_id)
    assert settle(kit, record.out_refund_no, "CLOSED").result == "processed"
    assert reload(kit.db, RefundRecord, info.refund_id).status == "closed"
    assert reload(kit.db, PaymentRecord, kit.payment_id).refunded_cents == 0
    assert "refund.failed" in topics(kit.db)
    kit.clock.advance(minutes=2)
    again = refund(kit, amount=10000)
    assert again.out_refund_no.endswith("02")  # 关闭后换新单号重新申请


def test_abnormal_refund_then_apply_abnormal_with_encrypted_fields(kit):
    info = refund(kit)
    record = reload(kit.db, RefundRecord, info.refund_id)
    assert settle(kit, record.out_refund_no, "ABNORMAL").result == "processed"
    record = reload(kit.db, RefundRecord, info.refund_id)
    assert record.status == "abnormal" and record.needs_manual and "refund.abnormal" in topics(kit.db)
    with pytest.raises(AppError):
        service.apply_abnormal_refund(kit.db, info.refund_id, refund_type="USER_BANK_CARD", staff_id=9, now=kit.clock.now)
    view = service.apply_abnormal_refund(kit.db, info.refund_id, refund_type="USER_BANK_CARD", bank_type="ICBC_DEBIT",
                                         bank_account="6222020202020202", real_name="张三", staff_id=9, now=kit.clock.now)
    assert view["status"] == "processing"
    sent = kit.fake.abnormal_requests[0]
    assert sent["bank_account"] == "6222020202020202" and sent["real_name"] == "张三"  # 假服务用平台私钥解开
    audit = kit.db.scalar(select(AuditLog).where(AuditLog.action == "payment.refund.abnormal"))
    assert "6222" not in (audit.after_json or "") and "张三" not in (audit.after_json or "")
    assert settle(kit, record.out_refund_no).result == "processed"


def test_abnormal_refund_requires_abnormal_status(kit):
    info = refund(kit)
    with pytest.raises(AppError) as error:
        service.apply_abnormal_refund(kit.db, info.refund_id, refund_type="MERCHANT_BANK_CARD", staff_id=9)
    assert error.value.code == ErrorCode.CONFLICT


# ---------------------------------------------------------------- 规则


def test_over_refund_rejected(kit):
    with pytest.raises(AppError) as error:
        refund(kit, amount=10001)
    assert error.value.data["reason"] == "exceeds_refundable"
    refund(kit, amount=6000)
    kit.clock.advance(minutes=1, seconds=1)
    with pytest.raises(AppError):
        refund(kit, amount=4001)


def test_partial_refunds_need_one_minute_interval(kit):
    refund(kit, amount=100)
    kit.clock.advance(seconds=30)
    with pytest.raises(AppError) as error:
        refund(kit, amount=100)
    assert error.value.data["reason"] == "refund_interval" and error.value.data["retry_after_s"] > 0
    kit.clock.advance(seconds=31)
    assert refund(kit, amount=100).out_refund_no.endswith("02")


def test_at_most_fifty_refunds(kit):
    for i in range(50):
        refund(kit, amount=1)
        kit.clock.advance(seconds=61)
    with pytest.raises(AppError) as error:
        refund(kit, amount=1)
    assert error.value.code == ErrorCode.LIMIT_REACHED
    assert reload(kit.db, PaymentRecord, kit.payment_id).refund_count == 50


def test_refund_window_365_days(kit):
    kit.db.execute(update(PaymentRecord).values(paid_at=kit.clock.now - timedelta(days=366)))
    kit.db.commit()
    with pytest.raises(AppError) as error:
        refund(kit)
    assert error.value.data["reason"] == "refund_window_expired"


def test_unpaid_payment_cannot_refund(kit):
    other = service.create_payment(kit.db, BT, 2, 7, 1, 500, kit.clock.now + timedelta(minutes=15), "x", now=kit.clock.now)
    with pytest.raises(AppError) as error:
        service.create_refund(kit.db, other["payment_id"], 100, "r", "x:1", now=kit.clock.now)
    assert error.value.code == ErrorCode.CONFLICT
    with pytest.raises(AppError) as missing:
        service.create_refund(kit.db, 99999, 100, "r", "x:1", now=kit.clock.now)
    assert missing.value.code == ErrorCode.NOT_FOUND


def test_concurrent_refunds_never_exceed_paid_amount(tmp_path):
    with wechat_pay_kit(refund_min_interval_s=0) as kit:
        engine = create_engine(f"sqlite:///{tmp_path / 'refund.db'}", connect_args={"check_same_thread": False, "timeout": 30})
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        service.register_on_paid(BT, lambda db, info: None)
        with factory() as db:
            payment_id = paid_payment(kit, db, amount=1000)

        def attempt(i):
            with factory() as db:
                try:
                    return service.create_refund(db, payment_id, 100, "并发", f"t:{i}", now=kit.clock.now).status
                except AppError as exc:
                    return exc.code.name

        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(attempt, range(50)))
        assert results.count("processing") == 10
        with factory() as db:
            p = db.get(PaymentRecord, payment_id)
            assert p.refunded_cents == 1000 and p.refund_count == 10
            assert len(db.scalars(select(RefundRecord)).all()) == 10
        assert sum(r["amount"] for r in kit.fake.refunds.values()) == 1000
        engine.dispose()


# ---------------------------------------------------------------- 重试与查询


def test_system_error_retries_with_same_out_refund_no(kit):
    kit.fake.fail_next("create_refund", "SYSTEM_ERROR")
    info = refund(kit)
    record = reload(kit.db, RefundRecord, info.refund_id)
    assert info.status == "pending" and record.attempts == 1 and record.last_error_code == "SYSTEM_ERROR"
    assert jobs.retry_refunds(kit.db, now=kit.clock.now).startswith("submitted=0")  # 未到重试时间
    kit.clock.advance(minutes=2)
    jobs.retry_refunds(kit.db, now=kit.clock.now)
    record = reload(kit.db, RefundRecord, info.refund_id)
    assert record.status == "processing"
    submitted = [arg for op, arg in kit.fake.calls if op == "create_refund"]
    assert submitted == [record.out_refund_no, record.out_refund_no]
    assert list(kit.fake.refunds) == [record.out_refund_no]


def test_accepted_but_response_lost_is_adopted_by_query(kit):
    kit.fake.timeout_next("create_refund", apply=True)  # 渠道已受理，应答超时
    info = refund(kit)
    assert info.status == "pending"
    kit.clock.advance(minutes=2)
    assert "adopted=1" in jobs.retry_refunds(kit.db, now=kit.clock.now)
    assert reload(kit.db, RefundRecord, info.refund_id).status == "processing"
    assert kit.fake.count("create_refund") == 1  # 查到已受理，不再提交


@pytest.mark.parametrize("code", ["FREQUENCY_LIMITED", "NOT_ENOUGH"])
def test_retryable_codes_keep_refund_pending(kit, code):
    kit.fake.fail_next("create_refund", code)
    assert refund(kit).status == "pending"
    assert reload(kit.db, PaymentRecord, kit.payment_id).refunded_cents == 1000  # 额度保持占用


def test_rejected_refund_fails_and_releases_amount(kit):
    kit.fake.fail_next("create_refund", "INVALID_REQUEST")
    info = refund(kit)
    assert info.status == "failed"
    assert reload(kit.db, PaymentRecord, kit.payment_id).refunded_cents == 0
    assert "refund.failed" in topics(kit.db) and kit.refunded == [("after_sales:1", "failed")]


def test_refund_gives_up_after_max_attempts(kit):
    set_config(kit.config.with_(refund_submit_max_attempts=2))
    kit.fake.fail_next("create_refund", "SYSTEM_ERROR", times=2)
    info = refund(kit)
    kit.clock.advance(minutes=2)
    jobs.retry_refunds(kit.db, now=kit.clock.now)  # 第二次仍失败
    record = reload(kit.db, RefundRecord, info.refund_id)
    assert record.status == "pending" and record.attempts == 2 and record.needs_manual
    kit.clock.advance(minutes=10)
    jobs.retry_refunds(kit.db, now=kit.clock.now)
    assert reload(kit.db, RefundRecord, info.refund_id).status == "failed"
    assert reload(kit.db, PaymentRecord, kit.payment_id).refunded_cents == 0


def test_refund_query_cadence_and_success(kit):
    info = refund(kit)
    record = reload(kit.db, RefundRecord, info.refund_id)
    delays = []
    for _ in range(7):
        kit.clock.now = record.next_query_at.replace(tzinfo=kit.clock.now.tzinfo)
        jobs.query_refunds(kit.db, now=kit.clock.now)
        record = reload(kit.db, RefundRecord, info.refund_id)
        delays.append(int((record.next_query_at.replace(tzinfo=kit.clock.now.tzinfo) - kit.clock.now).total_seconds()))
    assert delays == [60, 60, 60, 60, 300, 600, 1200]
    kit.fake.settle_refund(record.out_refund_no)
    kit.clock.now = record.next_query_at.replace(tzinfo=kit.clock.now.tzinfo)
    assert "finished=1" in jobs.query_refunds(kit.db, now=kit.clock.now)
    assert reload(kit.db, RefundRecord, info.refund_id).status == "succeeded"


def test_auto_refund_event_payload_has_no_pii(kit):
    info = refund(kit)
    settle(kit, reload(kit.db, RefundRecord, info.refund_id).out_refund_no)
    event = kit.db.scalar(select(OutboxEvent).where(OutboxEvent.topic == "refund.succeeded"))
    payload = json.loads(event.payload_json)
    assert payload["amount_cents"] == 1000 and payload["business_ref"] == "after_sales:1" and "openid" not in event.payload_json


def test_find_active_refund_by_business_ref(kit):
    import wechat_pay

    assert wechat_pay.find_active_refund(kit.db, "after_sales:1") is None
    assert wechat_pay.find_active_refund(kit.db, "") is None
    info = refund(kit, 1000, "after_sales:1")
    row = wechat_pay.find_active_refund(kit.db, "after_sales:1")
    assert row is not None and row.id == info.refund_id and row.status in wechat_pay.REFUND_ACTIVE_STATUSES
    assert wechat_pay.find_active_refund(kit.db, "after_sales:2") is None
    kit.db.execute(update(RefundRecord).where(RefundRecord.id == info.refund_id).values(status="failed"))
    kit.db.commit()
    assert wechat_pay.find_active_refund(kit.db, "after_sales:1") is None  # 失败的不算，可重新发起
    kit.db.execute(update(RefundRecord).where(RefundRecord.id == info.refund_id).values(status="abnormal"))
    kit.db.commit()
    assert wechat_pay.find_active_refund(kit.db, "after_sales:1").id == info.refund_id
