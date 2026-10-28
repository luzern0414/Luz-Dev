"""支付（微信假渠道 / 余额）、支付成功、取消、超时关单（先查单再关单）、关单后到账、钩子拒绝、事件订阅。"""
from datetime import timedelta

from sqlalchemy import select

import inventory
import order
import wallet
import wechat_pay
from luzback import ErrorCode
from order.testing import coupon_status, drain_events, events, give_coupon, sku_stock, topics
from wechat_pay import PaymentRecord, RefundRecord

from order_fixtures import *  # noqa: F401,F403
from order_fixtures import OTHER, USER, err


def payment(env, out_trade_no) -> PaymentRecord:
    env.db.expire_all()
    return env.db.scalar(select(PaymentRecord).where(PaymentRecord.out_trade_no == out_trade_no))


def reservation_status(env, order_id):
    from inventory.reservation import StockReservation
    env.db.expire_all()
    return {r.status for r in env.db.execute(select(StockReservation).where(
        StockReservation.ref == f"order:{order_id}")).scalars()}


# ---------------------------------------------------------------- 微信支付


def test_wechat_pay_success_redeems_coupon_confirms_stock(env):
    cid = give_coupon(env.db, USER, env.now)
    placed = env.place([{"kind": "goods", "sku_id": 1, "qty": 1}, {"kind": "goods", "sku_id": 2, "qty": 1}], [cid])
    oid = placed["order_id"]
    pay = env.pay_wechat(oid)
    assert pay["method"] == "wechat" and pay["amount_cents"] == 8000 and pay["pay_params"]["package"].startswith("prepay_id=")
    p = payment(env, pay["out_trade_no"])
    assert p.business_type == "order" and p.business_id == oid and p.goods_kind == "physical"
    assert p.expires_at.replace(tzinfo=None) == env.order(oid).pay_deadline_at.replace(tzinfo=None)
    assert env.pay_wechat(oid)["out_trade_no"] == pay["out_trade_no"]  # 继续支付复用同一支付单
    assert env.notify_paid(pay["out_trade_no"]).result == "processed"
    o = env.order(oid)
    assert o.status == order.PAID and o.paid_cents == 8000 and o.pay_method == "wechat"
    assert o.payment_id == p.id and o.out_trade_no == pay["out_trade_no"]
    assert coupon_status(env.db, cid) == "used" and reservation_status(env, oid) == {"confirmed"}
    paid_event = events(env.db, "order.paid")[0]
    assert paid_event == {**paid_event, "order_id": oid, "pay_method": "wechat", "paid_cents": 8000, "payment_id": p.id}
    headers, body = env.kit.fake.payment_notification(pay["out_trade_no"])
    assert wechat_pay.handle_pay_notify(env.db, headers, body, now=env.now).result == "duplicate"
    assert topics(env.db).count("order.paid") == 1
    assert drain_events(env.db, ("order.events",)) == ["payment.succeeded"]  # 订阅兜底：已成交则空操作
    assert env.order(oid).status == order.PAID


def test_pay_rejections(env):
    placed = env.place()
    oid = placed["order_id"]
    assert err(lambda: order.pay(env.db, OTHER, oid, "wechat", env.now)).code == ErrorCode.NOT_FOUND
    assert err(lambda: order.pay(env.db, USER, oid, "cash", env.now)).code == ErrorCode.BAD_REQUEST
    e = err(lambda: order.pay(env.db, USER, oid, "wechat", env.now + timedelta(minutes=16)))
    assert e.code == ErrorCode.CONFLICT and e.data["reason"] == "pay_expired"
    env.paid_order()
    order.set_service_catalog(lambda db, pid, store: {"name": "在线课", "price_cents": 100, "goods_kind": "virtual_content"})
    virtual = env.place([{"kind": "service", "product_id": 1, "qty": 1}])
    e = err(lambda: env.pay_wechat(virtual["order_id"]))
    assert e.code == ErrorCode.BUSINESS_RULE  # 小程序运营规范 2.5.13：虚拟内容须走虚拟支付


def test_hook_rejects_payment_for_cancelled_order(env):
    placed = env.place()
    pay = env.pay_wechat(placed["order_id"])
    order.cancel_order(env.db, placed["order_id"], now=env.now)  # v1 取消：不处理渠道支付单
    env.db.commit()
    assert env.notify_paid(pay["out_trade_no"]).http_status == 204
    p = payment(env, pay["out_trade_no"])
    assert p.status == "paid" and p.needs_manual and "business_rejected" in p.manual_reason
    assert env.db.scalar(select(RefundRecord).where(RefundRecord.payment_id == p.id)).amount_cents == 6000
    assert env.order(placed["order_id"]).status == order.CANCELLED


# ---------------------------------------------------------------- 余额支付


def test_balance_payment_consumes_per_line(env):
    wallet.recharge(env.db, USER, "seed-1", 20000)
    env.db.commit()
    placed = env.place([{"kind": "goods", "sku_id": 1, "qty": 1}, {"kind": "goods", "sku_id": 2, "qty": 1}])
    result = order.pay(env.db, USER, placed["order_id"], "balance", env.now)
    env.db.commit()
    assert result["status"] == order.PAID
    items = env.items(placed["order_id"])
    assert [i.pay_ref for i in items] == [f"order:{placed['order_id']}:item:{i.id}" for i in items]
    assert wallet.service.get_account(env.db, USER).principal_cents == 10000
    assert env.order(placed["order_id"]).pay_method == "balance"
    assert err(lambda: order.pay(env.db, USER, placed["order_id"], "balance", env.now)).code == ErrorCode.CONFLICT


def test_balance_insufficient_rolls_back(env):
    wallet.recharge(env.db, USER, "seed-2", 1000)
    env.db.commit()
    placed = env.place()
    e = err(lambda: order.pay(env.db, USER, placed["order_id"], "balance", env.now))
    assert e.code == ErrorCode.INSUFFICIENT_BALANCE
    env.db.rollback()
    assert env.order(placed["order_id"]).status == order.PENDING
    assert wallet.service.get_account(env.db, USER).principal_cents == 1000
    order.set_config(balance_payment_enabled=False)
    assert err(lambda: order.pay(env.db, USER, placed["order_id"], "balance", env.now)).data["reason"] == "balance_disabled"


def test_balance_payment_closes_open_wechat_payment_first(env):
    wallet.recharge(env.db, USER, "seed-3", 20000)
    env.db.commit()
    placed = env.place()
    pay = env.pay_wechat(placed["order_id"])
    order.pay(env.db, USER, placed["order_id"], "balance", env.now)
    env.db.commit()
    assert payment(env, pay["out_trade_no"]).status == "closed"
    assert env.kit.fake.orders[pay["out_trade_no"]]["trade_state"] == "CLOSED"
    assert env.order(placed["order_id"]).pay_method == "balance"


def test_balance_payment_detects_channel_payment(env):
    wallet.recharge(env.db, USER, "seed-4", 20000)
    env.db.commit()
    placed = env.place()
    pay = env.pay_wechat(placed["order_id"])
    env.kit.fake.pay(pay["out_trade_no"])  # 用户已在微信付款，回调还没到
    e = err(lambda: order.pay(env.db, USER, placed["order_id"], "balance", env.now))
    assert e.code == ErrorCode.CONFLICT and e.data["reason"] == "already_paid"
    env.db.rollback()
    o = env.order(placed["order_id"])
    assert o.status == order.PAID and o.pay_method == "wechat"
    assert wallet.service.get_account(env.db, USER).principal_cents == 20000


# ---------------------------------------------------------------- 取消


def test_user_cancel_releases_everything_and_closes_payment(env):
    cid = give_coupon(env.db, USER, env.now)
    placed = env.place([{"kind": "goods", "sku_id": 1, "qty": 2}], [cid])
    pay = env.pay_wechat(placed["order_id"])
    assert sku_stock(env.db, 1) == 8
    assert err(lambda: order.cancel(env.db, OTHER, placed["order_id"], "", env.now)).code == ErrorCode.NOT_FOUND
    order.cancel(env.db, USER, placed["order_id"], "买错了", env.now)
    env.db.commit()
    o = env.order(placed["order_id"])
    assert o.status == order.CANCELLED and o.cancel_type == "user" and o.cancel_reason == "买错了"
    assert sku_stock(env.db, 1) == 10 and coupon_status(env.db, cid) == "unused"
    assert payment(env, pay["out_trade_no"]).status == "closed"
    assert events(env.db, "order.cancelled")[0]["cancel_type"] == "user"
    assert err(lambda: order.cancel(env.db, USER, placed["order_id"], "", env.now)).code == ErrorCode.CONFLICT


def test_cancel_with_expired_coupon_marks_it_expired(env):
    cid = give_coupon(env.db, USER, env.now, valid_days=1, threshold_cents=0)
    placed = env.place([{"kind": "goods", "sku_id": 1, "qty": 1}], [cid])
    assert coupon_status(env.db, cid) == "locked"
    order.cancel(env.db, USER, placed["order_id"], "", env.now + timedelta(days=2))
    env.db.commit()
    assert coupon_status(env.db, cid) == "expired"


def test_cancel_when_channel_unavailable_keeps_order(env):
    placed = env.place()
    env.pay_wechat(placed["order_id"])
    env.kit.fake.fail_next("query", "SYSTEM_ERROR", times=5)
    e = err(lambda: order.cancel(env.db, USER, placed["order_id"], "", env.now))
    assert e.code == ErrorCode.UPSTREAM_FAILED
    env.db.rollback()
    assert env.order(placed["order_id"]).status == order.PENDING and sku_stock(env.db, 1) == 9


def test_cancel_finds_payment_already_made(env):
    placed = env.place()
    pay = env.pay_wechat(placed["order_id"])
    env.kit.fake.pay(pay["out_trade_no"])
    e = err(lambda: order.cancel(env.db, USER, placed["order_id"], "", env.now))
    assert e.code == ErrorCode.CONFLICT and e.data["reason"] == "already_paid"
    assert env.order(placed["order_id"]).status == order.PAID


def test_admin_close_requires_reason_and_audits(env):
    from luzback import AuditLog
    placed = env.place()
    assert err(lambda: order.admin_close(env.db, 3, placed["order_id"], "", now=env.now)).code == ErrorCode.BAD_REQUEST
    assert err(lambda: order.admin_close(env.db, 3, placed["order_id"], "缺货", store_ids=[5], now=env.now)).code == ErrorCode.NOT_FOUND
    order.admin_close(env.db, 3, placed["order_id"], "缺货", now=env.now)
    env.db.commit()
    assert env.order(placed["order_id"]).cancel_type == "admin"
    assert env.db.scalar(select(AuditLog.action).where(AuditLog.resource_id == str(placed["order_id"]))) == "order.close"


# ---------------------------------------------------------------- 超时关单


def test_close_expired_job_paths(env):
    unpaid = env.place()                                  # 调起过支付、未付款 → 查单 NOTPAY → 关单 → 关闭
    unpaid_pay = env.pay_wechat(unpaid["order_id"])
    never = env.place([{"kind": "goods", "sku_id": 2, "qty": 1}])   # 从未调起支付 → 直接关闭
    paid_late = env.place()                               # 渠道已付款、回调丢失 → 查单补成交
    late_pay = env.pay_wechat(paid_late["order_id"])
    env.kit.fake.pay(late_pay["out_trade_no"])
    fresh = env.place()                                   # 未到期 → 不动
    env.db.get(order.Order, fresh["order_id"]).pay_deadline_at = env.now + timedelta(hours=1)
    env.db.commit()
    later = env.now + timedelta(minutes=16)
    report = order.close_expired(env.db, later)
    assert sorted(report["closed"]) == sorted([unpaid["order_id"], never["order_id"]])
    assert report["paid"] == [paid_late["order_id"]]
    assert env.order(unpaid["order_id"]).status == order.CLOSED and env.order(unpaid["order_id"]).cancel_type == "timeout"
    assert payment(env, unpaid_pay["out_trade_no"]).status == "closed"
    assert env.kit.fake.orders[unpaid_pay["out_trade_no"]]["trade_state"] == "CLOSED"
    assert env.order(never["order_id"]).status == order.CLOSED
    assert env.order(paid_late["order_id"]).status == order.PAID
    assert env.order(fresh["order_id"]).status == order.PENDING
    assert sku_stock(env.db, 2) == 10
    assert order.close_expired(env.db, later) == {"closed": [], "paid": [], "retry": [], "skipped": []}  # 幂等


def test_close_expired_keeps_resources_when_channel_down(env):
    placed = env.place()
    env.pay_wechat(placed["order_id"])
    env.kit.fake.fail_next("query", "SYSTEM_ERROR")
    report = order.close_expired(env.db, env.now + timedelta(minutes=16))
    assert report["retry"] == [placed["order_id"]]
    assert env.order(placed["order_id"]).status == order.PENDING and sku_stock(env.db, 1) == 9
    assert order.close_expired(env.db, env.now + timedelta(minutes=17))["closed"] == [placed["order_id"]]


def test_payment_arriving_after_close_is_refunded_not_applied(env):
    placed = env.place()
    pay = env.pay_wechat(placed["order_id"])
    order.close_expired(env.db, env.now + timedelta(minutes=16))
    assert env.order(placed["order_id"]).status == order.CLOSED
    env.kit.fake.orders[pay["out_trade_no"]]["trade_state"] = "NOTPAY"  # 渠道关单前用户已完成支付（竞态）
    assert env.notify_paid(pay["out_trade_no"]).http_status == 204
    p = payment(env, pay["out_trade_no"])
    assert p.status == "paid" and p.needs_manual and "paid_after_close" in p.manual_reason
    assert env.db.scalar(select(RefundRecord.amount_cents).where(RefundRecord.payment_id == p.id)) == 6000
    assert env.order(placed["order_id"]).status == order.CLOSED


def test_jobs_and_subscriptions_are_wired():
    from order import events as order_events, jobs
    assert [j.name for j in jobs.JOBS] == ["order.close_expired", "order.auto_complete", "order.shipping_sync"]
    assert set(order_events.SUBSCRIPTIONS) == {"payment.succeeded"}


def test_job_functions_run(env):
    from order import jobs
    placed = env.place()
    env.db.get(order.Order, placed["order_id"]).pay_deadline_at = env.now - timedelta(days=1)
    env.db.commit()
    assert jobs.close_expired_job(env.db)["closed"] == 1
    assert jobs.auto_complete_job(env.db) == {"completed": 0, "skipped": 0}
    assert jobs.shipping_sync_job(env.db) == 0


def test_shipping_upload_after_ship(env):
    paid = env.paid_order()
    order.ship(env.db, 1, paid["order_id"], "顺丰速运", "SF1234567", now=env.now)
    env.db.commit()
    order.service.after_commit_shipping(env.db, paid["order_id"])
    o = env.order(paid["order_id"])
    assert o.shipping_uploaded_at is not None
    upload = env.kit.shipping.uploads[-1]
    assert upload["logistics_type"] == 1 and upload["shipping_list"][0]["tracking_no"] == "SF1234567"
    order.set_service_catalog(lambda db, pid, store: {"name": "保养", "price_cents": 5000})
    service = env.paid_order([{"kind": "service", "product_id": 1, "qty": 1}])
    env.kit.shipping.fail_next(10060012, transient=True)
    order.mark_fulfilled(env.db, 1, service["order_id"], now=env.now)
    env.db.commit()
    order.service.after_commit_shipping(env.db, service["order_id"])
    assert order.shipping_sync(env.db) == 0  # 已登记，09 的 shipping_retry 任务负责重试
    upload = env.db.scalar(select(wechat_pay.ShippingUpload).where(
        wechat_pay.ShippingUpload.payment_id == env.order(service["order_id"]).payment_id))
    assert upload.logistics_type == 3 and upload.status == "pending"  # 线下服务按虚拟发货录入
