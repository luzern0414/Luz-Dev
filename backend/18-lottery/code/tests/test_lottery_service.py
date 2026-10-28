"""用例：发布与冻结、5 万封顶、次数、抽奖、幂等、退化、发奖与重试、实物、事件、公示、保留期、任务。"""
import json
from datetime import timedelta

import pytest
from sqlalchemy import func, select

import lottery
from lottery import hooks, jobs, service
from lottery.config import set_config
from lottery.hooks import FixedRandom, RiskContext
from lottery.models import (
    LotteryActivity, LotteryAmendment, LotteryChanceAccount, LotteryChanceGrant, LotteryDraw, LotteryPrize,
)
from lottery.service import StaffActor
from lottery_testkit import ADMIN, HIT, NOW, activity_data, key, make_activity, prize_id, prizes, setup_hooks
from luzback import AppError, ErrorCode, OutboxEvent
from luzback.audit import AuditLog
from luzback.testing import memory_session


@pytest.fixture()
def db():
    setup_hooks()
    session = memory_session(lottery)
    yield session
    session.close()
    setup_hooks()


def events(db, topic):
    rows = db.execute(select(OutboxEvent).where(OutboxEvent.topic == topic).order_by(OutboxEvent.id)).scalars().all()
    return [json.loads(e.payload_json) for e in rows]


def draw(db, act, member=100, k=None, at=NOW, **kw):
    return service.draw(db, act["id"], member, k or key("draw"), now=at, **kw)


def code_of(fn, *a, **kw):
    with pytest.raises(AppError) as exc:
        fn(*a, **kw)
    return exc.value.code


# ------------------------------------------------------------------ 活动与发布

def test_publish_freezes_odds_and_hash(db):
    act = make_activity(db)
    assert act["status"] == "published" and len(act["rules_hash"]) == 64
    row = db.get(LotteryActivity, act["id"])
    odds = json.loads(row.odds_json)
    assert [o["weight_bp"] for o in odds] == [1000, 2000, 100, 900, 6000]
    assert code_of(service.publish, db, ADMIN, act["id"], key(), now=NOW) == ErrorCode.CONFLICT  # 不可重复发布
    assert db.execute(select(func.count()).select_from(AuditLog).where(
        AuditLog.action == "lottery.activity.publish")).scalar_one() == 1
    assert events(db, "lottery.activity_published")[0]["activity_id"] == act["id"]


def test_publish_validates_pool_and_rules_text(db):
    bad = prizes()
    bad[4]["weight_bp"] = 5000  # 合计 9000
    act = make_activity(db, publish=False, prize_list=bad)
    assert code_of(service.publish, db, ADMIN, act["id"], key(), now=NOW) == ErrorCode.BUSINESS_RULE
    act2 = make_activity(db, publish=False, rules_text="")
    assert code_of(service.publish, db, ADMIN, act2["id"], key(), now=NOW) == ErrorCode.BUSINESS_RULE
    act3 = make_activity(db, publish=False, end_at=NOW - timedelta(minutes=1), start_at=NOW - timedelta(hours=2))
    assert code_of(service.publish, db, ADMIN, act3["id"], key(), now=NOW) == ErrorCode.BUSINESS_RULE


def test_prize_cap_50000_yuan(db):
    over = prizes()
    over[2]["value_cents"] = 5_000_001
    assert code_of(service.create_activity, db, ADMIN, activity_data(budget_cents=20_000_000), key(), over) \
        == ErrorCode.BUSINESS_RULE
    at_cap = prizes()
    at_cap[2]["value_cents"] = 5_000_000
    act = make_activity(db, prize_list=at_cap, budget_cents=20_000_000)
    assert act["status"] == "published"


def test_cap_is_configurable_downwards_only(db):
    set_config(prize_value_cap_cents=50_000)
    assert code_of(make_activity, db) == ErrorCode.BUSINESS_RULE  # 80000 > 500 元项目上限
    with pytest.raises(ValueError):
        set_config(prize_value_cap_cents=6_000_000)


def test_published_rules_are_immutable(db):
    act = make_activity(db)
    aid = act["id"]
    assert code_of(service.set_prizes, db, ADMIN, aid, prizes(), key()) == ErrorCode.CONFLICT
    for change in ({"name": "新名字"}, {"rules_text": "改规则"}, {"end_at": NOW + timedelta(days=1)},
                   {"limit_daily": 1}, {"out_of_stock_policy": "exclude_disclosed"}, {"budget_cents": 500_000},
                   {"start_at": NOW - timedelta(days=1)}, {"free_daily_chances": 0}):
        assert code_of(service.update_activity, db, ADMIN, aid, change, key(), now=NOW) == ErrorCode.CONFLICT, change
    row = db.get(LotteryActivity, aid, populate_existing=True)
    assert row.name == "开业大转盘" and row.free_daily_chances == 1


def test_favourable_amendments_allowed_and_disclosed(db):
    act = make_activity(db)
    aid = act["id"]
    old_hash = act["rules_hash"]
    out = service.update_activity(db, ADMIN, aid, {"end_at": NOW + timedelta(days=10), "free_daily_chances": 2},
                                  key(), now=NOW)
    assert out["free_daily_chances"] == 2 and out["rules_hash"] != old_hash
    assert {a["field"] for a in out["amendments"]} == {"end_at", "free_daily_chances"}
    detail = service.activity_detail(db, aid)
    assert len(detail["amendments"]) == 2 and "disclosure" in detail
    stocked = service.add_prize_stock(db, ADMIN, aid, prize_id(act, 1), 10, key(), now=NOW)
    p1 = next(p for p in stocked["prizes"] if p["seq"] == 1)
    assert p1["stock_total"] == 110 and p1["stock_left"] == 110
    # 追加库存不得超预算
    assert code_of(service.add_prize_stock, db, ADMIN, aid, prize_id(act, 3), 100, key(), now=NOW) \
        == ErrorCode.BUSINESS_RULE
    # 谢谢参与 / 不限量奖品没有库存
    assert code_of(service.add_prize_stock, db, ADMIN, aid, prize_id(act, 2), 1, key(), now=NOW) \
        == ErrorCode.BUSINESS_RULE


def test_draft_is_freely_editable_and_deletable(db):
    act = make_activity(db, publish=False)
    out = service.update_activity(db, ADMIN, act["id"], {"name": "改名", "limit_daily": 3}, key(), now=NOW)
    assert out["name"] == "改名" and out["amendments"] == []
    assert code_of(service.update_activity, db, ADMIN, act["id"], {"limit_daily": 1}, key(), expected_version=1,
                   now=NOW) == ErrorCode.CONFLICT  # 版本过期
    new = service.set_prizes(db, ADMIN, act["id"], prizes()[:2] + prizes()[4:], key())
    assert len(new["prizes"]) == 3
    assert service.delete_activity(db, ADMIN, act["id"], key(), now=NOW)["deleted"]
    assert code_of(service.get_activity_admin, db, ADMIN, act["id"]) == ErrorCode.NOT_FOUND
    pub = make_activity(db)
    assert code_of(service.delete_activity, db, ADMIN, pub["id"], key(), now=NOW) == ErrorCode.CONFLICT


def test_end_activity_rules(db):
    running = make_activity(db)
    assert code_of(service.end_activity, db, ADMIN, running["id"], "提前结束", key(), now=NOW) == ErrorCode.BUSINESS_RULE
    allowed = make_activity(db, early_end_allowed=True)
    assert service.end_activity(db, ADMIN, allowed["id"], "奖品发完", key(), now=NOW)["status"] == "ended"
    future = make_activity(db, start_at=NOW + timedelta(days=1), end_at=NOW + timedelta(days=2))
    assert service.end_activity(db, ADMIN, future["id"], "撤下", key(), now=NOW)["status"] == "ended"
    # 终态不可变
    assert code_of(service.update_activity, db, ADMIN, future["id"], {"limit_total": 0}, key(), now=NOW) \
        == ErrorCode.CONFLICT
    assert code_of(service.end_activity, db, ADMIN, future["id"], "again", key(), now=NOW) == ErrorCode.CONFLICT
    assert code_of(draw, db, allowed) == ErrorCode.BUSINESS_RULE
    assert len(events(db, "lottery.activity_ended")) == 2


def test_store_scope(db):
    act = make_activity(db, store_id=1)
    other = StaffActor(id=2, store_scope=[2])
    assert code_of(service.get_activity_admin, db, other, act["id"]) == ErrorCode.SCOPE_FORBIDDEN
    assert service.list_activities(db, other, {})["total"] == 0
    assert service.list_activities(db, StaffActor(id=3, store_scope=[1]), {})["total"] == 1
    assert code_of(service.create_activity, db, other, activity_data(store_id=1), key(), prizes()) \
        == ErrorCode.SCOPE_FORBIDDEN


# ------------------------------------------------------------------ 抽奖

def test_draw_records_audit_fields_and_uses_secrets_by_default(db):
    act = make_activity(db)
    rec = draw(db, act)
    row = db.get(LotteryDraw, rec["id"])
    assert row.rng == "secrets" and 0 <= row.random_value < 10000 and row.random_range == 10000
    assert row.rules_hash == act["rules_hash"] and row.chance_source == "free"
    assert service.verify_draw(row)
    row.random_value = (row.random_value + 5000) % 10000  # 篡改后无法复现（大概率命中不同区间）
    if service.rules.pick(json.loads(row.odds_json), row.random_value)[0]["prize_id"] != row.hit_prize_id:
        assert not service.verify_draw(row)
    assert events(db, "lottery.drawn")[0]["draw_id"] == rec["id"]


def test_injected_rng_determines_prize(db):
    act = make_activity(db)
    for seq, member in ((1, 1), (2, 2), (4, 3), (5, 4)):
        hooks.set_random_source(FixedRandom(HIT[seq]))
        rec = draw(db, act, member=member)
        assert rec["prize"]["seq"] == seq and rec["won"] is (seq != 5)


def test_idempotent_draw_replays_same_result(db):
    act = make_activity(db)
    k = key("same")
    first = draw(db, act, k=k)
    second = draw(db, act, k=k)
    assert first == second
    assert db.execute(select(func.count()).select_from(LotteryDraw)).scalar_one() == 1
    assert service.my_chances(db, act["id"], 100, now=NOW)["free_left_today"] == 0
    with pytest.raises(AppError) as exc:
        service.draw(db, act["id"], 100, k, store_id=9, now=NOW)  # 同键不同请求体
    assert exc.value.code == ErrorCode.IDEMPOTENCY_MISMATCH
    assert code_of(service.draw, db, act["id"], 100, None, now=NOW) == ErrorCode.BAD_REQUEST


def test_chances_free_daily_then_earned_then_limits(db):
    act = make_activity(db, limit_daily=3, limit_monthly=5, limit_total=6)
    aid = act["id"]
    draw(db, act)
    err = code_of(draw, db, act)
    assert err == ErrorCode.LIMIT_REACHED
    service.grant_chances(db, aid, 100, 10, "event", "promo-1")
    assert service.grant_chances(db, aid, 100, 10, "event", "promo-1")["created"] is False  # 同来源只发一次
    draw(db, act)
    draw(db, act)
    assert code_of(draw, db, act) == ErrorCode.LIMIT_REACHED  # 每日 3 次
    c = service.my_chances(db, aid, 100, now=NOW)
    assert c["daily_left"] == 0 and c["earned_left"] == 8 and c["available"] == 0
    tomorrow = NOW + timedelta(days=1)
    draw(db, act, at=tomorrow)   # 免费
    draw(db, act, at=tomorrow)   # 获得
    assert code_of(draw, db, act, at=tomorrow) == ErrorCode.LIMIT_REACHED  # 每月 5 次
    acc = db.execute(select(LotteryChanceAccount).where(LotteryChanceAccount.member_id == 100)).scalar_one()
    db.refresh(acc)
    assert acc.used == 3 and acc.draws == 5


def test_failed_draw_rolls_back_chance(db):
    act = make_activity(db, free_daily_chances=0)
    service.grant_chances(db, act["id"], 100, 1, "event", "x")
    hooks.set_risk_checker(lambda db_, ctx: (_ for _ in ()).throw(AppError(ErrorCode.BUSINESS_RULE, "risk")))
    assert code_of(draw, db, act) == ErrorCode.BUSINESS_RULE
    hooks.set_risk_checker(None)
    assert service.my_chances(db, act["id"], 100, now=NOW)["earned_left"] == 1
    draw(db, act)
    assert service.my_chances(db, act["id"], 100, now=NOW)["earned_left"] == 0


def test_window_store_audience_and_rate_limit(db):
    act = make_activity(db, store_id=5)
    assert code_of(draw, db, act, at=NOW - timedelta(hours=2)) == ErrorCode.BUSINESS_RULE  # 未开始
    assert code_of(draw, db, act, at=NOW + timedelta(days=8)) == ErrorCode.BUSINESS_RULE   # 已结束
    assert code_of(draw, db, act, store_id=6) == ErrorCode.BUSINESS_RULE                   # 门店不符
    hooks.set_audience_checker(lambda db_, a, m: m != 13)
    assert code_of(draw, db, act, member=13) == ErrorCode.BUSINESS_RULE
    seen: list[RiskContext] = []
    hooks.set_risk_checker(lambda db_, ctx: seen.append(ctx))
    draw(db, act, member=14, ip="1.2.3.4")
    assert seen[0].ip == "1.2.3.4" and seen[0].member_id == 14
    setup_hooks(rate_limits=True)
    set_config(member_draws_per_minute=1)
    hooks.set_limiters(None, None)
    service.grant_chances(db, act["id"], 15, 5, "event", "r")
    draw(db, act, member=15)
    assert code_of(draw, db, act, member=15) == ErrorCode.RATE_LIMITED
    assert service.my_chances(db, act["id"], 15, now=NOW)["earned_left"] == 5  # 被限流的请求不扣次数


def test_lazy_start_and_job_transitions(db):
    act = make_activity(db, start_at=NOW + timedelta(minutes=5), end_at=NOW + timedelta(days=1))
    assert act["status"] == "published"
    draw(db, act, at=NOW + timedelta(minutes=6))
    assert db.get(LotteryActivity, act["id"], populate_existing=True).status == "running"
    other = make_activity(db, start_at=NOW + timedelta(minutes=5), end_at=NOW + timedelta(days=1))
    assert jobs.advance_activities(db, NOW + timedelta(minutes=10)) == "started=1 ended=0"
    assert jobs.advance_activities(db, NOW + timedelta(days=2)) == "started=0 ended=2"
    assert jobs.advance_activities(db, NOW + timedelta(days=2)) == "started=0 ended=0"  # 幂等
    assert db.get(LotteryActivity, other["id"], populate_existing=True).status == "ended"


def test_out_of_stock_falls_to_thanks_without_redistribution(db):
    act = make_activity(db)
    hooks.set_random_source(FixedRandom(HIT[3]))  # 实物，库存 2
    results = [draw(db, act, member=m) for m in (1, 2, 3)]
    assert [r["won"] for r in results] == [True, True, False]
    third = db.get(LotteryDraw, results[2]["id"])
    assert third.fallback_reason == "out_of_stock" and third.hit_prize_id == prize_id(act, 3)
    assert third.prize_kind == "thanks" and third.random_range == 10000  # 奖池未改变、不重抽
    prize = db.get(LotteryPrize, prize_id(act, 3), populate_existing=True)
    assert prize.stock_left == 0 and prize.win_count == 2
    act_row = db.get(LotteryActivity, act["id"], populate_existing=True)
    assert act_row.spent_cents == 160_000  # 退化的那次回退了预算


def test_exclude_disclosed_policy_removes_empty_prize_from_pool(db):
    act = make_activity(db, out_of_stock_policy="exclude_disclosed")
    hooks.set_random_source(FixedRandom(HIT[3]))
    draw(db, act, member=1)
    draw(db, act, member=2)
    rec = draw(db, act, member=3)
    row = db.get(LotteryDraw, rec["id"])
    assert row.random_range == 9900 and row.fallback_reason is None
    assert prize_id(act, 3) not in [o["prize_id"] for o in json.loads(row.odds_json)]
    assert service.verify_draw(row)
    assert "移出奖池" in service.activity_detail(db, act["id"])["disclosure"]["out_of_stock"]


def test_member_win_limit_and_budget_fallback(db):
    plist = prizes()
    plist[0]["per_member_limit"] = 1
    act = make_activity(db, prize_list=plist, free_daily_chances=3)
    hooks.set_random_source(FixedRandom(HIT[1]))
    assert draw(db, act)["won"]
    second = draw(db, act)
    assert not second["won"] and second["fallback_reason"] == "member_limit"
    # 预算：不限量积分奖在预算用尽后退化
    plist2 = prizes()
    act2 = make_activity(db, prize_list=plist2, budget_cents=410_100)  # 有限奖品 410000 + 1 次积分 100
    hooks.set_random_source(FixedRandom(HIT[2]))
    assert draw(db, act2, member=1)["won"]
    r = draw(db, act2, member=2)
    assert not r["won"] and r["fallback_reason"] == "budget"


# ------------------------------------------------------------------ 发奖

def test_virtual_prize_event_contract_ack_and_retry(db):
    set_config(member_draws_per_minute=0, ip_draws_per_minute=0, delivery_max_attempts=3)
    act = make_activity(db)
    hooks.set_random_source(FixedRandom(HIT[1]))
    rec = draw(db, act)
    assert rec["delivery_status"] == "pending"
    payload = events(db, "lottery.prize_won")[0]
    assert payload == {
        "draw_id": rec["id"], "activity_id": act["id"], "member_id": 100, "store_id": None,
        "prize_id": prize_id(act, 1), "prize_kind": "coupon", "prize_name": "满50减10券", "ref_id": 7, "quantity": 1,
        "value_cents": 1000, "delivery_key": f"lottery:draw:{rec['id']}", "attempt": 1, "won_at": NOW.isoformat(),
    }
    # 未回执 → 到期重发（attempt 递增），超过上限 → failed
    assert jobs.retry_deliveries(db, NOW + timedelta(minutes=5)) == "resent=0 failed=0"
    assert jobs.retry_deliveries(db, NOW + timedelta(minutes=11)) == "resent=1 failed=0"
    assert events(db, "lottery.prize_won")[-1]["attempt"] == 2
    assert jobs.retry_deliveries(db, NOW + timedelta(hours=1)) == "resent=1 failed=0"
    assert jobs.retry_deliveries(db, NOW + timedelta(days=1)) == "resent=0 failed=1"
    assert db.get(LotteryDraw, rec["id"], populate_existing=True).delivery_status == "failed"
    assert len(events(db, "lottery.prize_delivery_failed")) == 1
    # 人工重试 → pending → 消费方回执成功 → delivered；重复回执幂等
    assert service.retry_delivery(db, ADMIN, rec["id"], key(), now=NOW)["delivery_status"] == "pending"
    assert service.handle_event(db, "lottery.delivery_result", {"draw_id": rec["id"], "ok": True, "ref": "uc-1"},
                                event_id="e-ack-1") == "delivered"
    assert service.handle_event(db, "lottery.delivery_result", {"draw_id": rec["id"], "ok": True},
                                event_id="e-ack-1") == "duplicate"
    assert service.ack_delivery(db, rec["id"], True) == "duplicate"
    row = db.get(LotteryDraw, rec["id"], populate_existing=True)
    assert row.delivery_status == "delivered" and row.delivery_ref == "uc-1"
    assert code_of(service.retry_delivery, db, ADMIN, rec["id"], key(), now=NOW) == ErrorCode.CONFLICT


def test_failed_ack_schedules_retry(db):
    act = make_activity(db)
    hooks.set_random_source(FixedRandom(HIT[2]))
    rec = draw(db, act)
    assert service.ack_delivery(db, rec["id"], False, error="member frozen", now=NOW) == "retry_scheduled"
    db.commit()
    assert jobs.retry_deliveries(db, NOW + timedelta(seconds=1)) == "resent=1 failed=0"
    assert db.get(LotteryDraw, rec["id"], populate_existing=True).delivery_error == "member frozen"


def test_physical_claim_ship_receive_and_expiry(db):
    act = make_activity(db)
    hooks.set_random_source(FixedRandom(HIT[3]))
    rec = draw(db, act, member=1)
    assert rec["delivery_status"] == "awaiting_claim"
    assert code_of(service.ship_prize, db, ADMIN, rec["id"], "SF", "SF123456", key(), now=NOW) == ErrorCode.CONFLICT
    assert code_of(service.claim_prize, db, 2, rec["id"], "张三", "13800138000", "上海市浦东新区某路 1 号", key(),
                   now=NOW) == ErrorCode.NOT_FOUND  # 他人记录
    assert code_of(service.claim_prize, db, 1, rec["id"], "张三", "123", "上海市浦东新区某路 1 号", key(),
                   now=NOW) == ErrorCode.BAD_REQUEST
    claimed = service.claim_prize(db, 1, rec["id"], "张三", "13800138000", "上海市浦东新区某路 1 号", key(), now=NOW)
    assert claimed["delivery_status"] == "claimed"
    row = db.get(LotteryDraw, rec["id"], populate_existing=True)
    assert "13800138000" not in (row.claim_phone_enc or "")
    admin_view = service.get_record_admin(db, ADMIN, rec["id"])
    assert admin_view["claim"]["phone"] == "138****8000"
    contact = service.reveal_claim_contact(db, ADMIN, rec["id"])
    assert contact["phone"] == "13800138000"
    assert db.execute(select(func.count()).select_from(AuditLog).where(
        AuditLog.action == "lottery_draw.sensitive_read")).scalar_one() == 1
    shipped = service.ship_prize(db, ADMIN, rec["id"], "SF", "SF123456", key(), now=NOW)
    assert shipped["delivery_status"] == "shipped" and shipped["tracking_no"] == "SF123456"
    assert code_of(service.claim_prize, db, 1, rec["id"], "李四", "13800138001", "北京市朝阳区某路 2 号", key(),
                   now=NOW) == ErrorCode.CONFLICT  # 发货后不能改
    assert service.confirm_received(db, ADMIN, rec["id"], key(), now=NOW)["delivery_status"] == "delivered"
    # 逾期未领
    rec2 = draw(db, act, member=2)
    assert jobs.expire_unclaimed(db, NOW + timedelta(days=6)) == "expired=0"
    assert jobs.expire_unclaimed(db, NOW + timedelta(days=8)) == "expired=1"
    assert code_of(service.claim_prize, db, 2, rec2["id"], "王五", "13800138002", "广州市天河区某路 3 号", key(),
                   now=NOW + timedelta(days=8)) == ErrorCode.CONFLICT


# ------------------------------------------------------------------ 事件、公示、统计、保留

def test_payment_and_checkin_events_grant_chances_once(db):
    act = make_activity(db, earn_payment_min_cents=5000, earn_payment_chances=2, earn_checkin_chances=1)
    aid = act["id"]
    pay = {"payment_id": 9001, "user_id": 100, "amount_cents": 6000, "business_type": "order"}
    assert service.handle_event(db, "payment.succeeded", pay, "ev-1", now=NOW) == "granted:1"
    assert service.handle_event(db, "payment.succeeded", pay, "ev-1", now=NOW) == "duplicate"
    assert service.handle_event(db, "payment.succeeded", pay, "ev-2", now=NOW) == "granted:0"  # 同一笔支付
    assert service.handle_event(db, "payment.succeeded", dict(pay, payment_id=9002, amount_cents=4999), "ev-3",
                                now=NOW) == "granted:0"
    assert service.handle_event(db, "payment.succeeded", dict(pay, payment_id=9003, business_type="recharge"),
                                "ev-4", now=NOW) == "skipped"
    assert service.handle_event(db, "member.checked_in", {"user_id": 100, "checkin_id": 77}, "ev-5",
                                now=NOW) == "granted:1"
    assert service.my_chances(db, aid, 100, now=NOW)["earned_left"] == 3
    draw(db, act)  # 免费
    draw(db, act)  # 获得 1
    assert service.handle_event(db, "payment.refunded", {"payment_id": 9001}, "ev-6", now=NOW) == "revoked:1"
    acc = db.execute(select(LotteryChanceAccount).where(LotteryChanceAccount.member_id == 100)).scalar_one()
    db.refresh(acc)
    assert acc.granted - acc.used == 0  # 退款收回未用的 2 次中的余量，已用的不追溯
    assert db.execute(select(LotteryChanceGrant.status).where(LotteryChanceGrant.source == "payment")).scalar_one() \
        == "revoked"
    assert service.handle_event(db, "unknown.topic", {}, "ev-7") == "skipped"


def test_admin_grant_is_idempotent_and_audited(db):
    act = make_activity(db)
    k = key("grant")
    g1 = service.admin_grant(db, ADMIN, act["id"], 100, 3, "客诉补偿", k)
    g2 = service.admin_grant(db, ADMIN, act["id"], 100, 3, "客诉补偿", k)
    assert g1 == g2 and g1["source"] == "admin"
    assert service.my_chances(db, act["id"], 100, now=NOW)["earned_left"] == 3
    assert code_of(service.admin_grant, db, ADMIN, act["id"], 100, 1000, "x", key()) == ErrorCode.BAD_REQUEST
    assert service.list_grants(db, ADMIN, {"activity_id": str(act["id"])})["total"] == 1
    draft = make_activity(db, publish=False)
    assert code_of(service.admin_grant, db, ADMIN, draft["id"], 100, 1, "x", key()) == ErrorCode.CONFLICT


def test_winners_publicity_masks_and_filters(db):
    act = make_activity(db)
    hooks.set_random_source(FixedRandom(HIT[3]))
    draw(db, act, member=123456)
    hooks.set_random_source(FixedRandom(HIT[1]))
    draw(db, act, member=2)
    out = service.winners(db, act["id"], {}, "s" * 32)
    assert len(out["items"]) == 1
    item = out["items"][0]
    assert item["member"] == "用户****3456" and item["prize_name"] == "空气炸锅"
    assert item["delivery_status"] == "awaiting_claim"
    hooks.set_member_display(lambda db_, m: "张三丰")
    assert service.winners(db, act["id"], {}, "s" * 32)["items"][0]["member"] == "张**"


def test_records_list_export_and_stats(db):
    act = make_activity(db)
    hooks.set_random_source(FixedRandom(HIT[1]))
    draw(db, act, member=1)
    hooks.set_random_source(FixedRandom(HIT[5]))
    draw(db, act, member=2)
    page = service.list_records(db, ADMIN, {"activity_id": str(act["id"]), "won": "true"})
    assert page["total"] == 1 and page["items"][0]["member_id"] == 1
    assert service.list_records(db, ADMIN, {"keyword": "满50"})["total"] == 1
    assert service.list_records(db, ADMIN, {"delivery_status": "pending,none", "sort": "-value_cents"})["total"] == 2
    assert code_of(service.list_records, db, ADMIN, {"bogus": "1"}) == ErrorCode.BAD_REQUEST
    csv = b"".join(service.export_records(db, ADMIN, {"activity_id": str(act["id"])})).decode("utf-8-sig")
    assert csv.splitlines()[0].startswith("记录ID") and len(csv.splitlines()) == 3 and "10.00" in csv
    s = service.stats(db, ADMIN, act["id"])
    assert s["draws"] == 2 and s["wins"] == 1 and s["members"] == 2 and s["cost_cents"] == 1000
    assert s["win_rate_bp"] == 5000 and s["spent_cents"] == 1000
    sim = service.simulate(db, ADMIN, act["id"], 100_000, seed=7)
    assert abs(sim["rows"][4]["observed_bp"] - 6000) < 100
    assert sim["expected_cost_per_draw_cents"] == pytest.approx((1000 * 1000 + 100 * 2000 + 80000 * 100 + 3000 * 900)
                                                                / 10000)


def test_my_records_are_private(db):
    act = make_activity(db)
    rec = draw(db, act, member=1)
    assert service.my_record(db, 1, rec["id"])["id"] == rec["id"]
    assert code_of(service.my_record, db, 2, rec["id"]) == ErrorCode.NOT_FOUND
    assert len(service.my_records(db, 1, {}, "s" * 32)["items"]) == 1
    assert service.my_records(db, 2, {}, "s" * 32)["items"] == []


def test_draft_activity_is_not_public(db):
    act = make_activity(db, publish=False)
    assert code_of(service.activity_detail, db, act["id"]) == ErrorCode.NOT_FOUND
    assert code_of(draw, db, act) == ErrorCode.NOT_FOUND


def test_retention_never_purges_before_two_years(db):
    act = make_activity(db, early_end_allowed=True)
    draw(db, act)
    service.end_activity(db, ADMIN, act["id"], "结束", key(), now=NOW)
    assert jobs.purge_expired(db, NOW + timedelta(days=729)) == "purged=0"
    assert db.execute(select(func.count()).select_from(LotteryDraw)).scalar_one() == 1
    assert jobs.purge_expired(db, NOW + timedelta(days=731)) == "purged=1"
    assert db.execute(select(func.count()).select_from(LotteryDraw)).scalar_one() == 0
    assert db.get(LotteryActivity, act["id"], populate_existing=True).purged_at is not None  # 规则与奖池保留
    assert jobs.purge_expired(db, NOW + timedelta(days=800)) == "purged=0"


def test_amendment_rows_and_jobs_registry(db):
    act = make_activity(db)
    service.update_activity(db, ADMIN, act["id"], {"limit_total": 0, "claim_days": 14}, key(), now=NOW)
    assert db.execute(select(func.count()).select_from(LotteryAmendment)).scalar_one() == 1  # limit_total 本就为 0

    class Reg:
        def __init__(self):
            self.jobs = []

        def add(self, job):
            self.jobs.append(job.name)

    reg = Reg()
    jobs.register_jobs(reg)
    assert reg.jobs == [name for name, *_ in jobs.JOBS]
