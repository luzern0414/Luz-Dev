"""后台认证用例：登录（口令 + TOTP / 恢复码）、服务端会话、新鲜认证、改密、MFA 绑定与恢复码。

规则摘要（编号见 MODULE.md §4）：
- 账号不存在 / 口令错误 / 停用 / 临时口令过期 → 同一提示 4010 "invalid username or password"；账号不存在时也跑一次哈希。
- 节流按规范化账号名（不论账号是否存在）：窗口内失败达到阈值 → 锁定（4290 + Retry-After），锁定时长逐次翻倍；
  配置了人机验证时，失败达到 captcha_after_failures 次后要求 captcha_token。
- MFA 账号：口令通过后签发 5 分钟 mfa_token（mfa_pending 会话），/auth/mfa 校验 TOTP（±1 步，拒绝重放）或恢复码后
  换发正式会话（会话令牌轮换）。
- 会话：签名 JWT（kind=admin，sid=会话 id）+ 服务端会话行（只存令牌 SHA-256）；每请求校验会话行，
  空闲超时 + 绝对超时服务端强制；登出 / 改密 / 停用 / 角色变更撤销立即生效。
- 新鲜认证：auth_at 在 fresh_auth_seconds 内；/auth/reauth 重新验证口令（+ MFA）后轮换会话。
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import case, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from luzback import AppError, ErrorCode, create_token, decode_token, enqueue_event, get_settings
from luzback import log_events as ev
from luzback.logging import log_event
from luzback.crypto import FieldCipher

from . import config
from .access import Staff, audit, build_staff
from .models import (
    AMR_OTP, AMR_PASSWORD, AMR_RECOVERY, STAGE_ACTIVE, STAGE_MFA_PENDING, STATUS_ACTIVE, TOKEN_KIND_ACCESS,
    TOKEN_KIND_MFA, AdminLoginLog,
    AdminLoginThrottle, AdminRecoveryCode, AdminRole, AdminSession, AdminUser, AdminUserRole,
)
from .passwords import check_policy, dummy_verify, hash_secret, needs_rehash, verify_secret
from .rules import aware, lock_seconds, normalize_username, session_expiry_reason
from .totp import match_step, new_recovery_codes, new_secret, normalize_recovery_code, otpauth_uri

log = logging.getLogger("admin_rbac.auth")

GENERIC_LOGIN_ERROR = "invalid username or password"
LOCKED_ERROR = "too many failed attempts, try again later"


def cipher() -> FieldCipher:
    return FieldCipher(get_settings().field_encryption_key)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _ua(user_agent: str | None) -> str | None:
    return (user_agent or "")[:200] or None


# ---------- 登录日志 ----------

def _log(db: Session, user: AdminUser | None, username: str, factor: str, success: bool, reason: str,
         ip: str | None, user_agent: str | None, at: datetime) -> None:
    """登录日志表（后台可查）+ 失败与锁定的安全事件（security.*，单独成流告警；不记用户名与口令）。"""
    db.add(AdminLoginLog(user_id=user.id if user else None, username=username[:64], factor=factor, success=success,
                         reason=reason, ip=ip, user_agent=(user_agent or "")[:500] or None, created_at=at))
    if not success:
        event = ev.SECURITY_AUTHN_LOGIN_LOCK if reason == "locked" else ev.SECURITY_AUTHN_LOGIN_FAIL
        log_event(log, event, logging.WARNING, outcome="failure", reason=reason, factor=factor, actor="staff",
                  staff_ref=f"staff:{user.id}" if user else None, source_ip=ip)


# ---------- 节流 ----------

def throttle_status(db: Session, username: str, at: datetime) -> tuple[int, int]:
    """返回 (剩余锁定秒数, 窗口内失败次数)。"""
    row = db.get(AdminLoginThrottle, username)
    if row is None:
        return 0, 0
    db.refresh(row)
    locked_until = aware(row.locked_until)
    if locked_until is not None and locked_until > at:
        return max(1, int((locked_until - at).total_seconds() + 0.999)), row.failures
    if at - aware(row.window_started_at) >= timedelta(seconds=config.get_config().login_failure_window_seconds):
        return 0, 0
    return 0, row.failures


def _ensure_throttle_row(db: Session, username: str, at: datetime) -> None:
    """并发安全地建行：INSERT … ON CONFLICT DO NOTHING（SQLite / Postgres）；其他方言退化为查后插。"""
    values = {"username": username, "failures": 0, "window_started_at": at, "lock_count": 0}
    dialect = db.get_bind().dialect.name
    if dialect in ("sqlite", "postgresql"):
        if dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert as dialect_insert
        else:
            from sqlalchemy.dialects.postgresql import insert as dialect_insert
        db.execute(dialect_insert(AdminLoginThrottle).values(**values).on_conflict_do_nothing(index_elements=["username"]))
        return
    if db.get(AdminLoginThrottle, username) is None:  # pragma: no cover - 其他数据库
        db.add(AdminLoginThrottle(**values))
        try:
            db.flush()
        except IntegrityError:
            db.rollback()


def register_failure(db: Session, username: str, at: datetime) -> int:
    """失败 +1（条件 UPDATE）；达到阈值即锁定并清零计数。返回本次触发的锁定秒数（0 = 未锁）。"""
    cfg = config.get_config()
    _ensure_throttle_row(db, username, at)
    cutoff = at - timedelta(seconds=cfg.login_failure_window_seconds)
    decay = at - timedelta(seconds=cfg.login_lock_max_seconds)
    T = AdminLoginThrottle
    not_locked = or_(T.locked_until.is_(None), T.locked_until <= at)
    # 窗口过期：重新计数；长时间无失败则锁定级别归零
    db.execute(update(T).where(T.username == username, T.window_started_at <= cutoff, not_locked).values(
        failures=0, window_started_at=at,
        lock_count=case((or_(T.last_failure_at.is_(None), T.last_failure_at <= decay), 0), else_=T.lock_count)))
    db.execute(update(T).where(T.username == username).values(failures=T.failures + 1, last_failure_at=at))
    row = db.execute(select(T.failures, T.lock_count).where(T.username == username)).one()
    if row.failures < cfg.login_max_failures:
        return 0
    seconds = lock_seconds(row.lock_count + 1, cfg.login_lock_seconds, cfg.login_lock_max_seconds)
    locked = db.execute(update(T).where(T.username == username, T.lock_count == row.lock_count, T.failures >= cfg.login_max_failures,
                                        not_locked).values(
        locked_until=at + timedelta(seconds=seconds), lock_count=T.lock_count + 1, failures=0, window_started_at=at)).rowcount
    return seconds if locked == 1 else 0


def clear_throttle(db: Session, username: str, at: datetime) -> None:
    db.execute(update(AdminLoginThrottle).where(AdminLoginThrottle.username == username).values(
        failures=0, lock_count=0, locked_until=None, window_started_at=at))


def _locked(seconds: int) -> AppError:
    return AppError(ErrorCode.RATE_LIMITED, LOCKED_ERROR, data={"retry_after": seconds}, retry_after=seconds)


def _fail(db: Session, user: AdminUser | None, username: str, factor: str, reason: str, ip: str | None,
          user_agent: str | None, at: datetime, message: str) -> AppError:
    cfg = config.get_config()
    seconds = register_failure(db, username, at)
    _log(db, user, username, factor, False, "locked" if seconds else reason, ip, user_agent, at)
    if seconds and user is not None:
        enqueue_event(db, "staff.locked", {"staff_id": user.id, "seconds": seconds}, "admin_user", user.id)
    db.commit()
    if seconds:
        return _locked(seconds)
    _, failures = throttle_status(db, username, at)
    captcha = config.get_captcha_verifier() is not None and failures >= cfg.captcha_after_failures
    return AppError(ErrorCode.UNAUTHORIZED, message, data={"captcha_required": captcha})


# ---------- 会话 ----------

def _create_session(db: Session, user: AdminUser, stage: str, amr: str, at: datetime, ip: str | None,
                    user_agent: str | None, ttl_seconds: int | None = None) -> tuple[str, AdminSession]:
    cfg = config.get_config()
    ttl = ttl_seconds or cfg.session_absolute_seconds
    sess = AdminSession(staff_id=user.id, token_hash=secrets.token_hex(32), stage=stage, amr=amr, created_at=at, auth_at=at,
                        last_seen_at=at, expires_at=at + timedelta(seconds=ttl), ip=ip, user_agent=_ua(user_agent))
    db.add(sess)
    db.flush()
    s = get_settings()
    kind = TOKEN_KIND_ACCESS if stage == STAGE_ACTIVE else TOKEN_KIND_MFA
    token = create_token(str(user.id), s.jwt_secret, ttl, kind=kind, issuer=s.jwt_issuer,
                         extra={"sid": sess.id, "rv": sess.id, "role": "staff"})
    sess.token_hash = hash_token(token)
    db.flush()
    if stage == STAGE_ACTIVE:
        active = db.execute(select(AdminSession.id).where(
            AdminSession.staff_id == user.id, AdminSession.stage == STAGE_ACTIVE, AdminSession.revoked_at.is_(None),
            AdminSession.expires_at > at).order_by(AdminSession.created_at.desc(), AdminSession.id.desc())).scalars().all()
        overflow = active[cfg.max_sessions_per_staff:]
        if overflow:
            db.execute(update(AdminSession).where(AdminSession.id.in_(overflow), AdminSession.revoked_at.is_(None))
                       .values(revoked_at=at, revoke_reason="session_limit"))
    return token, sess


def revoke_sessions(db: Session, staff_id: int, reason: str, except_session_id: int | None = None,
                    at: datetime | None = None) -> int:
    """撤销员工的全部（或除当前外的）会话；不 commit。"""
    stmt = update(AdminSession).where(AdminSession.staff_id == staff_id, AdminSession.revoked_at.is_(None))
    if except_session_id is not None:
        stmt = stmt.where(AdminSession.id != except_session_id)
    return db.execute(stmt.values(revoked_at=at or config.now(), revoke_reason=reason[:32])).rowcount


def _revoke_one(db: Session, session_id: int, reason: str, at: datetime) -> int:
    return db.execute(update(AdminSession).where(AdminSession.id == session_id, AdminSession.revoked_at.is_(None))
                      .values(revoked_at=at, revoke_reason=reason)).rowcount


def _payload(user: AdminUser, token: str, sess: AdminSession, staff: Staff | None = None) -> dict:
    cfg = config.get_config()
    return {
        "token": token, "token_type": "Bearer", "session_id": sess.id, "staff_id": user.id,
        "expires_at": aware(sess.expires_at), "idle_timeout_seconds": cfg.session_idle_seconds,
        "mfa_required": False, "must_change_password": bool(user.must_change_password),
        "mfa_enrollment_required": bool(staff and staff.mfa_required and not staff.mfa_enabled),
    }


def _mark_login(db: Session, user: AdminUser, at: datetime, ip: str | None, user_agent: str | None) -> None:
    db.execute(update(AdminUser).where(AdminUser.id == user.id).values(last_login_at=at, last_login_ip=ip,
                                                                         last_login_ua=_ua(user_agent)))


def _rotate(db: Session, user: AdminUser, old: AdminSession, amr: str, at: datetime, ip: str | None,
            user_agent: str | None, reason: str = "rotated") -> tuple[str, AdminSession]:
    """认证状态变化（MFA 通过、重新认证、改密、绑定 MFA）后换发会话令牌（ASVS 7.2.4）。"""
    _revoke_one(db, old.id, reason, at)
    return _create_session(db, user, STAGE_ACTIVE, amr, at, ip, user_agent)


# ---------- 登录 ----------

def login(db: Session, username: str, password: str, ip: str | None = None, user_agent: str | None = None,
          captcha_token: str | None = None) -> dict:
    at = config.now()
    cfg = config.get_config()
    uname = normalize_username(username)
    locked, failures = throttle_status(db, uname, at)
    if locked:
        _log(db, None, uname, "password", False, "locked", ip, user_agent, at)
        db.commit()
        raise _locked(locked)
    verifier = config.get_captcha_verifier()
    if verifier is not None and failures >= cfg.captcha_after_failures:
        if not captcha_token or not verifier(captcha_token, ip):
            _log(db, None, uname, "password", False, "captcha", ip, user_agent, at)
            db.commit()
            raise AppError(ErrorCode.UNAUTHORIZED, GENERIC_LOGIN_ERROR, data={"captcha_required": True})
    user = db.execute(select(AdminUser).where(AdminUser.username == uname, AdminUser.deleted_at.is_(None))).scalar_one_or_none()
    if user is None:
        dummy_verify(password or "")
        raise _fail(db, None, uname, "password", "unknown_user", ip, user_agent, at, GENERIC_LOGIN_ERROR)
    if not verify_secret(password or "", user.password_hash):
        raise _fail(db, user, uname, "password", "bad_password", ip, user_agent, at, GENERIC_LOGIN_ERROR)
    if user.status != STATUS_ACTIVE:
        raise _fail(db, user, uname, "password", "disabled", ip, user_agent, at, GENERIC_LOGIN_ERROR)
    temp_exp = aware(user.temp_password_expires_at)
    if user.must_change_password and temp_exp is not None and temp_exp <= at:
        raise _fail(db, user, uname, "password", "temp_expired", ip, user_agent, at, GENERIC_LOGIN_ERROR)
    if needs_rehash(user.password_hash):
        user.password_hash = hash_secret(password)
    if user.mfa_enabled:
        token, _ = _create_session(db, user, STAGE_MFA_PENDING, AMR_PASSWORD, at, ip, user_agent, cfg.mfa_challenge_seconds)
        _log(db, user, uname, "password", True, "mfa_pending", ip, user_agent, at)
        db.commit()
        return {"mfa_required": True, "mfa_token": token, "expires_in": cfg.mfa_challenge_seconds, "staff_id": user.id}
    clear_throttle(db, uname, at)
    token, sess = _create_session(db, user, STAGE_ACTIVE, AMR_PASSWORD, at, ip, user_agent)
    _mark_login(db, user, at, ip, user_agent)
    _log(db, user, uname, "password", True, "ok", ip, user_agent, at)
    db.commit()
    db.refresh(user)
    return _payload(user, token, sess, build_staff(db, user))


def _check_second_factor(db: Session, user: AdminUser, code: str | None, recovery_code: str | None,
                         at: datetime) -> tuple[bool, str, str]:
    """返回 (是否通过, factor, 失败原因)。TOTP 成功时条件 UPDATE mfa_last_step，防并发重放。"""
    cfg = config.get_config()
    if code:
        if not user.mfa_secret_enc:
            return False, "totp", "bad_otp"
        secret = cipher().decrypt(user.mfa_secret_enc)
        step = match_step(secret, code, at.timestamp(), cfg.totp_drift_steps, last_step=user.mfa_last_step)
        if step is None:
            replay = match_step(secret, code, at.timestamp(), cfg.totp_drift_steps) is not None
            return False, "totp", "replay" if replay else "bad_otp"
        rows = db.execute(update(AdminUser).where(
            AdminUser.id == user.id, or_(AdminUser.mfa_last_step.is_(None), AdminUser.mfa_last_step < step))
            .values(mfa_last_step=step)).rowcount
        return rows == 1, "totp", "ok" if rows == 1 else "replay"
    if recovery_code:
        return consume_recovery_code(db, user.id, recovery_code, at), "recovery", "bad_recovery"
    raise AppError(ErrorCode.BAD_REQUEST, "code or recovery_code is required",
                   data={"errors": [{"field": "code", "message": "required"}]})


def verify_mfa(db: Session, mfa_token: str, code: str | None = None, recovery_code: str | None = None,
               ip: str | None = None, user_agent: str | None = None) -> dict:
    at = config.now()
    pending = _session_for_token(db, mfa_token, TOKEN_KIND_MFA)
    if (pending is None or pending.stage != STAGE_MFA_PENDING or pending.revoked_at is not None
            or aware(pending.expires_at) <= at):
        raise AppError(ErrorCode.UNAUTHORIZED, "verification expired, please sign in again")
    user = db.get(AdminUser, pending.staff_id)
    if user is None or user.deleted_at is not None or user.status != STATUS_ACTIVE or not user.mfa_enabled:
        _revoke_one(db, pending.id, "staff_inactive", at)
        db.commit()
        raise AppError(ErrorCode.UNAUTHORIZED, "verification expired, please sign in again")
    locked, _ = throttle_status(db, user.username, at)
    if locked:
        _log(db, user, user.username, "totp", False, "locked", ip, user_agent, at)
        db.commit()
        raise _locked(locked)
    ok, factor, reason = _check_second_factor(db, user, code, recovery_code, at)
    if not ok:
        raise _fail(db, user, user.username, factor, reason, ip, user_agent, at, "invalid verification code")
    clear_throttle(db, user.username, at)
    token, sess = _rotate(db, user, pending, AMR_RECOVERY if factor == "recovery" else AMR_OTP, at, ip, user_agent,
                          reason="mfa_upgraded")
    _mark_login(db, user, at, ip, user_agent)
    _log(db, user, user.username, factor, True, "ok", ip, user_agent, at)
    result_extra: dict[str, Any] = {}
    if factor == "recovery":
        remaining = recovery_codes_remaining(db, user.id)
        enqueue_event(db, "staff.recovery_code_used", {"staff_id": user.id, "remaining": remaining}, "admin_user", user.id)
        result_extra["recovery_codes_remaining"] = remaining
    db.commit()
    db.refresh(user)
    return {**_payload(user, token, sess, build_staff(db, user)), **result_extra}


# ---------- 会话校验（每请求）----------

def decode_access(token: str) -> dict:
    """校验后台会话令牌（JWT，kind=admin）签名与过期，返回声明；会话是否有效还需 load_active_staff / authenticate。"""
    s = get_settings()
    return decode_token(token, s.jwt_secret, kind=TOKEN_KIND_ACCESS, issuer=s.jwt_issuer)


def _session_for_token(db: Session, token: str | None, kind: str) -> AdminSession | None:
    if not token:
        return None
    s = get_settings()
    try:
        claims = decode_token(token, s.jwt_secret, kind=kind, issuer=s.jwt_issuer)
        sess = db.get(AdminSession, int(claims["sid"]))
    except (AppError, KeyError, TypeError, ValueError):
        return None
    if sess is None or sess.token_hash != hash_token(token) or str(sess.staff_id) != str(claims.get("sub")):
        return None
    return sess


def validate_session(db: Session, sess: AdminSession | None, user_agent: str | None = None, check_ua: bool = False,
                     staff_id: int | None = None) -> AdminUser:
    """会话有效性（服务端强制）：撤销 / 阶段 / 绝对与空闲超时 / UA / 员工在职；失效即撤销并 4010。活动时刷新 last_seen。"""
    at = config.now()
    cfg = config.get_config()
    if (sess is None or sess.revoked_at is not None or sess.stage != STAGE_ACTIVE
            or (staff_id is not None and sess.staff_id != staff_id)):
        raise AppError(ErrorCode.UNAUTHORIZED, "session expired, please sign in again")
    reason = session_expiry_reason(at, sess.last_seen_at, sess.expires_at, cfg.session_idle_seconds)
    if reason is None and check_ua and cfg.session_bind_user_agent and sess.user_agent is not None             and _ua(user_agent) != sess.user_agent:
        reason = "ua_mismatch"
    user = db.get(AdminUser, sess.staff_id) if reason is None else None
    if reason is None and (user is None or user.deleted_at is not None or user.status != STATUS_ACTIVE):
        reason = "staff_inactive"
    if reason is not None:
        _revoke_one(db, sess.id, reason, at)
        db.commit()
        raise AppError(ErrorCode.UNAUTHORIZED, "session expired, please sign in again", data={"reason": reason})
    if at - aware(sess.last_seen_at) >= timedelta(seconds=cfg.session_touch_seconds):
        db.execute(update(AdminSession).where(AdminSession.id == sess.id, AdminSession.revoked_at.is_(None))
                   .values(last_seen_at=at))
        db.commit()
    return user


def authenticate(db: Session, token: str | None, ip: str | None = None, user_agent: str | None = None,
                 trace_id: str | None = None) -> Staff:
    """Bearer 会话令牌 → Staff。签名 / 过期 / 会话撤销 / 超时 / UA 不符 / 员工停用 → 4010。"""
    if not token:
        raise AppError(ErrorCode.UNAUTHORIZED, "missing bearer token")
    sess = _session_for_token(db, token, TOKEN_KIND_ACCESS)
    user = validate_session(db, sess, user_agent=user_agent, check_ua=True)
    return build_staff(db, user, session_id=sess.id, auth_at=sess.auth_at, amr=sess.amr, ip=ip, user_agent=user_agent,
                       trace_id=trace_id)


def logout(db: Session, staff: Staff) -> None:
    if staff.session_id is not None:
        _revoke_one(db, staff.session_id, "logout", config.now())
    audit(db, staff, "staff.logout", "staff", staff.id)
    db.commit()


def _session_dict(sess: AdminSession, current_id: int | None) -> dict:
    return {"id": sess.id, "amr": sess.amr, "created_at": aware(sess.created_at), "auth_at": aware(sess.auth_at),
            "last_seen_at": aware(sess.last_seen_at), "expires_at": aware(sess.expires_at), "ip": sess.ip,
            "user_agent": sess.user_agent, "current": sess.id == current_id}


def list_sessions(db: Session, staff_id: int, current_session_id: int | None = None) -> list[dict]:
    """有效会话（未撤销、未超时）。"""
    at = config.now()
    cfg = config.get_config()
    rows = db.execute(select(AdminSession).where(
        AdminSession.staff_id == staff_id, AdminSession.stage == STAGE_ACTIVE, AdminSession.revoked_at.is_(None),
        AdminSession.expires_at > at).order_by(AdminSession.id.desc())).scalars().all()
    return [_session_dict(s, current_session_id) for s in rows
            if session_expiry_reason(at, s.last_seen_at, s.expires_at, cfg.session_idle_seconds) is None]


def revoke_own_session(db: Session, staff: Staff, session_id: int) -> None:
    rows = db.execute(update(AdminSession).where(AdminSession.id == session_id, AdminSession.staff_id == staff.id,
                                                 AdminSession.revoked_at.is_(None))
                      .values(revoked_at=config.now(), revoke_reason="user_revoked")).rowcount
    if rows != 1:
        db.rollback()
        raise AppError(ErrorCode.NOT_FOUND, "session not found")
    audit(db, staff, "staff.session.revoke", "admin_session", session_id)
    db.commit()


def revoke_other_sessions(db: Session, staff: Staff) -> int:
    count = revoke_sessions(db, staff.id, "user_revoked", except_session_id=staff.session_id)
    audit(db, staff, "staff.session.revoke_others", "staff", staff.id, after={"revoked": count})
    db.commit()
    return count


# ---------- 新鲜认证（step-up）----------

def _current(db: Session, staff: Staff) -> tuple[AdminUser, AdminSession]:
    user = db.get(AdminUser, staff.id)
    sess = db.get(AdminSession, staff.session_id) if staff.session_id else None
    if user is None or sess is None or sess.revoked_at is not None:
        raise AppError(ErrorCode.UNAUTHORIZED, "session expired, please sign in again")
    return user, sess


def reauthenticate(db: Session, staff: Staff, password: str, code: str | None = None, recovery_code: str | None = None,
                   ip: str | None = None, user_agent: str | None = None) -> dict:
    """重新验证口令（已绑定 MFA 时还要 TOTP / 恢复码）；成功后轮换会话并刷新 auth_at。失败计入节流。"""
    at = config.now()
    user, sess = _current(db, staff)
    locked, _ = throttle_status(db, user.username, at)
    if locked:
        raise _locked(locked)
    if not verify_secret(password or "", user.password_hash):
        raise _fail(db, user, user.username, "reauth", "bad_password", ip, user_agent, at, "verification failed")
    amr = AMR_PASSWORD
    if user.mfa_enabled:
        ok, factor, reason = _check_second_factor(db, user, code, recovery_code, at)
        if not ok:
            raise _fail(db, user, user.username, factor, reason, ip, user_agent, at, "verification failed")
        amr = AMR_RECOVERY if factor == "recovery" else AMR_OTP
    clear_throttle(db, user.username, at)
    token, new_sess = _rotate(db, user, sess, amr, at, ip, user_agent, reason="reauth")
    _log(db, user, user.username, "reauth", True, "ok", ip, user_agent, at)
    db.commit()
    return _payload(user, token, new_sess, build_staff(db, user))


# ---------- 口令 ----------

def change_password(db: Session, staff: Staff, old_password: str, new_password: str, ip: str | None = None,
                    user_agent: str | None = None) -> dict:
    """改密：必须提供当前口令（ASVS 6.2.3）；新口令过策略且不同于旧口令；撤销其他会话并轮换当前会话。"""
    at = config.now()
    cfg = config.get_config()
    user, sess = _current(db, staff)
    locked, _ = throttle_status(db, user.username, at)
    if locked:
        raise _locked(locked)
    if not verify_secret(old_password or "", user.password_hash):
        err = _fail(db, user, user.username, "reauth", "bad_password", ip, user_agent, at, "current password is incorrect")
        if err.code == ErrorCode.UNAUTHORIZED:  # 已登录场景：返回字段错误而不是 4010，避免前端误判为掉线
            err = AppError(ErrorCode.BAD_REQUEST, "current password is incorrect",
                           data={"errors": [{"field": "old_password", "message": "incorrect"}]})
        raise err
    new = check_policy(new_password, mfa=staff.mfa_enabled or staff.mfa_required,
                       context=(user.username, user.name, cfg.totp_issuer), field_name="new_password")
    if verify_secret(new, user.password_hash):
        raise AppError(ErrorCode.BAD_REQUEST, "new password must differ from the current one",
                       data={"errors": [{"field": "new_password", "message": "same as current"}]})
    db.execute(update(AdminUser).where(AdminUser.id == user.id).values(
        password_hash=hash_secret(new), must_change_password=False, temp_password_expires_at=None,
        password_changed_at=at, updated_by=str(staff.id)))
    revoke_sessions(db, user.id, "password_changed", except_session_id=sess.id, at=at)
    token, new_sess = _rotate(db, user, sess, sess.amr, at, ip, user_agent, reason="password_changed")
    clear_throttle(db, user.username, at)
    audit(db, staff, "staff.password.change", "staff", user.id)
    enqueue_event(db, "staff.password_changed", {"staff_id": user.id}, "admin_user", user.id)
    db.commit()
    db.refresh(user)
    return _payload(user, token, new_sess, build_staff(db, user))


# ---------- MFA 绑定与恢复码 ----------

def _store_recovery_codes(db: Session, staff_id: int, at: datetime) -> list[str]:
    cfg = config.get_config()
    codes = new_recovery_codes(cfg.recovery_code_count)
    db.execute(delete(AdminRecoveryCode).where(AdminRecoveryCode.staff_id == staff_id))
    for code in codes:
        db.add(AdminRecoveryCode(staff_id=staff_id, code_hash=hash_secret(code), created_at=at))
    return codes


def recovery_codes_remaining(db: Session, staff_id: int) -> int:
    return db.execute(select(func.count()).select_from(AdminRecoveryCode).where(
        AdminRecoveryCode.staff_id == staff_id, AdminRecoveryCode.used_at.is_(None))).scalar_one()


def consume_recovery_code(db: Session, staff_id: int, code: str, at: datetime) -> bool:
    """恢复码一次性：匹配后条件 UPDATE used_at（并发下只有一个请求成功）。"""
    normalized = normalize_recovery_code(code)
    rows = db.execute(select(AdminRecoveryCode).where(AdminRecoveryCode.staff_id == staff_id,
                                                      AdminRecoveryCode.used_at.is_(None))).scalars().all()
    for row in rows:
        if verify_secret(normalized, row.code_hash):
            used = db.execute(update(AdminRecoveryCode).where(AdminRecoveryCode.id == row.id, AdminRecoveryCode.used_at.is_(None))
                              .values(used_at=at)).rowcount
            return used == 1
    return False


def start_mfa_enrollment(db: Session, staff: Staff) -> dict:
    user, _ = _current(db, staff)
    if user.mfa_enabled:
        raise AppError(ErrorCode.CONFLICT, "MFA is already enabled")
    secret = new_secret()
    user.mfa_pending_secret_enc = cipher().encrypt(secret)
    user.updated_by = str(staff.id)
    audit(db, staff, "staff.mfa.enroll_start", "staff", user.id)
    db.commit()
    issuer = config.get_config().totp_issuer
    return {"secret": secret, "otpauth_uri": otpauth_uri(secret, user.username, issuer), "algorithm": "SHA1", "digits": 6,
            "period": 30}


def confirm_mfa_enrollment(db: Session, staff: Staff, code: str, ip: str | None = None, user_agent: str | None = None) -> dict:
    """用一个有效 TOTP 确认绑定；生成 10 个恢复码（只返回这一次）；轮换会话。"""
    at = config.now()
    cfg = config.get_config()
    user, sess = _current(db, staff)
    if user.mfa_enabled:
        raise AppError(ErrorCode.CONFLICT, "MFA is already enabled")
    if not user.mfa_pending_secret_enc:
        raise AppError(ErrorCode.CONFLICT, "start enrollment first")
    secret = cipher().decrypt(user.mfa_pending_secret_enc)
    step = match_step(secret, code, at.timestamp(), cfg.totp_drift_steps)
    if step is None:
        raise AppError(ErrorCode.BAD_REQUEST, "invalid verification code", data={"errors": [{"field": "code", "message": "invalid"}]})
    rows = db.execute(update(AdminUser).where(AdminUser.id == user.id, AdminUser.mfa_enabled.is_(False)).values(
        mfa_enabled=True, mfa_secret_enc=user.mfa_pending_secret_enc, mfa_pending_secret_enc=None, mfa_last_step=step,
        mfa_enrolled_at=at, updated_by=str(staff.id))).rowcount
    if rows != 1:
        db.rollback()
        raise AppError(ErrorCode.CONFLICT, "MFA is already enabled")
    codes = _store_recovery_codes(db, user.id, at)
    revoke_sessions(db, user.id, "mfa_enrolled", except_session_id=sess.id, at=at)
    token, new_sess = _rotate(db, user, sess, AMR_OTP, at, ip, user_agent, reason="mfa_enrolled")
    audit(db, staff, "staff.mfa.enable", "staff", user.id, after={"mfa_enabled": True})
    db.commit()
    db.refresh(user)
    return {**_payload(user, token, new_sess, build_staff(db, user)), "recovery_codes": codes}


def regenerate_recovery_codes(db: Session, staff: Staff) -> list[str]:
    user, _ = _current(db, staff)
    if not user.mfa_enabled:
        raise AppError(ErrorCode.CONFLICT, "MFA is not enabled")
    codes = _store_recovery_codes(db, user.id, config.now())
    audit(db, staff, "staff.mfa.recovery_regenerate", "staff", user.id)
    db.commit()
    return codes


def disable_own_mfa(db: Session, staff: Staff, code: str) -> dict:
    """自行关闭 MFA：角色要求 MFA 时 4220；需当前 TOTP。"""
    at = config.now()
    user, _ = _current(db, staff)
    if not user.mfa_enabled:
        raise AppError(ErrorCode.CONFLICT, "MFA is not enabled")
    if staff.mfa_required:
        raise AppError(ErrorCode.BUSINESS_RULE, "MFA is required for your role")
    ok, _, _ = _check_second_factor(db, user, code, None, at)
    if not ok:
        db.rollback()
        raise AppError(ErrorCode.BAD_REQUEST, "invalid verification code", data={"errors": [{"field": "code", "message": "invalid"}]})
    clear_mfa(db, user.id)
    revoke_sessions(db, user.id, "mfa_disabled", except_session_id=staff.session_id, at=at)
    audit(db, staff, "staff.mfa.disable", "staff", user.id, before={"mfa_enabled": True}, after={"mfa_enabled": False})
    db.commit()
    return {"mfa_enabled": False}


def clear_mfa(db: Session, staff_id: int) -> None:
    db.execute(update(AdminUser).where(AdminUser.id == staff_id).values(
        mfa_enabled=False, mfa_secret_enc=None, mfa_pending_secret_enc=None, mfa_last_step=None, mfa_enrolled_at=None))
    db.execute(delete(AdminRecoveryCode).where(AdminRecoveryCode.staff_id == staff_id))


# ---------- 本人信息 ----------

def me(db: Session, staff: Staff) -> dict:
    user = db.get(AdminUser, staff.id)
    roles = db.execute(select(AdminRole).join(AdminUserRole, AdminUserRole.role_id == AdminRole.id)
                       .where(AdminUserRole.user_id == staff.id).order_by(AdminRole.id)).scalars().all()
    sess = db.get(AdminSession, staff.session_id) if staff.session_id else None
    cfg = config.get_config()
    return {
        "id": staff.id, "username": staff.username, "name": staff.name,
        "roles": [{"id": r.id, "name": r.name, "builtin_code": r.builtin_code} for r in roles],
        "permissions": sorted(staff.permissions), "all_stores": staff.all_stores, "store_scope": staff.store_scope,
        "mfa_enabled": staff.mfa_enabled, "mfa_required": staff.mfa_required,
        "must_change_password": staff.must_change_password, "restricted_reason": staff.restricted_reason,
        "recovery_codes_remaining": recovery_codes_remaining(db, staff.id) if staff.mfa_enabled else None,
        "password_changed_at": aware(user.password_changed_at) if user else None,
        "last_login_at": aware(user.last_login_at) if user else None, "last_login_ip": user.last_login_ip if user else None,
        "session": None if sess is None else {
            "id": sess.id, "amr": sess.amr, "auth_at": aware(sess.auth_at), "expires_at": aware(sess.expires_at),
            "idle_timeout_seconds": cfg.session_idle_seconds, "fresh_auth_seconds": cfg.fresh_auth_seconds},
    }
