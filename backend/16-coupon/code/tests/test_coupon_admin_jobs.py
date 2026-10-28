"""模板状态机与发布后编辑、兑换码（一次性、防枚举）、批量发放任务、事件发券、过期与提醒任务、统计。"""
import json
import sys
from types import SimpleNamespace
from datetime import timedelta

import pytest
from sqlalchemy import func, select

import coupon
from coupon.events import SUBSCRIPTIONS, handle_event, make_consumer
from coupon import (
    CouponCode, CouponIssueBatch, CouponIssueBatchItem, CouponTemplate, MemberProfile, UserCoupon, jobs, service,
)
from luzback import AppError, AuditLog, ErrorCode, OutboxEvent, dispatch_pending, enqueue_event
from luzback.db import get_session_factory
from luzback.scheduler import Job, JobRegistry

from coupon_fixtures import *  # noqa: F401,F403
from coupon_fixtures import NOW, make_template, template_data


def err(fn):
    with pytest.raises(AppError) as error:
        fn()
    return error.value


def events(db, topic):
    return [json.loads(e.payload_json) for e in db.execute(select(OutboxEvent).where(OutboxEvent.topic == topic)
                                                           .order_by(OutboxEvent.id)).scalars()]


# ---------------------------------------------------------------- 模板状态机


def test_template_lifecycle_and_illegal_transitions(db):
    t = make_template(db, publish=False)
    assert t.status == "draft"
    assert err(lambda: service.pause_template(db, 1, t.id, now=NOW)).code == ErrorCode.CONFLICT
    assert err(lambda: service.resume_template(db, 1, t.id, now=NOW)).code == ErrorCode.CONFLICT
    service.publish_template(db, 1, t.id, now=NOW)
    service.publish_template(db, 1, t.id, now=NOW)  # 重复发布幂等
    service.pause_template(db, 1, t.id, now=NOW)
    service.resume_template(db, 1, t.id, now=NOW)
    service.end_template(db, 1, t.id, now=NOW)
    db.commit()
    assert db.get(CouponTemplate, t.id).status == "ended"
    assert err(lambda: service.publish_template(db, 1, t.id, now=NOW)).code == ErrorCode.CONFLICT  # 终态
    assert err(lambda: service.update_template(db, 1, t.id, {"name": "x"})).code == ErrorCode.CONFLICT
    assert err(lambda: service.delete_template(db, 1, t.id)).code == ErrorCode.CONFLICT
    actions = db.execute(select(AuditLog.action).where(AuditLog.resource_id == str(t.id))).scalars().all()
    assert {"coupon.template.create", "coupon.template.publish", "coupon.template.end"} <= set(actions)


def test_publish_rejects_expired_config_and_delete_only_draft(db):
    t = make_template(db, publish=False, claim_end_at=NOW - timedelta(hours=1), now=NOW - timedelta(days=1))
    assert err(lambda: service.publish_template(db, 1, t.id, now=NOW)).code == ErrorCode.BUSINESS_RULE
    service.delete_template(db, 1, t.id, now=NOW)
    db.commit()
    assert err(lambda: service.get_template(db, t.id)).code == ErrorCode.NOT_FOUND


def test_draft_edits_freely_published_only_loosens(db):
    t = make_template(db, publish=False, total_stock=10)
    service.update_template(db, 1, t.id, {"type": "percent_off", "rate_bp": 9000, "total_stock": 5}, now=NOW)
    service.publish_template(db, 1, t.id, now=NOW)
    db.commit()
    for uid in (1, 2, 3):
        service.claim(db, uid, t.id, now=NOW)
    db.commit()
    assert err(lambda: service.update_template(db, 1, t.id, {"total_stock": 2})).code == ErrorCode.BUSINESS_RULE
    assert err(lambda: service.update_template(db, 1, t.id, {"type": "amount_off", "amount_cents": 1})).code == \
        ErrorCode.BUSINESS_RULE
    assert err(lambda: service.update_template(db, 1, t.id, {"per_member_limit": 0, "per_member_daily_limit": 1}))\
        .code == ErrorCode.BUSINESS_RULE  # 每日限领从不限收紧到 1
    version = db.get(CouponTemplate, t.id).version
    assert err(lambda: service.update_template(db, 1, t.id, {"name": "x"}, expected_version=version - 1)).code == \
        ErrorCode.CONFLICT
    service.update_template(db, 1, t.id, {"total_stock": 3}, expected_version=version, now=NOW)  # 等于已发数量可以
    assert err(lambda: service.update_template(db, 1, t.id, {"bogus": 1})).code == ErrorCode.BAD_REQUEST


def test_rule_changes_only_affect_new_claims(db):
    t = make_template(db, per_member_limit=0)
    old = service.claim(db, 1, t.id, now=NOW)
    db.commit()
    service.update_template(db, 1, t.id, {"amount_cents": 1000, "threshold_cents": 20000}, now=NOW)  # 对新领的人更差
    new = service.claim(db, 2, t.id, now=NOW)
    db.commit()
    assert json.loads(db.get(UserCoupon, old.id).rules_json)["amount_cents"] == 2000  # 已发出的券不受影响
    assert json.loads(new.rules_json)["amount_cents"] == 1000
    assert new.template_version == old.template_version + 1
    e = err(lambda: service.update_template(db, 1, t.id, {"amount_cents": 500}, apply_to_issued=True))
    assert e.code == ErrorCode.BUSINESS_RULE and "amount_cents" in e.data["fields"]


def test_favorable_change_can_apply_to_issued_unused(db):
    t = make_template(db, per_member_limit=0, valid_type="fixed", use_start_at=NOW, use_end_at=NOW + timedelta(days=5))
    a = service.claim(db, 1, t.id, now=NOW)
    used = service.claim(db, 2, t.id, now=NOW)
    service.lock(db, 2, 1, [used.id], [coupon.CartLine(1, 1, 1, 20000)], now=NOW)
    db.commit()
    service.update_template(db, 1, t.id, {"amount_cents": 3000, "use_end_at": NOW + timedelta(days=10)},
                            apply_to_issued=True, now=NOW)
    db.commit()
    row = db.get(UserCoupon, a.id)
    assert json.loads(row.rules_json)["amount_cents"] == 3000
    assert row.valid_to.replace(tzinfo=NOW.tzinfo) == NOW + timedelta(days=10)
    assert json.loads(db.get(UserCoupon, used.id).rules_json)["amount_cents"] == 2000  # 锁定中的不改（金额已算入订单）


# ---------------------------------------------------------------- 兑换码


def _codes(db, batch_id):
    return [row[0] for row in service.export_code_rows(db, 1, batch_id)]


def test_redeem_code_single_use(db):
    t = make_template(db, per_member_limit=0, channels=["code"])
    batch = service.create_code_batch(db, 1, t.id, 3, "双11", now=NOW)
    db.commit()
    stored = db.execute(select(CouponCode)).scalars().all()
    codes = _codes(db, batch.id)
    assert len(set(codes)) == 3 and all(len(c) == 12 for c in codes)
    assert all(c not in (s.code_hash + s.code_enc) for c, s in zip(codes, stored))  # 不存明文
    c = service.redeem_code(db, 7, codes[0].lower(), now=NOW)
    db.commit()
    assert c.channel == "code" and c.source_ref == f"code_batch:{batch.id}"
    assert isinstance(err(lambda: service.redeem_code(db, 8, codes[0], now=NOW)), service.InvalidCode)
    assert isinstance(err(lambda: service.redeem_code(db, 8, "NOTACODE99", now=NOW)), service.InvalidCode)
    assert isinstance(err(lambda: service.redeem_code(db, 8, "bad", now=NOW)), service.InvalidCode)
    db.rollback()
    assert service.void_code_batch(db, 1, batch.id) == 2
    db.commit()
    assert isinstance(err(lambda: service.redeem_code(db, 8, codes[1], now=NOW)), service.InvalidCode)
    assert db.execute(select(AuditLog).where(AuditLog.action == "coupon.codes.export")).first() is not None


def test_code_failure_after_issue_rule_keeps_code_unused(db):
    t = make_template(db, per_member_limit=1, channels=["code", "claim_center"])
    batch = service.create_code_batch(db, 1, t.id, 2, "", now=NOW)
    db.commit()
    codes = _codes(db, batch.id)
    service.claim(db, 7, t.id, now=NOW)
    db.commit()
    assert err(lambda: service.redeem_code(db, 7, codes[0], now=NOW)).code == ErrorCode.LIMIT_REACHED
    db.rollback()
    assert db.execute(select(func.count()).select_from(CouponCode).where(CouponCode.status == "unused")).scalar_one() == 2


def test_redeem_code_throttles_brute_force(db):
    coupon.set_config(code_max_failures=3, code_window_seconds=600)
    t = make_template(db, channels=["code"])
    batch = service.create_code_batch(db, 1, t.id, 1, "", now=NOW)
    db.commit()
    code = _codes(db, batch.id)[0]
    for _ in range(3):
        with pytest.raises(service.InvalidCode):
            service.redeem_code(db, 7, "WRONGCODE123", now=NOW)
        db.rollback()
        service.record_code_failure(db, 7, now=NOW)
        db.commit()
    e = err(lambda: service.redeem_code(db, 7, code, now=NOW))  # 正确的码也先被限流
    assert e.code == ErrorCode.RATE_LIMITED and 0 < e.retry_after <= 600
    service.redeem_code(db, 8, code, now=NOW)  # 其他会员不受影响
    db.rollback()
    service.redeem_code(db, 7, code, now=NOW + timedelta(minutes=11))  # 下一个窗口恢复


def test_code_batch_rules(db):
    no_code = make_template(db, channels=["claim_center"])
    assert err(lambda: service.create_code_batch(db, 1, no_code.id, 1, "")).code == ErrorCode.BUSINESS_RULE
    limited = make_template(db, channels=["code"], total_stock=5)
    service.create_code_batch(db, 1, limited.id, 4, "", now=NOW)
    assert err(lambda: service.create_code_batch(db, 1, limited.id, 2, "")).code == ErrorCode.LIMIT_REACHED
    assert err(lambda: service.create_code_batch(db, 1, limited.id, 0, "")).code == ErrorCode.BAD_REQUEST


# ---------------------------------------------------------------- 批量发放任务


def test_issue_batch_job_is_idempotent_per_member(db):
    t = make_template(db, per_member_limit=1, channels=["admin", "claim_center"])
    service.claim(db, 3, t.id, now=NOW)  # 会员 3 已领过 → 批次里会失败（受每人限领约束）
    batch = service.create_issue_batch(db, 1, t.id, [1, 2, 3, 2], "会员日定向发放", now=NOW)
    db.commit()
    assert batch.total == 3
    result = jobs.process_issue_batches(db, now=NOW, limit=2)
    assert result.startswith("issued=2")
    jobs.process_issue_batches(db, now=NOW)
    jobs.process_issue_batches(db, now=NOW)  # 重跑：不重复发
    db.expire_all()
    b = db.get(CouponIssueBatch, batch.id)
    assert (b.issued, b.failed, b.status) == (2, 1, "done")
    failed = db.execute(select(CouponIssueBatchItem).where(CouponIssueBatchItem.status == "failed")).scalar_one()
    assert failed.user_id == 3 and failed.error == "per_member_limit"
    assert db.execute(select(func.count()).select_from(UserCoupon).where(UserCoupon.channel == "admin")).scalar_one() == 2
    audit = db.execute(select(AuditLog).where(AuditLog.action == "coupon.issue_batch.create")).scalar_one()
    assert audit.reason == "会员日定向发放"


def test_compensation_batch_can_ignore_limit_and_checks_stock(db):
    t = make_template(db, per_member_limit=1, total_stock=3, channels=["compensation", "claim_center"])
    service.claim(db, 1, t.id, now=NOW)
    db.commit()
    assert err(lambda: service.create_issue_batch(db, 1, t.id, [1, 2, 3], "补偿", channel="compensation")).code == \
        ErrorCode.LIMIT_REACHED
    service.create_issue_batch(db, 1, t.id, [1, 2], "客诉补偿", channel="compensation", ignore_limit=True, now=NOW)
    db.commit()
    jobs.process_issue_batches(db, now=NOW)
    assert db.execute(select(func.count()).select_from(UserCoupon).where(UserCoupon.user_id == 1)).scalar_one() == 2
    assert err(lambda: service.create_issue_batch(db, 1, t.id, [5], "补偿", channel="admin")).code == \
        ErrorCode.BUSINESS_RULE  # 模板未开放 admin 渠道
    assert err(lambda: service.create_issue_batch(db, 1, t.id, [5], "x")).code == ErrorCode.BAD_REQUEST


# ---------------------------------------------------------------- 事件发券


class Evt:
    def __init__(self, event_id):
        self.event_id = event_id


def test_registration_event_issues_trigger_templates_once(db):
    gift = make_template(db, channels=["event"], trigger_topic="member.registered",
                         audience={"type": "new_members", "days": 7})
    make_template(db, channels=["event"])  # 没有触发主题，不发
    coupon.set_member_profile_provider(lambda _db, uid: MemberProfile(registered_at=NOW))
    payload = {"user_id": 42}
    first = handle_event(db, "member.registered", payload, Evt("e-1"))
    assert first["issued"] and first["failed"] == []
    assert handle_event(db, "member.registered", payload, Evt("e-1")) == {"skipped": "duplicate"}
    again = handle_event(db, "member.registered", payload, Evt("e-2"))  # 另一事件、同业务键
    assert again["issued"] == first["issued"]
    assert db.execute(select(func.count()).select_from(UserCoupon)).scalar_one() == 1
    assert db.get(CouponTemplate, gift.id).issued_count == 1
    assert handle_event(db, "order.paid", payload) == {"skipped": "topic"}


def prize(template_id, draw_id, delivery_key, quantity=1, member_id=5, kind="coupon"):
    return {"draw_id": draw_id, "activity_id": 1, "member_id": member_id, "store_id": None, "prize_id": 3,
            "prize_kind": kind, "prize_name": "5元券", "ref_id": template_id, "quantity": quantity,
            "value_cents": 500, "delivery_key": delivery_key, "attempt": 1, "won_at": NOW.isoformat()}


def test_lottery_prize_issues_quantity_dedupes_on_delivery_key_and_acks(db, monkeypatch):
    acks = []
    monkeypatch.setitem(sys.modules, "lottery", SimpleNamespace(
        ack_delivery=lambda _db, draw_id, ok, ref=None, error=None: acks.append((draw_id, ok, ref, error))))
    t = make_template(db, per_member_limit=1, total_stock=3, channels=["lottery"])
    first = SUBSCRIPTIONS["lottery.prize_won"](db, prize(t.id, 1, "dk-1", quantity=2), "ev-1")
    resend = SUBSCRIPTIONS["lottery.prize_won"](db, prize(t.id, 1, "dk-1", quantity=2), "ev-2")  # 重发：新事件 id
    db.commit()
    assert len(first["issued"]) == 2 and resend["issued"] == first["issued"]  # 不受每人限领、按 delivery_key 去重
    assert acks[0] == (1, True, ",".join(map(str, first["issued"])), None) and acks[1][1] is True
    sold_out = SUBSCRIPTIONS["lottery.prize_won"](db, prize(t.id, 2, "dk-2", quantity=2), "ev-3")
    db.commit()  # 订阅函数不提交，由 worker 提交
    assert len(sold_out["issued"]) == 1 and sold_out["failed"] == "sold_out"
    assert acks[-1] == (2, False, None, "coupon:sold_out")
    assert events(db, "coupon.issue_failed")[0]["ref"] == "lottery:dk-2:2"
    row = db.get(UserCoupon, first["issued"][0])
    assert row.channel == "lottery" and row.source_ref == "lottery_draw:1"
    assert SUBSCRIPTIONS["lottery.prize_won"](db, prize(t.id, 3, "dk-3", kind="points"), "ev-4") == {
        "skipped": "not_coupon"}
    assert SUBSCRIPTIONS["lottery.prize_won"](db, prize(t.id, 1, "dk-1"), "ev-1") == {"skipped": "duplicate"}


def test_lottery_ack_falls_back_to_event(db, monkeypatch):
    monkeypatch.setitem(sys.modules, "lottery", None)  # 抽奖模块未装配
    t = make_template(db, channels=["lottery"])
    handle_event(db, "lottery.prize_won", prize(t.id, 9, "dk-9"), Evt("ev-9"))
    result = events(db, "lottery.delivery_result")
    assert result[0]["draw_id"] == 9 and result[0]["delivery_key"] == "dk-9" and result[0]["ok"] is True


def test_consumer_through_outbox_dispatcher(db):
    t = make_template(db, per_member_limit=0, channels=["compensation"])
    enqueue_event(db, "coupon.issue_requested", {"user_id": 9, "template_id": t.id, "ref": "ticket:77",
                                                 "channel": "compensation", "source_ref": "ticket:77"})
    db.commit()
    handler = make_consumer(get_session_factory())
    counts = dispatch_pending(db, handler)
    assert counts["sent"] >= 1
    db.expire_all()
    assert db.execute(select(UserCoupon.channel).where(UserCoupon.user_id == 9)).scalar_one() == "compensation"


# ---------------------------------------------------------------- 定时任务


def test_expire_job_is_idempotent_and_skips_locked(db):
    t = make_template(db, per_member_limit=0, valid_days=1)
    a = service.claim(db, 1, t.id, now=NOW)
    b = service.claim(db, 2, t.id, now=NOW)
    service.lock(db, 2, 300, [b.id], [coupon.CartLine(1, 1, 1, 20000)], now=NOW)
    later = service.claim(db, 3, t.id, now=NOW + timedelta(days=1))
    db.commit()
    assert jobs.expire_coupons(db, now=NOW + timedelta(days=1, hours=1)) == "expired=1"
    assert jobs.expire_coupons(db, now=NOW + timedelta(days=1, hours=1)) == "expired=0"
    db.expire_all()
    assert [db.get(UserCoupon, i).status for i in (a.id, b.id, later.id)] == ["expired", "locked", "unused"]
    assert events(db, "coupon.expired")[0]["user_coupon_id"] == a.id


def test_expire_job_batches(db):
    coupon.set_config(expire_batch_size=2)
    t = make_template(db, per_member_limit=0, valid_days=1)
    for uid in range(5):
        service.claim(db, uid + 1, t.id, now=NOW)
    db.commit()
    assert jobs.expire_coupons(db, now=NOW + timedelta(days=2)) == "expired=5"


def test_remind_expiring_once(db):
    t = make_template(db, per_member_limit=0, valid_days=3)
    c = service.claim(db, 1, t.id, now=NOW)
    db.commit()
    assert jobs.remind_expiring(db, now=NOW) == "reminded=0"  # 72 小时后到期，不在 48 小时提醒窗口内
    assert jobs.remind_expiring(db, now=NOW + timedelta(hours=25)) == "reminded=1"
    assert jobs.remind_expiring(db, now=NOW + timedelta(hours=26)) == "reminded=0"
    assert jobs.remind_expiring(db, now=NOW + timedelta(hours=80)) == "reminded=0"  # 已过期不提醒
    assert events(db, "coupon.expiring_soon")[0]["user_coupon_id"] == c.id


def test_register_jobs():
    registry = JobRegistry()
    names = [job.name for job in coupon.register_jobs(registry)]
    assert names == ["coupon.expire", "coupon.remind_expiring", "coupon.issue_batches"]
    assert all(isinstance(job, Job) and job.lock_ttl < job.interval_seconds or job.interval_seconds > 600
               for job in coupon.JOBS)


# ---------------------------------------------------------------- 统计


def test_template_stats(db):
    t = make_template(db, per_member_limit=0, total_stock=10)
    coupons = [service.claim(db, uid, t.id, now=NOW) for uid in (1, 2, 3, 4)]
    service.lock(db, 1, 11, [coupons[0].id], [coupon.CartLine(1, 1, 1, 20000)], now=NOW)
    service.lock(db, 2, 12, [coupons[1].id], [coupon.CartLine(1, 1, 1, 20000)], now=NOW)
    service.redeem(db, 11, now=NOW)
    service.redeem(db, 12, now=NOW)
    db.commit()
    stats = service.template_stats(db, t.id)
    assert stats["issued"] == 4 and stats["used"] == 2 and stats["used_amount_cents"] == 4000
    assert stats["conversion_bp"] == 5000 and stats["orders"] == 2 and stats["remaining_stock"] == 6
    assert stats["by_status"]["unused"] == 2 and stats["by_channel"] == {"claim_center": 4}


def test_subscriptions_do_not_commit(db):
    t = make_template(db, per_member_limit=0, channels=["compensation"])
    SUBSCRIPTIONS["coupon.issue_requested"](db, {"user_id": 3, "template_id": t.id, "ref": "ticket:1",
                                                 "channel": "compensation"}, "ev-x")
    db.rollback()  # worker 失败回滚：券与去重记录都不留下，重试时会重新发
    assert db.execute(select(func.count()).select_from(UserCoupon)).scalar_one() == 0
    assert set(SUBSCRIPTIONS) == {"member.registered", "lottery.prize_won", "coupon.issue_requested"}
