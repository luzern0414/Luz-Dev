"""购买规则：起售、单笔上限、周期限购（件数 / 次数）、门店范围、手机号维度、新客专享、退款与取消退回名额。"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

import order
from luzback import AppError, ErrorCode
from order import purchase

from order_fixtures import *  # noqa: F401,F403
from order_fixtures import OTHER, USER, err

PHONES = {USER: "13800000001", OTHER: "13800000001"}  # 两个账号绑定同一手机号（一人多号）


def catalog(rule: dict, price: int = 1000):
    order.set_service_catalog(lambda db, pid, store: {"name": f"服务{pid}", "price_cents": price,
                                                     "purchase_rule": rule} if pid in (1, 2) else None)


def line(qty: int = 1, pid: int = 1, **extra):
    return [{"kind": "service", "product_id": pid, "qty": qty, **extra}]


def reasons(quote) -> list[str]:
    return [i["reason"] for i in quote["issues"]]


def test_rule_parsing_rejects_bad_values_and_maps_legacy_field():
    rule = purchase.PurchaseRule.parse(None, default_key="service:1", legacy_limit=2)
    assert rule.limit_qty == 2 and rule.window == "all" and rule.key == "service:1"
    assert purchase.PurchaseRule.parse({}, default_key="x") is None
    for bad in ({"limit_qty": 1, "window": "year"}, {"min_qty": 3, "max_per_order": 2}, {"limit_qty": -1}):
        with pytest.raises(AppError) as e:
            purchase.PurchaseRule.parse(bad, default_key="x")
        assert e.value.data["reason"] == "invalid_purchase_rule"


def test_window_keys_follow_store_timezone():
    utc_late = datetime(2026, 10, 29, 17, 30, tzinfo=timezone.utc)  # 北京时间 10-30 01:30
    assert purchase.window_key("day", utc_late, "Asia/Shanghai")[0] == "d20261030"
    assert purchase.window_key("week", utc_late, "Asia/Shanghai")[0] == "w202644"
    assert purchase.window_key("month", utc_late, "Asia/Shanghai")[0] == "m202610"
    assert purchase.window_key("all", utc_late, "Asia/Shanghai") == ("all", None)


def test_min_qty_and_max_per_order(env):
    catalog({"min_qty": 2, "max_per_order": 3})
    assert reasons(env.preview(line(1))) == ["below_min_qty"]
    assert reasons(env.preview(line(4))) == ["over_order_limit"]
    assert env.preview(line(2))["submittable"] is True
    assert env.place(line(3))["status"] == order.PENDING


def test_buy_only_once_counts_orders_not_quantity(env):
    catalog({"limit_orders": 1, "max_per_order": 2})  # “只能购买一次”：一单内最多 2 件
    first = env.place(line(2))
    q = env.preview(line(1))
    assert reasons(q) == ["purchase_times_limit"] and q["purchase_rules"][0]["remaining_orders"] == 0
    assert err(lambda: env.submit(q)).code == ErrorCode.LIMIT_REACHED
    env.db.rollback()
    order.cancel(env.db, USER, first["order_id"], "重新下单", env.now)  # 取消退回次数
    env.db.commit()
    assert env.place(line(1))["status"] == order.PENDING


def test_daily_limit_resets_next_day_and_old_window_is_released_to_its_own_day(env):
    catalog({"limit_qty": 2, "window": "day"})
    first = env.place(line(2))
    assert reasons(env.preview(line(1))) == ["purchase_limit"]
    env.kit.clock.advance(seconds=86400)
    second = env.place(line(2))  # 新的一天重新计
    order.cancel(env.db, USER, first["order_id"], "", env.now)  # 昨天的单今天取消：退回昨天的周期
    env.db.commit()
    assert reasons(env.preview(line(1))) == ["purchase_limit"]
    assert second["status"] == order.PENDING


def test_shared_key_limits_several_products_together(env):
    catalog({"key": "campaign:9", "limit_qty": 3})
    env.place(line(2, pid=1))
    q = env.preview(line(2, pid=2))
    assert reasons(q) == ["purchase_limit"] and q["issues"][0]["remaining"] == 1


def test_store_scope_counts_each_store_separately(env):
    catalog({"limit_orders": 1, "scope": "store"})
    env.submit(env.preview(line(), store_id=1))
    assert reasons(env.preview(line(), store_id=1)) == ["purchase_times_limit"]
    assert env.preview(line(), store_id=2)["submittable"] is True


def test_phone_identity_blocks_second_account_and_requires_phone(env):
    order.set_buyer_contact_provider(lambda db, uid: PHONES.get(uid))
    catalog({"limit_orders": 1, "identity": "member_phone"})
    env.place(line())
    assert reasons(env.preview(line(), user=OTHER)) == ["purchase_times_limit"]  # 同一手机号的另一个账号
    order.set_buyer_contact_provider(lambda db, uid: None)
    assert reasons(env.preview(line(), user=9)) == ["phone_required"]


def test_new_customer_only_blocks_returning_buyers_and_second_pending_order(env):
    order.set_buyer_contact_provider(lambda db, uid: None)
    catalog({"new_customer_only": True})
    env.place(line(pid=1))  # 首单（待支付）占用新客资格
    q = env.preview(line(pid=2))
    assert q["submittable"] is True  # 尚无已支付订单，试算不拦
    assert err(lambda: env.submit(q)).data["reason"] == "new_customer_only"  # 提交时由新客计数行拦下
    env.db.rollback()
    placed = env.place([{"kind": "goods", "sku_id": 1, "qty": 1}], user=OTHER)  # 另一位会员先买过普通商品
    pay = order.pay(env.db, OTHER, placed["order_id"], "wechat", env.now)
    env.db.commit()
    assert env.notify_paid(pay["out_trade_no"]).http_status == 204
    assert reasons(env.preview(line(), user=OTHER)) == ["new_customer_only"]


def _paid_and_refunded(env, user: int) -> None:
    placed = env.place(line(), user=user)
    pay = order.pay(env.db, user, placed["order_id"], "wechat", env.now)
    env.db.commit()
    assert env.notify_paid(pay["out_trade_no"]).http_status == 204
    item = env.items(placed["order_id"])[0]
    order.reserve_refund(env.db, placed["order_id"], [(item.id, 1, 1000)])
    order.settle_refund(env.db, placed["order_id"], [(item.id, 1, 1000, False)], f"t:{user}", env.now)
    env.db.commit()


def test_refund_releases_quota_only_when_rule_allows(env):
    catalog({"key": "trial", "limit_orders": 1, "release_on_refund": True})
    _paid_and_refunded(env, 20)
    assert env.preview(line(), user=20)["submittable"] is True  # 全额退款后可以再买
    catalog({"key": "trial-once", "limit_orders": 1, "release_on_refund": False})
    _paid_and_refunded(env, 21)
    assert reasons(env.preview(line(), user=21)) == ["purchase_times_limit"]  # 规则不退回名额


def test_new_customer_after_full_refund_depends_on_basis(env):
    order.set_buyer_contact_provider(lambda db, uid: None)
    catalog({"key": "nc", "new_customer_only": True, "new_customer_basis": "no_kept_order"})
    _paid_and_refunded(env, 30)
    assert env.preview(line(), user=30)["submittable"] is True  # 全额退款的订单不算“保留中的订单”
    catalog({"key": "nc2", "new_customer_only": True, "new_customer_basis": "never_paid", "release_on_refund": False})
    assert reasons(env.preview(line(), user=30)) == ["new_customer_only"]  # 从未支付过才算新客


def test_counter_rows_use_subject_and_window(env):
    catalog({"limit_qty": 5, "window": "month"})
    env.place(line(2))
    env.db.expire_all()
    row = env.db.scalars(select(order.PurchaseQuota)).one()
    assert row.subject == f"user:{USER}" and row.counter_key.startswith("service:1|all|m") and row.qty == 2
    assert row.orders == 1 and row.window_end_at is not None


def test_purchase_status_for_product_page(env):
    catalog({"limit_qty": 2, "window": "day"})
    env.place(line(1))
    status = order.purchase_status(env.db, USER, {"items": line(1)}, env.now)
    view = status["rules"][0]
    assert view["bought_qty"] == 1 and view["remaining_qty"] == 1 and status["issues"] == []
    assert order.purchase_status(env.db, USER, {"items": line(2)}, env.now)["issues"][0]["reason"] == "purchase_limit"


def regular_catalog(rule: dict, promo: int = 990, regular: int | None = 2800):
    order.set_service_catalog(lambda db, pid, store: {"name": f"服务{pid}", "price_cents": promo,
                                                     "regular_price_cents": regular, "purchase_rule": rule}
                              if pid in (1, 2) else None)


def tiers(quote) -> list[tuple[str, int, int]]:
    return [(ln["price_tier"], ln["qty"], ln["unit_price_cents"]) for ln in quote["lines"]]


def test_over_limit_buys_the_rest_at_regular_price(env):
    regular_catalog({"limit_qty": 2, "window": "day", "over_limit": "regular_price"})
    q = env.preview(line(3))
    assert q["submittable"] is True and tiers(q) == [("promo", 2, 990), ("regular", 1, 2800)]
    assert q["total_cents"] == 2 * 990 + 2800
    placed = env.submit(q)
    items = env.items(placed["order_id"])
    assert sorted((it.qty, it.unit_price_cents) for it in items) == [(1, 2800), (2, 990)]
    q2 = env.preview(line(1))  # 今日活动价名额已用完：整行按原价，不报限购问题
    assert q2["submittable"] is True and tiers(q2) == [("regular", 1, 2800)]
    env.db.expire_all()
    assert env.db.scalars(select(order.PurchaseQuota)).one().qty == 2  # 原价部分不计名额


def test_over_limit_with_buy_once_and_new_customer(env):
    order.set_buyer_contact_provider(lambda db, uid: None)
    regular_catalog({"key": "trial", "limit_orders": 1, "over_limit": "regular_price"})
    env.place(line(2))  # 第一次：整行活动价
    assert tiers(env.preview(line(1))) == [("regular", 1, 2800)]  # 买过一次后按原价
    regular_catalog({"key": "newbie", "new_customer_only": True, "over_limit": "regular_price"})
    q = env.preview(line(1), user=OTHER)
    assert tiers(q) == [("promo", 1, 990)]  # 新客：活动价
    env.submit(q, user=OTHER)
    assert tiers(env.preview(line(1), user=OTHER)) == [("regular", 1, 2800)]  # 新客资格已用（首单待支付也算）


def test_over_limit_keeps_minimum_and_order_cap_on_the_whole_quantity(env):
    regular_catalog({"limit_qty": 1, "max_per_order": 3, "min_qty": 2, "over_limit": "regular_price"})
    assert reasons(env.preview(line(1))) == ["below_min_qty"]
    assert reasons(env.preview(line(4))) == ["over_order_limit"]
    assert tiers(env.preview(line(2))) == [("promo", 1, 990), ("regular", 1, 2800)]


def test_over_limit_requires_a_higher_regular_price_and_a_limit(env):
    regular_catalog({"limit_qty": 1, "over_limit": "regular_price"}, regular=None)
    assert err(lambda: env.preview(line(2))).data["fields"] == ["regular_price_cents"]
    with pytest.raises(AppError) as e:
        purchase.PurchaseRule.parse({"over_limit": "regular_price", "min_qty": 1}, default_key="x")
    assert "over_limit_without_limit" in e.value.data["fields"]


def test_regular_part_is_not_released_and_promo_part_is(env):
    regular_catalog({"limit_qty": 1, "over_limit": "regular_price"})
    placed = env.place(line(2))
    order.cancel(env.db, USER, placed["order_id"], "", env.now)
    env.db.commit()
    assert tiers(env.preview(line(2))) == [("promo", 1, 990), ("regular", 1, 2800)]  # 取消后活动价名额回来
