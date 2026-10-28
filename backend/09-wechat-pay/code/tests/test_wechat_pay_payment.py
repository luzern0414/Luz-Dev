"""支付单：创建 / 复用 / 重新下单、回调（重复 / 篡改 / 过期 / 探测 / 金额不符 / 并发）、查单、关单、补偿任务。"""
import base64
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import wechat_pay.models
from luzback import AppError, Base, ErrorCode, OutboxEvent
from luzback.testing import memory_session
from wechat_pay import jobs, service
from wechat_pay.models import PaymentRecord, PayNotifyLog, RefundRecord
from wechat_pay.testing import TEST_APPID, wechat_pay_kit

BT = "test_order"


@pytest.fixture()
def kit():
    with wechat_pay_kit() as k:
        k.db = memory_session(wechat_pay.models)
        k.paid_calls = []
        k.deadline = k.clock.now + timedelta(minutes=15)  # 业务侧稳定的支付截止时间
        service.register_on_paid(BT, lambda db, info: k.paid_calls.append(info.business_id))
        yield k
        k.db.close()


def create(kit, business_id=1, amount=8800, expires=None, **kwargs):
    expires = expires or kit.deadline
    return service.create_payment(kit.db, BT, business_id, 7, 1, amount, expires, "测试订单", now=kit.clock.now, **kwargs)


def topics(db):
    return [e.topic for e in db.scalars(select(OutboxEvent).order_by(OutboxEvent.id))]


def payment(db, out_trade_no):
    return db.scalar(select(PaymentRecord).where(PaymentRecord.out_trade_no == out_trade_no).execution_options(
        populate_existing=True))


def notify(kit, out_trade_no, **kwargs):
    headers, body = kit.fake.payment_notification(out_trade_no, **kwargs)
    return service.handle_pay_notify(kit.db, headers, body, now=kit.clock.now)


# ---------------------------------------------------------------- 创建与调起参数


def test_create_payment_returns_signed_pay_params(kit):
    result = create(kit)
    params = result["pay_params"]
    message = f"{TEST_APPID}\n{params['timeStamp']}\n{params['nonceStr']}\n{params['package']}\n".encode()
    kit.fake.merchant_key.public_key().verify(base64.b64decode(params["paySign"]), message, padding.PKCS1v15(), hashes.SHA256())
    record = payment(kit.db, result["out_trade_no"])
    assert record.status == "paying" and record.prepay_id and params["package"] == f"prepay_id={record.prepay_id}"
    sent = kit.fake.orders[record.out_trade_no]["body"]
    assert sent["amount"] == {"total": 8800, "currency": "CNY"} and sent["payer"] == {"openid": "openid-7"}
    assert sent["time_expire"].endswith("+08:00") and sent["notify_url"].startswith("https://")


def test_virtual_content_is_rejected(kit):
    with pytest.raises(AppError) as error:
        create(kit, goods_kind="virtual_content")
    assert error.value.code == ErrorCode.BUSINESS_RULE
    assert kit.fake.count("jsapi_prepay") == 0


def test_offline_service_is_allowed(kit):
    assert create(kit, goods_kind="offline_service")["status"] == "paying"


@pytest.mark.parametrize("amount", [0, -1])
def test_invalid_amount_rejected(kit, amount):
    with pytest.raises(AppError) as error:
        create(kit, amount=amount)
    assert error.value.code == ErrorCode.BAD_REQUEST


def test_missing_openid_rejected(kit):
    service.set_openid_resolver(lambda db, uid: None)
    with pytest.raises(AppError) as error:
        create(kit)
    assert error.value.data["reason"] == "openid_missing"


def test_deadline_too_close_rejected(kit):
    with pytest.raises(AppError):
        create(kit, expires=kit.clock.now + timedelta(seconds=20))


def test_prepay_reused_within_two_hours(kit):
    first = create(kit)
    kit.clock.advance(minutes=5)
    second = create(kit)
    assert second["out_trade_no"] == first["out_trade_no"]
    assert kit.fake.count("jsapi_prepay") == 1


def test_expired_prepay_reordered_with_same_out_trade_no_and_params(kit):
    expires = kit.clock.now + timedelta(hours=5)
    first = create(kit, expires=expires)
    kit.clock.advance(hours=2)
    second = create(kit, expires=expires)
    assert second["out_trade_no"] == first["out_trade_no"]
    assert kit.fake.count("jsapi_prepay") == 2 and len(kit.fake.orders[first["out_trade_no"]]["prepay_ids"]) == 2
    assert payment(kit.db, first["out_trade_no"]).prepay_id == kit.fake.orders[first["out_trade_no"]]["prepay_ids"][1]


def test_past_time_expire_closes_then_new_out_trade_no(kit):
    first = create(kit)
    kit.clock.advance(minutes=20)
    second = create(kit, expires=kit.clock.now + timedelta(minutes=15))
    assert second["out_trade_no"] != first["out_trade_no"]
    ops = [op for op, _ in kit.fake.calls]
    assert ops.index("query") < ops.index("close")  # 先查单再关单
    old = payment(kit.db, first["out_trade_no"])
    assert old.status == "closed" and old.close_reason == "replaced"
    assert kit.fake.orders[first["out_trade_no"]]["trade_state"] == "CLOSED"
    assert "payment.closed" in topics(kit.db)


def test_amount_change_replaces_open_payment(kit):
    first = create(kit, amount=8800)
    second = create(kit, amount=9900)
    assert second["out_trade_no"] != first["out_trade_no"] and second["amount_cents"] == 9900
    assert payment(kit.db, first["out_trade_no"]).status == "closed"


def test_replacing_a_paid_order_returns_conflict(kit):
    first = create(kit)
    kit.fake.pay(first["out_trade_no"])  # 回调还没到，但渠道已支付
    with pytest.raises(AppError) as error:
        create(kit, amount=100)
    assert error.value.code == ErrorCode.CONFLICT
    assert payment(kit.db, first["out_trade_no"]).status == "paid"  # 查单时落库了支付事实


def test_already_paid_business_rejected(kit):
    first = create(kit)
    kit.fake.pay(first["out_trade_no"])
    assert notify(kit, first["out_trade_no"]).http_status == 204
    with pytest.raises(AppError) as error:
        create(kit)
    assert error.value.data["reason"] == "already_paid"


def test_prepay_system_error_keeps_payment_and_retry_uses_same_out_trade_no(kit):
    kit.fake.fail_next("jsapi_prepay", "SYSTEM_ERROR")
    with pytest.raises(AppError) as error:
        create(kit)
    assert error.value.code == ErrorCode.UPSTREAM_FAILED and error.value.data["payment_unavailable"]
    record = kit.db.scalar(select(PaymentRecord))
    assert record.status == "created"
    retry = create(kit)
    assert retry["out_trade_no"] == record.out_trade_no and retry["status"] == "paying"


def test_prepay_timeout_is_transient(kit):
    kit.fake.timeout_next("jsapi_prepay")
    with pytest.raises(AppError) as error:
        create(kit)
    assert error.value.code == ErrorCode.UPSTREAM_FAILED


def test_prepay_param_error_fails_payment(kit):
    kit.fake.fail_next("jsapi_prepay", "PARAM_ERROR")
    with pytest.raises(AppError):
        create(kit)
    record = kit.db.scalar(select(PaymentRecord).execution_options(populate_existing=True))
    assert record.status == "failed" and record.fail_code == "PARAM_ERROR"
    assert "payment.failed" in topics(kit.db)
    assert create(kit)["out_trade_no"] != record.out_trade_no  # failed 不占用未完成名额


def test_circuit_breaker_opens_after_repeated_failures(kit):
    for _ in range(10):
        kit.fake.fail_next("jsapi_prepay", "SYSTEM_ERROR")
        with pytest.raises(AppError):
            create(kit)
    with pytest.raises(AppError) as error:
        create(kit)
    assert error.value.code == ErrorCode.SERVICE_UNAVAILABLE


# ---------------------------------------------------------------- 支付回调


def test_notify_marks_paid_once_with_event_and_hook(kit):
    result = create(kit)
    kit.fake.pay(result["out_trade_no"])
    first = notify(kit, result["out_trade_no"])
    assert (first.http_status, first.body, first.result) == (204, None, "processed")
    record = payment(kit.db, result["out_trade_no"])
    assert record.status == "paid" and record.channel_txn_id and record.paid_at
    assert kit.paid_calls == [1]
    event = kit.db.scalar(select(OutboxEvent).where(OutboxEvent.topic == "payment.succeeded"))
    payload = json.loads(event.payload_json)
    assert payload["business_id"] == 1 and payload["amount_cents"] == 8800 and "openid" not in event.payload_json
    again = notify(kit, result["out_trade_no"])
    assert (again.http_status, again.result) == (204, "duplicate")
    assert kit.paid_calls == [1] and topics(kit.db).count("payment.succeeded") == 1
    assert [l.result for l in kit.db.scalars(select(PayNotifyLog))] == ["processed", "duplicate"]


@pytest.mark.parametrize("kwargs,reason", [
    (dict(tamper=True), "bad_signature"), (dict(signature="WECHATPAY/SIGNTEST/xxx"), "sign_probe"),
    (dict(serial="PUB_KEY_ID_000000"), "unknown_serial"),
])
def test_bad_signature_notifications_rejected(kit, kwargs, reason):
    result = create(kit)
    kit.fake.pay(result["out_trade_no"])
    response = notify(kit, result["out_trade_no"], **kwargs)
    assert response.http_status == 401 and response.body == {"code": "FAIL", "message": "签名验证失败"}
    assert response.reason == reason
    assert payment(kit.db, result["out_trade_no"]).status == "paying" and kit.paid_calls == []


def test_replayed_notification_with_old_timestamp_rejected(kit):
    result = create(kit)
    kit.fake.pay(result["out_trade_no"])
    headers, body = kit.fake.payment_notification(result["out_trade_no"])
    kit.clock.advance(minutes=6)
    response = service.handle_pay_notify(kit.db, headers, body, now=kit.clock.now)
    assert response.http_status == 401 and response.reason == "stale_timestamp"


def test_amount_mismatch_rejected_and_flagged(kit):
    result = create(kit)
    kit.fake.pay(result["out_trade_no"], amount=1)
    response = notify(kit, result["out_trade_no"])
    assert response.http_status == 400 and response.reason == "amount_mismatch"
    record = payment(kit.db, result["out_trade_no"])
    assert record.status == "paying" and record.needs_manual and "amount_mismatch" in record.manual_reason
    assert kit.paid_calls == []


def test_appid_mismatch_rejected(kit):
    result = create(kit)
    kit.fake.pay(result["out_trade_no"])
    kit.fake.orders[result["out_trade_no"]]["body"]["appid"] = "wx-other"
    assert notify(kit, result["out_trade_no"]).reason == "merchant_mismatch"


def test_unknown_order_notification(kit):
    result = create(kit)
    kit.fake.pay(result["out_trade_no"])
    kit.db.execute(PaymentRecord.__table__.delete())
    kit.db.commit()
    response = notify(kit, result["out_trade_no"])
    assert response.http_status == 404 and response.body["code"] == "FAIL"


def test_non_success_event_type_is_acknowledged(kit):
    headers, body = kit.fake.signed_notification("TRANSACTION.OTHER", {"out_trade_no": "X"})
    assert service.handle_pay_notify(kit.db, headers, body, now=kit.clock.now).result == "ignored"


def test_business_hook_rejection_records_payment_and_auto_refunds(kit):
    def reject(db, info):
        raise AppError(ErrorCode.CONFLICT, "order cancelled")

    service.register_on_paid(BT, reject)
    result = create(kit)
    kit.fake.pay(result["out_trade_no"])
    assert notify(kit, result["out_trade_no"]).http_status == 204
    record = payment(kit.db, result["out_trade_no"])
    assert record.status == "paid" and record.needs_manual and "business_rejected" in record.manual_reason
    refund = kit.db.scalar(select(RefundRecord))
    assert refund.status == "pending" and refund.amount_cents == 8800 and refund.business_ref == "system:auto_refund"
    assert kit.fake.count("create_refund") == 0  # 回调内不发外部请求
    jobs.retry_refunds(kit.db, now=kit.clock.now)
    assert kit.db.get(RefundRecord, refund.id, populate_existing=True).status == "processing"


def test_unregistered_business_type_needs_manual_without_refund(kit):
    service._on_paid.pop(BT)
    result = create(kit)
    kit.fake.pay(result["out_trade_no"])
    notify(kit, result["out_trade_no"])
    record = payment(kit.db, result["out_trade_no"])
    assert record.status == "paid" and record.manual_reason == "no_on_paid_handler"
    assert kit.db.scalar(select(RefundRecord)) is None


def test_paid_after_close_is_recorded_and_refunded(kit):
    result = create(kit)
    record = payment(kit.db, result["out_trade_no"])
    kit.db.execute(PaymentRecord.__table__.update().where(PaymentRecord.id == record.id).values(status="closed"))
    kit.db.commit()
    kit.fake.pay(result["out_trade_no"])
    assert notify(kit, result["out_trade_no"]).result == "processed"
    record = payment(kit.db, result["out_trade_no"])
    assert record.status == "paid" and "paid_after_close" in record.manual_reason
    assert kit.db.scalar(select(RefundRecord)).amount_cents == 8800 and kit.paid_calls == []


def test_concurrent_duplicate_notifications_settle_once(tmp_path):
    with wechat_pay_kit() as kit:
        engine = create_engine(f"sqlite:///{tmp_path / 'notify.db'}", connect_args={"check_same_thread": False, "timeout": 30})
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        calls = []
        service.register_on_paid(BT, lambda db, info: calls.append(info.payment_id))
        with factory() as db:
            result = service.create_payment(db, BT, 1, 7, 1, 8800, kit.clock.now + timedelta(minutes=15), "并发",
                                            now=kit.clock.now)
        kit.fake.pay(result["out_trade_no"])
        headers, body = kit.fake.payment_notification(result["out_trade_no"])

        def deliver(_):
            with factory() as db:
                return service.handle_pay_notify(db, headers, body, now=kit.clock.now).result

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(deliver, range(20)))
        assert results.count("processed") == 1 and results.count("duplicate") == 19
        assert len(calls) == 1
        with factory() as db:
            assert [e.topic for e in db.scalars(select(OutboxEvent))].count("payment.succeeded") == 1
        engine.dispose()


# ---------------------------------------------------------------- 查单 / 关单 / 顾客查询


def test_query_and_reconcile_settles_without_notification(kit):
    result = create(kit)
    kit.fake.pay(result["out_trade_no"])
    assert service.query_and_reconcile(kit.db, result["out_trade_no"], now=kit.clock.now)["status"] == "paid"
    assert kit.paid_calls == [1]
    assert kit.db.scalar(select(PayNotifyLog)).source == "query"


def test_query_failure_is_upstream_error(kit):
    result = create(kit)
    kit.fake.fail_next("query", "SYSTEM_ERROR")
    with pytest.raises(AppError) as error:
        service.query_and_reconcile(kit.db, result["out_trade_no"], now=kit.clock.now)
    assert error.value.code == ErrorCode.UPSTREAM_FAILED


def test_close_payment_queries_first_and_closes_unpaid(kit):
    result = create(kit)
    closed = service.close_payment(kit.db, result["payment_id"], now=kit.clock.now)
    assert closed["status"] == "closed"
    assert kit.fake.orders[result["out_trade_no"]]["trade_state"] == "CLOSED"
    assert service.close_payment(kit.db, result["payment_id"], now=kit.clock.now)["status"] == "closed"


def test_close_payment_of_paid_order_reports_paid(kit):
    result = create(kit)
    kit.fake.pay(result["out_trade_no"])
    assert service.close_payment(kit.db, result["payment_id"], now=kit.clock.now)["status"] == "paid"
    assert kit.paid_calls == [1]


def test_close_failure_is_left_to_compensation(kit):
    result = create(kit)
    kit.fake.fail_next("close", "SYSTEM_ERROR")
    assert service.close_payment(kit.db, result["payment_id"], now=kit.clock.now)["status"] == "closing"
    record = payment(kit.db, result["out_trade_no"])
    assert record.close_requested_at is not None and record.status == "paying"
    kit.clock.advance(seconds=10)
    jobs.compensate_payments(kit.db, now=kit.clock.now)
    assert payment(kit.db, result["out_trade_no"]).status == "closed"


def test_user_status_hides_other_users_and_syncs(kit):
    result = create(kit)
    with pytest.raises(AppError) as error:
        service.payment_status_for_user(kit.db, 999, result["out_trade_no"], now=kit.clock.now)
    assert error.value.code == ErrorCode.NOT_FOUND
    kit.fake.pay(result["out_trade_no"])
    status = service.payment_status_for_user(kit.db, 7, result["out_trade_no"], now=kit.clock.now)
    assert status["status"] == "paid" and status["degraded"] is False
    assert service.paid_total(kit.db, BT, 1) == 8800


def test_user_status_degrades_when_channel_down(kit):
    result = create(kit)
    kit.fake.fail_next("query", "SYSTEM_ERROR")
    status = service.payment_status_for_user(kit.db, 7, result["out_trade_no"], now=kit.clock.now)
    assert status["status"] == "paying" and status["degraded"] is True
    # 节流：3 秒内不再查单
    calls = kit.fake.count("query")
    service.payment_status_for_user(kit.db, 7, result["out_trade_no"], now=kit.clock.now)
    assert kit.fake.count("query") == calls


# ---------------------------------------------------------------- 补偿任务


def test_compensation_follows_query_schedule_then_closes_after_expiry(kit):
    result = create(kit)
    record = payment(kit.db, result["out_trade_no"])
    assert record.next_query_at.replace(tzinfo=None) == (kit.clock.now + timedelta(seconds=5)).replace(tzinfo=None)
    assert jobs.compensate_payments(kit.db, now=kit.clock.now).startswith("checked=0")
    delays = []
    for _ in range(3):
        kit.clock.now = payment(kit.db, result["out_trade_no"]).next_query_at.replace(tzinfo=kit.clock.now.tzinfo)
        jobs.compensate_payments(kit.db, now=kit.clock.now)
        nxt = payment(kit.db, result["out_trade_no"]).next_query_at.replace(tzinfo=kit.clock.now.tzinfo)
        delays.append(int((nxt - kit.clock.now).total_seconds()))
    assert delays == [30, 60, 180]
    kit.clock.advance(minutes=16)
    kit.db.execute(PaymentRecord.__table__.update().values(next_query_at=kit.clock.now))
    kit.db.commit()
    jobs.compensate_payments(kit.db, now=kit.clock.now)
    assert payment(kit.db, result["out_trade_no"]).status == "closed"
    assert kit.fake.orders[result["out_trade_no"]]["trade_state"] == "CLOSED"


def test_compensation_settles_paid_orders(kit):
    result = create(kit)
    kit.fake.pay(result["out_trade_no"])
    kit.clock.advance(seconds=6)
    assert "paid=1" in jobs.compensate_payments(kit.db, now=kit.clock.now)
    assert kit.paid_calls == [1]


def test_compensation_closes_never_created_payment_after_expiry(kit):
    kit.fake.fail_next("jsapi_prepay", "SYSTEM_ERROR")
    with pytest.raises(AppError):
        create(kit)
    kit.clock.advance(minutes=16)
    jobs.compensate_payments(kit.db, now=kit.clock.now)
    record = kit.db.scalar(select(PaymentRecord).execution_options(populate_existing=True))
    assert record.status == "closed" and record.close_reason == "never_created"


def test_compensation_keeps_going_when_query_fails(kit):
    result = create(kit)
    kit.fake.fail_next("query", "FREQUENCY_LIMITED")
    kit.clock.advance(seconds=6)
    jobs.compensate_payments(kit.db, now=kit.clock.now)
    record = payment(kit.db, result["out_trade_no"])
    assert record.status == "paying" and record.query_count == 1


# ---------------------------------------------------------------- 任务与事件接线


def test_jobs_and_subscriptions_wiring(kit):
    from luzback.scheduler import Job, JobRegistry
    from wechat_pay.events import SUBSCRIPTIONS

    assert all(isinstance(job, Job) and job.name.startswith("wechat_pay.") for job in jobs.JOBS)
    assert {j.name for j in jobs.register_jobs(JobRegistry()).all()} == {j.name for j in jobs.JOBS}
    for job in jobs.JOBS:
        job.fn(kit.db)  # 只需要会话即可运行
    service.register_on_paid("order", lambda db, info: None)
    result = service.create_payment(kit.db, "order", 42, 7, 1, 100, kit.deadline, "订单", now=kit.clock.now)
    handler = SUBSCRIPTIONS["order.cancelled"]
    assert handler(kit.db, {"order_id": 42}, "evt-1") == 1
    assert handler(kit.db, {"order_id": 42}, "evt-1") == 1  # 幂等：重复投递结果相同
    kit.db.commit()
    record = payment(kit.db, result["out_trade_no"])
    assert record.close_requested_at is not None
    from luzback import utcnow
    jobs.compensate_payments(kit.db, now=max(kit.clock.now, utcnow()) + timedelta(seconds=1))
    assert payment(kit.db, result["out_trade_no"]).status == "closed"
