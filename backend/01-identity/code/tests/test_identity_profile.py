"""identity：资料（白名单、内容安全、头像异步检测）、注销（新鲜令牌、阻断检查、冷静期、撤销、匿名化）、定时任务。"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

import identity
from identity import Blocker, ContentCheck, User, UserSession, jobs
from identity_testkit import *  # noqa: F401,F403
from identity_testkit import audits, events, fresh_claims, login, raises, set_config, stale_claims
from luzback import ErrorCode, utcnow
from luzback.scheduler import JobRegistry
from wechat_mp import FakeMiniProgram

PHONE = "13800138000"


def _span(out):
    from datetime import datetime
    return datetime.fromisoformat(out["due_at"]) - datetime.fromisoformat(out["requested_at"])


# ================================================================ 资料

def test_profile_update_whitelist_and_validation(db, mp):
    uid = login(db, mp)["user_id"]
    out = identity.update_profile(db, uid, mp, nickname="  小卢 ", gender=1, birthday="1990-05-01")
    assert (out["nickname"], out["gender"], out["birthday"]) == ("小卢", 1, "1990-05-01")
    assert audits(db, "member.profile.update")
    raises(ErrorCode.BAD_REQUEST, identity.service.prepare_profile_update, db, uid, {"status": "active"}, mp)
    raises(ErrorCode.BAD_REQUEST, identity.update_profile, db, uid, mp, nickname="x" * 21)
    raises(ErrorCode.BAD_REQUEST, identity.update_profile, db, uid, mp, gender=5)
    raises(ErrorCode.BAD_REQUEST, identity.update_profile, db, uid, mp, birthday="2999-01-01")
    raises(ErrorCode.BAD_REQUEST, identity.update_profile, db, uid, mp, birthday="1990-02-30")
    raises(ErrorCode.BAD_REQUEST, identity.update_profile, db, uid, mp, avatar_url="http://insecure/x.png")


def test_nickname_content_security(db, mp):
    uid = login(db, mp)["user_id"]
    err = raises(ErrorCode.BUSINESS_RULE, identity.update_profile, db, uid, mp, nickname="违禁昵称")
    assert err.data == {"field": "nickname", "reason": "content_risky"}
    identity.update_profile(db, uid, mp, nickname="待审昵称")
    assert db.get(User, uid).content_review is True
    identity.update_profile(db, uid, mp, nickname="正常")
    assert db.get(User, uid).content_review is False


def test_content_check_fails_closed_by_default_and_open_when_configured(db, mp, monkeypatch):
    uid = login(db, mp)["user_id"]
    mp.server.fail_next("/wxa/msg_sec_check", "timeout", "timeout")
    raises(ErrorCode.UPSTREAM_FAILED, identity.update_profile, db, uid, mp, nickname="小卢")
    set_config(monkeypatch, IDENTITY_CONTENT_CHECK_FAIL_OPEN="true")
    mp.server.fail_next("/wxa/msg_sec_check", "timeout", "timeout")
    assert identity.update_profile(db, uid, mp, nickname="小卢")["nickname"] == "小卢"


def test_avatar_applies_only_after_media_check_passes(db, mp):
    uid = login(db, mp)["user_id"]
    out = identity.update_profile(db, uid, mp, avatar_url="https://cdn.example.com/a.png")
    assert out["avatar_url"] == "" and out["avatar_pending"] is True
    check = db.execute(select(ContentCheck)).scalar_one()
    assert identity.handle_media_check_event(db, mp.media_result(check.trace_id, "pass"))
    db.commit()
    assert identity.profile(db, uid)["avatar_url"] == "https://cdn.example.com/a.png"
    assert identity.handle_media_check_event(db, mp.media_result(check.trace_id, "risky", 20002)) is False  # 重复推送
    assert identity.handle_media_check_event(db, {"Event": "wxa_media_check", "trace_id": "not-ours", "errcode": 0}) is False


def test_risky_or_failed_avatar_is_not_applied_and_newer_upload_supersedes(db, mp):
    uid = login(db, mp)["user_id"]
    identity.update_profile(db, uid, mp, avatar_url="https://cdn.example.com/1.png")
    identity.update_profile(db, uid, mp, avatar_url="https://cdn.example.com/2.png")
    first, second = db.execute(select(ContentCheck).order_by(ContentCheck.id)).scalars().all()
    assert first.status == "superseded"
    identity.handle_media_check_event(db, mp.media_result(second.trace_id, "risky", 20002))
    db.commit()
    assert db.get(User, uid).avatar_url == "" and db.get(ContentCheck, second.id).status == "risky"
    identity.update_profile(db, uid, mp, avatar_url="https://cdn.example.com/3.png")
    third = db.execute(select(ContentCheck).order_by(ContentCheck.id.desc())).scalars().first()
    identity.handle_media_check_event(db, mp.media_result(third.trace_id, "pass", errcode=-1008))
    db.commit()
    assert db.get(ContentCheck, third.id).status == "failed" and db.get(User, uid).avatar_url == ""


def test_media_check_timeout_job(db, mp):
    uid = login(db, mp)["user_id"]
    identity.update_profile(db, uid, mp, avatar_url="https://cdn.example.com/a.png")
    assert jobs.expire_content_checks(db, now=utcnow() + timedelta(minutes=30)) == "timeout=0"
    assert jobs.expire_content_checks(db, now=utcnow() + timedelta(minutes=61)) == "timeout=1"
    assert jobs.expire_content_checks(db, now=utcnow() + timedelta(minutes=120)) == "timeout=0"  # 幂等


def test_content_check_can_be_disabled(db, mp, monkeypatch):
    set_config(monkeypatch, IDENTITY_CONTENT_CHECK="false")
    uid = login(db, mp)["user_id"]
    out = identity.update_profile(db, uid, mp, nickname="违禁也不检查", avatar_url="https://cdn.example.com/a.png")
    assert out["avatar_url"] == "https://cdn.example.com/a.png"


# ================================================================ 注销

def test_deletion_requires_fresh_token(db, mp):
    tokens = login(db, mp)
    err = raises(ErrorCode.UNAUTHORIZED, identity.request_deletion, db, tokens["user_id"], stale_claims(tokens))
    assert err.data == {"reason": "fresh_required"}


def test_deletion_blocked_by_registered_checkers(db, mp):
    tokens = login(db, mp)
    identity.register_deletion_checker("order", lambda s, uid: [Blocker("unfinished_orders", "还有 2 笔未完成订单")])
    identity.register_deletion_checker("wallet", lambda s, uid: [])
    err = raises(ErrorCode.BUSINESS_RULE, identity.request_deletion, db, tokens["user_id"], fresh_claims(tokens))
    assert err.data["blockers"] == [{"code": "unfinished_orders", "message": "还有 2 笔未完成订单"}]
    assert identity.deletion_status(db, tokens["user_id"])["blockers"][0]["code"] == "unfinished_orders"
    assert db.get(User, tokens["user_id"]).status == "active"


def test_deletion_request_keeps_current_session_and_cancel(db, mp):
    current = login(db, mp, code="a")
    other = login(db, mp, code="b")
    out = identity.request_deletion(db, current["user_id"], fresh_claims(current), "不再使用")
    assert out["status"] == "deleting" and _span(out) == timedelta(days=15)
    raises(ErrorCode.UNAUTHORIZED, identity.refresh_tokens, db, other["refresh_token"])
    identity.service.assert_session_active(db, fresh_claims(current))  # 当前会话保留，可撤销注销
    raises(ErrorCode.CONFLICT, identity.request_deletion, db, current["user_id"], fresh_claims(current))
    assert identity.cancel_deletion(db, current["user_id"]) == {"status": "active"}
    raises(ErrorCode.CONFLICT, identity.cancel_deletion, db, current["user_id"])
    assert audits(db, "member.deletion.request") and audits(db, "member.deletion.cancel")


def test_login_during_cooling_off_cancels_deletion(db, mp):
    tokens = login(db, mp)
    identity.request_deletion(db, tokens["user_id"], fresh_claims(tokens))
    again = login(db, mp, code="c2")
    assert again["deletion_cancelled"] is True and db.get(User, tokens["user_id"]).status == "active"


def test_anonymize_after_cooling_off(db, mp):
    tokens = login(db, mp, session_key="SK")
    uid = tokens["user_id"]
    identity.attach_phone(db, uid, PHONE, "test")
    identity.update_profile(db, uid, mp, nickname="小卢", birthday="1990-01-01")
    identity.request_deletion(db, uid, fresh_claims(tokens))
    member_no = db.get(User, uid).member_no
    assert jobs.anonymize_due(db, now=utcnow() + timedelta(days=14)) == "deleted=0 held=0 skipped=0"
    assert jobs.anonymize_due(db, now=utcnow() + timedelta(days=15, minutes=1)) == "deleted=1 held=0 skipped=0"
    db.expire_all()
    user = db.get(User, uid)
    assert user.status == "deleted" and user.deleted_at is not None and user.member_no == member_no
    assert user.openid.startswith("deleted:") and user.openid != "o-1"
    assert (user.nickname, user.birthday, user.phone_enc, user.phone_hash, user.wx_session_key_enc) == ("已注销用户", None, None, None, None)
    assert all(s.status == "revoked" and s.ip is None for s in db.execute(select(UserSession)).scalars())
    assert events(db, "member.deleted")[0]["user_id"] == uid
    assert audits(db, "member.anonymize")
    raises(ErrorCode.NOT_FOUND, identity.profile, db, uid)
    fresh = login(db, mp, code="c-new")  # 同一微信再登录 → 全新账号
    assert fresh["user_id"] != uid and fresh["is_new_user"]
    assert jobs.anonymize_due(db, now=utcnow() + timedelta(days=30)) == "deleted=0 held=0 skipped=0"  # 幂等


def test_anonymize_holds_when_blocker_appears_during_cooling_off(db, mp):
    tokens = login(db, mp)
    identity.request_deletion(db, tokens["user_id"], fresh_claims(tokens))
    identity.register_deletion_checker("order", lambda s, uid: [Blocker("unfinished_orders", "有未完成订单")])
    assert jobs.anonymize_due(db, now=utcnow() + timedelta(days=16)) == "deleted=0 held=1 skipped=0"
    assert db.get(User, tokens["user_id"]).deletion_hold == "unfinished_orders"
    identity.unregister_deletion_checker("order")
    assert jobs.anonymize_due(db, now=utcnow() + timedelta(days=17)) == "deleted=1 held=0 skipped=0"


def test_cooling_off_is_configurable_but_capped_at_15_days(db, mp, monkeypatch):
    set_config(monkeypatch, IDENTITY_DELETION_COOLING_DAYS=7)
    tokens = login(db, mp)
    out = identity.request_deletion(db, tokens["user_id"], fresh_claims(tokens))
    assert _span(out) == timedelta(days=7)
    monkeypatch.setenv("IDENTITY_DELETION_COOLING_DAYS", "16")
    with pytest.raises(ValueError):
        identity.reload_config()
    monkeypatch.delenv("IDENTITY_DELETION_COOLING_DAYS")


# ================================================================ 任务

def test_jobs_register_and_purge(db, mp):
    registry = JobRegistry()
    jobs.register(registry)
    assert {j.name for j in registry.all()} == {"identity.anonymize_due", "identity.expire_content_checks",
                                                 "identity.purge_auth_records"}
    tokens = login(db, mp)
    identity.logout(db, tokens["user_id"])
    from identity import FakeSmsSender
    identity.send_sms_code(db, PHONE, "bind", FakeSmsSender())
    out = jobs.purge_auth_records(db, now=utcnow() + timedelta(days=200))
    assert out == "sms=1 tokens=1 sessions=1"
    assert jobs.purge_auth_records(db, now=utcnow() + timedelta(days=200)) == "sms=0 tokens=0 sessions=0"


def test_admin_disable_revokes_sessions_and_enable(db, mp):
    tokens = login(db, mp)
    out = identity.disable_member(db, 7, tokens["user_id"], "违规")
    assert out["revoked_sessions"] == 1 and events(db, "member.disabled") == [{"user_id": tokens["user_id"]}]
    raises(ErrorCode.UNAUTHORIZED, identity.refresh_tokens, db, tokens["refresh_token"])
    raises(ErrorCode.CONFLICT, identity.disable_member, db, 7, tokens["user_id"], "再次停用")
    assert identity.freeze(db, tokens["user_id"], False, 7, "申诉通过")["status"] == "active"
    raises(ErrorCode.CONFLICT, identity.enable_member, db, 7, tokens["user_id"], "重复启用")
    raises(ErrorCode.NOT_FOUND, identity.disable_member, db, 7, 999, "不存在")
    assert len(audits(db, "member.disable")) == 1 and len(audits(db, "member.enable")) == 1


def test_fake_client_unused_guard():
    assert FakeMiniProgram().server.appid
