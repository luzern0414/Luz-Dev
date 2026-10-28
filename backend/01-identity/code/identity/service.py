"""identity 用例：登录、令牌、手机绑定、资料、注销。所有函数只操作会话，事务由本层 commit。

安全规则（SECURITY.md、ASVS）：
- code2session 经熔断与超时；失败归 5020，不泄露上游细节。
- 刷新令牌轮换：rv 条件 UPDATE，旧令牌即刻失效；重放返回 4010。
- 短信验证码：6 位随机数只存哈希；5 分钟有效；最多 5 次尝试；同手机号 1 次/分钟、10 次/天。
- 手机号加密存储；绑定时按哈希做唯一校验；注销要求 15 分钟内登录的"新鲜"令牌。
- 冻结或注销的账号登录返回 4030。
"""
from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from luzback import AppError, ErrorCode, UpstreamError, create_token, decode_token, get_settings, utcnow, write_audit
from luzback.crypto import FieldCipher
from luzback.resilience import BreakerRegistry, TransientError, resilient
from luzback.security import mask_phone, random_code

from .models import STATUS_ACTIVE, STATUS_DEACTIVATED, STATUS_FROZEN, SmsCode, User

Code2Session = Callable[[str], dict]  # wx.login code → {"openid", "unionid"?, "session_key"?}
PHONE_RE = re.compile(r"^1[3-9]\d{9}$")
SMS_TTL = timedelta(minutes=5)
SMS_MAX_ATTEMPTS = 5
breakers = BreakerRegistry()


def _cipher() -> FieldCipher:
    return FieldCipher(get_settings().field_encryption_key)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------- 令牌 ----------

def issue_tokens(user: User) -> dict:
    s = get_settings()
    subject = str(user.id)
    claims = {"role": "user", "pv": 1 if user.phone_verified_at else 0}
    return {
        "user_id": user.id,
        "access_token": create_token(subject, s.jwt_secret, s.access_ttl_seconds, kind="access", extra=claims, issuer=s.jwt_issuer),
        "refresh_token": create_token(subject, s.jwt_secret, s.refresh_ttl_seconds, kind="refresh",
                                      extra={"rv": user.refresh_version}, issuer=s.jwt_issuer),
        "expires_in": s.access_ttl_seconds,
    }


def decode_access(token: str) -> dict:
    s = get_settings()
    return decode_token(token, s.jwt_secret, kind="access", issuer=s.jwt_issuer)


def current_user_id(token: str, secret: str | None = None) -> int:
    """兼容旧签名：secret 参数忽略，统一用 settings。"""
    return int(decode_access(token)["sub"])


def require_fresh(claims: dict) -> None:
    s = get_settings()
    if utcnow().timestamp() - float(claims.get("iat", 0)) > s.fresh_window_seconds:
        raise AppError(ErrorCode.UNAUTHORIZED, "please login again to continue", data={"reason": "fresh_required"})


# ---------- 登录 ----------

def _get_or_create_user(db: Session, openid: str, unionid: str | None) -> User:
    user = db.execute(select(User).where(User.openid == openid)).scalar_one_or_none()
    if user is not None:
        return user
    user = User(openid=openid, unionid=unionid)
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()  # 并发首次登录：另一请求已创建
        user = db.execute(select(User).where(User.openid == openid)).scalar_one()
    return user


def _assert_active(user: User) -> None:
    if user.status == STATUS_FROZEN:
        raise AppError(ErrorCode.FORBIDDEN, "account is frozen, contact support")
    if user.status == STATUS_DEACTIVATED or user.deleted_at is not None:
        raise AppError(ErrorCode.FORBIDDEN, "account is deactivated")


def login_with_code(db: Session, code: str, code2session: Code2Session, *_legacy, **_legacy_kw) -> dict:
    """微信登录：code → openid（熔断 + 超时）→ upsert 用户 → 签发令牌。"""
    if not code:
        raise AppError(ErrorCode.BAD_REQUEST, "code required")
    s = get_settings()

    def call() -> dict:
        try:
            return code2session(code)
        except AppError:
            raise
        except Exception as exc:  # noqa: BLE001 网络 / 微信错误 → 瞬时错误，计入熔断
            raise TransientError(str(exc)) from exc

    try:
        info = resilient("wechat_code2session", call, timeout_s=s.http_timeout_read, attempts=2,
                         breaker=breakers.get("wechat_code2session"), sleep=lambda _: None)
    except UpstreamError as exc:
        raise AppError(ErrorCode.UPSTREAM_FAILED, "wechat login failed, please retry") from exc
    openid = info.get("openid") if isinstance(info, dict) else None
    if not openid:
        raise AppError(ErrorCode.UNAUTHORIZED, "wechat returned no openid")
    user = _get_or_create_user(db, openid, info.get("unionid"))
    _assert_active(user)
    user.last_login_at = utcnow()
    db.commit()
    return issue_tokens(user)


def refresh_tokens(db: Session, refresh_token: str, *_legacy, **_legacy_kw) -> dict:
    """刷新轮换：rv 必须等于数据库版本；条件 UPDATE 保证并发只有一个成功；旧令牌失效。"""
    s = get_settings()
    payload = decode_token(refresh_token, s.jwt_secret, kind="refresh", issuer=s.jwt_issuer)
    user_id = int(payload["sub"])
    user = db.get(User, user_id)
    if user is None or user.refresh_version != payload.get("rv"):
        raise AppError(ErrorCode.UNAUTHORIZED, "refresh token revoked")
    _assert_active(user)
    rows = db.execute(
        update(User).where(User.id == user_id, User.refresh_version == user.refresh_version)
        .values(refresh_version=User.refresh_version + 1)
    ).rowcount
    if rows != 1:
        db.rollback()
        raise AppError(ErrorCode.UNAUTHORIZED, "refresh token already used")
    db.commit()
    db.refresh(user)
    return issue_tokens(user)


def logout(db: Session, user_id: int) -> None:
    """撤销全部刷新令牌（版本 +1）；访问令牌到期自然失效（≤ 2 小时）。"""
    db.execute(update(User).where(User.id == user_id).values(refresh_version=User.refresh_version + 1))
    write_audit(db, "user", user_id, "user.logout", "user", user_id)
    db.commit()


# ---------- 手机号 ----------

def _validate_phone(phone: str) -> str:
    phone = (phone or "").strip()
    if not PHONE_RE.match(phone):
        raise AppError(ErrorCode.BAD_REQUEST, "invalid phone number")
    return phone


def send_sms_code(db: Session, phone: str, purpose: str, sender: Callable[[str, str], None], now: datetime | None = None) -> dict:
    """发送验证码：1 次/分钟、10 次/天（按手机号）；只存哈希；发送失败不写记录。"""
    phone = _validate_phone(phone)
    if purpose not in {"bind", "login"}:
        raise AppError(ErrorCode.BAD_REQUEST, "invalid purpose")
    now = now or utcnow()
    s = get_settings()
    phone_hash = _cipher().lookup_hash(phone)
    last = db.execute(
        select(SmsCode).where(SmsCode.phone_hash == phone_hash, SmsCode.purpose == purpose).order_by(SmsCode.id.desc())
    ).scalars().first()
    if last is not None and _aware(last.created_at) > now - timedelta(seconds=60):
        raise AppError(ErrorCode.RATE_LIMITED, "please wait before requesting another code", retry_after=60)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today = db.execute(
        select(func.count()).select_from(SmsCode).where(SmsCode.phone_hash == phone_hash, SmsCode.created_at >= day_start)
    ).scalar_one()
    if today >= s.sms_daily_limit:
        raise AppError(ErrorCode.LIMIT_REACHED, "daily code limit reached")
    code = random_code(6)
    try:
        sender(phone, code)
    except Exception as exc:  # noqa: BLE001
        raise AppError(ErrorCode.UPSTREAM_FAILED, "sms service unavailable") from exc
    db.add(SmsCode(phone_hash=phone_hash, purpose=purpose, code_hash=hashlib.sha256(f"{phone}:{code}".encode()).hexdigest(),
                   expires_at=now + SMS_TTL, created_at=now))
    db.commit()
    return {"expires_in": int(SMS_TTL.total_seconds()), "retry_after": 60}


def _consume_sms_code(db: Session, phone: str, purpose: str, code: str, now: datetime) -> None:
    phone_hash = _cipher().lookup_hash(phone)
    record = db.execute(
        select(SmsCode).where(SmsCode.phone_hash == phone_hash, SmsCode.purpose == purpose, SmsCode.used_at.is_(None))
        .order_by(SmsCode.id.desc())
    ).scalars().first()
    if record is None or _aware(record.expires_at) < now:
        raise AppError(ErrorCode.BAD_REQUEST, "code expired, request a new one")
    if record.attempts >= SMS_MAX_ATTEMPTS:
        raise AppError(ErrorCode.LIMIT_REACHED, "too many attempts, request a new code")
    expected = hashlib.sha256(f"{phone}:{code}".encode()).hexdigest()
    if not hmac.compare_digest(expected, record.code_hash):
        record.attempts += 1
        db.commit()
        raise AppError(ErrorCode.BAD_REQUEST, "wrong code", data={"attempts_left": SMS_MAX_ATTEMPTS - record.attempts})
    record.used_at = now


def bind_phone_with_code(db: Session, user_id: int, phone: str, code: str, now: datetime | None = None) -> dict:
    phone = _validate_phone(phone)
    now = now or utcnow()
    user = db.get(User, user_id)
    if user is None:
        raise AppError(ErrorCode.NOT_FOUND, "user not found")
    _consume_sms_code(db, phone, "bind", code, now)
    return _attach_phone(db, user, phone, now)


def bind_phone_from_wechat(db: Session, user_id: int, phone_code: str, resolver: Callable[[str], str], now: datetime | None = None) -> dict:
    """微信一键手机号：resolver(phone_code) → 明文手机号（调用 getuserphonenumber）。"""
    now = now or utcnow()
    user = db.get(User, user_id)
    if user is None:
        raise AppError(ErrorCode.NOT_FOUND, "user not found")
    try:
        phone = _validate_phone(resolver(phone_code))
    except AppError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise AppError(ErrorCode.UPSTREAM_FAILED, "wechat phone service unavailable") from exc
    return _attach_phone(db, user, phone, now)


def _attach_phone(db: Session, user: User, phone: str, now: datetime) -> dict:
    cipher = _cipher()
    phone_hash = cipher.lookup_hash(phone)
    owner = db.execute(select(User).where(User.phone_hash == phone_hash, User.id != user.id)).scalar_one_or_none()
    if owner is not None:
        db.rollback()
        raise AppError(ErrorCode.CONFLICT, "phone already bound to another account")
    user.phone_enc = cipher.encrypt(phone)
    user.phone_hash = phone_hash
    user.phone_verified_at = now
    write_audit(db, "user", user.id, "user.phone.bind", "user", user.id, after={"phone": mask_phone(phone)})
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise AppError(ErrorCode.CONFLICT, "phone already bound to another account") from exc
    return {"phone": mask_phone(phone), "phone_verified": True}


# ---------- 资料 ----------

def profile(db: Session, user_id: int, reveal_phone: bool = False) -> dict:
    user = db.get(User, user_id)
    if user is None or user.deleted_at is not None:
        raise AppError(ErrorCode.NOT_FOUND, "user not found")
    phone = _cipher().decrypt(user.phone_enc) if user.phone_enc else None
    return {
        "user_id": user.id,
        "nickname": user.nickname,
        "avatar_url": user.avatar_url,
        "gender": user.gender,
        "birthday": user.birthday,
        "phone": phone if reveal_phone else mask_phone(phone),
        "phone_verified": user.phone_verified_at is not None,
        "status": user.status,
        "created_at": user.created_at,
    }


def update_profile(db: Session, user_id: int, nickname: str | None = None, avatar_url: str | None = None,
                   gender: int | None = None, birthday: str | None = None) -> dict:
    user = db.get(User, user_id)
    if user is None:
        raise AppError(ErrorCode.NOT_FOUND, "user not found")
    before = {"nickname": user.nickname, "avatar_url": user.avatar_url, "gender": user.gender, "birthday": user.birthday}
    if nickname is not None:
        nickname = nickname.strip()
        if not 1 <= len(nickname) <= 20:
            raise AppError(ErrorCode.BAD_REQUEST, "nickname must be 1-20 chars")
        user.nickname = nickname
    if avatar_url is not None:
        if avatar_url and not avatar_url.startswith("https://"):
            raise AppError(ErrorCode.BAD_REQUEST, "avatar_url must be https")
        user.avatar_url = avatar_url[:500]
    if gender is not None:
        if gender not in (0, 1, 2):
            raise AppError(ErrorCode.BAD_REQUEST, "gender must be 0/1/2")
        user.gender = gender
    if birthday is not None:
        if birthday and not re.match(r"^\d{4}-\d{2}-\d{2}$", birthday):
            raise AppError(ErrorCode.BAD_REQUEST, "birthday must be YYYY-MM-DD")
        user.birthday = birthday or None
    after = {"nickname": user.nickname, "avatar_url": user.avatar_url, "gender": user.gender, "birthday": user.birthday}
    if before != after:
        write_audit(db, "user", user_id, "user.profile.update", "user", user_id, before=before, after=after)
    db.commit()
    return profile(db, user_id)


def deactivate(db: Session, user_id: int, claims: dict, reason: str | None = None) -> dict:
    """注销：需要新鲜令牌；状态 deactivated、软删除、撤销刷新令牌；手机号哈希解绑以便重新注册。"""
    require_fresh(claims)
    user = db.get(User, user_id)
    if user is None:
        raise AppError(ErrorCode.NOT_FOUND, "user not found")
    now = utcnow()
    rows = db.execute(
        update(User).where(User.id == user_id, User.status != STATUS_DEACTIVATED)
        .values(status=STATUS_DEACTIVATED, deleted_at=now, deactivated_at=now, phone_hash=None,
                refresh_version=User.refresh_version + 1)
    ).rowcount
    if rows != 1:
        raise AppError(ErrorCode.CONFLICT, "account already deactivated")
    write_audit(db, "user", user_id, "user.deactivate", "user", user_id, reason=reason)
    db.commit()
    return {"status": STATUS_DEACTIVATED, "deactivated_at": now}


def freeze(db: Session, user_id: int, frozen: bool, staff_id: int, reason: str) -> dict:
    """后台冻结 / 解冻（member:write）；冻结即撤销刷新令牌。"""
    status = STATUS_FROZEN if frozen else STATUS_ACTIVE
    rows = db.execute(
        update(User).where(User.id == user_id, User.status != STATUS_DEACTIVATED)
        .values(status=status, refresh_version=User.refresh_version + 1)
    ).rowcount
    if rows != 1:
        raise AppError(ErrorCode.NOT_FOUND, "user not found")
    write_audit(db, "staff", staff_id, "user.freeze" if frozen else "user.unfreeze", "user", user_id, reason=reason)
    db.commit()
    return {"user_id": user_id, "status": status}
