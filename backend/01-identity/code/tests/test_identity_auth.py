"""identity：微信登录与注册、令牌（会话 / 轮换 / 复用检测 / 并发）、短信验证码、手机号绑定。"""
from __future__ import annotations

import hashlib
import hmac
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

import identity
from identity import FakeAgreements, FakeSmsSender, RefreshToken, SmsCode, User, UserSession
from identity.ports import AgreementAccept
from identity_testkit import *  # noqa: F401,F403
from identity_testkit import FIELD_KEY, audits, events, login, raises, set_config
from luzback import Base, ErrorCode, create_token, get_settings, utcnow
from luzback.security import b64url
from wechat_mp import FakeMiniProgram, FakeWechatServer

PHONE = "13800138000"


# ================================================================ 登录与注册

def test_first_login_registers_member_with_agreements_and_encrypted_session_key(db, mp):
    gateway = FakeAgreements({1: "2026-01", 2: "2026-02"})
    agreements = [AgreementAccept(1, "2026-01", "a" * 64), AgreementAccept(2, "2026-02", "b" * 64)]
    result = login(db, mp, session_key="SK-secret", agreements=agreements, agreement_gateway=gateway)
    assert result["is_new_user"] and result["pending_agreements"] == []
    assert "session_key" not in json.dumps(result) and "SK-secret" not in json.dumps(result)
    user = db.get(User, result["user_id"])
    assert user.member_no.startswith("M") and len(user.member_no) == 11
    assert user.agreed_document_ids == "1,2"
    assert user.wx_session_key_enc.startswith("v1:") and "SK-secret" not in user.wx_session_key_enc
    assert identity.session_key_of(db, user.id) == "SK-secret"
    assert events(db, "member.registered") == [{"user_id": user.id, "member_no": user.member_no, "channel": "miniprogram"}]
    assert audits(db, "member.register")


def test_registration_without_required_agreements_is_rejected_and_creates_nothing(db, mp):
    gateway = FakeAgreements({1: "2026-01"})
    err = raises(ErrorCode.BUSINESS_RULE, login, db, mp, agreement_gateway=gateway)
    assert err.data["reason"] == "agreement_required" and err.data["pending"][0]["document_id"] == 1
    assert db.execute(select(func.count()).select_from(User)).scalar_one() == 0
    stale = raises(ErrorCode.CONFLICT, login, db, mp, code="c2", agreement_gateway=gateway,
                   agreements=[AgreementAccept(1, "2025-old", "a" * 64)])
    assert stale.message


def test_existing_member_login_reuses_account_and_reports_new_agreement_versions(db, mp):
    gateway = FakeAgreements({})
    first = login(db, mp, agreement_gateway=gateway)
    gateway.required[3] = "2026-10"  # 隐私政策更新
    again = login(db, mp, code="c2", agreement_gateway=gateway)
    assert again["user_id"] == first["user_id"] and not again["is_new_user"]
    assert again["pending_agreements"] == [{"document_id": 3, "version": "2026-10"}]
    assert db.execute(select(func.count()).select_from(User)).scalar_one() == 1


def test_login_errors_map_to_typed_codes(db, mp):
    raises(ErrorCode.UNAUTHORIZED, identity.login_with_code, db, "never-issued", mp)
    mp.add_login_code("c", "o")
    mp.server.fail_next("/sns/jscode2session", 40226)
    raises(ErrorCode.FORBIDDEN, identity.login_with_code, db, "c", mp)
    mp.server.fail_next("/sns/jscode2session", "timeout")
    raises(ErrorCode.UPSTREAM_FAILED, identity.login_with_code, db, "c", mp)
    mp.server.fail_next("/sns/jscode2session", 45011)
    raises(ErrorCode.RATE_LIMITED, identity.login_with_code, db, "c", mp)
    raises(ErrorCode.BAD_REQUEST, identity.login_with_code, db, "", mp)


def test_legacy_code2session_callable_still_supported(db):
    result = identity.login_with_code(db, "abc", lambda code: {"openid": f"openid-{code}"})
    assert db.get(User, result["user_id"]).openid == "openid-abc"


def test_disabled_member_login_is_4030_with_reason(db, mp):
    first = login(db, mp)
    identity.disable_member(db, 9, first["user_id"], "刷单")
    err = raises(ErrorCode.FORBIDDEN, login, db, mp, code="c2")
    assert err.data == {"reason": "account_disabled"}
    assert audits(db, "member.login.denied")


def test_concurrent_first_login_creates_exactly_one_member(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'login.db'}", connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    mp = FakeMiniProgram()
    for i in range(20):
        mp.add_login_code(f"c{i}", "o-same")

    def go(i):
        with factory() as s:
            return identity.login_with_code(s, f"c{i}", mp, agreement_gateway=FakeAgreements())["user_id"]

    with ThreadPoolExecutor(max_workers=10) as pool:
        ids = list(pool.map(go, range(20)))
    assert len(set(ids)) == 1
    with factory() as s:
        assert s.execute(select(func.count()).select_from(User)).scalar_one() == 1
        assert s.execute(select(func.count()).select_from(UserSession)).scalar_one() == 20
    engine.dispose()


# ================================================================ 访问令牌

def _forge(claims: dict, alg="HS256", secret=None) -> str:
    head = b64url(json.dumps({"alg": alg, "typ": "JWT"}).encode())
    body = b64url(json.dumps(claims).encode())
    key = (secret or get_settings().jwt_secret).encode()
    sig = b64url(hmac.new(key, f"{head}.{body}".encode(), hashlib.sha256).digest()) if alg != "none" else ""
    return f"{head}.{body}.{sig}"


def test_access_token_claims_and_validation(db, mp):
    tokens = login(db, mp)
    claims = identity.decode_access(tokens["access_token"])
    assert claims["aud"] == "luz-app" and claims["iss"] == get_settings().jwt_issuer and claims["kind"] == "access"
    assert claims["sid"] == tokens["session_id"] and claims["jti"] and claims["exp"] - claims["iat"] == 7200
    identity.service.assert_session_active(db, claims)
    s = get_settings()
    base = {k: v for k, v in claims.items()}
    raises(ErrorCode.UNAUTHORIZED, identity.decode_access, _forge(base, secret="wrong-secret"))
    raises(ErrorCode.UNAUTHORIZED, identity.decode_access, _forge(base, alg="none"))
    raises(ErrorCode.UNAUTHORIZED, identity.decode_access, _forge({**base, "aud": "other-app"}))
    raises(ErrorCode.UNAUTHORIZED, identity.decode_access, _forge({k: v for k, v in base.items() if k != "sid"}))
    raises(ErrorCode.UNAUTHORIZED, identity.decode_access, _forge({**base, "kind": "admin"}))
    raises(ErrorCode.UNAUTHORIZED, identity.decode_access, tokens["refresh_token"])
    expired = create_token(str(tokens["user_id"]), s.jwt_secret, -120, kind="access", issuer=s.jwt_issuer,
                           extra={"aud": "luz-app", "sid": tokens["session_id"]})
    raises(ErrorCode.UNAUTHORIZED, identity.decode_access, expired)
    skewed = create_token(str(tokens["user_id"]), s.jwt_secret, -10, kind="access", issuer=s.jwt_issuer,
                          extra={"aud": "luz-app", "sid": tokens["session_id"]})
    assert identity.decode_access(skewed)  # 10 秒内的时钟偏差可接受（leeway 30）


def test_session_check_rejects_tokens_of_revoked_sessions(db, mp):
    tokens = login(db, mp)
    claims = identity.decode_access(tokens["access_token"])
    identity.logout(db, tokens["user_id"], session_id=tokens["session_id"])
    raises(ErrorCode.UNAUTHORIZED, identity.service.assert_session_active, db, claims)


# ================================================================ 刷新令牌

def test_refresh_rotates_and_stores_only_hashes(db, mp):
    tokens = login(db, mp)
    rotated = identity.refresh_tokens(db, tokens["refresh_token"])
    assert rotated["refresh_token"] != tokens["refresh_token"] and rotated["session_id"] == tokens["session_id"]
    stored = db.execute(select(RefreshToken.token_hash)).scalars().all()
    assert tokens["refresh_token"] not in stored and len(stored) == 2
    assert db.execute(select(RefreshToken).where(RefreshToken.parent_id.is_not(None))).scalar_one().status == "active"


def test_reuse_within_grace_is_rejected_without_revoking_family(db, mp):
    tokens = login(db, mp)
    now = utcnow()
    rotated = identity.refresh_tokens(db, tokens["refresh_token"], now=now)
    err = raises(ErrorCode.UNAUTHORIZED, identity.refresh_tokens, db, tokens["refresh_token"], now=now + timedelta(seconds=5))
    assert err.data["reason"] == "refresh_rotated"
    assert identity.refresh_tokens(db, rotated["refresh_token"], now=now + timedelta(seconds=6))


def test_reuse_after_grace_revokes_whole_family(db, mp):
    tokens = login(db, mp)
    other = login(db, mp, code="c-other")  # 另一台设备的会话不受影响
    now = utcnow()
    rotated = identity.refresh_tokens(db, tokens["refresh_token"], now=now)
    raises(ErrorCode.UNAUTHORIZED, identity.refresh_tokens, db, tokens["refresh_token"], now=now + timedelta(seconds=60))
    raises(ErrorCode.UNAUTHORIZED, identity.refresh_tokens, db, rotated["refresh_token"], now=now + timedelta(seconds=61))
    raises(ErrorCode.UNAUTHORIZED, identity.service.assert_session_active, db, identity.decode_access(rotated["access_token"]))
    assert audits(db, "member.session.reuse_detected")
    assert identity.refresh_tokens(db, other["refresh_token"])


def test_reuse_detection_with_zero_grace(db, mp, monkeypatch):
    set_config(monkeypatch, IDENTITY_REFRESH_REUSE_GRACE=0)
    tokens = login(db, mp)
    rotated = identity.refresh_tokens(db, tokens["refresh_token"])
    raises(ErrorCode.UNAUTHORIZED, identity.refresh_tokens, db, tokens["refresh_token"])
    raises(ErrorCode.UNAUTHORIZED, identity.refresh_tokens, db, rotated["refresh_token"])


def test_refresh_idle_and_absolute_lifetime(db, mp):
    tokens = login(db, mp)
    raises(ErrorCode.UNAUTHORIZED, identity.refresh_tokens, db, tokens["refresh_token"], now=utcnow() + timedelta(days=15))
    current = login(db, mp, code="c2")["refresh_token"]
    start = utcnow()
    for day in (10, 20):  # 持续使用可以续到绝对期限
        current = identity.refresh_tokens(db, current, now=start + timedelta(days=day))["refresh_token"]
    err = raises(ErrorCode.UNAUTHORIZED, identity.refresh_tokens, db, current, now=start + timedelta(days=30, seconds=1))
    assert err.data["reason"] == "session_expired"


def test_forged_or_unknown_refresh_token(db, mp):
    login(db, mp)
    raises(ErrorCode.UNAUTHORIZED, identity.refresh_tokens, db, "x" * 43)
    raises(ErrorCode.UNAUTHORIZED, identity.refresh_tokens, db, "")


def test_concurrent_refresh_only_one_wins(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'refresh.db'}", connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    mp = FakeMiniProgram()
    mp.add_login_code("c", "o")
    with factory() as s:
        tokens = identity.login_with_code(s, "c", mp, agreement_gateway=FakeAgreements())

    def go(_):
        with factory() as s:
            try:
                identity.refresh_tokens(s, tokens["refresh_token"])
                return "ok"
            except identity.service.AppError as exc:
                return exc.data["reason"]

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(go, range(20)))
    assert results.count("ok") == 1 and set(results) <= {"ok", "refresh_rotated"}
    with factory() as s:
        assert s.execute(select(UserSession)).scalar_one().status == "active"  # 并发不触发家族吊销
        assert s.execute(select(func.count()).select_from(RefreshToken)).scalar_one() == 2
    engine.dispose()


def test_logout_current_vs_all_sessions(db, mp):
    a = login(db, mp, code="a")
    b = login(db, mp, code="b")
    assert identity.logout(db, a["user_id"], session_id=a["session_id"]) == {"revoked_sessions": 1}
    raises(ErrorCode.UNAUTHORIZED, identity.refresh_tokens, db, a["refresh_token"])
    assert identity.refresh_tokens(db, b["refresh_token"])
    c = login(db, mp, code="c")
    assert identity.logout(db, a["user_id"]) == {"revoked_sessions": 2}
    raises(ErrorCode.UNAUTHORIZED, identity.refresh_tokens, db, c["refresh_token"])


# ================================================================ 短信验证码

@pytest.fixture
def sms():
    return FakeSmsSender()


def test_sms_send_returns_request_and_stores_only_hash(db, sms):
    out = identity.send_sms_code(db, PHONE, "bind", sms, ip="1.1.1.1")
    assert set(out) == {"request_id", "expires_in", "retry_after"} and out["expires_in"] == 300
    code = sms.last_code(PHONE)
    assert len(code) == 6 and code.isdigit()
    row = db.execute(select(SmsCode)).scalar_one()
    assert code not in row.code_hash and PHONE not in row.phone_hash


def test_sms_resend_interval_supersedes_old_code(db, sms):
    now = utcnow()
    first = identity.send_sms_code(db, PHONE, "bind", sms, now=now)
    first_code = sms.last_code(PHONE)
    err = raises(ErrorCode.RATE_LIMITED, identity.send_sms_code, db, PHONE, "bind", sms, now=now + timedelta(seconds=30))
    assert 0 < err.retry_after <= 30
    login_purpose = identity.send_sms_code(db, PHONE, "login", sms, now=now + timedelta(seconds=30))  # 用途独立
    assert login_purpose["request_id"]
    identity.send_sms_code(db, PHONE, "bind", sms, now=now + timedelta(seconds=61))
    user = _user(db)
    raises(ErrorCode.BAD_REQUEST, identity.bind_phone_with_sms, db, user.id, PHONE, first["request_id"], first_code,
           now=now + timedelta(seconds=62))


def _user(db, openid="o-u"):
    user = User(member_no=f"M{abs(hash(openid)) % 10**10:010d}", openid=openid)
    db.add(user)
    db.commit()
    return user


def test_sms_daily_cap_per_phone_and_per_ip_limit(db, sms, monkeypatch):
    set_config(monkeypatch, IDENTITY_SMS_DAILY_PER_PHONE=3, IDENTITY_SMS_HOURLY_PER_IP=4)
    start = utcnow().replace(hour=2, minute=0, second=0, microsecond=0)  # 上海时间上午，避开跨日
    for i in range(3):
        identity.send_sms_code(db, PHONE, "bind", sms, now=start + timedelta(minutes=2 * i))
    err = raises(ErrorCode.RATE_LIMITED, identity.send_sms_code, db, PHONE, "bind", sms, now=start + timedelta(minutes=10))
    assert err.retry_after > 0
    for i in range(4):
        identity.send_sms_code(db, f"1390000000{i}", "login", sms, ip="9.9.9.9", now=start + timedelta(minutes=i))
    raises(ErrorCode.RATE_LIMITED, identity.send_sms_code, db, "13900000009", "login", sms, ip="9.9.9.9",
           now=start + timedelta(minutes=5))
    identity.send_sms_code(db, "13900000009", "login", sms, ip="8.8.8.8", now=start + timedelta(minutes=5))


def test_sms_attempt_limit_then_even_correct_code_fails(db, sms):
    user = _user(db)
    out = identity.send_sms_code(db, PHONE, "bind", sms)
    code = sms.last_code(PHONE)
    wrong = "000000" if code != "000000" else "111111"
    errors = [raises(ErrorCode.BAD_REQUEST, identity.bind_phone_with_sms, db, user.id, PHONE, out["request_id"], wrong)
              for _ in range(5)]
    assert {e.message for e in errors} == {"verification code is incorrect or expired"}
    assert db.execute(select(SmsCode)).scalar_one().attempts == 5
    raises(ErrorCode.BAD_REQUEST, identity.bind_phone_with_sms, db, user.id, PHONE, out["request_id"], code)
    assert db.get(User, user.id).phone_hash is None


def test_sms_code_expiry_single_use_and_binding(db, sms):
    user = _user(db)
    now = utcnow()
    out = identity.send_sms_code(db, PHONE, "bind", sms, now=now)
    code = sms.last_code(PHONE)
    raises(ErrorCode.BAD_REQUEST, identity.bind_phone_with_sms, db, user.id, PHONE, out["request_id"], code,
           now=now + timedelta(seconds=301))
    out = identity.send_sms_code(db, PHONE, "bind", sms, now=now + timedelta(seconds=400))
    code = sms.last_code(PHONE)
    raises(ErrorCode.BAD_REQUEST, identity.login_with_sms, db, PHONE, out["request_id"], code)       # 用途不符
    raises(ErrorCode.BAD_REQUEST, identity.bind_phone_with_sms, db, user.id, "13900000000", out["request_id"], code)  # 号码不符
    raises(ErrorCode.BAD_REQUEST, identity.bind_phone_with_sms, db, user.id, PHONE, "f" * 32, code)  # 请求不符
    assert identity.bind_phone_with_sms(db, user.id, PHONE, out["request_id"], code)["phone"] == "138****8000"
    raises(ErrorCode.BAD_REQUEST, identity.bind_phone_with_sms, db, user.id, PHONE, out["request_id"], code)  # 只能用一次


def test_sms_responses_are_enumeration_safe(db, mp, sms):
    registered = login(db, mp)
    identity.attach_phone(db, registered["user_id"], PHONE, "test")
    a = identity.send_sms_code(db, PHONE, "login", sms)
    b = identity.send_sms_code(db, "13900001111", "login", sms)
    assert set(a) == set(b) and a["expires_in"] == b["expires_in"] and len(sms.sent) == 2
    tokens = identity.login_with_sms(db, PHONE, a["request_id"], sms.last_code(PHONE))
    assert tokens["user_id"] == registered["user_id"] and db.get(UserSession, 2).channel == "sms"
    err = raises(ErrorCode.UNAUTHORIZED, identity.login_with_sms, db, "13900001111", b["request_id"], sms.last_code("13900001111"))
    assert err.data["reason"] == "phone_not_registered"


def test_sms_gateway_failure_is_5020_and_does_not_block_resend(db, sms):
    sms.fail_next = 1
    raises(ErrorCode.UPSTREAM_FAILED, identity.send_sms_code, db, PHONE, "bind", sms)
    assert db.execute(select(SmsCode)).scalar_one().status == "failed"
    assert identity.send_sms_code(db, PHONE, "bind", sms)["request_id"]


def test_sms_invalid_input_and_config_limits(db, sms, monkeypatch):
    raises(ErrorCode.BAD_REQUEST, identity.send_sms_code, db, "12345", "bind", sms)
    raises(ErrorCode.BAD_REQUEST, identity.send_sms_code, db, PHONE, "reset", sms)
    monkeypatch.setenv("IDENTITY_SMS_TTL", "900")
    with pytest.raises(ValueError):
        identity.reload_config()
    monkeypatch.delenv("IDENTITY_SMS_TTL")
    set_config(monkeypatch, IDENTITY_SMS_ENABLED="false")
    raises(ErrorCode.BUSINESS_RULE, identity.send_sms_code, db, PHONE, "bind", sms)


def test_concurrent_sms_send_for_same_phone_sends_once(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'sms.db'}", connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    sender = FakeSmsSender()
    now = utcnow()

    def go(_):
        with factory() as s:
            try:
                identity.send_sms_code(s, PHONE, "login", sender, now=now)
                return "ok"
            except identity.service.AppError as exc:
                return int(exc.code)

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(go, range(20)))
    assert results.count("ok") == 1 and set(results) - {"ok"} == {4290}
    assert len(sender.sent) == 1
    engine.dispose()


# ================================================================ 手机号

def test_bind_phone_from_wechat_encrypts_hashes_masks_and_emits_event(db, mp):
    tokens = login(db, mp)
    mp.add_phone_code("p1", PHONE, openid="o-1")
    assert identity.bind_phone_from_wechat(db, tokens["user_id"], "p1", mp) == {"phone": "138****8000", "phone_verified": True}
    user = db.get(User, tokens["user_id"])
    assert user.phone_enc.startswith("v1:") and PHONE not in user.phone_enc and user.phone_masked == "138****8000"
    assert events(db, "member.phone_bound") == [{"user_id": user.id, "source": "wechat"}]
    assert identity.profile(db, user.id)["phone"] == "138****8000"
    assert identity.profile(db, user.id, reveal_phone=True)["phone"] == PHONE


def test_phone_in_use_conflict(db, mp):
    a = login(db, mp, code="a", openid="o-a")
    b = login(db, mp, code="b", openid="o-b")
    identity.attach_phone(db, a["user_id"], PHONE, "test")
    mp.add_phone_code("p", PHONE, openid="o-b")
    err = raises(ErrorCode.CONFLICT, identity.bind_phone_from_wechat, db, b["user_id"], "p", mp)
    assert err.data == {"reason": "PHONE_IN_USE"}


def test_wechat_phone_code_must_belong_to_member(db, mp):
    tokens = login(db, mp)
    mp.add_phone_code("p", PHONE, openid="o-someone-else")
    raises(ErrorCode.UNAUTHORIZED, identity.bind_phone_from_wechat, db, tokens["user_id"], "p", mp)


def test_international_phone_normalized(db, mp):
    tokens = login(db, mp)
    mp.add_phone_code("p", "2025550123", country_code="1", openid="o-1")
    identity.bind_phone_from_wechat(db, tokens["user_id"], "p", mp)
    assert identity.profile(db, tokens["user_id"], reveal_phone=True)["phone"] == "+12025550123"


def test_field_key_is_used_for_lookup_hash(db, mp):
    from luzback.crypto import FieldCipher

    tokens = login(db, mp)
    identity.attach_phone(db, tokens["user_id"], PHONE, "test")
    assert db.get(User, tokens["user_id"]).phone_hash == FieldCipher(FIELD_KEY).lookup_hash(PHONE)


def test_wechat_server_fixture_isolated():
    assert FakeWechatServer().appid == "wx-fake-appid"


def test_default_gateway_uses_27_legal_public_functions(db, mp):
    legal = pytest.importorskip("legal")
    if not all(hasattr(legal, name) for name in ("create_draft", "publish", "accept", "pending")):
        pytest.skip("27-legal public API changed")
    now = utcnow()
    terms = legal.create_draft(db, "terms", "2026-10", "用户协议", "条款正文")
    privacy = legal.create_draft(db, "privacy", "2026-10", "隐私政策", "隐私正文")
    legal.publish(db, terms.id, 1, now - timedelta(minutes=1))
    legal.publish(db, privacy.id, 1, now - timedelta(minutes=1))
    db.commit()
    err = raises(ErrorCode.BUSINESS_RULE, login, db, mp)
    assert {p["kind"] for p in err.data["pending"]} == {"terms", "privacy"}
    agreed = [AgreementAccept(d.id, d.version, d.content_sha256) for d in (terms, privacy)]
    result = login(db, mp, code="c2", agreements=agreed)
    assert result["is_new_user"] and result["pending_agreements"] == []
    assert db.get(User, result["user_id"]).agreed_document_ids == f"{terms.id},{privacy.id}"
