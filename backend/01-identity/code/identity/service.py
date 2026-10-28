"""identity 用例：微信登录与注册、令牌（会话 / 刷新轮换 / 复用检测）、短信验证码、手机号、资料、注销。

规则编号见 MODULE.md §4。约定：
- 外部调用（code2Session、手机号、内容安全、短信）都在数据库写事务之外完成：先结束读事务再调用。
- 状态与计数一律条件 UPDATE + rowcount；唯一约束兜底并发（openid、member_no、phone_hash、短信时间片）。
- 带 commit 参数的用例：接口层在 run_idempotent 内调用时传 commit=False，由幂等执行器提交。
- 事件 payload 只放 id，不放手机号 / openid。
"""
from __future__ import annotations

import hmac
import json
import logging
import secrets
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from luzback import (
    AppError, ErrorCode, IdempotencyRecord, create_token, decode_token, enqueue_event, get_settings, utcnow, write_audit,
)
from luzback.crypto import FieldCipher

from . import rules
from .config import get_config
from .models import (
    CHECK_FAILED, CHECK_PASS, CHECK_PENDING, CHECK_REVIEW, CHECK_RISKY, CHECK_SUPERSEDED, SESSION_ACTIVE, SESSION_REVOKED,
    SMS_ACTIVE, SMS_FAILED, SMS_PURPOSES, SMS_SUPERSEDED, SMS_USED, STATUS_ACTIVE, STATUS_DELETED, STATUS_DELETING,
    STATUS_DISABLED, TOKEN_ACTIVE, TOKEN_REVOKED, TOKEN_ROTATED, ContentCheck, RefreshToken, SmsCode, User, UserSession,
)
from .ports import (
    AgreementAccept, AgreementGateway, LegacyCode2Session, SmsSender, default_agreements, run_deletion_checks, send_via,
)

log = logging.getLogger("identity.auth")


def _cipher() -> FieldCipher:
    return FieldCipher(get_settings().field_encryption_key)


def _mp(client: Any) -> Any:
    """client 可以是 MiniProgramClient、旧式 code2session 函数，或 None（取 wechat_mp 进程级客户端）。"""
    if client is None:
        from wechat_mp import get_client

        return get_client()
    if callable(client) and not hasattr(client, "code2session"):
        return LegacyCode2Session(client)
    return client


def _iso(value: datetime | None) -> str | None:
    """响应里的时间统一为 ISO 8601 带时区（首次响应与幂等重放一致）。"""
    return rules.aware(value).isoformat() if value is not None else None


def _get_user(db: Session, user_id: int, statuses: tuple[str, ...] = (STATUS_ACTIVE, STATUS_DELETING)) -> User:
    user = db.get(User, user_id)
    if user is None or user.deleted_at is not None or user.status not in statuses:
        raise AppError(ErrorCode.NOT_FOUND, "user not found")
    return user


# ================================================================ 令牌与会话

def decode_access(token: str) -> dict:
    """校验 alg（框架只允许 HS256）、签名、exp / iat（小偏差）、kind、iss、aud、sid、jti。"""
    s, c = get_settings(), get_config()
    claims = decode_token(token, s.jwt_secret, kind="access", issuer=s.jwt_issuer, leeway=c.jwt_leeway_seconds)
    aud = claims.get("aud")
    if not (aud == c.jwt_audience or (isinstance(aud, list) and c.jwt_audience in aud)):
        raise AppError(ErrorCode.UNAUTHORIZED, "wrong audience")
    if not claims.get("sid") or not claims.get("jti") or not str(claims.get("sub", "")).isdigit():
        raise AppError(ErrorCode.UNAUTHORIZED, "malformed token")
    return claims


def current_user_id(token: str, secret: str | None = None) -> int:
    """只校验令牌本身（不查会话）。secret 参数为兼容旧签名，忽略。"""
    return int(decode_access(token)["sub"])


def assert_session_active(db: Session, claims: Mapping[str, Any], now: datetime | None = None) -> None:
    """access 令牌对应会话必须仍有效（登出 / 停用 / 复用检测 / 注销后立即失效，ASVS 7.4.1 / 7.4.2）。"""
    if not get_config().session_check:
        return
    now = now or utcnow()
    row = db.execute(
        select(UserSession.status, UserSession.absolute_expires_at, User.status, User.deleted_at)
        .join(User, User.id == UserSession.user_id)
        .where(UserSession.sid == str(claims.get("sid")), UserSession.user_id == int(claims["sub"]))
    ).first()
    if (row is None or row[0] != SESSION_ACTIVE or rules.aware(row[1]) <= now or row[3] is not None
            or row[2] not in (STATUS_ACTIVE, STATUS_DELETING)):
        raise AppError(ErrorCode.UNAUTHORIZED, "session expired, please login again", data={"reason": "session_revoked"})


def require_fresh(claims: Mapping[str, Any]) -> None:
    """敏感操作（注销）要求 access 令牌在 fresh 窗口（默认 15 分钟）内签发。"""
    s = get_settings()
    if utcnow().timestamp() - float(claims.get("iat", 0)) > s.fresh_window_seconds:
        raise AppError(ErrorCode.UNAUTHORIZED, "please login again to continue", data={"reason": "fresh_required"})


def _access_token(user: User, sid: str) -> str:
    s, c = get_settings(), get_config()
    return create_token(str(user.id), s.jwt_secret, c.access_ttl_seconds, kind="access", issuer=s.jwt_issuer,
                        extra={"aud": c.jwt_audience, "sid": sid, "role": "user", "pv": 1 if user.phone_verified_at else 0})


def _issue_refresh(db: Session, session: UserSession, parent_id: int | None, now: datetime) -> tuple[str, datetime]:
    c = get_config()
    raw = secrets.token_urlsafe(32)  # 256 bit，CSPRNG（ASVS 7.2.3）；库里只存 SHA-256
    expires = min(now + timedelta(days=c.refresh_idle_days), rules.aware(session.absolute_expires_at))
    db.add(RefreshToken(session_id=session.id, user_id=session.user_id, token_hash=rules.token_hash(raw),
                        parent_id=parent_id, status=TOKEN_ACTIVE, created_at=now, expires_at=expires))
    return raw, expires


def _open_session(db: Session, user: User, channel: str, now: datetime, ip: str | None, user_agent: str | None) -> dict:
    c = get_config()
    session = UserSession(sid=uuid.uuid4().hex, user_id=user.id, status=SESSION_ACTIVE, channel=channel, created_at=now,
                          last_seen_at=now, absolute_expires_at=now + timedelta(days=c.refresh_absolute_days),
                          ip=(ip or None), user_agent=(user_agent or "")[:200] or None)
    db.add(session)
    db.flush()
    raw, expires = _issue_refresh(db, session, None, now)
    return _token_pair(user, session, raw, expires, now)


def _token_pair(user: User, session: UserSession, refresh_raw: str, refresh_expires: datetime, now: datetime) -> dict:
    return {
        "user_id": user.id,
        "access_token": _access_token(user, session.sid),
        "token_type": "Bearer",
        "expires_in": get_config().access_ttl_seconds,
        "refresh_token": refresh_raw,
        "refresh_expires_in": max(0, int((refresh_expires - now).total_seconds())),
        "session_id": session.sid,
    }


def issue_tokens(db: Session, user: User, channel: str = "wechat", now: datetime | None = None,
                 ip: str | None = None, user_agent: str | None = None) -> dict:
    """为已存在的用户开一个新会话并提交（其他模块的测试可用它快速得到登录态）。"""
    result = _open_session(db, user, channel, now or utcnow(), ip, user_agent)
    db.commit()
    return result


def revoke_sessions(db: Session, user_id: int, reason: str, now: datetime, except_sid: str | None = None) -> int:
    """吊销用户的全部会话（可保留当前会话）及其刷新令牌；不提交。"""
    stmt = update(UserSession).where(UserSession.user_id == user_id, UserSession.status == SESSION_ACTIVE)
    if except_sid:
        stmt = stmt.where(UserSession.sid != except_sid)
    count = db.execute(stmt.values(status=SESSION_REVOKED, revoked_at=now, revoke_reason=reason[:32])).rowcount
    revoked_ids = select(UserSession.id).where(UserSession.user_id == user_id, UserSession.status == SESSION_REVOKED)
    db.execute(update(RefreshToken).where(RefreshToken.user_id == user_id, RefreshToken.status == TOKEN_ACTIVE,
                                          RefreshToken.session_id.in_(revoked_ids)).values(status=TOKEN_REVOKED))
    return count


def _revoke_family(db: Session, session_id: int, reason: str, now: datetime) -> None:
    db.execute(update(UserSession).where(UserSession.id == session_id, UserSession.status == SESSION_ACTIVE)
               .values(status=SESSION_REVOKED, revoked_at=now, revoke_reason=reason[:32]))
    db.execute(update(RefreshToken).where(RefreshToken.session_id == session_id, RefreshToken.status == TOKEN_ACTIVE)
               .values(status=TOKEN_REVOKED))


def refresh_tokens(db: Session, refresh_token: str, now: datetime | None = None) -> dict:
    """刷新轮换（RFC 9700 §4.14.2）：每次换新、旧的作废；已轮换的令牌再次出现 → 吊销整个家族。

    并发宽限：同一令牌在轮换后 grace 秒内再次提交（同一客户端并发刷新）只返回 4010，不吊销家族。
    """
    now = now or utcnow()
    c = get_config()
    invalid = AppError(ErrorCode.UNAUTHORIZED, "refresh token invalid, please login again", data={"reason": "refresh_invalid"})
    if not refresh_token or len(refresh_token) > 256:
        raise invalid
    row = db.execute(select(RefreshToken).where(RefreshToken.token_hash == rules.token_hash(refresh_token))).scalar_one_or_none()
    if row is None:
        raise invalid
    session = db.get(UserSession, row.session_id)
    if row.status != TOKEN_ACTIVE:
        if row.status == TOKEN_ROTATED and row.used_at is not None \
                and now - rules.aware(row.used_at) <= timedelta(seconds=c.refresh_reuse_grace_seconds):
            raise AppError(ErrorCode.UNAUTHORIZED, "refresh token already rotated", data={"reason": "refresh_rotated"})
        if row.status == TOKEN_ROTATED and session is not None and session.status == SESSION_ACTIVE:
            _revoke_family(db, row.session_id, "reuse_detected", now)
            write_audit(db, "user", row.user_id, "member.session.reuse_detected", "user_session", session.sid)
            db.commit()
            log.warning("refresh_reuse_detected", extra={"fields": {"event": "refresh_reuse_detected", "user_id": row.user_id}})
        raise invalid
    if (session is None or session.status != SESSION_ACTIVE or rules.aware(row.expires_at) <= now
            or rules.aware(session.absolute_expires_at) <= now):
        raise AppError(ErrorCode.UNAUTHORIZED, "session expired, please login again", data={"reason": "session_expired"})
    user = db.get(User, row.user_id)
    if user is None or user.deleted_at is not None or user.status not in (STATUS_ACTIVE, STATUS_DELETING):
        raise invalid
    rows = db.execute(update(RefreshToken).where(RefreshToken.id == row.id, RefreshToken.status == TOKEN_ACTIVE)
                      .values(status=TOKEN_ROTATED, used_at=now)).rowcount
    if rows != 1:  # 并发刷新：另一请求已轮换
        db.rollback()
        raise AppError(ErrorCode.UNAUTHORIZED, "refresh token already rotated", data={"reason": "refresh_rotated"})
    raw, expires = _issue_refresh(db, session, row.id, now)
    db.execute(update(UserSession).where(UserSession.id == session.id).values(last_seen_at=now))
    db.commit()
    return _token_pair(user, session, raw, expires, now)


def logout(db: Session, user_id: int, session_id: str | None = None, now: datetime | None = None) -> dict:
    """登出：session_id 给定时只结束当前会话，否则结束全部会话（服务端作废，OWASP 会话管理）。"""
    now = now or utcnow()
    if session_id:
        session = db.execute(select(UserSession).where(UserSession.sid == session_id, UserSession.user_id == user_id)).scalar_one_or_none()
        count = 0
        if session is not None and session.status == SESSION_ACTIVE:
            _revoke_family(db, session.id, "logout", now)
            count = 1
    else:
        count = revoke_sessions(db, user_id, "logout_all", now)
    write_audit(db, "user", user_id, "member.logout" if session_id else "member.logout_all", "user", user_id)
    db.commit()
    return {"revoked_sessions": count}


# ================================================================ 登录与注册

def _register(db: Session, openid: str, unionid: str | None, channel: str, agreements: list[AgreementAccept],
              gateway: AgreementGateway, now: datetime) -> tuple[User, bool]:
    for _ in range(3):
        user = User(member_no=rules.new_member_no(), openid=openid, unionid=unionid, register_channel=channel,
                    status=STATUS_ACTIVE)
        db.add(user)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()  # 并发首次登录（openid 唯一）或会员号碰撞
            existing = db.execute(select(User).where(User.openid == openid)).scalar_one_or_none()
            if existing is not None:
                return existing, False
            continue
        try:
            accepted = gateway.accept(db, user.id, agreements, "miniprogram", now)
            missing = gateway.pending(db, user.id, now)
        except Exception:
            db.rollback()
            raise
        if missing:
            db.rollback()
            raise AppError(ErrorCode.BUSINESS_RULE, "please read and agree to the user agreement and privacy policy",
                           data={"reason": "agreement_required", "pending": missing})
        user.agreed_document_ids = ",".join(str(i) for i in accepted)[:200]
        return user, True
    raise AppError(ErrorCode.CONFLICT, "please retry")


def _assert_login_allowed(db: Session, user: User) -> None:
    if user.status == STATUS_DISABLED:
        write_audit(db, "user", user.id, "member.login.denied", "user", user.id, reason="account_disabled")
        db.commit()
        raise AppError(ErrorCode.FORBIDDEN, "account disabled, please contact customer service",
                       data={"reason": "account_disabled"})
    if user.status == STATUS_DELETED or user.deleted_at is not None:
        db.rollback()
        raise AppError(ErrorCode.UNAUTHORIZED, "login failed", data={"reason": "account_deleted"})


def _cancel_deletion_on_login(db: Session, user: User, now: datetime) -> bool:
    if user.status != STATUS_DELETING:
        return False
    rows = db.execute(update(User).where(User.id == user.id, User.status == STATUS_DELETING).values(
        status=STATUS_ACTIVE, deletion_requested_at=None, deletion_due_at=None, deletion_reason=None, deletion_hold=None)).rowcount
    if rows == 1:
        write_audit(db, "user", user.id, "member.deletion.cancel", "user", user.id, reason="login")
        db.refresh(user)
    return rows == 1


def _login_payload(db: Session, user: User, tokens: dict, created: bool, cancelled: bool, pending: list) -> dict:
    return {**tokens, "member_no": user.member_no, "is_new_user": created, "phone_bound": user.phone_hash is not None,
            "deletion_cancelled": cancelled, "pending_agreements": pending}


def login_with_code(db: Session, code: str, client: Any = None, *, agreements: Iterable[AgreementAccept | Mapping] = (),
                    agreement_gateway: AgreementGateway | None = None, ip: str | None = None,
                    user_agent: str | None = None, now: datetime | None = None) -> dict:
    """微信登录：code2Session（事务外）→ 取回或注册（记录同意的协议版本）→ 停用拦截 → 冷静期内登录即撤销注销 →
    session_key 加密保存 → 开新会话签发令牌。"""
    if not code:
        raise AppError(ErrorCode.BAD_REQUEST, "code required")
    info = _mp(client).code2session(code)
    now = now or utcnow()
    gateway = agreement_gateway or default_agreements()
    items = [a if isinstance(a, AgreementAccept) else AgreementAccept(int(a["document_id"]), str(a["version"]), str(a["sha256"]))
             for a in agreements]
    user = db.execute(select(User).where(User.openid == info.openid)).scalar_one_or_none()
    created = False
    if user is None:
        user, created = _register(db, info.openid, info.unionid, "miniprogram", items, gateway, now)
    elif items:
        gateway.accept(db, user.id, items, "miniprogram", now)
    _assert_login_allowed(db, user)
    cancelled = _cancel_deletion_on_login(db, user, now)
    if info.session_key:
        user.wx_session_key_enc = _cipher().encrypt(info.session_key)  # 只在服务端加密保存，不下发（官方要求）
    if info.unionid and not user.unionid:
        user.unionid = info.unionid
    user.last_login_at = now
    tokens = _open_session(db, user, "wechat", now, ip, user_agent)
    if created:
        enqueue_event(db, "member.registered", {"user_id": user.id, "member_no": user.member_no, "channel": "miniprogram"},
                      "user", user.id)
        write_audit(db, "user", user.id, "member.register", "user", user.id, after={"agreed_document_ids": user.agreed_document_ids})
    pending = gateway.pending(db, user.id, now)
    db.commit()
    log.info("login", extra={"fields": {"event": "login", "method": "wechat", "user_id": user.id, "new": created}})
    return _login_payload(db, user, tokens, created, cancelled, pending)


def session_key_of(db: Session, user_id: int) -> str | None:
    """服务端内部使用（虚拟支付用户态签名、旧版加密数据解密）；绝不通过接口返回。"""
    user = db.get(User, user_id)
    if user is None or not user.wx_session_key_enc:
        return None
    return _cipher().decrypt(user.wx_session_key_enc)


# ================================================================ 短信验证码

def _sms_invalid() -> AppError:
    return AppError(ErrorCode.BAD_REQUEST, "verification code is incorrect or expired", data={"reason": "sms_code_invalid"})


def _sms_hash(cipher: FieldCipher, request_id: str, phone: str, purpose: str, code: str) -> str:
    return cipher.lookup_hash(f"sms|{request_id}|{phone}|{purpose}|{code}")  # type: ignore[return-value]


def send_sms_code(db: Session, phone: str, purpose: str, sender: SmsSender, *, ip: str | None = None,
                  now: datetime | None = None) -> dict:
    """发送验证码。响应与号码是否注册无关（防枚举）；限制：重发间隔、每号每日、每 IP 每小时 / 每日。"""
    c, s = get_config(), get_settings()
    if not c.sms_enabled:
        raise AppError(ErrorCode.BUSINESS_RULE, "sms verification is not enabled")
    phone = rules.normalize_cn_mobile(phone)
    if purpose not in SMS_PURPOSES:
        raise AppError(ErrorCode.BAD_REQUEST, "invalid purpose")
    now = now or utcnow()
    cipher = _cipher()
    phone_hash = cipher.lookup_hash(phone)
    ip_hash = cipher.lookup_hash(f"ip|{ip}") if ip else None
    counted = SmsCode.status != SMS_FAILED
    last = db.execute(select(func.max(SmsCode.created_at)).where(
        SmsCode.phone_hash == phone_hash, SmsCode.purpose == purpose, counted)).scalar_one()
    if last is not None:
        wait = int((rules.aware(last) + timedelta(seconds=c.sms_resend_seconds) - now).total_seconds())
        if wait > 0:
            raise AppError(ErrorCode.RATE_LIMITED, "please wait before requesting another code", retry_after=wait)
    start = rules.day_start(now, s.timezone)
    next_day = int((start + timedelta(days=1) - now).total_seconds()) or 1
    sent_today = db.execute(select(func.count()).select_from(SmsCode).where(
        SmsCode.phone_hash == phone_hash, SmsCode.created_at >= start, counted)).scalar_one()
    if sent_today >= c.sms_daily_per_phone:
        raise AppError(ErrorCode.RATE_LIMITED, "daily verification code limit reached", retry_after=next_day)
    if ip_hash:
        hour = db.execute(select(func.count()).select_from(SmsCode).where(
            SmsCode.ip_hash == ip_hash, SmsCode.created_at >= now - timedelta(hours=1))).scalar_one()
        day = db.execute(select(func.count()).select_from(SmsCode).where(
            SmsCode.ip_hash == ip_hash, SmsCode.created_at >= start)).scalar_one()
        if hour >= c.sms_hourly_per_ip or day >= c.sms_daily_per_ip:
            raise AppError(ErrorCode.RATE_LIMITED, "too many verification codes from this network", retry_after=3600)
    request_id = uuid.uuid4().hex
    code = rules.new_sms_code()
    db.execute(update(SmsCode).where(SmsCode.phone_hash == phone_hash, SmsCode.purpose == purpose,
                                     SmsCode.status == SMS_ACTIVE).values(status=SMS_SUPERSEDED))
    db.add(SmsCode(request_id=request_id, phone_hash=phone_hash, purpose=purpose,
                   code_hash=_sms_hash(cipher, request_id, phone, purpose, code), attempts=0, status=SMS_ACTIVE,
                   send_slot=int(now.timestamp()) // max(1, c.sms_resend_seconds), ip_hash=ip_hash,
                   expires_at=now + timedelta(seconds=c.sms_ttl_seconds), created_at=now))
    try:
        db.commit()  # 先落库再发送：外部调用不在事务里
    except IntegrityError as exc:  # 同一时间片并发发送
        db.rollback()
        raise AppError(ErrorCode.RATE_LIMITED, "please wait before requesting another code",
                       retry_after=c.sms_resend_seconds) from exc
    try:
        send_via(sender, phone, code, purpose, c.sms_ttl_seconds)
    except AppError:
        db.execute(update(SmsCode).where(SmsCode.request_id == request_id).values(status=SMS_FAILED, send_slot=-SmsCode.id))
        db.commit()
        raise
    return {"request_id": request_id, "expires_in": c.sms_ttl_seconds, "retry_after": c.sms_resend_seconds}


def _consume_sms(db: Session, phone: str, purpose: str, request_id: str, code: str, now: datetime) -> None:
    """校验并消费验证码：绑定 手机号 + 用途 + 请求；每次校验先条件计数（上限 max_attempts），正确才置 used。
    所有失败返回同一提示。"""
    c = get_config()
    cipher = _cipher()
    if not request_id or not code or not code.isdigit():
        raise _sms_invalid()
    record = db.execute(select(SmsCode).where(SmsCode.request_id == request_id)).scalar_one_or_none()
    if (record is None or record.phone_hash != cipher.lookup_hash(phone) or record.purpose != purpose
            or record.status != SMS_ACTIVE or rules.aware(record.expires_at) <= now):
        raise _sms_invalid()
    counted = db.execute(update(SmsCode).where(SmsCode.id == record.id, SmsCode.status == SMS_ACTIVE,
                                               SmsCode.attempts < c.sms_max_attempts)
                         .values(attempts=SmsCode.attempts + 1)).rowcount
    if counted != 1:
        db.rollback()
        raise _sms_invalid()
    if not hmac.compare_digest(_sms_hash(cipher, request_id, phone, purpose, code), record.code_hash):
        db.commit()  # 错误次数必须落库
        raise _sms_invalid()
    used = db.execute(update(SmsCode).where(SmsCode.id == record.id, SmsCode.status == SMS_ACTIVE)
                      .values(status=SMS_USED, used_at=now)).rowcount
    if used != 1:
        db.rollback()
        raise _sms_invalid()


def find_user_by_phone(db: Session, phone: str | None) -> User | None:
    """按手机号查会员（其他模块用：后台按手机号搜索、线下充值 / 发卡找人）。

    规范化（去空格 / 短横线、去 +86 / 86 前缀）后用 phone_hash（HMAC）等值查询；不做模糊匹配、不解密。
    号码不合法、未绑定、已注销（deleted_at 非空 / 已匿名化）→ None。只读，不写审计（需要展示明文手机号仍走 reveal_phone）。
    """
    raw = "".join(ch for ch in (phone or "") if ch not in " -")
    if raw.startswith("+86"):
        raw = raw[3:]
    elif raw.startswith("86") and len(raw) == 13:
        raw = raw[2:]
    try:
        normalized = rules.normalize_cn_mobile(raw)
    except AppError:
        return None
    return db.execute(select(User).where(User.phone_hash == _cipher().lookup_hash(normalized),
                                         User.deleted_at.is_(None))).scalar_one_or_none()


def login_with_sms(db: Session, phone: str, request_id: str, code: str, *, ip: str | None = None,
                   user_agent: str | None = None, now: datetime | None = None) -> dict:
    """短信登录：只登录已绑定该手机号的账号（注册走微信登录）。验证码通过后才告知未绑定。"""
    phone = rules.normalize_cn_mobile(phone)
    now = now or utcnow()
    _consume_sms(db, phone, "login", request_id, code, now)
    user = db.execute(select(User).where(User.phone_hash == _cipher().lookup_hash(phone), User.deleted_at.is_(None))).scalar_one_or_none()
    if user is None:
        db.commit()
        raise AppError(ErrorCode.UNAUTHORIZED, "no account is bound to this phone, please login with WeChat",
                       data={"reason": "phone_not_registered"})
    _assert_login_allowed(db, user)
    cancelled = _cancel_deletion_on_login(db, user, now)
    user.last_login_at = now
    tokens = _open_session(db, user, "sms", now, ip, user_agent)
    db.commit()
    log.info("login", extra={"fields": {"event": "login", "method": "sms", "user_id": user.id}})
    return _login_payload(db, user, tokens, False, cancelled, [])


# ================================================================ 手机号

def attach_phone(db: Session, user_id: int, phone: str, source: str, now: datetime | None = None, commit: bool = True) -> dict:
    """绑定 / 换绑手机号：加密 + 哈希 + 脱敏；被他人占用 → 4090 PHONE_IN_USE（唯一索引兜底并发）。"""
    now = now or utcnow()
    user = _get_user(db, user_id, (STATUS_ACTIVE,))
    cipher = _cipher()
    phone_hash = cipher.lookup_hash(phone)
    in_use = AppError(ErrorCode.CONFLICT, "phone number is already bound to another account", data={"reason": "PHONE_IN_USE"})
    owner = db.execute(select(User.id).where(User.phone_hash == phone_hash, User.id != user.id)).first()
    if owner is not None:
        raise in_use
    changed = user.phone_hash != phone_hash
    before = user.phone_masked
    user.phone_enc = cipher.encrypt(phone)
    user.phone_hash = phone_hash
    user.phone_masked = rules.masked(phone)
    user.phone_verified_at = now
    if changed:
        write_audit(db, "user", user.id, "member.phone.bind", "user", user.id, before={"phone": before},
                    after={"phone": user.phone_masked, "source": source})
        enqueue_event(db, "member.phone_bound", {"user_id": user.id, "source": source}, "user", user.id)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise in_use from exc
    if commit:
        db.commit()
    return {"phone": user.phone_masked, "phone_verified": True}


def bind_phone_with_sms(db: Session, user_id: int, phone: str, request_id: str, code: str,
                        now: datetime | None = None, commit: bool = True) -> dict:
    phone = rules.normalize_cn_mobile(phone)
    now = now or utcnow()
    _get_user(db, user_id, (STATUS_ACTIVE,))
    _consume_sms(db, phone, "bind", request_id, code, now)
    return attach_phone(db, user_id, phone, "sms", now, commit)


def resolve_wechat_phone(db: Session, user_id: int, phone_code: str, client: Any = None) -> str:
    """getuserphonenumber（带 openid 校验 code 归属）；在事务外调用。"""
    if not phone_code:
        raise AppError(ErrorCode.BAD_REQUEST, "phone_code required")
    openid = _get_user(db, user_id, (STATUS_ACTIVE,)).openid
    db.rollback()  # 结束读事务再调外部接口
    info = _mp(client).get_phone_number(phone_code, openid=openid)
    return rules.normalize_wechat_phone(info.pure_phone_number, info.country_code)


def bind_phone_from_wechat(db: Session, user_id: int, phone_code: str, client: Any = None,
                           now: datetime | None = None, commit: bool = True) -> dict:
    phone = resolve_wechat_phone(db, user_id, phone_code, client)
    return attach_phone(db, user_id, phone, "wechat", now, commit)


# ================================================================ 资料

def profile(db: Session, user_id: int, reveal_phone: bool = False) -> dict:
    """本人资料（手机号脱敏）。reveal_phone=True 仅供服务端内部（CONTRACTS：储值核对会员等），不得直接返回给前端。"""
    user = _get_user(db, user_id)
    pending = db.execute(select(ContentCheck.id).where(ContentCheck.user_id == user.id, ContentCheck.field == "avatar",
                                                       ContentCheck.status == CHECK_PENDING)).first()
    data = {
        "user_id": user.id, "member_no": user.member_no, "nickname": user.nickname, "avatar_url": user.avatar_url,
        "avatar_pending": pending is not None, "gender": user.gender, "birthday": user.birthday,
        "phone": (_cipher().decrypt(user.phone_enc) if reveal_phone and user.phone_enc else user.phone_masked),
        "phone_verified": user.phone_verified_at is not None, "status": user.status, "registered_at": _iso(user.created_at),
    }
    if user.status == STATUS_DELETING:
        data["deletion"] = {"requested_at": _iso(user.deletion_requested_at), "due_at": _iso(user.deletion_due_at)}
    return data


@dataclass
class ProfilePlan:
    changes: dict[str, Any]
    avatar_url: str | None = None        # 直接生效的头像（清空或未启用检测）
    avatar_check_url: str | None = None  # 送检中的头像
    avatar_trace_id: str | None = None
    nickname_review: bool | None = None


def _content_call(fn):
    c = get_config()
    try:
        return fn()
    except AppError as exc:
        if c.content_check_fail_open and exc.code in (ErrorCode.UPSTREAM_FAILED, ErrorCode.SERVICE_UNAVAILABLE):
            log.warning("content_check_degraded", extra={"fields": {"event": "content_check_degraded", "degraded": "wechat_mp"}})
            return None
        raise


def prepare_profile_update(db: Session, user_id: int, changes: Mapping[str, Any], client: Any = None,
                           today: Any = None) -> ProfilePlan:
    """校验白名单字段 → 昵称同步内容安全（risky 拒绝、review 标记）→ 头像提交异步检测（通过后才生效）。
    只读数据库；外部调用前结束读事务。"""
    unknown = sorted(set(changes) - set(rules.PROFILE_FIELDS))
    if unknown:
        raise AppError(ErrorCode.BAD_REQUEST, f"field not editable: {', '.join(unknown)}")
    user = _get_user(db, user_id, (STATUS_ACTIVE,))
    openid, current_nick, current_avatar = user.openid, user.nickname, user.avatar_url
    today = today or utcnow().date()
    out: dict[str, Any] = {}
    if changes.get("nickname") is not None:
        out["nickname"] = rules.validate_nickname(changes["nickname"])
    if changes.get("gender") is not None:
        out["gender"] = rules.validate_gender(changes["gender"])
    if "birthday" in changes:
        out["birthday"] = rules.validate_birthday(changes["birthday"], today)
    avatar = rules.validate_avatar_url(changes["avatar_url"]) if changes.get("avatar_url") is not None else None
    db.rollback()
    c = get_config()
    plan = ProfilePlan(changes=out)
    if "nickname" in out and out["nickname"] != current_nick:
        plan.nickname_review = False
        if c.content_check:
            result = _content_call(lambda: _mp(client).msg_sec_check(openid, out["nickname"], scene=1, nickname=out["nickname"]))
            if result is not None and result.risky:
                raise AppError(ErrorCode.BUSINESS_RULE, "nickname contains content that is not allowed",
                               data={"field": "nickname", "reason": "content_risky"})
            plan.nickname_review = bool(result is not None and result.suggest == "review")
    if avatar is not None and avatar != current_avatar:
        if avatar and c.content_check:
            trace_id = _content_call(lambda: _mp(client).media_check_async(openid, avatar, 2, 1))
            if trace_id is None:
                plan.avatar_url = avatar
            else:
                plan.avatar_check_url, plan.avatar_trace_id = avatar, trace_id
        else:
            plan.avatar_url = avatar
    return plan


def apply_profile_update(db: Session, user_id: int, plan: ProfilePlan, now: datetime | None = None, commit: bool = True) -> dict:
    now = now or utcnow()
    user = _get_user(db, user_id, (STATUS_ACTIVE,))
    fields = ("nickname", "avatar_url", "gender", "birthday")
    before = {f: getattr(user, f) for f in fields}
    for key, value in plan.changes.items():
        setattr(user, key, value)
    if plan.nickname_review is not None:
        user.content_review = plan.nickname_review
    if plan.avatar_url is not None:
        user.avatar_url = plan.avatar_url
    if plan.avatar_trace_id:
        db.execute(update(ContentCheck).where(ContentCheck.user_id == user.id, ContentCheck.field == "avatar",
                                              ContentCheck.status == CHECK_PENDING).values(status=CHECK_SUPERSEDED, decided_at=now))
        db.add(ContentCheck(user_id=user.id, field="avatar", value=plan.avatar_check_url or "", trace_id=plan.avatar_trace_id,
                            status=CHECK_PENDING, created_at=now))
    after = {f: getattr(user, f) for f in fields}
    if before != after or plan.avatar_trace_id:
        write_audit(db, "user", user.id, "member.profile.update", "user", user.id, before=before,
                    after={**after, "avatar_check": plan.avatar_trace_id is not None})
    db.flush()
    result = profile(db, user_id)
    if commit:
        db.commit()
    return result


def update_profile(db: Session, user_id: int, client: Any = None, **changes: Any) -> dict:
    """服务层便捷入口：prepare + apply（单测与内部调用）。"""
    plan = prepare_profile_update(db, user_id, {k: v for k, v in changes.items() if v is not None or k == "birthday"}, client)
    return apply_profile_update(db, user_id, plan)


def handle_media_check_event(db: Session, message: Mapping[str, Any], now: datetime | None = None) -> bool:
    """wxa_media_check 推送：只处理本模块的 trace_id；pass → 头像生效；risky / review / 失败 → 不生效。重复推送幂等。不提交。"""
    from wechat_mp import parse_media_check_event

    event = parse_media_check_event(dict(message))
    check = db.execute(select(ContentCheck).where(ContentCheck.trace_id == event.trace_id)).scalar_one_or_none()
    if check is None or check.status != CHECK_PENDING:
        return False
    now = now or utcnow()
    status = CHECK_PASS if event.passed else CHECK_RISKY if event.risky else CHECK_REVIEW if event.valid else CHECK_FAILED
    rows = db.execute(update(ContentCheck).where(ContentCheck.id == check.id, ContentCheck.status == CHECK_PENDING)
                      .values(status=status, label=event.label, decided_at=now)).rowcount
    if rows != 1:
        return False
    if status == CHECK_PASS:
        db.execute(update(User).where(User.id == check.user_id, User.deleted_at.is_(None),
                                      User.status.in_((STATUS_ACTIVE, STATUS_DELETING))).values(avatar_url=check.value))
    write_audit(db, "system", "wechat_mp", "member.avatar.check", "user", check.user_id, after={"result": status, "label": event.label})
    return True


# ================================================================ 注销

def deletion_status(db: Session, user_id: int) -> dict:
    user = _get_user(db, user_id)
    blockers = run_deletion_checks(db, user_id) if user.status == STATUS_ACTIVE else []
    return {"status": user.status, "requested_at": _iso(user.deletion_requested_at), "due_at": _iso(user.deletion_due_at),
            "cooling_days": get_config().deletion_cooling_days, "blockers": [asdict(b) for b in blockers]}


def request_deletion(db: Session, user_id: int, claims: Mapping[str, Any], reason: str | None = None,
                     now: datetime | None = None, commit: bool = True) -> dict:
    """申请注销：新鲜令牌 → 阻断检查（各模块注册）→ active→deleting（条件 UPDATE）→ 结束其他会话。"""
    require_fresh(claims)
    now = now or utcnow()
    c = get_config()
    user = _get_user(db, user_id)
    if user.status == STATUS_DELETING:
        raise AppError(ErrorCode.CONFLICT, "account deletion already requested")
    blockers = run_deletion_checks(db, user_id)
    if blockers:
        raise AppError(ErrorCode.BUSINESS_RULE, "account cannot be deleted yet",
                       data={"reason": "deletion_blocked", "blockers": [asdict(b) for b in blockers]})
    due = rules.deletion_due(now, c.deletion_cooling_days)
    rows = db.execute(update(User).where(User.id == user_id, User.status == STATUS_ACTIVE).values(
        status=STATUS_DELETING, deletion_requested_at=now, deletion_due_at=due,
        deletion_reason=(reason or "")[:200] or None, deletion_hold=None)).rowcount
    if rows != 1:
        raise AppError(ErrorCode.CONFLICT, "account state changed, refresh and retry")
    revoke_sessions(db, user_id, "deletion_requested", now, except_sid=str(claims.get("sid") or "") or None)
    write_audit(db, "user", user_id, "member.deletion.request", "user", user_id, after={"due_at": due}, reason=reason)
    if commit:
        db.commit()
    return {"status": STATUS_DELETING, "requested_at": _iso(now), "due_at": _iso(due), "cooling_days": c.deletion_cooling_days}


def cancel_deletion(db: Session, user_id: int, now: datetime | None = None, commit: bool = True) -> dict:
    rows = db.execute(update(User).where(User.id == user_id, User.status == STATUS_DELETING, User.deleted_at.is_(None)).values(
        status=STATUS_ACTIVE, deletion_requested_at=None, deletion_due_at=None, deletion_reason=None, deletion_hold=None)).rowcount
    if rows != 1:
        raise AppError(ErrorCode.CONFLICT, "no pending deletion to cancel")
    write_audit(db, "user", user_id, "member.deletion.cancel", "user", user_id, reason="user")
    if commit:
        db.commit()
    return {"status": STATUS_ACTIVE}


def anonymize_member(db: Session, user_id: int, now: datetime | None = None) -> str:
    """到期匿名化（不可逆）：openid 换成随机值、清空个人信息、吊销会话、删验证码与检测记录、发 member.deleted。
    到期时再次运行阻断检查，不通过则挂起（deletion_hold）等待下次。返回 deleted | held | skipped。"""
    now = now or utcnow()
    user = db.get(User, user_id)
    if user is None or user.status != STATUS_DELETING or user.deletion_due_at is None or rules.aware(user.deletion_due_at) > now:
        return "skipped"
    blockers = run_deletion_checks(db, user_id)
    if blockers:
        hold = ",".join(b.code for b in blockers)[:200]
        db.execute(update(User).where(User.id == user_id, User.status == STATUS_DELETING).values(deletion_hold=hold))
        write_audit(db, "system", "identity", "member.deletion.hold", "user", user_id, reason=hold)
        db.commit()
        return "held"
    old_phone_hash = user.phone_hash
    rows = db.execute(update(User).where(User.id == user_id, User.status == STATUS_DELETING).values(
        status=STATUS_DELETED, openid=f"deleted:{uuid.uuid4().hex}", unionid=None, wx_session_key_enc=None,
        nickname="已注销用户", avatar_url="", gender=0, birthday=None, phone_enc=None, phone_hash=None, phone_masked=None,
        phone_verified_at=None, disabled_reason=None, deletion_reason=None, deletion_hold=None, content_review=False,
        deleted_at=now)).rowcount
    if rows != 1:
        db.rollback()
        return "skipped"
    revoke_sessions(db, user_id, "deleted", now)
    db.execute(update(UserSession).where(UserSession.user_id == user_id).values(ip=None, user_agent=None))
    if old_phone_hash:
        db.execute(delete(SmsCode).where(SmsCode.phone_hash == old_phone_hash))
    db.execute(delete(ContentCheck).where(ContentCheck.user_id == user_id))
    enqueue_event(db, "member.deleted", {"user_id": user_id, "deleted_at": now.isoformat()}, "user", user_id)
    write_audit(db, "system", "identity", "member.anonymize", "user", user_id)
    db.commit()
    return "deleted"


# ================================================================ 幂等辅助

def stored_response(db: Session, scope: str, key: str, actor: str, fingerprint: str) -> Any | None:
    """幂等重放预检：已完成的同键请求直接返回首次结果（用于需要先做外部调用的写接口）；不同请求体 4093。"""
    record = db.execute(select(IdempotencyRecord).where(IdempotencyRecord.actor == actor, IdempotencyRecord.scope == scope,
                                                        IdempotencyRecord.key == key)).scalar_one_or_none()
    if record is None:
        return None
    if record.fingerprint and record.fingerprint != fingerprint:
        raise AppError(ErrorCode.IDEMPOTENCY_MISMATCH, "idempotency key reused with a different request body")
    if record.status == "done" and record.response_json is not None:
        return json.loads(record.response_json)
    return None
