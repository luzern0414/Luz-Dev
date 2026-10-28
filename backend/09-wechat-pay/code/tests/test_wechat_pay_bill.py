"""账单下载（GZIP + SHA1）与对账差异、对账任务、小程序发货信息上传与重试。"""
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select, update

import wechat_pay.models
from luzback import AppError, ErrorCode
from luzback.testing import memory_session
from wechat_pay import jobs, service
from wechat_pay.config import set_config
from wechat_pay.models import PaymentRecord, ReconBatch, ReconDiff, RefundRecord, ShippingUpload
from wechat_pay.testing import wechat_pay_kit

BT = "test_order"
BILL_DAY = date(2026, 10, 27)


@pytest.fixture()
def kit():
    with wechat_pay_kit() as k:
        k.db = memory_session(wechat_pay.models)
        service.register_on_paid(BT, lambda db, info: None)
        k.clock.now = datetime(2026, 10, 27, 3, 0, tzinfo=timezone.utc)  # 北京时间 10-27 11:00
        yield k
        k.db.close()


def new_payment(kit, business_id, amount=1000):
    return service.create_payment(kit.db, BT, business_id, 7, 1, amount, kit.clock.now + timedelta(minutes=15), "对账",
                                  now=kit.clock.now)


def pay(kit, business_id, amount=1000, notify=True):
    result = new_payment(kit, business_id, amount)
    kit.fake.pay(result["out_trade_no"])
    if notify:
        headers, body = kit.fake.payment_notification(result["out_trade_no"])
        assert service.handle_pay_notify(kit.db, headers, body, now=kit.clock.now).result == "processed"
    return result


def next_day_noon(kit):
    kit.clock.now = datetime(2026, 10, 28, 4, 0, tzinfo=timezone.utc)  # 北京时间 10-28 12:00


def diffs(db):
    return sorted((d.diff_type, d.ref_type, d.ref_no) for d in db.scalars(select(ReconDiff)))


# ---------------------------------------------------------------- 对账


def test_reconciliation_records_all_difference_types(kit):
    ok = pay(kit, 1)
    local_only = pay(kit, 2)
    remote_only = pay(kit, 3)
    stuck = pay(kit, 4, notify=False)
    amount = pay(kit, 5)
    refunded = service.create_refund(kit.db, ok["payment_id"], 300, "退一点", "after_sales:1", now=kit.clock.now)
    kit.fake.settle_refund(refunded.out_refund_no)
    headers, body = kit.fake.refund_notification(refunded.out_refund_no)
    service.handle_refund_notify(kit.db, headers, body, now=kit.clock.now)
    # 构造差异：渠道缺单、本地缺单、金额不符、渠道有本地无的退款
    del kit.fake.orders[local_only["out_trade_no"]]
    kit.db.execute(delete(PaymentRecord).where(PaymentRecord.id == remote_only["payment_id"]))
    kit.db.commit()
    kit.fake.orders[amount["out_trade_no"]]["paid_total"] = 999
    kit.fake.refunds["RGHOST01"] = {**kit.fake.refunds[refunded.out_refund_no], "out_refund_no": "RGHOST01", "refund_id": "5099"}

    next_day_noon(kit)
    result = service.reconcile_bill(kit.db, BILL_DAY, now=kit.clock.now)
    assert result["status"] == "done" and result["diff_count"] == 5
    assert result["remote_payments"] == 4 and result["remote_refunds"] == 2 and result["local_refunds"] == 1
    assert diffs(kit.db) == sorted([
        ("missing_remote", "payment", local_only["out_trade_no"]),
        ("missing_local", "payment", remote_only["out_trade_no"]),
        ("status_mismatch", "payment", stuck["out_trade_no"]),
        ("amount_mismatch", "payment", amount["out_trade_no"]),
        ("missing_local", "refund", "RGHOST01"),
    ])
    stuck_row = kit.db.get(PaymentRecord, stuck["payment_id"], populate_existing=True)
    assert stuck_row.next_query_at.replace(tzinfo=timezone.utc) == kit.clock.now  # 唤醒补偿查单
    amount_diff = kit.db.scalar(select(ReconDiff).where(ReconDiff.diff_type == "amount_mismatch"))
    assert (amount_diff.local_amount_cents, amount_diff.remote_amount_cents) == (1000, 999)


def test_rerun_is_idempotent_and_keeps_resolved(kit):
    local_only = pay(kit, 1)
    pay(kit, 2)
    del kit.fake.orders[local_only["out_trade_no"]]
    next_day_noon(kit)
    service.reconcile_bill(kit.db, BILL_DAY, now=kit.clock.now)
    diff = kit.db.scalar(select(ReconDiff))
    service.resolve_diff(kit.db, diff.id, staff_id=3, note="线下核实")
    with pytest.raises(AppError):
        service.resolve_diff(kit.db, diff.id, staff_id=3, note="again")
    assert service.reconcile_bill(kit.db, BILL_DAY, now=kit.clock.now)["status"] == "done"  # 已完成：直接返回
    service.reconcile_bill(kit.db, BILL_DAY, now=kit.clock.now, force=True)
    rows = kit.db.scalars(select(ReconDiff)).all()
    assert len(rows) == 1 and rows[0].resolved


def test_bill_hash_mismatch_is_rejected_and_retried_later(kit):
    pay(kit, 1)
    kit.fake.bill_hash_overrides[BILL_DAY.isoformat()] = "0" * 40
    next_day_noon(kit)
    result = service.reconcile_bill(kit.db, BILL_DAY, now=kit.clock.now)
    assert result["status"] == "failed" and result["last_error"] == "hash_mismatch"
    assert kit.db.scalar(select(ReconDiff)) is None
    kit.fake.bill_hash_overrides.clear()
    assert jobs.reconcile_due(kit.db, now=kit.clock.now, lookback_days=1) == "none"  # 30 分钟内不重试
    kit.clock.advance(minutes=31)
    assert jobs.reconcile_due(kit.db, now=kit.clock.now, lookback_days=1) == f"{BILL_DAY.isoformat()}:done"


def test_plain_bill_without_gzip_is_supported(kit):
    pay(kit, 1)
    content = kit.fake.bill_text(BILL_DAY.isoformat()).encode()
    kit.fake.bill_overrides[BILL_DAY.isoformat()] = content
    original = kit.fake.download_bill
    kit.fake.download_bill = lambda url: content if original(url) else content
    next_day_noon(kit)
    assert service.reconcile_bill(kit.db, BILL_DAY, now=kit.clock.now)["diff_count"] == 0


def test_no_statement_marks_local_payments_missing_remote(kit):
    local = pay(kit, 1)
    kit.fake.bill_status[BILL_DAY.isoformat()] = "NO_STATEMENT_EXIST"
    next_day_noon(kit)
    assert service.reconcile_bill(kit.db, BILL_DAY, now=kit.clock.now)["status"] == "no_statement"
    assert diffs(kit.db) == [("missing_remote", "payment", local["out_trade_no"])]


def test_statement_creating_retries_every_thirty_minutes(kit):
    kit.fake.bill_status[BILL_DAY.isoformat()] = "STATEMENT_CREATING"
    next_day_noon(kit)
    result = service.reconcile_bill(kit.db, BILL_DAY, now=kit.clock.now)
    assert result["status"] == "creating" and result["attempts"] == 1
    batch = kit.db.scalar(select(ReconBatch))
    assert batch.next_attempt_at.replace(tzinfo=timezone.utc) == kit.clock.now + timedelta(minutes=30)


def test_bill_date_rules(kit):
    kit.clock.now = datetime(2026, 10, 28, 1, 0, tzinfo=timezone.utc)  # 北京时间 09:00，账单未就绪
    with pytest.raises(AppError) as early:
        service.reconcile_bill(kit.db, BILL_DAY, now=kit.clock.now)
    assert early.value.data["reason"] == "bill_not_ready"
    assert jobs.reconcile_due(kit.db, now=kit.clock.now, lookback_days=1) == "none"  # 10:00 前任务不处理昨天
    with pytest.raises(AppError):
        service.reconcile_bill(kit.db, date(2026, 10, 28), now=kit.clock.now)  # 当天
    with pytest.raises(AppError):
        service.reconcile_bill(kit.db, date(2026, 6, 1), now=kit.clock.now)  # 超过三个月


# ---------------------------------------------------------------- 发货信息


def test_shipping_upload_virtual_goods(kit):
    paid = pay(kit, 1)
    view = service.request_shipping_upload(kit.db, paid["payment_id"], "会员月卡*1", now=kit.clock.now)
    assert view["status"] == "uploaded" and view["logistics_type"] == 3
    payload = kit.shipping.uploads[0]
    record = kit.db.get(PaymentRecord, paid["payment_id"])
    assert payload["order_key"] == {"order_number_type": 2, "transaction_id": record.channel_txn_id}
    assert payload["shipping_list"] == [{"item_desc": "会员月卡*1"}] and payload["delivery_mode"] == 1
    assert payload["payer"] == {"openid": "openid-7"} and payload["upload_time"].endswith("+08:00")
    again = service.request_shipping_upload(kit.db, paid["payment_id"], "会员月卡*1", now=kit.clock.now)
    assert again["id"] == view["id"] and len(kit.shipping.uploads) == 1


def test_shipping_busy_is_retried_by_job(kit):
    paid = pay(kit, 1)
    kit.shipping.fail_next(-1)
    view = service.request_shipping_upload(kit.db, paid["payment_id"], "储值卡", now=kit.clock.now)
    assert view["status"] == "pending" and view["attempts"] == 1 and view["last_errcode"] == -1
    assert jobs.retry_shipping(kit.db, now=kit.clock.now) == "none"
    kit.clock.advance(minutes=2)
    assert jobs.retry_shipping(kit.db, now=kit.clock.now) == "uploaded=1"


def test_shipping_network_error_is_retryable(kit):
    paid = pay(kit, 1)
    kit.shipping.fail_next(0, transient=True)
    assert service.request_shipping_upload(kit.db, paid["payment_id"], "储值卡", now=kit.clock.now)["status"] == "pending"


@pytest.mark.parametrize("errcode,status", [(10060001, "failed"), (10060002, "uploaded"), (10060031, "failed")])
def test_shipping_error_codes(kit, errcode, status):
    paid = pay(kit, 1)
    kit.shipping.fail_next(errcode)
    assert service.request_shipping_upload(kit.db, paid["payment_id"], "x", now=kit.clock.now)["status"] == status


def test_shipping_gives_up_after_max_attempts_and_admin_retry(kit):
    set_config(kit.config.with_(shipping_max_attempts=2))
    paid = pay(kit, 1)
    kit.shipping.fail_next(10060012, times=2)
    service.request_shipping_upload(kit.db, paid["payment_id"], "x", now=kit.clock.now)
    kit.clock.advance(minutes=2)
    jobs.retry_shipping(kit.db, now=kit.clock.now)
    record = kit.db.scalar(select(ShippingUpload).execution_options(populate_existing=True))
    assert record.status == "failed" and record.attempts == 2
    assert service.retry_shipping_upload(kit.db, record.id, staff_id=3, now=kit.clock.now)["status"] == "uploaded"


def test_shipping_validation(kit):
    unpaid = new_payment(kit, 9)
    with pytest.raises(AppError) as error:
        service.request_shipping_upload(kit.db, unpaid["payment_id"], "x", now=kit.clock.now)
    assert error.value.code == ErrorCode.CONFLICT
    paid = pay(kit, 1)
    with pytest.raises(AppError) as express:
        service.request_shipping_upload(kit.db, paid["payment_id"], "x", logistics_type=1, now=kit.clock.now)
    assert express.value.code == ErrorCode.BAD_REQUEST
    set_config(kit.config.with_(shipping_enabled=False))
    assert service.request_shipping_upload(kit.db, paid["payment_id"], "x")["status"] == "skipped"


def test_refund_rows_reconcile_against_local_success(kit):
    paid = pay(kit, 1)
    info = service.create_refund(kit.db, paid["payment_id"], 1000, "全退", "after_sales:2", now=kit.clock.now)
    kit.fake.settle_refund(info.out_refund_no)
    # 本地尚未收到退款结果：渠道 SUCCESS，本地 processing → 不在本地成功窗口，记为本地缺失或状态不符
    next_day_noon(kit)
    service.reconcile_bill(kit.db, BILL_DAY, now=kit.clock.now)
    assert ("status_mismatch", "refund", info.out_refund_no) in diffs(kit.db)
    kit.db.execute(update(RefundRecord).values(status="succeeded", succeeded_at=datetime(2026, 10, 27, 3, 0, tzinfo=timezone.utc)))
    kit.db.commit()
    service.reconcile_bill(kit.db, BILL_DAY, now=kit.clock.now, force=True)
    assert diffs(kit.db) == []
