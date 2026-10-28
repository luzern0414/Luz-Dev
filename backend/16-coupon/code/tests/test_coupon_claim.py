"""领取与发放：库存、每人 / 每日限领、领取期、渠道、人群、快照、业务键幂等、不提交事务、并发不超发。"""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

import coupon
from coupon import CouponTemplate, MemberProfile, UserCoupon, service
from luzback import AppError, ErrorCode, OutboxEvent

from coupon_fixtures import *  # noqa: F401,F403
from coupon_fixtures import NOW, make_template


def code_of(fn):
    with pytest.raises(AppError) as error:
        fn()
    return error.value


def test_claim_takes_stock_and_snapshots_rules(db):
    t = make_template(db, total_stock=2, valid_days=7, start_delay_days=1)
    c = service.claim(db, 7, t.id, now=NOW)
    db.commit()
    assert db.get(CouponTemplate, t.id).issued_count == 1
    assert c.slot == 1 and c.status == "unused" and c.channel == "claim_center"
    assert json.loads(c.rules_json)["amount_cents"] == 2000
    assert c.valid_from.replace(tzinfo=NOW.tzinfo) == NOW + timedelta(days=1)
    assert c.valid_to.replace(tzinfo=NOW.tzinfo) == NOW + timedelta(days=8)
    assert service.coupon_view(c, NOW)["display_status"] == "not_started"
    topics = db.execute(select(OutboxEvent.topic)).scalars().all()
    assert "coupon.claimed" in topics


def test_claim_does_not_commit_the_callers_transaction(db):
    t = make_template(db)
    service.claim(db, 7, t.id, now=NOW)
    db.rollback()  # 调用方回滚：券与库存都不留下（旧实现会在 claim 内部提前提交）
    assert db.execute(select(func.count()).select_from(UserCoupon)).scalar_one() == 0
    assert db.get(CouponTemplate, t.id).issued_count == 0


def test_per_member_limit_and_sold_out(db):
    t = make_template(db, total_stock=2, per_member_limit=1)
    service.claim(db, 1, t.id, now=NOW)
    db.commit()
    err = code_of(lambda: service.claim(db, 1, t.id, now=NOW))
    assert err.code == ErrorCode.LIMIT_REACHED and err.data["reason"] == "per_member_limit"
    db.rollback()
    service.claim(db, 2, t.id, now=NOW)
    db.commit()
    err = code_of(lambda: service.claim(db, 3, t.id, now=NOW))
    assert err.code == ErrorCode.LIMIT_REACHED and err.data["reason"] == "sold_out"


def test_daily_limit_resets_next_local_day(db):
    t = make_template(db, per_member_limit=0, per_member_daily_limit=2)
    service.claim(db, 1, t.id, now=NOW)
    service.claim(db, 1, t.id, now=NOW + timedelta(hours=1))
    db.commit()
    err = code_of(lambda: service.claim(db, 1, t.id, now=NOW + timedelta(hours=2)))
    assert err.data["reason"] == "daily_limit"
    db.rollback()
    c = service.claim(db, 1, t.id, now=NOW + timedelta(hours=13))  # 北京时间次日 01:00
    assert c.slot == 3 and c.day_slot == 1


def test_template_must_be_active_and_in_claim_window(db):
    draft = make_template(db, publish=False)
    assert code_of(lambda: service.claim(db, 1, draft.id, now=NOW)).data["reason"] == "not_active"
    window = make_template(db, claim_start_at=NOW + timedelta(hours=1), claim_end_at=NOW + timedelta(days=1),
                           now=NOW - timedelta(days=1))
    assert code_of(lambda: service.claim(db, 1, window.id, now=NOW)).data["reason"] == "not_started"
    assert code_of(lambda: service.claim(db, 1, window.id, now=NOW + timedelta(days=2))).data["reason"] == "claim_ended"
    service.claim(db, 1, window.id, now=NOW + timedelta(hours=2))
    db.commit()
    service.pause_template(db, 1, window.id, now=NOW)
    db.commit()
    assert code_of(lambda: service.claim(db, 2, window.id, now=NOW + timedelta(hours=2))).code == ErrorCode.BUSINESS_RULE
    assert code_of(lambda: service.claim(db, 1, 999, now=NOW)).code == ErrorCode.NOT_FOUND


def test_fixed_validity_ended_and_channel_not_allowed(db):
    fixed = make_template(db, valid_type="fixed", use_start_at=NOW - timedelta(days=2), use_end_at=NOW + timedelta(hours=1))
    assert code_of(lambda: service.claim(db, 1, fixed.id, now=NOW + timedelta(hours=2))).data["reason"] in (
        "validity_ended", "claim_ended")
    admin_only = make_template(db, channels=["admin"])
    assert code_of(lambda: service.claim(db, 1, admin_only.id, now=NOW)).data["reason"] == "channel"
    c = service.issue(db, admin_only.id, 1, "manual:1", channel="admin", now=NOW)
    assert c.channel == "admin"


def test_audience_rules_use_profile_provider(db):
    newbie = make_template(db, audience={"type": "new_members", "days": 7})
    assert code_of(lambda: service.claim(db, 1, newbie.id, now=NOW)).data["reason"] == "audience_unavailable"
    profiles = {1: MemberProfile(registered_at=NOW - timedelta(days=2), level=2, tags=frozenset({"vip"})),
                2: MemberProfile(registered_at=NOW - timedelta(days=30), level=1)}
    coupon.set_member_profile_provider(lambda _db, uid: profiles.get(uid))
    service.claim(db, 1, newbie.id, now=NOW)
    assert code_of(lambda: service.claim(db, 2, newbie.id, now=NOW)).data["reason"] == "not_new_member"
    assert code_of(lambda: service.claim(db, 3, newbie.id, now=NOW)).data["reason"] == "member_unknown"
    levels = make_template(db, audience={"type": "levels", "levels": [2, 3]})
    tags = make_template(db, audience={"type": "tags", "tags": ["vip"]})
    service.claim(db, 1, levels.id, now=NOW)
    service.claim(db, 1, tags.id, now=NOW)
    assert code_of(lambda: service.claim(db, 2, levels.id, now=NOW)).data["reason"] == "level_not_matched"
    assert code_of(lambda: service.claim(db, 2, tags.id, now=NOW)).data["reason"] == "tag_not_matched"
    coupon.set_audience_checker(lambda _db, uid, audience, now: None if uid == 2 else "blocked")
    service.claim(db, 2, tags.id, now=NOW)
    assert code_of(lambda: service.claim(db, 5, tags.id, now=NOW)).data["reason"] == "blocked"


def test_issue_with_ref_key_is_idempotent(db):
    t = make_template(db, per_member_limit=3)
    a = service.issue(db, t.id, 9, "birthday:9:2026", channel="event", source_ref="birthday:2026", now=NOW)
    b = service.issue(db, t.id, 9, "birthday:9:2026", channel="event", now=NOW)
    db.commit()
    assert a.id == b.id
    assert db.get(CouponTemplate, t.id).issued_count == 1
    other = make_template(db)
    assert code_of(lambda: service.issue(db, other.id, 9, "birthday:9:2026", now=NOW)).code == ErrorCode.CONFLICT
    assert code_of(lambda: service.issue(db, t.id, 9, "", now=NOW)).code == ErrorCode.BAD_REQUEST


def test_issue_can_ignore_per_member_limit_for_prizes(db):
    t = make_template(db, per_member_limit=1)
    service.issue(db, t.id, 1, "lottery:1", channel="lottery", enforce_limits=False, now=NOW)
    second = service.issue(db, t.id, 1, "lottery:2", channel="lottery", enforce_limits=False, now=NOW)
    assert second.slot == 2


def _claim_with_retry(factory, user_id, template_id):
    for _ in range(50):
        session = factory()
        try:
            service.claim(session, user_id, template_id, now=NOW)
            session.commit()
            return "ok"
        except AppError as exc:
            session.rollback()
            if exc.code == ErrorCode.CONFLICT:
                continue
            return exc.data.get("reason", exc.code.name) if isinstance(exc.data, dict) else exc.code.name
        except OperationalError:  # SQLite 写锁繁忙：客户端重试
            session.rollback()
            continue
        finally:
            session.close()
    return "gave_up"


def test_fifty_threads_never_over_issue(file_factory):
    with file_factory() as s:
        t = make_template(s, total_stock=10, per_member_limit=1)
    members = [i // 2 + 1 for i in range(50)]  # 25 个会员，每人并发点两次
    with ThreadPoolExecutor(max_workers=50) as pool:
        results = list(pool.map(lambda uid: _claim_with_retry(file_factory, uid, t.id), members))
    assert results.count("ok") == 10
    assert set(results) <= {"ok", "per_member_limit", "sold_out"}
    with file_factory() as s:
        assert s.get(CouponTemplate, t.id).issued_count == 10
        rows = s.execute(select(UserCoupon.user_id, func.count()).group_by(UserCoupon.user_id)).all()
        assert sum(n for _, n in rows) == 10 and all(n == 1 for _, n in rows)


def test_same_member_concurrent_claims_respect_limit(file_factory):
    with file_factory() as s:
        t = make_template(s, total_stock=100, per_member_limit=2)
    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(lambda _: _claim_with_retry(file_factory, 5, t.id), range(20)))
    assert results.count("ok") == 2
    with file_factory() as s:
        assert s.get(CouponTemplate, t.id).issued_count == 2
        assert sorted(s.execute(select(UserCoupon.slot)).scalars().all()) == [1, 2]


def test_default_profile_provider_from_membership(db, monkeypatch):
    """未注入时用 14-membership 的 member_profile（dict 或对象均可），项目注入的优先。"""
    import sys
    import types

    from coupon_fixtures import ORIGINAL_MEMBERSHIP_PROVIDER

    stub = types.ModuleType("membership")
    stub.member_profile = lambda _db, uid: {"registered_at": NOW - timedelta(days=1), "level": "2", "tags": ["vip"]} \
        if uid == 1 else None
    monkeypatch.setitem(sys.modules, "membership", stub)
    monkeypatch.setattr(coupon.config, "_membership_provider", ORIGINAL_MEMBERSHIP_PROVIDER)
    newbie = make_template(db, audience={"type": "levels", "levels": [2]})
    service.claim(db, 1, newbie.id, now=NOW)
    assert code_of(lambda: service.claim(db, 3, newbie.id, now=NOW)).data["reason"] == "member_unknown"
    coupon.set_member_profile_provider(lambda _db, uid: MemberProfile(level=9))
    assert code_of(lambda: service.claim(db, 4, newbie.id, now=NOW)).data["reason"] == "level_not_matched"
