"""下单使用：可用券与原因、推荐、报价与锁券一致、锁定 / 核销 / 解锁 / 退券状态边、过期处理、来源收回、并发竞争。"""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

import coupon
import pricing
from coupon import CartLine, CouponTemplate, UserCoupon, service
from luzback import AppError, ErrorCode, OutboxEvent

from coupon_fixtures import *  # noqa: F401,F403
from coupon_fixtures import NOW, make_template

LINES = [CartLine(sku_id=11, product_id=1, qty=2, unit_price_cents=4000, category_id=3),
         CartLine(sku_id=12, product_id=2, qty=1, unit_price_cents=4000, category_id=4)]  # 12000


def err(fn):
    with pytest.raises(AppError) as error:
        fn()
    return error.value


def give(db, user_id=7, **template_kw):
    t = make_template(db, per_member_limit=0, **template_kw)
    c = service.claim(db, user_id, t.id, now=NOW)
    db.commit()
    return c


def status(db, coupon_id):
    db.expire_all()
    return db.get(UserCoupon, coupon_id).status


def topics(db):
    return db.execute(select(OutboxEvent.topic)).scalars().all()


# ---------------------------------------------------------------- 可用券


def test_available_coupons_lists_reasons_and_best(db):
    good = give(db, amount_cents=2000)                                          # 满100减20 → 可用
    better = give(db, type="percent_off", rate_bp=8000, threshold_cents=0)       # 八折 → 2400
    short = give(db, threshold_cents=20000)                                      # 还差 80 元
    scope = give(db, threshold_cents=0, scope_type="categories", scope_ids=[99])
    later = give(db, start_delay_days=2)                                         # 未生效
    other = give(db, user_id=8)                                                  # 别人的券不出现
    result = service.available_coupons(db, 7, LINES, now=NOW)
    assert [c["id"] for c in result["usable"]] == [better.id, good.id]
    assert result["best"] == better.id
    assert result["usable"][0]["discount_cents"] == 2400
    reasons = {c["id"]: c["unusable"] for c in result["unusable"]}
    assert reasons[short.id]["reason"] == "below_threshold" and reasons[short.id]["message"] == "还差80元可用"
    assert reasons[scope.id]["reason"] == "no_scope_items"
    assert reasons[later.id]["reason"] == "not_started"
    assert other.id not in reasons
    assert service.best_coupon(db, 7, LINES, now=NOW)["id"] == better.id
    expired = service.available_coupons(db, 7, LINES, now=NOW + timedelta(days=40))
    assert expired["usable"] == [] and {c["unusable"]["reason"] for c in expired["unusable"]} == {"expired"}


def test_price_with_coupons_validations(db):
    a = give(db)
    b = give(db)
    assert err(lambda: service.price_with_coupons(db, 8, [a.id], LINES, now=NOW)).code == ErrorCode.NOT_FOUND
    e = err(lambda: service.price_with_coupons(db, 7, [a.id, b.id], LINES, now=NOW))
    assert e.code == ErrorCode.BUSINESS_RULE and e.data["reason"] == "stack_conflict"
    coupon.set_config(max_coupons_per_order=1)
    assert err(lambda: service.price_with_coupons(db, 7, [a.id, b.id], LINES, now=NOW)).code == ErrorCode.BUSINESS_RULE
    short = give(db, threshold_cents=50000)
    e = err(lambda: service.price_with_coupons(db, 7, [short.id], LINES, now=NOW))
    assert e.data["reason"] == "below_threshold" and e.data["user_coupon_id"] == short.id


def test_stacking_coupons_from_different_groups(db):
    a = give(db, stack_group="shop", threshold_cents=0, amount_cents=1000)
    b = give(db, stack_group="platform", type="percent_off", rate_bp=9000, threshold_cents=0)
    result = service.price_with_coupons(db, 7, [a.id, b.id], LINES, now=NOW)
    assert result.coupon_cents == sum(result.line_coupon_cents)
    assert {c["user_coupon_id"] for c in result.coupons} == {a.id, b.id}


# ---------------------------------------------------------------- 锁定 / 核销 / 解锁


def test_lock_allocates_via_pricing_and_is_idempotent_per_order(db):
    c = give(db)
    campaign = [pricing.Promotion("立减", pricing.FULL_REDUCTION, amount_cents=500, sku_ids=frozenset({12}))]
    quote = service.price_with_coupons(db, 7, [c.id], LINES, campaign, now=NOW)
    locked = service.lock(db, 7, 100, [c.id], LINES, campaign, now=NOW)
    db.commit()
    assert locked.to_dict() == quote.to_dict()  # 报价与下单同一计算
    row = db.get(UserCoupon, c.id)
    assert row.status == "locked" and row.order_id == 100 and row.discount_cents == 2000
    assert sum(s["amount_cents"] for s in json.loads(row.alloc_json)) == 2000
    assert locked.to_dict()["payable_cents"] == 12000 - 500 - 2000
    again = service.lock(db, 7, 100, [c.id], LINES, campaign, now=NOW)  # 同一订单重复提交
    assert again.coupon_cents == 2000
    e = err(lambda: service.lock(db, 7, 101, [c.id], LINES, now=NOW))
    assert e.code == ErrorCode.BUSINESS_RULE and e.data["reason"] == "unavailable"
    assert "coupon.locked" in topics(db)


def test_lock_rejects_expired_or_not_started(db):
    c = give(db, valid_days=1)
    assert err(lambda: service.lock(db, 7, 1, [c.id], LINES, now=NOW + timedelta(days=2))).data["reason"] == "expired"
    d = give(db, start_delay_days=1)
    assert err(lambda: service.lock(db, 7, 1, [d.id], LINES, now=NOW)).data["reason"] == "not_started"


def test_lock_runs_inside_callers_transaction(db):
    c = give(db)
    service.lock(db, 7, 100, [c.id], LINES, now=NOW)
    db.rollback()  # 下单事务失败（例如库存不足）→ 券仍未使用
    assert status(db, c.id) == "unused"


def test_redeem_unlock_state_edges(db):
    c = give(db)
    assert service.redeem(db, 100, now=NOW) == []  # 没有锁定的券：无事发生
    service.lock(db, 7, 100, [c.id], LINES, now=NOW)
    assert service.unlock(db, 999, now=NOW) == {"unlocked": [], "expired": []}  # 其他订单不影响
    assert service.redeem(db, 100, now=NOW) == [c.id]
    assert service.redeem(db, 100, now=NOW) == [c.id]  # 重复支付回调幂等
    db.commit()
    assert status(db, c.id) == "used"
    assert db.get(CouponTemplate, c.template_id).used_count == 1
    assert service.unlock(db, 100, now=NOW) == {"unlocked": [], "expired": []}  # 已核销的不能解锁
    assert status(db, c.id) == "used"
    assert "coupon.redeemed" in topics(db)


def test_unlock_returns_valid_coupon(db):
    c = give(db)
    service.lock(db, 7, 100, [c.id], LINES, now=NOW)
    assert service.unlock(db, 100, now=NOW + timedelta(minutes=15)) == {"unlocked": [c.id], "expired": []}
    db.commit()
    row = db.get(UserCoupon, c.id)
    assert row.status == "unused" and row.order_id is None and row.discount_cents == 0
    assert service.unlock(db, 100, now=NOW) == {"unlocked": [], "expired": []}
    assert "coupon.unlocked" in topics(db)


def test_unlock_after_expiry_marks_expired_not_available(db):
    """002Gym 缺陷：订单超时关闭时券已过期，却被放回“可用”。"""
    c = give(db, valid_days=1)
    service.lock(db, 7, 100, [c.id], LINES, now=NOW + timedelta(hours=23, minutes=50))
    result = service.unlock(db, 100, now=NOW + timedelta(days=1, minutes=5))
    db.commit()
    assert result == {"unlocked": [], "expired": [c.id]}
    assert status(db, c.id) == "expired"
    assert service.available_coupons(db, 7, LINES, now=NOW)["usable"] == []


def test_redeem_after_lock_even_if_expired_at_payment_time(db):
    c = give(db, valid_days=1)
    service.lock(db, 7, 100, [c.id], LINES, now=NOW + timedelta(hours=23, minutes=55))
    assert service.redeem(db, 100, now=NOW + timedelta(days=1, minutes=2)) == [c.id]


# ---------------------------------------------------------------- 退款退券


def _used(db, **kw):
    c = give(db, **kw)
    service.lock(db, 7, 100, [c.id], LINES, now=NOW)
    service.redeem(db, 100, now=NOW)
    db.commit()
    return c


def test_full_refund_returns_valid_coupon(db):
    c = _used(db)
    result = service.return_on_refund(db, 100, full_refund=True, now=NOW + timedelta(days=1))
    db.commit()
    assert result["returned"] == [c.id]
    row = db.get(UserCoupon, c.id)
    assert row.status == "unused" and row.order_id is None and row.returned_at is not None
    assert db.get(CouponTemplate, c.template_id).used_count == 0
    assert service.return_on_refund(db, 100, full_refund=True, now=NOW)["returned"] == []  # 幂等
    assert "coupon.returned" in topics(db)


def test_partial_refund_keeps_coupon_by_default(db):
    c = _used(db)
    result = service.return_on_refund(db, 100, full_refund=False, now=NOW)
    assert result["kept"] == [{"user_coupon_id": c.id, "reason": "partial_refund"}]
    assert status(db, c.id) == "used"
    any_policy = service.return_on_refund(db, 100, full_refund=False, now=NOW, policy="any_refund")
    assert any_policy["returned"] == [c.id]


def test_never_policy_and_return_window(db):
    c = _used(db)
    coupon.set_config(return_policy="never")
    assert service.return_on_refund(db, 100, True, now=NOW)["kept"][0]["reason"] == "policy"
    coupon.set_config(return_policy="full_refund", return_window_hours=48)
    assert service.return_on_refund(db, 100, True, now=NOW + timedelta(hours=49))["kept"][0]["reason"] == \
        "return_window_passed"
    assert service.return_on_refund(db, 100, True, now=NOW + timedelta(hours=47))["returned"] == [c.id]


def test_refund_after_validity_marks_expired(db):
    c = _used(db, valid_days=1)
    result = service.return_on_refund(db, 100, True, now=NOW + timedelta(days=2))
    assert result == {"returned": [], "expired": [c.id], "kept": []}
    assert status(db, c.id) == "expired"


# ---------------------------------------------------------------- 收回与作废


def test_revoke_by_source_and_admin_revoke(db):
    t = make_template(db, per_member_limit=0)
    a = service.issue(db, t.id, 7, "gift:order:55:1", source_ref="order:55", now=NOW)
    b = service.issue(db, t.id, 7, "gift:order:55:2", source_ref="order:55", now=NOW)
    service.lock(db, 7, 900, [b.id], LINES, now=NOW)
    result = service.revoke_by_source(db, "order:55", "source order refunded", now=NOW)
    assert result["revoked"] == [a.id] and result["kept"] == [{"user_coupon_id": b.id, "status": "locked"}]
    assert service.revoke_by_source(db, "order:none", "x") == {"revoked": [], "kept": []}
    c = service.issue(db, t.id, 7, "manual:9", channel="admin", now=NOW)
    service.revoke(db, 1, c.id, "issued by mistake", now=NOW)
    assert status(db, c.id) == "revoked"
    assert err(lambda: service.revoke(db, 1, c.id, "again")).code == ErrorCode.CONFLICT  # 终态不可变
    assert err(lambda: service.revoke(db, 1, 12345, "missing")).code == ErrorCode.NOT_FOUND
    assert err(lambda: service.revoke(db, 1, b.id, "x")).code == ErrorCode.BAD_REQUEST


# ---------------------------------------------------------------- 并发


def test_one_coupon_cannot_be_locked_by_two_orders(file_factory):
    with file_factory() as s:
        c = give(s)

    def use(order_id):
        for _ in range(50):
            session = file_factory()
            try:
                service.lock(session, 7, order_id, [c.id], LINES, now=NOW)
                session.commit()
                return "ok"
            except AppError as exc:
                session.rollback()
                return exc.code.name
            except OperationalError:
                session.rollback()
            finally:
                session.close()
        return "gave_up"

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(use, range(100, 120)))
    assert results.count("ok") == 1
    assert set(results) <= {"ok", "CONFLICT", "BUSINESS_RULE"}


def test_payment_and_close_race_has_one_winner(file_factory):
    """支付回调（redeem）与超时关单（unlock）并发：结果只能是 used 或 unused，二者不会同时生效。"""
    for round_no in range(10):
        with file_factory() as s:
            c = give(s, user_id=100 + round_no)
            service.lock(s, 100 + round_no, 500 + round_no, [c.id], LINES, now=NOW)
            s.commit()
        barrier = threading.Barrier(2)
        outcome = {}

        def run(name, fn):
            barrier.wait()
            for _ in range(50):
                session = file_factory()
                try:
                    outcome[name] = fn(session)
                    session.commit()
                    return
                except OperationalError:
                    session.rollback()
                finally:
                    session.close()

        order_id = 500 + round_no
        threads = [threading.Thread(target=run, args=("redeem", lambda s: service.redeem(s, order_id, now=NOW))),
                   threading.Thread(target=run, args=("unlock", lambda s: service.unlock(s, order_id, now=NOW)))]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        with file_factory() as s:
            final = s.get(UserCoupon, c.id)
            used_count = s.get(CouponTemplate, c.template_id).used_count
        redeemed = outcome["redeem"] == [c.id]
        unlocked = outcome["unlock"]["unlocked"] == [c.id]
        assert redeemed != unlocked
        assert final.status == ("used" if redeemed else "unused")
        assert used_count == (1 if redeemed else 0)
