"""admin_rbac 用例：登录 / MFA / 刷新 / 登出、口令、员工、角色、权限解析、门店范围、审计查询。

规则（MODULES-DESIGN 22 §规则）：
- 登录失败 `failed_attempts + 1` 为单条条件 UPDATE（CASE 到阈值即写 locked_until）；锁定返回 4290 带剩余秒；成功清零。
- 锁到期后首次登录先条件清零（WHERE locked_until <= now），避免旧计数立刻再次锁定。
- mfa_enabled 的账号先拿 kind=mfa 的 5 分钟令牌，/auth/mfa 验 TOTP（漂移 ±1）后签发 access(kind=admin) + refresh(kind=admin_refresh)。
- access 令牌带 rv；停用 / 改密 / 重置 MFA / 登出 都 refresh_version + 1，现有 access 与 refresh 立即失效。
- 权限码按 (user_id, refresh_version) 缓存 60 秒；本进程内的角色 / 权限写入立即清缓存。
- 不能改自己的角色与门店范围；不能停用自己；编辑自己持有的角色时，新权限集必须是自己当前权限的子集。
- 所有后台写操作同事务写审计；每次登录尝试写登录日志（保留 180 天）。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import case, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, object_session

from luzback import AppError, ErrorCode, create_token, decode_token, get_settings, utcnow, write_audit
from luzback.audit import AuditLog
from luzback.crypto import FieldCipher
from luzback.pagination import parse_page
from luzback.security import (
    check_password_policy, hash_password, mask_phone, password_needs_rehash, random_token, totp_secret, totp_verify,
    verify_password,
)

from .models import (
    STATUS_ACTIVE, STATUS_DISABLED, TOKEN_KIND_ACCESS, TOKEN_KIND_MFA, TOKEN_KIND_REFRESH, AdminLoginLog, AdminPermission,
    AdminRole, AdminRolePermission, AdminStoreScope, AdminUser, AdminUserRole,
)
from .seed import ALL_CODES, DEFAULT_ROLES, PERMISSIONS, expand_codes

MFA_TOKEN_TTL_SECONDS = 300
PERMISSION_CACHE_TTL_SECONDS = 60
PASSWORD_REMIND_DAYS = 90
LOGIN_LOG_RETENTION_DAYS = 180
INITIAL_PASSWORD_BYTES = 9  # token_urlsafe(9) → 12 位，再拼数字保证策略通过

clock = time.monotonic  # 测试可替换


@dataclass(frozen=True)
class Actor:
    """执行后台写操作的员工及请求上下文（写审计 / 登录日志用）。"""

    id: int
    ip: str | None = None
    user_agent: str | None = None
    trace_id: str | None = None


def _actor(actor: Actor | int) -> Actor:
    return actor if isinstance(actor, Actor) else Actor(id=int(actor))


def _cipher() -> FieldCipher:
    return FieldCipher(get_settings().field_encryption_key)


def aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _audit(db: Session, actor: Actor, action: str, resource: str, resource_id: Any, before=None, after=None, reason=None) -> None:
    write_audit(db, "staff", actor.id, action, resource, resource_id, before=before, after=after, reason=reason,
                ip=actor.ip, user_agent=actor.user_agent, trace_id=actor.trace_id)


# ---------- 种子 ----------

def ensure_seed(db: Session) -> dict[str, int]:
    """幂等写入权限码全集与默认角色；返回新增数量。已存在的角色不覆盖其权限（允许项目调整）。"""
    existing = set(db.execute(select(AdminPermission.code)).scalars().all())
    added_perms = 0
    for code, module, description in PERMISSIONS:
        if code not in existing:
            db.add(AdminPermission(code=code, module=module, description=description))
            added_perms += 1
    db.flush()
    names = set(db.execute(select(AdminRole.name)).scalars().all())
    added_roles = 0
    for name, description, patterns in DEFAULT_ROLES:
        if name in names:
            continue
        role = AdminRole(name=name, description=description, builtin=True)
        db.add(role)
        db.flush()
        for code in expand_codes(patterns):
            db.add(AdminRolePermission(role_id=role.id, code=code))
        added_roles += 1
    db.commit()
    return {"permissions": added_perms, "roles": added_roles}


def bootstrap_admin(db: Session, username: str, password: str, name: str = "管理员") -> AdminUser:
    """首个系统管理员（部署脚本调用）：存在则返回现有账号，不改密码。"""
    ensure_seed(db)
    user = db.execute(select(AdminUser).where(AdminUser.username == username)).scalar_one_or_none()
    if user is not None:
        return user
    check_password_policy(password, get_settings().password_min_length)
    user = AdminUser(username=username, password_hash=hash_password(password), name=name, must_change_password=True,
                     created_by="system")
    db.add(user)
    db.flush()
    role = db.execute(select(AdminRole).where(AdminRole.name == "系统管理员")).scalar_one()
    db.add(AdminUserRole(user_id=user.id, role_id=role.id))
    write_audit(db, "system", "bootstrap", "staff.create", "staff", user.id, after={"username": username, "roles": [role.id]})
    db.commit()
    return user


# ---------- 令牌 ----------

def issue_tokens(user: AdminUser) -> dict:
    s = get_settings()
    subject = str(user.id)
    return {
        "staff_id": user.id,
        "access_token": create_token(subject, s.jwt_secret, s.admin_access_ttl_seconds, kind=TOKEN_KIND_ACCESS,
                                     extra={"role": "staff", "rv": user.refresh_version}, issuer=s.jwt_issuer),
        "refresh_token": create_token(subject, s.jwt_secret, s.admin_refresh_ttl_seconds, kind=TOKEN_KIND_REFRESH,
                                      extra={"rv": user.refresh_version}, issuer=s.jwt_issuer),
        "expires_in": s.admin_access_ttl_seconds,
        "must_change_password": user.must_change_password,
        "mfa_required": False,
    }


def decode_access(token: str) -> dict:
    s = get_settings()
    return decode_token(token, s.jwt_secret, kind=TOKEN_KIND_ACCESS, issuer=s.jwt_issuer)


def assert_fresh(claims: dict) -> None:
    """敏感操作要求 15 分钟内签发的 access（iat）；否则 4010 + reason=fresh_required，前端引导重新登录。"""
    s = get_settings()
    if utcnow().timestamp() - float(claims.get("iat", 0)) > s.fresh_window_seconds:
        raise AppError(ErrorCode.UNAUTHORIZED, "please login again to continue", data={"reason": "fresh_required"})


def load_active_staff(db: Session, staff_id: int, rv: int | None = None) -> AdminUser:
    """按令牌取员工：不存在 / 已删除 / 已停用 / 版本不符 → 4010（令牌视为已撤销）。"""
    user = db.get(AdminUser, staff_id)
    if user is None or user.deleted_at is not None or user.status != STATUS_ACTIVE:
        raise AppError(ErrorCode.UNAUTHORIZED, "session revoked")
    if rv is not None and user.refresh_version != rv:
        raise AppError(ErrorCode.UNAUTHORIZED, "session revoked")
    return user


# ---------- 登录 ----------

def _log_login(db: Session, user: AdminUser | None, username: str, actor_ip: str | None, user_agent: str | None,
               success: bool, reason: str, now: datetime) -> None:
    db.add(AdminLoginLog(user_id=user.id if user else None, username=username[:32], ip=actor_ip,
                         user_agent=(user_agent or "")[:500] or None, success=success, reason=reason, created_at=now))


def _register_failure(db: Session, user: AdminUser, now: datetime) -> int:
    """失败计数 +1；到阈值时同一条 UPDATE 写入 locked_until。返回剩余锁定秒数（0 表示未锁）。"""
    s = get_settings()
    lock_until = now + timedelta(seconds=s.login_lock_seconds)
    db.execute(
        update(AdminUser).where(AdminUser.id == user.id).values(
            failed_attempts=AdminUser.failed_attempts + 1,
            locked_until=case((AdminUser.failed_attempts + 1 >= s.login_max_failures, lock_until), else_=AdminUser.locked_until),
        )
    )
    db.refresh(user)
    locked_until = aware(user.locked_until)
    if locked_until is not None and locked_until > now:
        return max(1, int((locked_until - now).total_seconds()))
    return 0


def _assert_not_locked(db: Session, user: AdminUser, username: str, ip: str | None, ua: str | None, now: datetime) -> None:
    locked_until = aware(user.locked_until)
    if locked_until is None:
        return
    if locked_until > now:
        remaining = max(1, int((locked_until - now).total_seconds()))
        _log_login(db, user, username, ip, ua, False, "locked", now)
        db.commit()
        raise AppError(ErrorCode.RATE_LIMITED, "account locked, try again later", data={"locked_seconds": remaining},
                       retry_after=remaining)
    # 锁已到期：条件清零，重新计数
    db.execute(update(AdminUser).where(AdminUser.id == user.id, AdminUser.locked_until <= now)
               .values(failed_attempts=0, locked_until=None))
    db.refresh(user)


def _fail(db: Session, user: AdminUser, username: str, ip: str | None, ua: str | None, reason: str, now: datetime,
          message: str) -> AppError:
    remaining = _register_failure(db, user, now)
    _log_login(db, user, username, ip, ua, False, "locked" if remaining else reason, now)
    db.commit()
    if remaining:
        return AppError(ErrorCode.RATE_LIMITED, "too many failures, account locked", data={"locked_seconds": remaining},
                        retry_after=remaining)
    s = get_settings()
    return AppError(ErrorCode.UNAUTHORIZED, message, data={"attempts_left": max(0, s.login_max_failures - user.failed_attempts)})


def _succeed(db: Session, user: AdminUser, username: str, ip: str | None, ua: str | None, reason: str, now: datetime,
             password: str | None = None) -> None:
    values: dict[str, Any] = {"failed_attempts": 0, "locked_until": None, "last_login_at": now}
    if password is not None and password_needs_rehash(user.password_hash):
        values["password_hash"] = hash_password(password)
    db.execute(update(AdminUser).where(AdminUser.id == user.id).values(**values))
    _log_login(db, user, username, ip, ua, True, reason, now)
    db.commit()
    db.refresh(user)


def login(db: Session, username: str, password: str, ip: str | None = None, user_agent: str | None = None,
          now: datetime | None = None) -> dict:
    """账号密码登录。未知账号与密码错误同为 4010（不区分原因）；停用 4030；锁定 4290。"""
    now = now or utcnow()
    username = (username or "").strip()
    user = db.execute(select(AdminUser).where(AdminUser.username == username, AdminUser.deleted_at.is_(None))).scalar_one_or_none()
    if user is None:
        _log_login(db, None, username, ip, user_agent, False, "unknown_user", now)
        db.commit()
        raise AppError(ErrorCode.UNAUTHORIZED, "invalid username or password")
    if user.status != STATUS_ACTIVE:
        _log_login(db, user, username, ip, user_agent, False, "disabled", now)
        db.commit()
        raise AppError(ErrorCode.FORBIDDEN, "account disabled")
    _assert_not_locked(db, user, username, ip, user_agent, now)
    if not verify_password(password or "", user.password_hash):
        raise _fail(db, user, username, ip, user_agent, "bad_password", now, "invalid username or password")
    if user.mfa_enabled:
        s = get_settings()
        _log_login(db, user, username, ip, user_agent, True, "password_ok", now)
        db.commit()
        return {
            "staff_id": user.id,
            "mfa_required": True,
            "mfa_token": create_token(str(user.id), s.jwt_secret, MFA_TOKEN_TTL_SECONDS, kind=TOKEN_KIND_MFA,
                                      extra={"rv": user.refresh_version}, issuer=s.jwt_issuer),
            "expires_in": MFA_TOKEN_TTL_SECONDS,
        }
    _succeed(db, user, username, ip, user_agent, "ok", now, password=password)
    return issue_tokens(user)


def verify_mfa(db: Session, mfa_token: str, code: str, ip: str | None = None, user_agent: str | None = None,
               now: datetime | None = None) -> dict:
    """第二步：kind=mfa 令牌 + TOTP。错码计入失败次数（同样会锁定）。"""
    now = now or utcnow()
    s = get_settings()
    claims = decode_token(mfa_token, s.jwt_secret, kind=TOKEN_KIND_MFA, issuer=s.jwt_issuer)
    user = load_active_staff(db, int(claims["sub"]), claims.get("rv"))
    if not user.mfa_enabled or not user.mfa_secret_enc:
        raise AppError(ErrorCode.CONFLICT, "mfa not enabled")
    _assert_not_locked(db, user, user.username, ip, user_agent, now)
    secret = _cipher().decrypt(user.mfa_secret_enc)
    if not totp_verify(secret, code or "", at=now.timestamp()):
        raise _fail(db, user, user.username, ip, user_agent, "bad_mfa", now, "invalid mfa code")
    _succeed(db, user, user.username, ip, user_agent, "mfa", now)
    return issue_tokens(user)


def refresh_tokens(db: Session, refresh_token: str) -> dict:
    """刷新轮换：rv 条件 UPDATE，旧令牌（含旧 access）立即失效；重放 4010。"""
    s = get_settings()
    claims = decode_token(refresh_token, s.jwt_secret, kind=TOKEN_KIND_REFRESH, issuer=s.jwt_issuer)
    user = load_active_staff(db, int(claims["sub"]), claims.get("rv"))
    rows = db.execute(
        update(AdminUser).where(AdminUser.id == user.id, AdminUser.refresh_version == user.refresh_version)
        .values(refresh_version=AdminUser.refresh_version + 1)
    ).rowcount
    if rows != 1:
        db.rollback()
        raise AppError(ErrorCode.UNAUTHORIZED, "refresh token already used")
    db.commit()
    db.refresh(user)
    return issue_tokens(user)


def logout(db: Session, staff_id: int) -> None:
    db.execute(update(AdminUser).where(AdminUser.id == staff_id).values(refresh_version=AdminUser.refresh_version + 1))
    write_audit(db, "staff", staff_id, "staff.logout", "staff", staff_id)
    invalidate_permissions(staff_id)
    db.commit()


# ---------- 口令与 MFA（本人） ----------

def change_password(db: Session, actor: Actor | int, old_password: str, new_password: str, now: datetime | None = None) -> dict:
    """改密：旧密码必须正确；新密码满足策略且不同于旧密码；撤销其他会话（rv + 1）。"""
    actor = _actor(actor)
    now = now or utcnow()
    s = get_settings()
    user = db.get(AdminUser, actor.id)
    if user is None:
        raise AppError(ErrorCode.NOT_FOUND, "staff not found")
    if not verify_password(old_password or "", user.password_hash):
        raise AppError(ErrorCode.BAD_REQUEST, "old password is incorrect", data={"errors": [{"field": "old_password", "message": "incorrect"}]})
    check_password_policy(new_password, s.password_min_length)
    if new_password == old_password:
        raise AppError(ErrorCode.BAD_REQUEST, "new password must differ from the old one")
    db.execute(update(AdminUser).where(AdminUser.id == user.id).values(
        password_hash=hash_password(new_password), must_change_password=False, password_changed_at=now,
        refresh_version=AdminUser.refresh_version + 1, updated_by=str(actor.id)))
    _audit(db, actor, "staff.password.change", "staff", user.id)
    db.commit()
    db.refresh(user)
    invalidate_permissions(user.id)
    return issue_tokens(user)


def enroll_mfa(db: Session, actor: Actor | int) -> dict:
    """生成 TOTP 密钥（加密存储，未确认前 mfa_enabled 仍为 False）；返回密钥与 otpauth URI 供扫码。"""
    actor = _actor(actor)
    user = db.get(AdminUser, actor.id)
    if user is None:
        raise AppError(ErrorCode.NOT_FOUND, "staff not found")
    if user.mfa_enabled:
        raise AppError(ErrorCode.CONFLICT, "mfa already enabled; reset first")
    secret = totp_secret()
    user.mfa_secret_enc = _cipher().encrypt(secret)
    user.updated_by = str(actor.id)
    _audit(db, actor, "staff.mfa.enroll", "staff", user.id)
    db.commit()
    issuer = get_settings().service_name
    return {"secret": secret, "otpauth_uri": f"otpauth://totp/{issuer}:{user.username}?secret={secret}&issuer={issuer}&digits=6&period=30"}


def confirm_mfa(db: Session, actor: Actor | int, code: str, now: datetime | None = None) -> dict:
    actor = _actor(actor)
    now = now or utcnow()
    user = db.get(AdminUser, actor.id)
    if user is None:
        raise AppError(ErrorCode.NOT_FOUND, "staff not found")
    if user.mfa_enabled:
        raise AppError(ErrorCode.CONFLICT, "mfa already enabled")
    if not user.mfa_secret_enc:
        raise AppError(ErrorCode.CONFLICT, "enroll first")
    if not totp_verify(_cipher().decrypt(user.mfa_secret_enc), code or "", at=now.timestamp()):
        raise AppError(ErrorCode.BAD_REQUEST, "invalid mfa code", data={"errors": [{"field": "code", "message": "invalid"}]})
    rows = db.execute(update(AdminUser).where(AdminUser.id == user.id, AdminUser.mfa_enabled.is_(False))
                      .values(mfa_enabled=True, updated_by=str(actor.id))).rowcount
    if rows != 1:
        db.rollback()
        raise AppError(ErrorCode.CONFLICT, "mfa already enabled")
    _audit(db, actor, "staff.mfa.confirm", "staff", user.id, after={"mfa_enabled": True})
    db.commit()
    db.refresh(user)
    return {"mfa_enabled": True}


# ---------- 权限与门店范围 ----------

_perm_lock = threading.Lock()
_perm_cache: dict[tuple[int, int], tuple[float, frozenset[str], list[int] | None]] = {}


def invalidate_permissions(staff_id: int | None = None) -> None:
    with _perm_lock:
        if staff_id is None:
            _perm_cache.clear()
        else:
            for key in [k for k in _perm_cache if k[0] == staff_id]:
                _perm_cache.pop(key, None)


def _load_grants(db: Session, staff_id: int) -> tuple[frozenset[str], list[int] | None]:
    codes = db.execute(
        select(AdminRolePermission.code).join(AdminUserRole, AdminUserRole.role_id == AdminRolePermission.role_id)
        .where(AdminUserRole.user_id == staff_id)
    ).scalars().all()
    stores = db.execute(select(AdminStoreScope.store_id).where(AdminStoreScope.user_id == staff_id).order_by(AdminStoreScope.store_id)).scalars().all()
    return frozenset(codes), (list(stores) or None)


def grants_of(db: Session, staff: AdminUser) -> tuple[frozenset[str], list[int] | None]:
    """权限码与门店范围，按 (id, refresh_version) 缓存 60 秒；桩对象（测试）直接读其 _permissions / _store_scope。"""
    stub = getattr(staff, "_permissions", None)
    if stub is not None:
        return frozenset(stub), getattr(staff, "_store_scope", None)
    key = (staff.id, staff.refresh_version)
    now = clock()
    with _perm_lock:
        hit = _perm_cache.get(key)
        if hit is not None and hit[0] > now:
            return hit[1], hit[2]
    codes, stores = _load_grants(db, staff.id)
    with _perm_lock:
        _perm_cache[key] = (now + PERMISSION_CACHE_TTL_SECONDS, codes, stores)
    return codes, stores


def permissions_of(db: Session, staff: AdminUser) -> frozenset[str]:
    return grants_of(db, staff)[0]


def store_scope(staff: AdminUser) -> list[int] | None:
    """None = 全部门店；否则为可访问门店 id 列表。无会话的桩对象读 _store_scope（默认全部）。"""
    stub = getattr(staff, "_store_scope", None)
    if stub is not None or getattr(staff, "_permissions", None) is not None:
        return list(stub) if stub is not None else None
    db = object_session(staff)
    if db is None:
        return None
    return grants_of(db, staff)[1]


def assert_in_scope(staff: AdminUser, store_id: int | None) -> None:
    """门店越界 4031。store_id 为 None / 0（全局对象）只允许全门店员工访问。"""
    scope = store_scope(staff)
    if scope is None:
        return
    if not store_id or int(store_id) not in scope:
        raise AppError(ErrorCode.SCOPE_FORBIDDEN, "store out of scope", data={"store_id": store_id})


def assert_permission(db: Session, staff: AdminUser, code: str) -> None:
    codes = permissions_of(db, staff)
    if code not in codes and "*" not in codes:
        raise AppError(ErrorCode.FORBIDDEN, f"permission required: {code}", data={"required": code})


# ---------- 本人信息 ----------

def _roles_of(db: Session, staff_id: int) -> list[dict]:
    rows = db.execute(select(AdminRole).join(AdminUserRole, AdminUserRole.role_id == AdminRole.id)
                      .where(AdminUserRole.user_id == staff_id).order_by(AdminRole.id)).scalars().all()
    return [{"id": r.id, "name": r.name} for r in rows]


def me(db: Session, staff: AdminUser, now: datetime | None = None) -> dict:
    now = now or utcnow()
    codes, stores = grants_of(db, staff)
    changed_at = aware(staff.password_changed_at) or aware(staff.created_at) or now
    age_days = (now - changed_at).days
    return {
        "staff_id": staff.id,
        "username": staff.username,
        "name": staff.name,
        "roles": _roles_of(db, staff.id),
        "permissions": sorted(codes),
        "store_scope": stores,
        "mfa_enabled": staff.mfa_enabled,
        "must_change_password": staff.must_change_password,
        "password_age_days": age_days,
        "password_expires_in_days": PASSWORD_REMIND_DAYS - age_days,
        "last_login_at": staff.last_login_at,
    }


# ---------- 员工管理 ----------

def _staff_dict(db: Session, user: AdminUser, with_roles: bool = True) -> dict:
    phone = _cipher().decrypt(user.phone_enc) if user.phone_enc else None
    data = {
        "id": user.id, "username": user.username, "name": user.name, "phone": mask_phone(phone), "status": user.status,
        "mfa_enabled": user.mfa_enabled, "must_change_password": user.must_change_password,
        "locked_until": aware(user.locked_until), "last_login_at": user.last_login_at, "created_at": user.created_at,
    }
    if with_roles:
        data["roles"] = _roles_of(db, user.id)
        data["store_scope"] = db.execute(select(AdminStoreScope.store_id).where(AdminStoreScope.user_id == user.id)
                                         .order_by(AdminStoreScope.store_id)).scalars().all() or None
    return data


def _get_staff(db: Session, staff_id: int) -> AdminUser:
    user = db.get(AdminUser, staff_id)
    if user is None or user.deleted_at is not None:
        raise AppError(ErrorCode.NOT_FOUND, "staff not found")
    return user


def _validate_roles(db: Session, role_ids: list[int]) -> list[int]:
    role_ids = sorted(set(int(r) for r in role_ids))
    if not role_ids:
        return []
    found = set(db.execute(select(AdminRole.id).where(AdminRole.id.in_(role_ids))).scalars().all())
    missing = [r for r in role_ids if r not in found]
    if missing:
        raise AppError(ErrorCode.BAD_REQUEST, "unknown role", data={"errors": [{"field": "role_ids", "message": f"unknown: {missing}"}]})
    return role_ids


def _validate_phone(phone: str | None) -> str | None:
    if phone is None or phone == "":
        return None
    phone = phone.strip()
    if not (len(phone) == 11 and phone.isdigit() and phone[0] == "1"):
        raise AppError(ErrorCode.BAD_REQUEST, "invalid phone", data={"errors": [{"field": "phone", "message": "invalid"}]})
    return phone


def _initial_password() -> str:
    return random_token(INITIAL_PASSWORD_BYTES) + "9a"


def create_staff(db: Session, actor: Actor | int, username: str, name: str, role_ids: list[int], store_ids: list[int] | None = None,
                 phone: str | None = None, password: str | None = None) -> dict:
    """新建员工：初始密码一次性返回（未传则随机生成），must_change_password=True。"""
    actor = _actor(actor)
    s = get_settings()
    username = (username or "").strip()
    if not (3 <= len(username) <= 32) or not all(c.isalnum() or c in "._-" for c in username):
        raise AppError(ErrorCode.BAD_REQUEST, "username must be 3-32 chars of letters, digits, . _ -",
                       data={"errors": [{"field": "username", "message": "invalid"}]})
    initial = password or _initial_password()
    check_password_policy(initial, s.password_min_length)
    role_ids = _validate_roles(db, role_ids)
    phone = _validate_phone(phone)
    cipher = _cipher()
    user = AdminUser(username=username, password_hash=hash_password(initial), name=(name or "").strip()[:32],
                     phone_enc=cipher.encrypt(phone) if phone else None, phone_hash=cipher.lookup_hash(phone) if phone else None,
                     must_change_password=True, created_by=str(actor.id), updated_by=str(actor.id))
    db.add(user)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise AppError(ErrorCode.CONFLICT, "username already exists") from exc
    for role_id in role_ids:
        db.add(AdminUserRole(user_id=user.id, role_id=role_id))
    for store_id in sorted(set(store_ids or [])):
        db.add(AdminStoreScope(user_id=user.id, store_id=int(store_id)))
    _audit(db, actor, "staff.create", "staff", user.id,
           after={"username": username, "name": user.name, "phone": mask_phone(phone), "roles": role_ids, "store_scope": store_ids or None})
    db.commit()
    data = _staff_dict(db, user)
    data["initial_password"] = initial
    return data


def update_staff(db: Session, actor: Actor | int, staff_id: int, name: str | None = None, phone: str | None = None,
                 role_ids: list[int] | None = None, store_ids: list[int] | None = None, reason: str | None = None) -> dict:
    """改资料 / 角色 / 门店范围。改自己的角色或门店范围 → 4030（防自我升权）。"""
    actor = _actor(actor)
    user = _get_staff(db, staff_id)
    before = _staff_dict(db, user)
    if actor.id == user.id and (role_ids is not None or store_ids is not None):
        raise AppError(ErrorCode.FORBIDDEN, "cannot change your own roles or store scope")
    if name is not None:
        user.name = name.strip()[:32]
    if phone is not None:
        phone = _validate_phone(phone)
        cipher = _cipher()
        user.phone_enc = cipher.encrypt(phone) if phone else None
        user.phone_hash = cipher.lookup_hash(phone) if phone else None
    if role_ids is not None:
        role_ids = _validate_roles(db, role_ids)
        db.execute(delete(AdminUserRole).where(AdminUserRole.user_id == user.id))
        for role_id in role_ids:
            db.add(AdminUserRole(user_id=user.id, role_id=role_id))
    if store_ids is not None:
        db.execute(delete(AdminStoreScope).where(AdminStoreScope.user_id == user.id))
        for store_id in sorted(set(store_ids)):
            db.add(AdminStoreScope(user_id=user.id, store_id=int(store_id)))
    user.updated_by = str(actor.id)
    db.flush()
    after = _staff_dict(db, user)
    _audit(db, actor, "staff.update", "staff", user.id, before=before, after=after, reason=reason)
    db.commit()
    invalidate_permissions(user.id)
    return after


def disable_staff(db: Session, actor: Actor | int, staff_id: int, reason: str | None = None) -> dict:
    """停用：不能停用自己；条件 UPDATE active → disabled 并 rv + 1（会话立即失效）；已停用 4090。"""
    actor = _actor(actor)
    if actor.id == staff_id:
        raise AppError(ErrorCode.FORBIDDEN, "cannot disable yourself")
    user = _get_staff(db, staff_id)
    rows = db.execute(update(AdminUser).where(AdminUser.id == staff_id, AdminUser.status == STATUS_ACTIVE)
                      .values(status=STATUS_DISABLED, refresh_version=AdminUser.refresh_version + 1, updated_by=str(actor.id))).rowcount
    if rows != 1:
        db.rollback()
        raise AppError(ErrorCode.CONFLICT, "staff already disabled")
    _audit(db, actor, "staff.disable", "staff", staff_id, before={"status": STATUS_ACTIVE}, after={"status": STATUS_DISABLED}, reason=reason)
    db.commit()
    db.refresh(user)
    invalidate_permissions(staff_id)
    return _staff_dict(db, user, with_roles=False)


def enable_staff(db: Session, actor: Actor | int, staff_id: int, reason: str | None = None) -> dict:
    actor = _actor(actor)
    user = _get_staff(db, staff_id)
    rows = db.execute(update(AdminUser).where(AdminUser.id == staff_id, AdminUser.status == STATUS_DISABLED)
                      .values(status=STATUS_ACTIVE, failed_attempts=0, locked_until=None, updated_by=str(actor.id))).rowcount
    if rows != 1:
        db.rollback()
        raise AppError(ErrorCode.CONFLICT, "staff is not disabled")
    _audit(db, actor, "staff.enable", "staff", staff_id, before={"status": STATUS_DISABLED}, after={"status": STATUS_ACTIVE}, reason=reason)
    db.commit()
    db.refresh(user)
    return _staff_dict(db, user, with_roles=False)


def unlock_staff(db: Session, actor: Actor | int, staff_id: int, reason: str | None = None) -> dict:
    """人工解锁：清零失败计数与 locked_until。"""
    actor = _actor(actor)
    user = _get_staff(db, staff_id)
    db.execute(update(AdminUser).where(AdminUser.id == staff_id).values(failed_attempts=0, locked_until=None, updated_by=str(actor.id)))
    _audit(db, actor, "staff.unlock", "staff", staff_id, reason=reason)
    db.commit()
    db.refresh(user)
    return _staff_dict(db, user, with_roles=False)


def reset_password(db: Session, actor: Actor | int, staff_id: int, reason: str | None = None) -> dict:
    """重置密码：生成一次性初始密码，must_change_password=True，rv + 1。"""
    actor = _actor(actor)
    user = _get_staff(db, staff_id)
    initial = _initial_password()
    db.execute(update(AdminUser).where(AdminUser.id == staff_id).values(
        password_hash=hash_password(initial), must_change_password=True, password_changed_at=None,
        failed_attempts=0, locked_until=None, refresh_version=AdminUser.refresh_version + 1, updated_by=str(actor.id)))
    _audit(db, actor, "staff.password.reset", "staff", staff_id, reason=reason)
    db.commit()
    db.refresh(user)
    invalidate_permissions(staff_id)
    return {"id": staff_id, "initial_password": initial}


def reset_mfa(db: Session, actor: Actor | int, staff_id: int, reason: str | None = None) -> dict:
    actor = _actor(actor)
    user = _get_staff(db, staff_id)
    db.execute(update(AdminUser).where(AdminUser.id == staff_id).values(
        mfa_enabled=False, mfa_secret_enc=None, refresh_version=AdminUser.refresh_version + 1, updated_by=str(actor.id)))
    _audit(db, actor, "staff.mfa.reset", "staff", staff_id, before={"mfa_enabled": user.mfa_enabled}, after={"mfa_enabled": False}, reason=reason)
    db.commit()
    db.refresh(user)
    invalidate_permissions(staff_id)
    return {"id": staff_id, "mfa_enabled": False}


def get_staff(db: Session, staff_id: int) -> dict:
    return _staff_dict(db, _get_staff(db, staff_id))


def list_staff(db: Session, page: int | None = None, page_size: int | None = None, keyword: str | None = None,
               status: str | None = None) -> dict:
    page, size = parse_page(page, page_size)
    stmt = select(AdminUser).where(AdminUser.deleted_at.is_(None))
    if keyword:
        like = f"%{keyword.strip()}%"
        stmt = stmt.where(or_(AdminUser.username.like(like), AdminUser.name.like(like)))
    if status:
        stmt = stmt.where(AdminUser.status == status)
    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = db.execute(stmt.order_by(AdminUser.id.desc()).offset((page - 1) * size).limit(size)).scalars().all()
    return {"items": [_staff_dict(db, u) for u in rows], "page": page, "page_size": size, "total": total}


# ---------- 角色 ----------

def _role_dict(db: Session, role: AdminRole) -> dict:
    codes = db.execute(select(AdminRolePermission.code).where(AdminRolePermission.role_id == role.id).order_by(AdminRolePermission.code)).scalars().all()
    users = db.execute(select(func.count()).select_from(AdminUserRole).where(AdminUserRole.role_id == role.id)).scalar_one()
    return {"id": role.id, "name": role.name, "description": role.description, "builtin": role.builtin,
            "permissions": list(codes), "user_count": users}


def _validate_codes(codes: list[str]) -> list[str]:
    codes = sorted(set(c.strip() for c in codes))
    unknown = [c for c in codes if c not in ALL_CODES]
    if unknown:
        raise AppError(ErrorCode.BAD_REQUEST, "unknown permission code", data={"errors": [{"field": "permissions", "message": f"unknown: {unknown}"}]})
    return codes


def _assert_no_self_elevation(db: Session, actor: Actor, role_id: int, new_codes: list[str]) -> None:
    """编辑自己持有的角色：新权限集必须 ⊆ 自己当前权限集，否则 4030。"""
    holds = db.execute(select(AdminUserRole).where(AdminUserRole.user_id == actor.id, AdminUserRole.role_id == role_id)).first()
    if holds is None:
        return
    own = _load_grants(db, actor.id)[0]
    extra = sorted(set(new_codes) - own)
    if extra:
        raise AppError(ErrorCode.FORBIDDEN, "cannot grant yourself permissions you do not have", data={"extra": extra})


def list_roles(db: Session) -> list[dict]:
    return [_role_dict(db, r) for r in db.execute(select(AdminRole).order_by(AdminRole.id)).scalars().all()]


def create_role(db: Session, actor: Actor | int, name: str, permissions: list[str], description: str = "") -> dict:
    actor = _actor(actor)
    name = (name or "").strip()
    if not 1 <= len(name) <= 32:
        raise AppError(ErrorCode.BAD_REQUEST, "role name must be 1-32 chars", data={"errors": [{"field": "name", "message": "invalid"}]})
    codes = _validate_codes(permissions)
    role = AdminRole(name=name, description=(description or "")[:200])
    db.add(role)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise AppError(ErrorCode.CONFLICT, "role name already exists") from exc
    for code in codes:
        db.add(AdminRolePermission(role_id=role.id, code=code))
    _audit(db, actor, "role.create", "role", role.id, after={"name": name, "permissions": codes})
    db.commit()
    return _role_dict(db, role)


def update_role(db: Session, actor: Actor | int, role_id: int, name: str | None = None, permissions: list[str] | None = None,
                description: str | None = None, reason: str | None = None) -> dict:
    actor = _actor(actor)
    role = db.get(AdminRole, role_id)
    if role is None:
        raise AppError(ErrorCode.NOT_FOUND, "role not found")
    before = _role_dict(db, role)
    if name is not None:
        name = name.strip()
        if not 1 <= len(name) <= 32:
            raise AppError(ErrorCode.BAD_REQUEST, "role name must be 1-32 chars")
        role.name = name
    if description is not None:
        role.description = description[:200]
    if permissions is not None:
        codes = _validate_codes(permissions)
        _assert_no_self_elevation(db, actor, role_id, codes)
        db.execute(delete(AdminRolePermission).where(AdminRolePermission.role_id == role_id))
        for code in codes:
            db.add(AdminRolePermission(role_id=role_id, code=code))
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise AppError(ErrorCode.CONFLICT, "role name already exists") from exc
    after = _role_dict(db, role)
    _audit(db, actor, "role.update", "role", role_id, before=before, after=after, reason=reason)
    db.commit()
    invalidate_permissions()
    return after


def delete_role(db: Session, actor: Actor | int, role_id: int, reason: str | None = None) -> None:
    """内置角色 4220；仍有用户绑定 4090。"""
    actor = _actor(actor)
    role = db.get(AdminRole, role_id)
    if role is None:
        raise AppError(ErrorCode.NOT_FOUND, "role not found")
    if role.builtin:
        raise AppError(ErrorCode.BUSINESS_RULE, "builtin role cannot be deleted")
    users = db.execute(select(func.count()).select_from(AdminUserRole).where(AdminUserRole.role_id == role_id)).scalar_one()
    if users:
        raise AppError(ErrorCode.CONFLICT, "role still assigned to staff", data={"user_count": users})
    before = _role_dict(db, role)
    db.execute(delete(AdminRolePermission).where(AdminRolePermission.role_id == role_id))
    db.delete(role)
    _audit(db, actor, "role.delete", "role", role_id, before=before, reason=reason)
    db.commit()


def list_permissions(db: Session) -> list[dict]:
    rows = db.execute(select(AdminPermission).order_by(AdminPermission.module, AdminPermission.code)).scalars().all()
    return [{"code": p.code, "module": p.module, "description": p.description} for p in rows]


# ---------- 审计查询 ----------

def _audit_dict(row: AuditLog) -> dict:
    return {"id": row.id, "actor_type": row.actor_type, "actor_id": row.actor_id, "action": row.action, "resource": row.resource,
            "resource_id": row.resource_id, "before_json": row.before_json, "after_json": row.after_json, "reason": row.reason,
            "ip": row.ip, "trace_id": row.trace_id, "created_at": row.created_at}


def list_audit_logs(db: Session, page: int | None = None, page_size: int | None = None, actor_id: str | None = None,
                    action: str | None = None, resource: str | None = None, resource_id: str | None = None,
                    since: datetime | None = None, until: datetime | None = None) -> dict:
    page, size = parse_page(page, page_size)
    stmt = select(AuditLog)
    if actor_id:
        stmt = stmt.where(AuditLog.actor_id == str(actor_id))
    if action:
        stmt = stmt.where(AuditLog.action.like(f"{action}%"))
    if resource:
        stmt = stmt.where(AuditLog.resource == resource)
    if resource_id:
        stmt = stmt.where(AuditLog.resource_id == str(resource_id))
    if since is not None:
        stmt = stmt.where(AuditLog.created_at >= since)
    if until is not None:
        stmt = stmt.where(AuditLog.created_at < until)
    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = db.execute(stmt.order_by(AuditLog.id.desc()).offset((page - 1) * size).limit(size)).scalars().all()
    return {"items": [_audit_dict(r) for r in rows], "page": page, "page_size": size, "total": total}


def get_audit_log(db: Session, log_id: int) -> dict:
    row = db.get(AuditLog, log_id)
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, "audit log not found")
    return _audit_dict(row)


def purge_login_logs(db: Session, now: datetime | None = None) -> int:
    cutoff = (now or utcnow()) - timedelta(days=LOGIN_LOG_RETENTION_DAYS)
    deleted = db.execute(delete(AdminLoginLog).where(AdminLoginLog.created_at < cutoff)).rowcount
    db.commit()
    return deleted
