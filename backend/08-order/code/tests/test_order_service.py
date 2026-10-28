"""试算、下单、快照与分摊、报价令牌、限购、条款、行种类、状态机、履约、退款额度。"""
import json
from datetime import timedelta

import pytest
from sqlalchemy import select

import inventory
import order
import pricing
from luzback import ErrorCode
from order import rules
from order.testing import coupon_status, events, give_coupon, sku_stock, topics

from order_fixtures import *  # noqa: F401,F403
from order_fixtures import OTHER, USER, err

GOODS = [{"kind": "goods", "sku_id": 1, "qty": 1}, {"kind": "goods", "sku_id": 2, "qty": 1}]  # 6000 + 4000


# ---------------------------------------------------------------- 试算


def test_preview_prices_server_side_with_campaign_and_coupon(env):
    order.set_promotions_provider(lambda db, uid, store, lines, now: [
        pricing.Promotion(id="full-90-10", kind=pricing.FULL_REDUCTION, threshold_cents=9000, amount_cents=1000)])
    cid = give_coupon(env.db, USER, env.now, threshold_cents=5000)
    q = env.preview(GOODS, [cid])
    assert q["items_cents"] == 10000 and q["campaign_discount_cents"] == 1000 and q["coupon_cents"] == 2000
    assert q["total_cents"] == 7000 and sum(ln["paid_cents"] for ln in q["lines"]) == 7000
    # 每行分摊：活动 1000 按 6000:4000 → 600/400；券 2000 按活动后 5400:3600 → 1200/800
    assert [(ln["campaign_alloc_cents"], ln["coupon_alloc_cents"]) for ln in q["lines"]] == [(600, 1200), (400, 800)]
    assert q["promotions"] == [{"promotion_id": "full-90-10", "amount_cents": 1000}]
    assert q["coupons"]["applied"][0]["user_coupon_id"] == cid and q["coupon_error"] is None
    assert q["quote_token"] and q["submittable"] and q["goods_kind"] == "physical"


def test_preview_reports_unusable_coupon_reasons_without_failing(env):
    short = give_coupon(env.db, USER, env.now, threshold_cents=50000)
    q = env.preview(GOODS, [short])
    assert q["coupon_error"]["reason"] == "below_threshold" and q["coupon_cents"] == 0 and q["total_cents"] == 10000
    reasons = {c["id"]: c["unusable"]["reason"] for c in q["coupons"]["unusable"]}
    assert reasons[short] == "below_threshold"
    other = give_coupon(env.db, OTHER, env.now)
    assert env.preview(GOODS, [other])["coupon_error"]["code"] == ErrorCode.NOT_FOUND


def test_preview_validation_and_issues(env):
    assert err(lambda: env.preview([{"kind": "goods", "sku_id": 99, "qty": 1}])).code == ErrorCode.NOT_FOUND
    assert err(lambda: env.preview([{"kind": "goods", "sku_id": 1, "qty": 0}])).code == ErrorCode.BAD_REQUEST
    assert err(lambda: env.preview([{"kind": "nope", "sku_id": 1, "qty": 1}])).code == ErrorCode.BAD_REQUEST
    assert err(lambda: env.preview([])).code == ErrorCode.BAD_REQUEST
    q = env.preview([{"kind": "goods", "sku_id": 1, "qty": 11}])
    assert q["issues"][0]["reason"] == "out_of_stock" and not q["submittable"]
    merged = env.preview([{"kind": "goods", "sku_id": 1, "qty": 1}, {"kind": "goods", "sku_id": 1, "qty": 2}])
    assert len(merged["lines"]) == 1 and merged["lines"][0]["qty"] == 3


# ---------------------------------------------------------------- 下单


def test_submit_snapshots_reserves_locks_and_emits(env):
    cid = give_coupon(env.db, USER, env.now)
    q = env.preview(GOODS, [cid])
    placed = env.submit(q, remark="尽快发货")
    o = env.order(placed["order_id"])
    assert o.status == order.PENDING and o.total_cents == 8000 and o.coupon_cents == 2000 and o.remark == "尽快发货"
    assert o.pay_deadline_at and o.terms_version == "purchase-v1" and o.terms_accepted_at
    assert len(o.order_no) <= 32 and o.order_no.isdigit()
    items = env.items(o.id)
    assert [(i.name, i.unit_price_cents, i.qty, i.paid_cents) for i in items] == [("跑鞋", 6000, 1, 4800), ("袜子", 4000, 1, 3200)]
    assert sum(i.paid_cents for i in items) == o.total_cents
    assert sku_stock(env.db, 1) == 9 and sku_stock(env.db, 2) == 9
    assert coupon_status(env.db, cid) == "locked"
    created = events(env.db, "order.created")[0]
    assert created["order_id"] == o.id and created["coupon_ids"] == [cid] and created["total_cents"] == 8000
    # 下单后改价不影响订单快照
    env.db.get(inventory.Sku, 1).price_cents = 1
    env.db.commit()
    assert env.items(o.id)[0].unit_price_cents == 6000


def test_quote_token_rules(env):
    q = env.preview(GOODS)
    assert err(lambda: env.submit(q, user=OTHER)).code == ErrorCode.QUOTE_EXPIRED
    assert err(lambda: env.submit({"quote_token": "missing-token"})).code == ErrorCode.QUOTE_EXPIRED
    env.submit(q)
    e = err(lambda: env.submit(q))
    assert e.code == ErrorCode.CONFLICT and e.data["reason"] == "quote_used"
    q2 = env.preview(GOODS)
    env.kit.clock.advance(seconds=601)
    e = err(lambda: env.submit(q2))
    assert e.code == ErrorCode.QUOTE_EXPIRED and e.data["reason"] == "quote_expired"


def test_submit_rejects_changed_price(env):
    q = env.preview(GOODS)
    env.db.get(inventory.Sku, 1).price_cents = 6500
    env.db.commit()
    e = err(lambda: env.submit(q))
    assert e.code == ErrorCode.QUOTE_EXPIRED and e.data["reason"] == "price_changed" and e.data["total_cents"] == 10500
    assert sku_stock(env.db, 1) == 10  # 整个事务回滚
    q = env.preview(GOODS)
    assert err(lambda: env.submit(q, expected_total_cents=1)).code == ErrorCode.QUOTE_EXPIRED


def test_submit_requires_terms_and_rolls_back_on_stock(env):
    q = env.preview(GOODS)
    e = err(lambda: order.submit(env.db, USER, {"quote_token": q["quote_token"]}, env.now))
    assert e.code == ErrorCode.BUSINESS_RULE and e.data["reason"] == "terms_required"
    e = err(lambda: order.submit(env.db, USER, {"quote_token": q["quote_token"], "terms_accepted": True,
                                                 "terms_version": "old"}, env.now))
    assert e.data["reason"] == "terms_outdated"
    env.db.rollback()
    q = env.preview([{"kind": "goods", "sku_id": 1, "qty": 11}])
    assert err(lambda: env.submit(q)).code == ErrorCode.OUT_OF_STOCK
    env.db.rollback()
    assert sku_stock(env.db, 1) == 10
    order.set_config(terms_required=False)
    assert env.submit(env.preview(GOODS), terms_accepted=False)["status"] == order.PENDING


def test_purchase_limit_and_release_on_cancel(env):
    order.set_service_catalog(lambda db, pid, store: {"name": "体验课", "price_cents": 9900, "limit_per_user": 2}
                              if pid == 5 else None)
    lesson = [{"kind": "service", "product_id": 5, "qty": 1}]
    first = env.place(lesson)
    env.place(lesson)
    q = env.preview(lesson)
    assert q["issues"][0]["reason"] == "purchase_limit"
    e = err(lambda: env.submit(q))
    assert e.code == ErrorCode.LIMIT_REACHED
    env.db.rollback()
    order.cancel(env.db, USER, first["order_id"], "不想要了", env.now)
    env.db.commit()
    assert env.place(lesson)["status"] == order.PENDING
    assert env.place(lesson, user=OTHER)["status"] == order.PENDING  # 限购按人计


def test_service_line_goods_kind_and_manual_fulfilment(env):
    order.set_service_catalog(lambda db, pid, store: {"name": "私教课", "price_cents": 30000})
    paid = env.paid_order([{"kind": "service", "product_id": 3, "qty": 2}])
    o = env.order(paid["order_id"])
    assert o.goods_kind == "offline_service" and o.kind == "service" and o.status == order.PAID
    assert env.items(o.id)[0].fulfil_status == "pending"
    order.mark_fulfilled(env.db, 1, o.id, now=env.now)
    env.db.commit()
    assert env.order(o.id).status == order.FULFILLED and env.items(o.id)[0].fulfil_status == "fulfilled"


class FakeCards(order.MemberCardPort):
    def __init__(self):
        self.issued = []
        self.quote = 5000

    def product(self, db, product_id):
        from types import SimpleNamespace
        return SimpleNamespace(id=product_id, name="年卡", price_cents=200000, status="on", deleted_at=None,
                               goods_kind="offline_service", per_member_limit=1, kind="period") if product_id == 9 else None

    def issue(self, db, product, user_id, store_id, ref, paid_cents, paid_at):
        self.issued.append((user_id, ref, paid_cents))
        return 77

    def refund_quote(self, db, card_id, now):
        return self.quote


def test_member_card_line_issues_on_payment_and_caps_refund(env):
    cards = FakeCards()
    order.set_member_card_port(cards)
    assert err(lambda: env.preview([{"kind": "member_card", "product_id": 9, "qty": 2}])).code == ErrorCode.BAD_REQUEST
    paid = env.paid_order([{"kind": "member_card", "product_id": 9, "qty": 1}])
    o = env.order(paid["order_id"])
    item = env.items(o.id)[0]
    assert cards.issued == [(USER, f"order_item:{item.id}", 200000)]
    assert item.fulfil_ref == "member_card:77" and o.status == order.FULFILLED
    assert "order.fulfilled" in topics(env.db)
    assert order.refund_quote(env.db, o.id, item.id, 1, env.now)["max_cents"] == 5000  # 会员卡模块按已用量给出上限


def test_zero_total_order_is_paid_on_submit(env):
    cid = give_coupon(env.db, USER, env.now, amount_cents=4000, threshold_cents=0)
    placed = env.place([{"kind": "goods", "sku_id": 2, "qty": 1}], [cid])
    o = env.order(placed["order_id"])
    assert o.total_cents == 0 and o.status == order.PAID and o.pay_method == "free"
    assert coupon_status(env.db, cid) == "used"


# ---------------------------------------------------------------- 状态机


def test_order_state_machine_edges():
    legal = {(s, t) for s, targets in rules.ORDER_FLOW.transitions.items() for t in targets}
    for source in rules.STATUSES:
        for target in rules.STATUSES:
            assert rules.ORDER_FLOW.can(source, target) == ((source, target) in legal)
    for terminal in (order.CANCELLED, order.CLOSED, order.REFUNDED):
        assert not rules.ORDER_FLOW.transitions.get(terminal)
    with pytest.raises(Exception):
        rules.ORDER_FLOW.check(order.PENDING, order.COMPLETED)


def test_fulfilment_and_receipt_edges(env):
    placed = env.place()
    oid = placed["order_id"]
    assert err(lambda: order.confirm_receipt(env.db, USER, oid, env.now)).code == ErrorCode.CONFLICT
    assert err(lambda: order.ship(env.db, 1, oid, "顺丰", "SF123456", now=env.now)).code == ErrorCode.CONFLICT
    pay = env.pay_wechat(oid)
    env.notify_paid(pay["out_trade_no"])
    assert err(lambda: order.confirm_receipt(env.db, USER, oid, env.now)).code == ErrorCode.CONFLICT
    assert err(lambda: order.ship(env.db, 1, oid, "", "", now=env.now)).code == ErrorCode.BAD_REQUEST
    order.ship(env.db, 1, oid, "顺丰", "SF123456", now=env.now)
    env.db.commit()
    assert env.order(oid).status == order.FULFILLING and "order.fulfilling" in topics(env.db)
    assert err(lambda: order.ship(env.db, 1, oid, "顺丰", "SF1", now=env.now)).code == ErrorCode.CONFLICT
    assert err(lambda: order.confirm_receipt(env.db, OTHER, oid, env.now)).code == ErrorCode.NOT_FOUND
    order.confirm_receipt(env.db, USER, oid, env.now)
    env.db.commit()
    o = env.order(oid)
    assert o.status == order.COMPLETED and o.fulfilled_at and o.completed_at
    assert err(lambda: order.cancel(env.db, USER, oid, "", env.now)).code == ErrorCode.CONFLICT
    logs = env.db.execute(select(order.OrderStatusLog.to_status).where(order.OrderStatusLog.order_id == oid)
                          .order_by(order.OrderStatusLog.id)).scalars().all()
    assert logs == [order.PENDING, order.PAID, order.FULFILLING, order.COMPLETED]


def test_auto_complete_job(env):
    a = env.paid_order()
    b = env.paid_order()
    c = env.paid_order()
    order.mark_fulfilled(env.db, 1, a["order_id"], now=env.now)
    order.ship(env.db, 1, b["order_id"], "顺丰", "SF0001", now=env.now)
    order.mark_fulfilled(env.db, 1, c["order_id"], now=env.now)
    env.db.commit()
    item = env.items(c["order_id"])[0]
    order.reserve_refund(env.db, c["order_id"], [(item.id, 1, 100)])  # 有处理中售后：不自动完成
    env.db.commit()
    assert order.auto_complete(env.db, env.now + timedelta(days=6))["completed"] == []
    report = order.auto_complete(env.db, env.now + timedelta(days=7, seconds=1))
    assert sorted(report["completed"]) == sorted([a["order_id"], b["order_id"]])
    assert env.order(b["order_id"]).fulfilled_at is not None
    assert order.auto_complete(env.db, env.now + timedelta(days=8))["completed"] == []  # 幂等


# ---------------------------------------------------------------- 退款额度


def test_refund_quote_cumulative_formula_and_reservation(env):
    add_three = [{"kind": "goods", "sku_id": 2, "qty": 3}]   # 12000
    cid = give_coupon(env.db, USER, env.now, amount_cents=1000, threshold_cents=0)
    paid = env.paid_order(add_three, [cid])
    oid = paid["order_id"]
    item = env.items(oid)[0]
    assert item.paid_cents == 11000
    q1 = order.refund_quote(env.db, oid, item.id, 1, env.now)
    assert q1["max_cents"] == 3666
    order.reserve_refund(env.db, oid, [(item.id, 1, 3666)])
    env.db.commit()
    assert order.refund_quote(env.db, oid, item.id, 1, env.now)["max_cents"] == 3667
    assert order.refund_quote(env.db, oid, item.id, 2, env.now)["max_cents"] == 11000 - 3666  # 退完剩余 = 剩余实付
    e = err(lambda: order.refund_quote(env.db, oid, item.id, 3, env.now))
    assert e.data["reason"] == "qty_exceeds"
    assert err(lambda: order.reserve_refund(env.db, oid, [(item.id, 2, 7335)])).code == ErrorCode.CONFLICT
    env.db.rollback()
    order.reserve_refund(env.db, oid, [(item.id, 1, 3666)])
    o = env.order(oid)
    assert o.refund_status == order.REFUNDING and o.refund_reserved_cents == 7332
    order.release_refund(env.db, oid, [(item.id, 1, 3666)])
    order.settle_refund(env.db, oid, [(item.id, 1, 3666, True)], "test:1", env.now)
    env.db.commit()
    o = env.order(oid)
    assert o.refunded_cents == 3666 and o.refund_status == order.PARTIALLY_REFUNDED and o.status == order.PAID
    assert sku_stock(env.db, 2) == 8  # 10 − 3 + 1 回库
    assert coupon_status(env.db, cid) == "used"  # 默认策略：整单全额退款才退券
    order.reserve_refund(env.db, oid, [(item.id, 2, 7334)])
    order.settle_refund(env.db, oid, [(item.id, 2, 7334, False)], "test:2", env.now)
    env.db.commit()
    o = env.order(oid)
    assert o.refunded_cents == 11000 and o.refund_status == order.REFUND_DONE and o.status == order.REFUNDED
    assert coupon_status(env.db, cid) == "unused"  # 全额退款 → 券按原方式退回（七日办法第十五条）
    refunded = events(env.db, "order.refunded")
    assert [e["full"] for e in refunded] == [False, True]


def test_reserve_refund_requires_paid_order(env):
    placed = env.place()
    item = env.items(placed["order_id"])[0]
    assert err(lambda: order.reserve_refund(env.db, placed["order_id"], [(item.id, 1, 100)])).code == ErrorCode.CONFLICT


def test_legacy_create_order_and_mark_paid(env):
    created = order.create_order(env.db, USER, [(1, 2)], idempotency_key="legacy-1", now=env.now)
    again = order.create_order(env.db, USER, [(1, 2)], idempotency_key="legacy-1", now=env.now)
    assert created == again and sku_stock(env.db, 1) == 8
    order.mark_paid(env.db, created["order_id"], now=env.now)
    env.db.commit()
    assert env.order(created["order_id"]).status == order.PAID
    other = order.create_order(env.db, USER, [(2, 1)], idempotency_key="legacy-2", now=env.now)
    order.cancel_order(env.db, other["order_id"], now=env.now)
    env.db.commit()
    assert env.order(other["order_id"]).status == order.CANCELLED and sku_stock(env.db, 2) == 10


def test_address_snapshot_is_encrypted_and_masked_for_admin(env):
    import address
    addr = address.create_address(env.db, USER, {"contact": "张三", "phone": "13812345678", "province": "湖南",
                                                  "city": "长沙", "district": "岳麓", "detail": "麓山南路 932 号"})
    env.db.commit()
    placed = env.submit(env.preview(GOODS), address_id=addr.id)
    o = env.order(placed["order_id"])
    assert "13812345678" not in (o.address_snapshot_json or "")
    mine = order.app_detail(env.db, USER, o.id)["address"]
    assert mine["phone"] == "13812345678"
    admin = order.admin_detail(env.db, o.id)["address"]
    assert admin["phone"] == "138****5678" and admin["contact"] == "张*"
    assert err(lambda: env.submit(env.preview(GOODS, user=OTHER), address_id=addr.id, user=OTHER)).code == ErrorCode.NOT_FOUND
