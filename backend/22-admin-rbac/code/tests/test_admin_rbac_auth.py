"""认证用例：登录、防枚举、节流锁定（FakeClock）、验证码钩子、临时口令、MFA、恢复码、会话超时与撤销、新鲜认证。"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from admin_rbac import auth, config, service
from admin_rbac.access import REASON_MFA_ENROLL, REASON_PASSWORD_CHANGE, assert_fresh
from admin_rbac.models import AdminLoginLog, AdminLoginThrottle, AdminRecoveryCode, AdminSession, AdminUser
from luzback import AppError, ErrorCode
from luzback.outbox import OutboxEvent
from luzback.security import hash_password as framework_hash

from admin_rbac_testkit import (
    PASSWORD, UA, actor, code_at, enable_mfa, login, make_user, new_db, reset_hooks, staff_of, token_of,
)


@pytest.fixture()
def clock():
    c = reset_hooks()
    yield c
    reset_hooks()


@pytest.fixture()
def db(clock):
    session = new_db()
    yield session
    session.close()


def err(fn) -> AppError:
    with pytest.raises(AppError) as exc:
        fn()
    return exc.value


# ---------- 登录与防枚举 ----------

def test_login_success_creates_session_and_log(db):
    user = make_user(db, "boss")
    result = login(db, "BOSS ")
    assert result["token_type"] == "Bearer" and result["mfa_required"] is False
    staff = staff_of(db, result["token"])
    assert staff.id == user.id and staff.is_super_admin and staff.has("order:refund")
    sess = db.get(AdminSession, result["session_id"])
    assert sess.token_hash != result["token"] and len(sess.token_hash) == 64  # 只存哈希
    log = db.execute(select(AdminLoginLog).order_by(AdminLoginLog.id.desc())).scalars().first()
    assert log.success and log.reason == "ok" and log.ip == "10.0.0.1"
    assert db.get(AdminUser, user.id).last_login_ip == "10.0.0.1"


def test_failures_are_indistinguishable(db):
    make_user(db, "boss")
    disabled = make_user(db, "gone")
    disabled.status = "disabled"
    db.commit()
    errors = [err(lambda: login(db, "nobody", PASSWORD)), err(lambda: login(db, "boss", "wrong-password-x")),
              err(lambda: login(db, "gone", PASSWORD))]
    assert {(e.code, e.message, str(e.data)) for e in errors} == {
        (ErrorCode.UNAUTHORIZED, "invalid username or password", "{'captcha_required': False}")}
    reasons = [r.reason for r in db.execute(select(AdminLoginLog).order_by(AdminLoginLog.id)).scalars()]
    assert reasons == ["unknown_user", "bad_password", "disabled"]  # 内部日志区分原因


def test_lockout_after_five_failures_then_unlocks(db, clock):
    make_user(db, "boss")
    for i in range(4):
        assert err(lambda: login(db, "boss", "bad-password-x")).code == ErrorCode.UNAUTHORIZED
    locked = err(lambda: login(db, "boss", "bad-password-x"))
    assert locked.code == ErrorCode.RATE_LIMITED and locked.retry_after == 900
    # 锁定期间正确口令也拒绝
    assert err(lambda: login(db, "boss")).code == ErrorCode.RATE_LIMITED
    clock.advance(899)
    assert err(lambda: login(db, "boss")).retry_after == 1
    clock.advance(1)
    assert login(db, "boss")["token"]
    assert db.get(AdminLoginThrottle, "boss").failures == 0
    assert db.execute(select(OutboxEvent).where(OutboxEvent.topic == "staff.locked")).scalars().first() is not None


def test_unknown_username_locks_the_same_way(db):
    for _ in range(4):
        err(lambda: login(db, "ghost", "bad-password-x"))
    assert err(lambda: login(db, "ghost", "bad-password-x")).code == ErrorCode.RATE_LIMITED


def test_failure_window_resets(db, clock):
    make_user(db, "boss")
    for _ in range(4):
        err(lambda: login(db, "boss", "bad-password-x"))
    clock.advance(901)
    for _ in range(4):
        assert err(lambda: login(db, "boss", "bad-password-x")).code == ErrorCode.UNAUTHORIZED


def test_progressive_lock_duration_and_success_resets_level(db, clock):
    make_user(db, "boss")
    for _ in range(5):
        e = err(lambda: login(db, "boss", "bad-password-x"))
    assert e.retry_after == 900
    clock.advance(900)
    for _ in range(5):
        e = err(lambda: login(db, "boss", "bad-password-x"))
    assert e.retry_after == 1800
    clock.advance(1800)
    login(db, "boss")
    for _ in range(5):
        e = err(lambda: login(db, "boss", "bad-password-x"))
    assert e.retry_after == 900


def test_configurable_thresholds(db):
    config.set_config(login_max_failures=3, login_lock_seconds=60)
    make_user(db, "boss")
    err(lambda: login(db, "boss", "bad-password-x"))
    err(lambda: login(db, "boss", "bad-password-x"))
    assert err(lambda: login(db, "boss", "bad-password-x")).retry_after == 60


def test_captcha_required_after_three_failures_when_hook_configured(db):
    make_user(db, "boss")
    config.set_captcha_verifier(lambda token, ip: token == "human")
    for _ in range(2):
        assert err(lambda: login(db, "boss", "bad-password-x")).data == {"captcha_required": False}
    assert err(lambda: login(db, "boss", "bad-password-x")).data == {"captcha_required": True}
    assert err(lambda: login(db, "boss")).data == {"captcha_required": True}  # 没带验证码
    assert err(lambda: auth.login(db, "boss", PASSWORD, captcha_token="robot", user_agent=UA)).data == {"captcha_required": True}
    assert auth.login(db, "boss", PASSWORD, captcha_token="human", user_agent=UA)["token"]


def test_legacy_hash_upgraded_on_login(db):
    user = make_user(db, "boss")
    user.password_hash = framework_hash(PASSWORD)
    db.commit()
    login(db, "boss")
    db.refresh(user)
    assert user.password_hash.startswith("$argon2id$v=19$m=19456,t=2,p=1$")


# ---------- 临时口令与受限会话 ----------

def test_created_staff_must_change_temp_password(db, clock):
    boss = make_user(db, "boss")
    created = service.create_staff(db, actor(db, boss.id), "clerk", "店员", role_ids=[], all_stores=True)
    db.commit()
    result = login(db, "clerk", created["temporary_password"])
    assert result["must_change_password"] is True
    staff = staff_of(db, result["token"])
    assert staff.restricted_reason == REASON_PASSWORD_CHANGE
    # 新口令不满足策略
    assert err(lambda: auth.change_password(db, staff, created["temporary_password"], "short")).code == ErrorCode.BAD_REQUEST
    changed = auth.change_password(db, staff, created["temporary_password"], "a-brand-new-passphrase", user_agent=UA)
    assert changed["must_change_password"] is False
    assert err(lambda: staff_of(db, result["token"])).code == ErrorCode.UNAUTHORIZED  # 旧令牌已轮换
    assert staff_of(db, changed["token"]).restricted_reason is None


def test_temp_password_expires(db, clock):
    boss = make_user(db, "boss")
    created = service.create_staff(db, actor(db, boss.id), "clerk", role_ids=[], all_stores=True)
    db.commit()
    clock.advance(72 * 3600)
    e = err(lambda: login(db, "clerk", created["temporary_password"]))
    assert e.code == ErrorCode.UNAUTHORIZED and e.message == "invalid username or password"


def test_mfa_required_role_forces_enrollment(db, clock):
    make_user(db, "fin", roles=("finance",))
    staff = staff_of(db, token_of(db, "fin"))
    assert staff.mfa_required and staff.restricted_reason == REASON_MFA_ENROLL


def test_mfa_required_by_permission_config(db):
    make_user(db, "manager", roles=("store_manager",))  # 持有 after_sale:*（覆盖 after_sale:approve）
    assert staff_of(db, token_of(db, "manager")).mfa_required
    config.set_config(mfa_required_permissions=())
    assert not staff_of(db, token_of(db, "manager")).mfa_required


# ---------- MFA ----------

def test_mfa_enroll_login_replay_and_drift(db, clock):
    make_user(db, "boss")
    secret, codes, _ = enable_mfa(db, token_of(db, "boss"), clock)
    assert len(codes) == 10
    hashes = db.execute(select(AdminRecoveryCode.code_hash)).scalars().all()
    assert len(hashes) == 10 and all(h.startswith("$argon2id$") for h in hashes)
    clock.advance(30)
    first = login(db, "boss")
    assert first["mfa_required"] is True and "token" not in first
    code = code_at(secret, clock.ts)
    ok = auth.verify_mfa(db, first["mfa_token"], code=code, user_agent=UA)
    assert db.get(AdminSession, ok["session_id"]).amr == "pwd+otp"
    assert err(lambda: auth.verify_mfa(db, first["mfa_token"], code=code)).code == ErrorCode.UNAUTHORIZED  # 挑战已用
    # 同一时间步的码再次登录：重放拒绝
    second = login(db, "boss")
    e = err(lambda: auth.verify_mfa(db, second["mfa_token"], code=code))
    assert e.code == ErrorCode.UNAUTHORIZED and e.message == "invalid verification code"
    assert db.execute(select(AdminLoginLog.reason).order_by(AdminLoginLog.id.desc())).scalars().first() == "replay"
    # 下一时间步：允许 −1 步漂移（客户端慢 30 秒），但 −1 步就是已用过的步 → 等新步
    clock.advance(30)
    assert auth.verify_mfa(db, second["mfa_token"], code=code_at(secret, clock.ts - 0), user_agent=UA)["token"]
    clock.advance(30)
    third = login(db, "boss")
    late = code_at(secret, clock.ts + 30)  # 客户端快一步
    assert auth.verify_mfa(db, third["mfa_token"], code=late, user_agent=UA)["token"]
    clock.advance(120)
    fourth = login(db, "boss")
    assert err(lambda: auth.verify_mfa(db, fourth["mfa_token"], code=code_at(secret, clock.ts - 60))).code == ErrorCode.UNAUTHORIZED


def test_mfa_challenge_expires(db, clock):
    make_user(db, "boss")
    secret, _, _ = enable_mfa(db, token_of(db, "boss"), clock)
    clock.advance(30)
    pending = login(db, "boss")
    clock.advance(301)
    assert err(lambda: auth.verify_mfa(db, pending["mfa_token"], code=code_at(secret, clock.ts))).code == ErrorCode.UNAUTHORIZED


def test_mfa_failures_count_towards_lock(db, clock):
    make_user(db, "boss")
    enable_mfa(db, token_of(db, "boss"), clock)
    pending = login(db, "boss")
    for _ in range(4):
        assert err(lambda: auth.verify_mfa(db, pending["mfa_token"], code="000000")).code == ErrorCode.UNAUTHORIZED
    assert err(lambda: auth.verify_mfa(db, pending["mfa_token"], code="000000")).code == ErrorCode.RATE_LIMITED
    assert err(lambda: login(db, "boss")).code == ErrorCode.RATE_LIMITED


def test_recovery_codes_single_use(db, clock):
    make_user(db, "boss")
    _, codes, _ = enable_mfa(db, token_of(db, "boss"), clock)
    pending = login(db, "boss")
    ok = auth.verify_mfa(db, pending["mfa_token"], recovery_code=codes[0].lower(), user_agent=UA)
    assert ok["recovery_codes_remaining"] == 9
    assert db.get(AdminSession, ok["session_id"]).amr == "pwd+recovery"
    again = login(db, "boss")
    assert err(lambda: auth.verify_mfa(db, again["mfa_token"], recovery_code=codes[0])).code == ErrorCode.UNAUTHORIZED
    assert auth.verify_mfa(db, again["mfa_token"], recovery_code=codes[1], user_agent=UA)["recovery_codes_remaining"] == 8
    assert db.execute(select(OutboxEvent).where(OutboxEvent.topic == "staff.recovery_code_used")).scalars().first()


def test_regenerate_recovery_codes_invalidates_old(db, clock):
    make_user(db, "boss")
    _, codes, token = enable_mfa(db, token_of(db, "boss"), clock)
    fresh = auth.regenerate_recovery_codes(db, staff_of(db, token))
    assert set(fresh).isdisjoint(codes)
    pending = login(db, "boss")
    assert err(lambda: auth.verify_mfa(db, pending["mfa_token"], recovery_code=codes[0])).code == ErrorCode.UNAUTHORIZED
    assert auth.verify_mfa(db, pending["mfa_token"], recovery_code=fresh[0], user_agent=UA)["token"]


def test_enrollment_requires_valid_code_and_no_double_enroll(db, clock):
    make_user(db, "boss")
    token = token_of(db, "boss")
    staff = staff_of(db, token)
    assert err(lambda: auth.confirm_mfa_enrollment(db, staff, "123456")).code == ErrorCode.CONFLICT  # 未开始
    auth.start_mfa_enrollment(db, staff)
    assert err(lambda: auth.confirm_mfa_enrollment(db, staff, "000000")).code == ErrorCode.BAD_REQUEST
    secret = auth.start_mfa_enrollment(db, staff)["secret"]
    result = auth.confirm_mfa_enrollment(db, staff, code_at(secret, clock.ts), user_agent=UA)
    staff2 = staff_of(db, result["token"])
    assert staff2.mfa_enabled
    assert err(lambda: auth.start_mfa_enrollment(db, staff2)).code == ErrorCode.CONFLICT


def test_disable_own_mfa_blocked_when_role_requires(db, clock):
    make_user(db, "boss")  # 超级管理员角色要求 MFA
    secret, _, token = enable_mfa(db, token_of(db, "boss"), clock)
    clock.advance(30)
    assert err(lambda: auth.disable_own_mfa(db, staff_of(db, token), code_at(secret, clock.ts))).code == ErrorCode.BUSINESS_RULE


def test_disable_own_mfa_allowed_for_optional_role(db, clock):
    make_user(db, "op", roles=("operator",))
    secret, _, token = enable_mfa(db, token_of(db, "op"), clock)
    clock.advance(30)
    assert auth.disable_own_mfa(db, staff_of(db, token), code_at(secret, clock.ts)) == {"mfa_enabled": False}
    assert login(db, "op")["mfa_required"] is False


def test_reset_mfa_requires_another_admin_and_reason(db, clock):
    a = make_user(db, "boss")
    b = make_user(db, "boss2")
    _, _, token_b = enable_mfa(db, token_of(db, "boss2"), clock)
    assert err(lambda: service.reset_mfa(db, actor(db, b.id), b.id, "lost phone")).code == ErrorCode.FORBIDDEN
    assert err(lambda: service.reset_mfa(db, actor(db, a.id), b.id, "")).code == ErrorCode.BAD_REQUEST
    service.reset_mfa(db, actor(db, a.id), b.id, "lost phone")
    db.commit()
    assert err(lambda: staff_of(db, token_b)).code == ErrorCode.UNAUTHORIZED
    assert not db.get(AdminUser, b.id).mfa_enabled
    assert db.execute(select(AdminRecoveryCode).where(AdminRecoveryCode.staff_id == b.id)).first() is None
    assert login(db, "boss2")["mfa_required"] is False


# ---------- 会话 ----------

def test_idle_timeout(db, clock):
    make_user(db, "boss")
    token = token_of(db, "boss")
    clock.advance(600)
    staff_of(db, token)  # 有活动，刷新 last_seen
    clock.advance(899)
    staff_of(db, token)
    clock.advance(900)
    e = err(lambda: staff_of(db, token))
    assert e.code == ErrorCode.UNAUTHORIZED and e.data == {"reason": "idle_timeout"}
    assert err(lambda: staff_of(db, token)).data is None  # 已撤销


def test_absolute_timeout_despite_activity(db, clock):
    make_user(db, "boss")
    token = token_of(db, "boss")
    for _ in range(47):  # 47 × 10 分钟 = 7 小时 50 分
        clock.advance(600)
        staff_of(db, token)
    clock.advance(600)
    assert err(lambda: staff_of(db, token)).data == {"reason": "absolute_timeout"}


def test_user_agent_binding(db):
    make_user(db, "boss")
    token = token_of(db, "boss")
    assert err(lambda: staff_of(db, token, ua="curl/8")).data == {"reason": "ua_mismatch"}
    assert err(lambda: staff_of(db, token)).code == ErrorCode.UNAUTHORIZED


def test_logout_revokes(db):
    make_user(db, "boss")
    token = token_of(db, "boss")
    auth.logout(db, staff_of(db, token))
    assert err(lambda: staff_of(db, token)).code == ErrorCode.UNAUTHORIZED


def test_password_change_revokes_other_sessions(db):
    make_user(db, "boss")
    t1, t2 = token_of(db, "boss"), token_of(db, "boss")
    result = auth.change_password(db, staff_of(db, t1), PASSWORD, "another-good-passphrase", user_agent=UA)
    for t in (t1, t2):
        assert err(lambda: staff_of(db, t)).code == ErrorCode.UNAUTHORIZED
    assert staff_of(db, result["token"])
    e = err(lambda: auth.change_password(db, staff_of(db, result["token"]), "wrong-old-pass", "yet-another-passphrase"))
    assert e.code == ErrorCode.BAD_REQUEST and e.data["errors"][0]["field"] == "old_password"
    e = err(lambda: auth.change_password(db, staff_of(db, result["token"]), "another-good-passphrase", "another-good-passphrase"))
    assert e.code == ErrorCode.BAD_REQUEST


def test_disable_and_role_change_revoke_sessions(db):
    a = make_user(db, "boss")
    b = make_user(db, "clerk", roles=("front_desk",))
    c = make_user(db, "clerk2", roles=("front_desk",))
    tb, tc = token_of(db, "clerk"), token_of(db, "clerk2")
    service.disable_staff(db, actor(db, a.id), b.id)
    service.set_staff_access(db, actor(db, a.id), c.id, role_ids=[], all_stores=True)
    db.commit()
    assert err(lambda: staff_of(db, tb)).code == ErrorCode.UNAUTHORIZED
    assert err(lambda: staff_of(db, tc)).code == ErrorCode.UNAUTHORIZED


def test_list_and_revoke_own_sessions_and_limit(db):
    make_user(db, "boss")
    tokens = [token_of(db, "boss") for _ in range(3)]
    me = staff_of(db, tokens[-1])
    sessions = auth.list_sessions(db, me.id, me.session_id)
    assert len(sessions) == 3 and sum(s["current"] for s in sessions) == 1
    auth.revoke_own_session(db, me, sessions[-1]["id"])
    assert err(lambda: staff_of(db, tokens[0])).code == ErrorCode.UNAUTHORIZED
    assert err(lambda: auth.revoke_own_session(db, me, 999999)).code == ErrorCode.NOT_FOUND
    assert auth.revoke_other_sessions(db, me) == 1
    assert len(auth.list_sessions(db, me.id)) == 1
    many = [token_of(db, "boss") for _ in range(6)]  # 上限 5：最早的被挤掉
    assert err(lambda: staff_of(db, tokens[-1])).code == ErrorCode.UNAUTHORIZED
    assert staff_of(db, many[-1])


# ---------- 新鲜认证 ----------

def test_step_up_reauth(db, clock):
    make_user(db, "op", roles=("operator",))
    token = token_of(db, "op")
    assert_fresh(staff_of(db, token))
    clock.advance(301)
    e = err(lambda: assert_fresh(staff_of(db, token)))
    assert e.code == ErrorCode.MFA_REQUIRED and e.data["reason"] == "reauth_required"
    assert err(lambda: auth.reauthenticate(db, staff_of(db, token), "wrong-password")).code == ErrorCode.UNAUTHORIZED
    result = auth.reauthenticate(db, staff_of(db, token), PASSWORD, user_agent=UA)
    assert err(lambda: staff_of(db, token)).code == ErrorCode.UNAUTHORIZED  # 轮换
    assert_fresh(staff_of(db, result["token"]))


def test_step_up_requires_second_factor_when_enrolled(db, clock):
    make_user(db, "boss")
    secret, _, token = enable_mfa(db, token_of(db, "boss"), clock)
    clock.advance(30)
    assert err(lambda: auth.reauthenticate(db, staff_of(db, token), PASSWORD)).code == ErrorCode.BAD_REQUEST
    result = auth.reauthenticate(db, staff_of(db, token), PASSWORD, code=code_at(secret, clock.ts), user_agent=UA)
    assert db.get(AdminSession, result["session_id"]).amr == "pwd+otp"


def test_me_payload(db, clock):
    make_user(db, "manager", roles=("store_manager",), all_stores=False, store_ids=(3, 1))
    staff = staff_of(db, token_of(db, "manager"))
    info = auth.me(db, staff)
    assert info["store_scope"] == [1, 3] and info["all_stores"] is False
    assert "order:*" in info["permissions"] and "staff:write" not in info["permissions"]
    assert staff.has("order:close") and staff.has("after_sale:approve") and not staff.has("wallet:adjust")
    assert info["roles"][0]["builtin_code"] == "store_manager"
    assert info["session"]["idle_timeout_seconds"] == 900


# ---------- 兼容层（其他模块的旧适配器）----------

def test_legacy_adapter_flow_enforces_server_side_session(db, clock):
    from admin_rbac import assert_permission, decode_access, grants_of, load_active_staff, store_scope
    from luzback import decode_token, get_settings

    make_user(db, "manager", roles=("store_manager",), all_stores=False, store_ids=(2,))
    config.set_config(mfa_required_permissions=())
    token = token_of(db, "manager")
    s = get_settings()
    claims = decode_token(token, s.jwt_secret, kind="admin", issuer=s.jwt_issuer)  # 旧适配器的第一步
    assert decode_access(token)["sid"] == claims["rv"]
    staff = load_active_staff(db, int(claims["sub"]), claims.get("rv"))
    assert_permission(db, staff, "order:close")  # 旧签名
    assert err(lambda: assert_permission(db, staff, "staff:write")).code == ErrorCode.FORBIDDEN
    assert grants_of(db, staff)[1] == [2] and store_scope(staff) == [2]
    auth.logout(db, staff_of(db, token))
    assert err(lambda: load_active_staff(db, int(claims["sub"]), claims["rv"])).code == ErrorCode.UNAUTHORIZED
    other = token_of(db, "manager")
    c2 = decode_access(other)
    clock.advance(901)
    e = err(lambda: load_active_staff(db, int(c2["sub"]), c2["rv"]))
    assert e.code == ErrorCode.UNAUTHORIZED and e.data == {"reason": "idle_timeout"}
    assert err(lambda: load_active_staff(db, 999, c2["rv"])).code == ErrorCode.UNAUTHORIZED  # sub 与会话不符


def test_legacy_adapter_rejects_restricted_and_mfa_tokens(db, clock):
    from admin_rbac import decode_access, load_active_staff

    make_user(db, "fin", roles=("finance",))
    claims = decode_access(token_of(db, "fin"))
    e = err(lambda: load_active_staff(db, int(claims["sub"]), claims["rv"]))
    assert e.code == ErrorCode.MFA_REQUIRED and e.data["reason"] == "mfa_enrollment_required"
    make_user(db, "boss")
    enable_mfa(db, token_of(db, "boss"), clock)
    pending = login(db, "boss")
    assert err(lambda: decode_access(pending["mfa_token"])).code == ErrorCode.UNAUTHORIZED  # kind 不同
    assert err(lambda: staff_of(db, pending["mfa_token"])).code == ErrorCode.UNAUTHORIZED


def test_forged_or_mismatched_token_rejected(db):
    from luzback import create_token, get_settings

    make_user(db, "boss")
    good = token_of(db, "boss")
    claims = auth.decode_access(good)
    s = get_settings()
    forged = create_token(claims["sub"], s.jwt_secret, 3600, kind="admin", issuer=s.jwt_issuer,
                          extra={"sid": claims["sid"], "rv": claims["rv"]})  # 同会话 id 但不是签发的那张令牌
    assert err(lambda: staff_of(db, forged)).code == ErrorCode.UNAUTHORIZED
    assert err(lambda: staff_of(db, good[:-2] + "xx")).code == ErrorCode.UNAUTHORIZED
