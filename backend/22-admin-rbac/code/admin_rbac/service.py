"""admin_rbac 管理用例：种子与首个超级管理员、员工、角色、权限码、审计与登录日志查询导出。

写用例不 commit（由 api 层 run_idempotent 统一提交，失败整体回滚）；只读与导出用例自行 commit 审计行。
防越权（MODULE.md §4 R5.x）：
- 不能改自己的角色 / 门店范围，不能停用 / 删除 / 重置自己（改密走本人接口）。
- 只能授予自己持有的权限码与门店；只有超级管理员能授予超级管理员角色。
- 只能管理“权限与门店范围都不超过自己”的员工；范围外的员工按不存在处理（4040）。
- 任何操作后必须至少保留一名在职超级管理员（4220）。
"""
from __future__ import annotations

import secrets
from collections.abc import Iterable, Mapping
from datetime import timedelta
from typing import Any

from sqlalchemy import Select, delete, exists, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from luzback import AppError, ErrorCode, Field, ListSpec, enqueue_event, write_audit
from luzback.audit import AuditLog
from luzback.export import export_csv
from luzback.security import mask_phone

from . import config
from .access import (  # noqa: F401  # assert_permission / store_scope 供旧适配器 `admin_rbac.service.*` 使用
    REASON_PASSWORD_CHANGE, Staff, assert_permission, audit, build_staff, load_grants, load_staff, log_sensitive_read,
    store_scope,
)
from .auth import cipher, clear_mfa, clear_throttle, decode_access, revoke_sessions, validate_session  # noqa: F401
from .models import (
    STAFF_STATUSES, AdminSession, STATUS_ACTIVE, STATUS_DISABLED, AdminLoginLog, AdminLoginThrottle, AdminRole, AdminRolePermission,
    AdminStoreScope, AdminUser, AdminUserRole,
)
from .passwords import hash_secret
from .permissions import WILDCARD, is_module_wildcard, registered, registered_codes, sync_permissions, validate_grant
from .rules import aware, field_error, validate_phone, validate_username
from .seed import BUILTIN_ROLES, SUPER_ADMIN

# ---------- 兼容层：其他模块的旧适配器（先 decode_token(kind="admin") 再调用这里）----------


def load_active_staff(db: Session, staff_id: int, rv: Any = None) -> Staff:
    """按令牌声明取员工：rv（= sid，会话 id）存在时完整校验服务端会话（撤销、空闲 / 绝对超时、在职）；
    受限会话（首登改密 / 必须绑定 MFA）→ 4011。返回 Staff（.id / .has / .store_scope / .permissions）。"""
    if rv is not None:
        try:
            sess = db.get(AdminSession, int(rv))
        except (TypeError, ValueError):
            sess = None
        user = validate_session(db, sess, staff_id=int(staff_id))
        staff = build_staff(db, user, session_id=sess.id, auth_at=sess.auth_at, amr=sess.amr)
    else:
        staff = load_staff(db, int(staff_id))
    if staff.restricted_reason:
        raise AppError(ErrorCode.MFA_REQUIRED, "complete account security setup first", data={"reason": staff.restricted_reason})
    return staff


def grants_of(db: Session, staff: Staff | AdminUser) -> tuple[frozenset[str], list[int] | None]:
    """(权限码, 门店范围)；门店范围 None = 全部门店，[] = 无门店（注意：不是“全部”）。"""
    st = staff if isinstance(staff, Staff) else build_staff(db, staff)
    return st.permissions, st.store_scope


# ---------- 种子 ----------


def ensure_seed(db: Session) -> dict[str, int]:
    """幂等：同步权限码注册表；补齐缺失的内置角色（已存在的不覆盖，允许项目调整）。

    内置角色的 `module:*` 原样存为通配授予（前缀匹配，模块以后新增的码自动覆盖），精确码原样存储；
    因此种子不依赖业务模块的导入顺序。"""
    added_perms = sync_permissions(db)
    existing = set(db.execute(select(AdminRole.builtin_code).where(AdminRole.builtin_code.is_not(None))).scalars().all())
    added_roles = 0
    for code, name, description, patterns, mfa in BUILTIN_ROLES:
        if code in existing:
            continue
        role = AdminRole(name=name, builtin_code=code, description=description, builtin=True, mfa_required=mfa,
                         created_by="system")
        db.add(role)
        db.flush()
        if code != SUPER_ADMIN:
            for perm in sorted(set(patterns)):
                db.add(AdminRolePermission(role_id=role.id, code=validate_grant(perm)))
        added_roles += 1
    db.commit()
    return {"permissions": added_perms, "roles": added_roles}


def _super_role(db: Session, lock: bool = False) -> AdminRole:
    stmt = select(AdminRole).where(AdminRole.builtin_code == SUPER_ADMIN)
    if lock:
        stmt = stmt.with_for_update()
    role = db.execute(stmt).scalar_one_or_none()
    if role is None:
        raise AppError(ErrorCode.CONFLICT, "roles not seeded; run ensure_seed first")
    return role


def _active_super_admins(db: Session) -> int:
    role = _super_role(db)
    return db.execute(select(func.count()).select_from(AdminUser).join(AdminUserRole, AdminUserRole.user_id == AdminUser.id)
                      .where(AdminUserRole.role_id == role.id, AdminUser.status == STATUS_ACTIVE,
                             AdminUser.deleted_at.is_(None))).scalar_one()


def _assert_super_admin_remains(db: Session) -> None:
    db.flush()
    if _active_super_admins(db) < 1:
        raise AppError(ErrorCode.BUSINESS_RULE, "at least one active super admin must remain",
                       data={"reason": "last_super_admin"})


def _temp_password() -> str:
    return secrets.token_urlsafe(15)  # 20 位，约 120 bit，满足无 MFA 时 ≥ 15 位


def bootstrap_super_admin(db: Session, username: str, name: str = "") -> dict:
    """部署时创建首个超级管理员（已有在职超级管理员 → 4090）。返回一次性临时口令，首登必须改密并绑定 MFA。"""
    ensure_seed(db)
    if _active_super_admins(db) > 0:
        raise AppError(ErrorCode.CONFLICT, "a super admin already exists")
    uname = validate_username(username)
    at = config.now()
    temp = _temp_password()
    user = AdminUser(username=uname, password_hash=hash_secret(temp), name=(name or "")[:32], all_stores=True,
                     must_change_password=True, temp_password_expires_at=at + timedelta(seconds=config.get_config().temp_password_ttl_seconds),
                     created_by="system", updated_by="system")
    db.add(user)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise AppError(ErrorCode.CONFLICT, "username already exists") from exc
    db.add(AdminUserRole(user_id=user.id, role_id=_super_role(db).id))
    write_audit(db, "system", "bootstrap", "staff.create", "staff", user.id, after={"username": uname, "role": SUPER_ADMIN})
    db.commit()
    return {"id": user.id, "username": uname, "temporary_password": temp}


# ---------- 防越权助手 ----------


def _roles(db: Session, role_ids: Iterable[int]) -> list[AdminRole]:
    ids = sorted({int(r) for r in role_ids})
    if not ids:
        return []
    rows = db.execute(select(AdminRole).where(AdminRole.id.in_(ids))).scalars().all()
    missing = sorted(set(ids) - {r.id for r in rows})
    if missing:
        raise field_error("role_ids", f"unknown role: {missing}")
    return list(rows)


def _codes_of(db: Session, roles: list[AdminRole]) -> frozenset[str]:
    if any(r.builtin_code == SUPER_ADMIN for r in roles):
        return frozenset(registered_codes() | {WILDCARD})
    ids = [r.id for r in roles]
    if not ids:
        return frozenset()
    return frozenset(db.execute(select(AdminRolePermission.code).where(AdminRolePermission.role_id.in_(ids))).scalars().all())


def _escalation(message: str, extra: Any = None) -> AppError:
    return AppError(ErrorCode.FORBIDDEN, message, data={"reason": "privilege_escalation", "extra": extra})


def assert_can_grant_codes(actor: Staff, codes: Iterable[str]) -> None:
    if actor.has(WILDCARD):
        return
    extra = sorted(c for c in set(codes) if not actor.has(c))
    if extra:
        raise _escalation("you cannot grant permissions you do not hold", extra)


def assert_can_grant_scope(actor: Staff, all_stores: bool, store_ids: Iterable[int]) -> None:
    if actor.all_stores:
        return
    if all_stores:
        raise _escalation("you cannot grant access to all stores")
    extra = sorted(set(store_ids) - set(actor.store_ids))
    if extra:
        raise _escalation("you cannot grant stores outside your scope", extra)


def _assert_can_assign_roles(db: Session, actor: Staff, roles: list[AdminRole]) -> None:
    if any(r.builtin_code == SUPER_ADMIN for r in roles) and not actor.is_super_admin:
        raise _escalation("only a super admin can grant the super admin role")
    assert_can_grant_codes(actor, _codes_of(db, roles))


def _visible(actor: Staff, user: AdminUser, stores: list[int]) -> bool:
    if actor.all_stores:
        return True
    return not user.all_stores and set(stores) <= set(actor.store_ids)


def _get_target(db: Session, actor: Staff, staff_id: int, manage: bool = True) -> AdminUser:
    """取目标员工：不存在 / 已删除 / 不在范围 → 4040；manage=True 时权限超过自己 → 4030。"""
    user = db.get(AdminUser, staff_id)
    if user is None or user.deleted_at is not None:
        raise AppError(ErrorCode.NOT_FOUND, "staff not found")
    codes, roles, stores = load_grants(db, user.id)
    if not _visible(actor, user, stores):
        raise AppError(ErrorCode.NOT_FOUND, "staff not found")
    if manage and user.id != actor.id:
        if any(r.builtin_code == SUPER_ADMIN for r in roles) and not actor.is_super_admin:
            raise _escalation("only a super admin can manage a super admin")
        assert_can_grant_codes(actor, codes)
    return user


def _not_self(actor: Staff, staff_id: int, what: str) -> None:
    if actor.id == staff_id:
        raise AppError(ErrorCode.FORBIDDEN, f"you cannot {what} yourself", data={"reason": "self_action"})


def _reason_required(reason: str | None) -> str:
    if not reason or not reason.strip():
        raise field_error("reason", "reason is required")
    return reason.strip()[:200]


# ---------- 员工：读取与序列化 ----------


def _batch_access(db: Session, user_ids: list[int]) -> tuple[dict[int, list[dict]], dict[int, list[int]]]:
    roles: dict[int, list[dict]] = {uid: [] for uid in user_ids}
    stores: dict[int, list[int]] = {uid: [] for uid in user_ids}
    if not user_ids:
        return roles, stores
    for uid, rid, name, code in db.execute(
            select(AdminUserRole.user_id, AdminRole.id, AdminRole.name, AdminRole.builtin_code)
            .join(AdminRole, AdminRole.id == AdminUserRole.role_id).where(AdminUserRole.user_id.in_(user_ids))
            .order_by(AdminRole.id)).all():
        roles[uid].append({"id": rid, "name": name, "builtin_code": code})
    for uid, sid in db.execute(select(AdminStoreScope.user_id, AdminStoreScope.store_id)
                               .where(AdminStoreScope.user_id.in_(user_ids)).order_by(AdminStoreScope.store_id)).all():
        stores[uid].append(sid)
    return roles, stores


def _locks(db: Session, usernames: list[str]) -> dict[str, Any]:
    if not usernames:
        return {}
    at = config.now()
    out = {}
    for name, locked_until in db.execute(select(AdminLoginThrottle.username, AdminLoginThrottle.locked_until)
                                         .where(AdminLoginThrottle.username.in_(usernames))).all():
        lu = aware(locked_until)
        out[name] = lu if lu is not None and lu > at else None
    return out


def _staff_dicts(db: Session, users: list[AdminUser]) -> list[dict]:
    ids = [u.id for u in users]
    roles, stores = _batch_access(db, ids)
    locks = _locks(db, [u.username for u in users])
    c = cipher()
    return [{
        "id": u.id, "username": u.username, "name": u.name, "phone": mask_phone(c.decrypt(u.phone_enc)) if u.phone_enc else None,
        "status": u.status, "all_stores": u.all_stores, "store_ids": stores[u.id], "roles": roles[u.id],
        "mfa_enabled": u.mfa_enabled, "must_change_password": u.must_change_password, "locked_until": locks.get(u.username),
        "last_login_at": aware(u.last_login_at), "last_login_ip": u.last_login_ip, "created_at": aware(u.created_at),
        "updated_at": aware(u.updated_at),
    } for u in users]


def staff_dict(db: Session, user: AdminUser) -> dict:
    return _staff_dicts(db, [user])[0]


STAFF_SPEC = ListSpec(
    fields={
        "status": Field(AdminUser.status, ops=("eq", "in"), choices=STAFF_STATUSES),
        "mfa_enabled": Field(AdminUser.mfa_enabled, kind="bool"),
        "all_stores": Field(AdminUser.all_stores, kind="bool"),
        "username": Field(AdminUser.username, sortable=True),
        "created_at": Field(AdminUser.created_at, kind="datetime", ops=("range",), sortable=True),
        "last_login_at": Field(AdminUser.last_login_at, kind="datetime", ops=("range", "null"), sortable=True),
    },
    id_column=AdminUser.id,
    keyword_columns=(AdminUser.username, AdminUser.name),
    default_sort="-created_at",
    extra_params=frozenset({"role_id", "store_id", "phone"}),
)


def _int_param(params: Mapping[str, Any], key: str) -> int | None:
    raw = params.get(key)
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise AppError(ErrorCode.BAD_REQUEST, f"{key}: expected integer") from exc


def _staff_base(actor: Staff, params: Mapping[str, Any]) -> Select:
    stmt = select(AdminUser).where(AdminUser.deleted_at.is_(None))
    if not actor.all_stores:  # 只看范围内员工：非全门店，且没有范围外门店
        stmt = stmt.where(AdminUser.all_stores.is_(False), ~exists().where(
            AdminStoreScope.user_id == AdminUser.id, AdminStoreScope.store_id.not_in(list(actor.store_ids) or [-1])))
    role_id = _int_param(params, "role_id")
    if role_id is not None:
        stmt = stmt.where(AdminUser.id.in_(select(AdminUserRole.user_id).where(AdminUserRole.role_id == role_id)))
    store_id = _int_param(params, "store_id")
    if store_id is not None:
        stmt = stmt.where(AdminUser.all_stores.is_(True) | AdminUser.id.in_(
            select(AdminStoreScope.user_id).where(AdminStoreScope.store_id == store_id)))
    phone = params.get("phone")
    if phone:
        stmt = stmt.where(AdminUser.phone_hash == cipher().lookup_hash(validate_phone(str(phone))))
    return stmt


def list_staff(db: Session, actor: Staff, params: Mapping[str, Any]) -> dict:
    result = STAFF_SPEC.page(db, _staff_base(actor, params), params)
    result["items"] = _staff_dicts(db, result["items"])
    return result


STAFF_EXPORT_HEADERS = ("ID", "账号", "姓名", "手机号", "状态", "全部门店", "门店", "角色", "MFA", "最后登录", "创建时间")


def export_staff(db: Session, actor: Staff, params: Mapping[str, Any]) -> bytes:
    roles_map: dict[int, list[str]] = {}
    for uid, name in db.execute(select(AdminUserRole.user_id, AdminRole.name).join(AdminRole, AdminRole.id == AdminUserRole.role_id)):
        roles_map.setdefault(uid, []).append(name)
    stores_map: dict[int, list[str]] = {}
    for uid, sid in db.execute(select(AdminStoreScope.user_id, AdminStoreScope.store_id)):
        stores_map.setdefault(uid, []).append(str(sid))
    c = cipher()

    def row(u: AdminUser) -> list[Any]:
        return [u.id, u.username, u.name, mask_phone(c.decrypt(u.phone_enc)) if u.phone_enc else "", u.status, u.all_stores,
                " ".join(stores_map.get(u.id, [])), "、".join(roles_map.get(u.id, [])), u.mfa_enabled,
                aware(u.last_login_at), aware(u.created_at)]

    data = b"".join(export_csv(db, STAFF_SPEC, _staff_base(actor, params), params, STAFF_EXPORT_HEADERS, row,
                               hard_limit=config.get_config().export_max_rows))
    audit(db, actor, "staff.export", "staff", "-", after={"filters": _filters(params), "bytes": len(data)})
    db.commit()
    return data


def get_staff(db: Session, actor: Staff, staff_id: int) -> dict:
    return staff_dict(db, _get_target(db, actor, staff_id, manage=False))


def reveal_phone(db: Session, actor: Staff, staff_id: int, reason: str | None) -> dict:
    """明文手机号（staff:sensitive）：写敏感查看审计。"""
    user = _get_target(db, actor, staff_id, manage=False)
    reason = _reason_required(reason)
    phone = cipher().decrypt(user.phone_enc) if user.phone_enc else None
    log_sensitive_read(db, actor, "staff", user.id, ["phone"], reason=reason)
    db.commit()
    return {"id": user.id, "phone": phone}


# ---------- 员工：写 ----------


def _replace_access(db: Session, user_id: int, role_ids: list[int], store_ids: list[int]) -> None:
    db.execute(delete(AdminUserRole).where(AdminUserRole.user_id == user_id))
    db.execute(delete(AdminStoreScope).where(AdminStoreScope.user_id == user_id))
    for rid in sorted(set(role_ids)):
        db.add(AdminUserRole(user_id=user_id, role_id=rid))
    for sid in sorted(set(store_ids)):
        db.add(AdminStoreScope(user_id=user_id, store_id=int(sid)))


def create_staff(db: Session, actor: Staff, username: str, name: str = "", phone: str | None = None,
                 role_ids: Iterable[int] = (), all_stores: bool = False, store_ids: Iterable[int] = ()) -> dict:
    """新建员工：系统生成一次性临时口令（只在本次响应返回），首登必须改密；不 commit。"""
    uname = validate_username(username)
    phone = validate_phone(phone)
    roles = _roles(db, role_ids)
    store_ids = sorted({int(s) for s in store_ids})
    if all_stores:
        store_ids = []
    _assert_can_assign_roles(db, actor, roles)
    assert_can_grant_scope(actor, all_stores, store_ids)
    at = config.now()
    temp = _temp_password()
    c = cipher()
    user = AdminUser(username=uname, password_hash=hash_secret(temp), name=(name or "").strip()[:32],
                     phone_enc=c.encrypt(phone) if phone else None, phone_hash=c.lookup_hash(phone) if phone else None,
                     all_stores=bool(all_stores), must_change_password=True,
                     temp_password_expires_at=at + timedelta(seconds=config.get_config().temp_password_ttl_seconds),
                     created_by=str(actor.id), updated_by=str(actor.id))
    db.add(user)
    try:
        db.flush()
    except IntegrityError as exc:
        raise AppError(ErrorCode.CONFLICT, "username already exists",
                       data={"errors": [{"field": "username", "message": "exists"}]}) from exc
    _replace_access(db, user.id, [r.id for r in roles], store_ids)
    db.flush()
    after = staff_dict(db, user)
    audit(db, actor, "staff.create", "staff", user.id, after=after)
    enqueue_event(db, "staff.created", {"staff_id": user.id}, "admin_user", user.id)
    return {**after, "temporary_password": temp}


def update_staff(db: Session, actor: Staff, staff_id: int, name: str | None = None, phone: str | None = None,
                 reason: str | None = None) -> dict:
    """改资料（姓名 / 手机号）。手机号传空串表示清除。"""
    user = _get_target(db, actor, staff_id)
    before = staff_dict(db, user)
    if name is not None:
        user.name = name.strip()[:32]
    if phone is not None:
        phone = validate_phone(phone)
        c = cipher()
        user.phone_enc = c.encrypt(phone) if phone else None
        user.phone_hash = c.lookup_hash(phone) if phone else None
    user.updated_by = str(actor.id)
    db.flush()
    after = staff_dict(db, user)
    audit(db, actor, "staff.update", "staff", user.id, before=before, after=after, reason=reason)
    return after


def set_staff_access(db: Session, actor: Staff, staff_id: int, role_ids: Iterable[int], all_stores: bool = False,
                     store_ids: Iterable[int] = (), reason: str | None = None) -> dict:
    """改角色与门店范围：不能改自己；只能授予自己持有的；撤销该员工全部会话；必须保留超级管理员。"""
    _not_self(actor, staff_id, "change the roles or store scope of")
    _super_role(db, lock=True)
    user = _get_target(db, actor, staff_id)
    roles = _roles(db, role_ids)
    store_ids = [] if all_stores else sorted({int(s) for s in store_ids})
    _assert_can_assign_roles(db, actor, roles)
    assert_can_grant_scope(actor, all_stores, store_ids)
    before = staff_dict(db, user)
    _replace_access(db, user.id, [r.id for r in roles], store_ids)
    user.all_stores = bool(all_stores)
    user.updated_by = str(actor.id)
    _assert_super_admin_remains(db)
    revoked = revoke_sessions(db, user.id, "access_changed")
    after = staff_dict(db, user)
    audit(db, actor, "staff.access.update", "staff", user.id, before=before, after={**after, "sessions_revoked": revoked},
          reason=reason)
    enqueue_event(db, "staff.access_changed", {"staff_id": user.id}, "admin_user", user.id)
    return after


def disable_staff(db: Session, actor: Staff, staff_id: int, reason: str | None = None) -> dict:
    _not_self(actor, staff_id, "disable")
    _super_role(db, lock=True)
    user = _get_target(db, actor, staff_id)
    rows = db.execute(update(AdminUser).where(AdminUser.id == staff_id, AdminUser.status == STATUS_ACTIVE)
                      .values(status=STATUS_DISABLED, updated_by=str(actor.id))).rowcount
    if rows != 1:
        raise AppError(ErrorCode.CONFLICT, "staff is already disabled")
    _assert_super_admin_remains(db)
    revoke_sessions(db, staff_id, "staff_disabled")
    audit(db, actor, "staff.disable", "staff", staff_id, before={"status": STATUS_ACTIVE}, after={"status": STATUS_DISABLED},
          reason=reason)
    enqueue_event(db, "staff.disabled", {"staff_id": staff_id}, "admin_user", staff_id)
    db.refresh(user)
    return staff_dict(db, user)


def enable_staff(db: Session, actor: Staff, staff_id: int, reason: str | None = None) -> dict:
    user = _get_target(db, actor, staff_id)
    rows = db.execute(update(AdminUser).where(AdminUser.id == staff_id, AdminUser.status == STATUS_DISABLED)
                      .values(status=STATUS_ACTIVE, updated_by=str(actor.id))).rowcount
    if rows != 1:
        raise AppError(ErrorCode.CONFLICT, "staff is not disabled")
    clear_throttle(db, user.username, config.now())
    audit(db, actor, "staff.enable", "staff", staff_id, before={"status": STATUS_DISABLED}, after={"status": STATUS_ACTIVE},
          reason=reason)
    enqueue_event(db, "staff.enabled", {"staff_id": staff_id}, "admin_user", staff_id)
    db.refresh(user)
    return staff_dict(db, user)


def delete_staff(db: Session, actor: Staff, staff_id: int, reason: str | None = None) -> dict:
    """软删除：deleted_at + 停用 + 撤销会话；账号名不复用，审计可追溯。"""
    _not_self(actor, staff_id, "delete")
    _super_role(db, lock=True)
    user = _get_target(db, actor, staff_id)
    at = config.now()
    rows = db.execute(update(AdminUser).where(AdminUser.id == staff_id, AdminUser.deleted_at.is_(None))
                      .values(deleted_at=at, status=STATUS_DISABLED, updated_by=str(actor.id))).rowcount
    if rows != 1:
        raise AppError(ErrorCode.NOT_FOUND, "staff not found")
    _assert_super_admin_remains(db)
    revoke_sessions(db, staff_id, "staff_deleted", at=at)
    audit(db, actor, "staff.delete", "staff", staff_id, before={"username": user.username, "status": user.status},
          reason=reason)
    enqueue_event(db, "staff.deleted", {"staff_id": staff_id}, "admin_user", staff_id)
    return {"id": staff_id, "deleted": True}


def unlock_staff(db: Session, actor: Staff, staff_id: int, reason: str | None = None) -> dict:
    user = _get_target(db, actor, staff_id)
    clear_throttle(db, user.username, config.now())
    audit(db, actor, "staff.unlock", "staff", staff_id, reason=reason)
    return staff_dict(db, user)


def reset_password(db: Session, actor: Staff, staff_id: int, reason: str | None = None) -> dict:
    """管理员重置口令：生成一次性临时口令（72 小时内首登必须改），撤销全部会话。不能重置自己。"""
    _not_self(actor, staff_id, "reset the password of")
    user = _get_target(db, actor, staff_id)
    at = config.now()
    temp = _temp_password()
    db.execute(update(AdminUser).where(AdminUser.id == staff_id).values(
        password_hash=hash_secret(temp), must_change_password=True, updated_by=str(actor.id),
        temp_password_expires_at=at + timedelta(seconds=config.get_config().temp_password_ttl_seconds)))
    clear_throttle(db, user.username, at)
    revoked = revoke_sessions(db, staff_id, "password_reset", at=at)
    audit(db, actor, "staff.password.reset", "staff", staff_id, after={"sessions_revoked": revoked}, reason=reason)
    enqueue_event(db, "staff.password_reset", {"staff_id": staff_id}, "admin_user", staff_id)
    return {"id": staff_id, "temporary_password": temp, "must_change_password": True}


def reset_mfa(db: Session, actor: Staff, staff_id: int, reason: str | None) -> dict:
    """重置他人 MFA（丢失设备）：必须由另一名管理员执行并填写原因；清密钥与恢复码，撤销全部会话。"""
    _not_self(actor, staff_id, "reset the MFA of")
    reason = _reason_required(reason)
    user = _get_target(db, actor, staff_id)
    before = {"mfa_enabled": user.mfa_enabled}
    clear_mfa(db, staff_id)
    revoked = revoke_sessions(db, staff_id, "mfa_reset")
    audit(db, actor, "staff.mfa.reset", "staff", staff_id, before=before, after={"mfa_enabled": False, "sessions_revoked": revoked},
          reason=reason)
    enqueue_event(db, "staff.mfa_reset", {"staff_id": staff_id, "by": actor.id}, "admin_user", staff_id)
    return {"id": staff_id, "mfa_enabled": False}


def staff_sessions(db: Session, actor: Staff, staff_id: int) -> list[dict]:
    from .auth import list_sessions

    _get_target(db, actor, staff_id, manage=False)
    return list_sessions(db, staff_id)


def revoke_staff_sessions(db: Session, actor: Staff, staff_id: int, reason: str | None = None) -> dict:
    """管理员强制下线（ASVS 7.4.5）。"""
    _get_target(db, actor, staff_id)
    count = revoke_sessions(db, staff_id, "admin_revoked", except_session_id=actor.session_id if actor.id == staff_id else None)
    audit(db, actor, "staff.session.revoke_all", "staff", staff_id, after={"revoked": count}, reason=reason)
    return {"id": staff_id, "revoked": count}


# ---------- 角色 ----------

ROLE_SPEC = ListSpec(
    fields={
        "builtin": Field(AdminRole.builtin, kind="bool"),
        "mfa_required": Field(AdminRole.mfa_required, kind="bool"),
        "name": Field(AdminRole.name, sortable=True),
        "created_at": Field(AdminRole.created_at, kind="datetime", ops=("range",), sortable=True),
    },
    id_column=AdminRole.id,
    keyword_columns=(AdminRole.name, AdminRole.description),
    default_sort="id",
)


def _role_dicts(db: Session, roles: list[AdminRole]) -> list[dict]:
    ids = [r.id for r in roles]
    perms: dict[int, list[str]] = {i: [] for i in ids}
    counts: dict[int, int] = {i: 0 for i in ids}
    if ids:
        for rid, code in db.execute(select(AdminRolePermission.role_id, AdminRolePermission.code)
                                    .where(AdminRolePermission.role_id.in_(ids)).order_by(AdminRolePermission.code)).all():
            perms[rid].append(code)
        for rid, n in db.execute(select(AdminUserRole.role_id, func.count()).join(AdminUser, AdminUser.id == AdminUserRole.user_id)
                                 .where(AdminUserRole.role_id.in_(ids), AdminUser.deleted_at.is_(None))
                                 .group_by(AdminUserRole.role_id)).all():
            counts[rid] = n
    return [{"id": r.id, "name": r.name, "builtin_code": r.builtin_code, "description": r.description, "builtin": r.builtin,
             "mfa_required": r.mfa_required, "editable": r.builtin_code != SUPER_ADMIN,
             "permissions": [WILDCARD] if r.builtin_code == SUPER_ADMIN else perms[r.id], "user_count": counts[r.id],
             "created_at": aware(r.created_at), "updated_at": aware(r.updated_at)} for r in roles]


def list_roles(db: Session, params: Mapping[str, Any]) -> dict:
    result = ROLE_SPEC.page(db, select(AdminRole), params)
    result["items"] = _role_dicts(db, result["items"])
    return result


def _get_role(db: Session, role_id: int) -> AdminRole:
    role = db.get(AdminRole, role_id)
    if role is None:
        raise AppError(ErrorCode.NOT_FOUND, "role not found")
    return role


def get_role(db: Session, role_id: int) -> dict:
    return _role_dicts(db, [_get_role(db, role_id)])[0]


def _validate_codes(codes: Iterable[str]) -> list[str]:
    """精确码必须已登记；`module:*` 要求该模块至少登记了一个码。`*` 只属于超级管理员角色，不能授予。"""
    codes = sorted({c.strip() for c in codes})
    known = registered_codes()
    prefixes = {c.split(":", 1)[0] for c in known}
    unknown = [c for c in codes
               if not (c in known or (is_module_wildcard(c) and c.split(":", 1)[0] in prefixes))]
    if unknown:
        raise field_error("permissions", f"unknown permission code: {unknown}")
    return codes


def _validate_role_name(name: str | None) -> str:
    name = (name or "").strip()
    if not 1 <= len(name) <= 32:
        raise field_error("name", "role name must be 1-32 characters")
    return name


def create_role(db: Session, actor: Staff, name: str, permissions: Iterable[str], description: str = "",
                mfa_required: bool = False) -> dict:
    name = _validate_role_name(name)
    codes = _validate_codes(permissions)
    assert_can_grant_codes(actor, codes)
    role = AdminRole(name=name, description=(description or "")[:200], mfa_required=bool(mfa_required),
                     created_by=str(actor.id), updated_by=str(actor.id))
    db.add(role)
    try:
        db.flush()
    except IntegrityError as exc:
        raise AppError(ErrorCode.CONFLICT, "role name already exists",
                       data={"errors": [{"field": "name", "message": "exists"}]}) from exc
    for code in codes:
        db.add(AdminRolePermission(role_id=role.id, code=code))
    db.flush()
    after = get_role(db, role.id)
    audit(db, actor, "role.create", "role", role.id, after=after)
    return after


def update_role(db: Session, actor: Staff, role_id: int, name: str | None = None, permissions: Iterable[str] | None = None,
                description: str | None = None, mfa_required: bool | None = None, reason: str | None = None) -> dict:
    """超级管理员角色不可编辑（4220）；只能编辑权限不超过自己的角色，新权限集也不能超过自己。"""
    role = _get_role(db, role_id)
    if role.builtin_code == SUPER_ADMIN:
        raise AppError(ErrorCode.BUSINESS_RULE, "the super admin role cannot be edited")
    before = get_role(db, role_id)
    assert_can_grant_codes(actor, before["permissions"])
    if name is not None:
        role.name = _validate_role_name(name)
    if description is not None:
        role.description = description[:200]
    if mfa_required is not None:
        role.mfa_required = bool(mfa_required)
    if permissions is not None:
        codes = _validate_codes(permissions)
        assert_can_grant_codes(actor, codes)
        db.execute(delete(AdminRolePermission).where(AdminRolePermission.role_id == role_id))
        for code in codes:
            db.add(AdminRolePermission(role_id=role_id, code=code))
    role.updated_by = str(actor.id)
    try:
        db.flush()
    except IntegrityError as exc:
        raise AppError(ErrorCode.CONFLICT, "role name already exists",
                       data={"errors": [{"field": "name", "message": "exists"}]}) from exc
    after = get_role(db, role_id)
    audit(db, actor, "role.update", "role", role_id, before=before, after=after, reason=reason)
    return after


def delete_role(db: Session, actor: Staff, role_id: int, reason: str | None = None) -> dict:
    """内置角色 4220；仍有员工使用 4090。"""
    role = _get_role(db, role_id)
    if role.builtin:
        raise AppError(ErrorCode.BUSINESS_RULE, "built-in roles cannot be deleted")
    before = get_role(db, role_id)
    assert_can_grant_codes(actor, before["permissions"])
    users = db.execute(select(func.count()).select_from(AdminUserRole).where(AdminUserRole.role_id == role_id)).scalar_one()
    if users:
        raise AppError(ErrorCode.CONFLICT, "role is still assigned to staff", data={"user_count": users})
    db.execute(delete(AdminRolePermission).where(AdminRolePermission.role_id == role_id))
    db.delete(role)
    audit(db, actor, "role.delete", "role", role_id, before=before, reason=reason)
    return {"id": role_id, "deleted": True}


def list_permissions() -> list[dict]:
    return [{"code": p.code, "module": p.module, "description": p.description} for p in registered()]


# ---------- 审计与登录日志 ----------

AUDIT_SPEC = ListSpec(
    fields={
        "actor_type": Field(AuditLog.actor_type, ops=("eq", "in"), choices=frozenset({"user", "staff", "system", "agent"})),
        "actor_id": Field(AuditLog.actor_id, ops=("eq", "in")),
        "action": Field(AuditLog.action, ops=("eq", "in"), sortable=True),
        "resource": Field(AuditLog.resource, ops=("eq", "in")),
        "resource_id": Field(AuditLog.resource_id),
        "trace_id": Field(AuditLog.trace_id),
        "created_at": Field(AuditLog.created_at, kind="datetime", ops=("range",), sortable=True),
    },
    id_column=AuditLog.id,
    keyword_columns=(AuditLog.action, AuditLog.resource_id, AuditLog.reason),
    default_sort="-created_at",
)

LOGIN_SPEC = ListSpec(
    fields={
        "user_id": Field(AdminLoginLog.user_id, kind="int"),
        "username": Field(AdminLoginLog.username),
        "success": Field(AdminLoginLog.success, kind="bool"),
        "factor": Field(AdminLoginLog.factor, ops=("eq", "in")),
        "reason": Field(AdminLoginLog.reason, ops=("eq", "in")),
        "ip": Field(AdminLoginLog.ip),
        "created_at": Field(AdminLoginLog.created_at, kind="datetime", ops=("range",), sortable=True),
    },
    id_column=AdminLoginLog.id,
    keyword_columns=(AdminLoginLog.username, AdminLoginLog.ip),
    default_sort="-created_at",
)


def _filters(params: Mapping[str, Any]) -> dict:
    return {k: str(v)[:100] for k, v in params.items() if k not in ("page", "page_size")}


def audit_dict(row: AuditLog) -> dict:
    return {"id": row.id, "actor_type": row.actor_type, "actor_id": row.actor_id, "action": row.action,
            "resource": row.resource, "resource_id": row.resource_id, "before_json": row.before_json,
            "after_json": row.after_json, "reason": row.reason, "ip": row.ip, "user_agent": row.user_agent,
            "trace_id": row.trace_id, "created_at": aware(row.created_at)}


def login_log_dict(row: AdminLoginLog) -> dict:
    return {"id": row.id, "user_id": row.user_id, "username": row.username, "factor": row.factor, "success": row.success,
            "reason": row.reason, "ip": row.ip, "user_agent": row.user_agent, "created_at": aware(row.created_at)}


def list_audit_logs(db: Session, params: Mapping[str, Any]) -> dict:
    return AUDIT_SPEC.page(db, select(AuditLog), params, serialize=audit_dict)


def get_audit_log(db: Session, log_id: int) -> dict:
    row = db.get(AuditLog, log_id)
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, "audit log not found")
    return audit_dict(row)


AUDIT_EXPORT_HEADERS = ("ID", "时间", "操作者类型", "操作者", "动作", "资源", "资源ID", "原因", "IP", "追踪ID", "变更前", "变更后")


def export_audit_logs(db: Session, actor: Staff, params: Mapping[str, Any]) -> bytes:
    """导出（与列表同一套筛选）；导出行为本身写审计（操作人、筛选条件、大小）。"""
    def row(r: AuditLog) -> list[Any]:
        return [r.id, aware(r.created_at), r.actor_type, r.actor_id, r.action, r.resource, r.resource_id, r.reason, r.ip,
                r.trace_id, r.before_json, r.after_json]

    data = b"".join(export_csv(db, AUDIT_SPEC, select(AuditLog), params, AUDIT_EXPORT_HEADERS, row,
                               hard_limit=config.get_config().export_max_rows))
    audit(db, actor, "audit.export", "audit_log", "-", after={"filters": _filters(params), "bytes": len(data)})
    db.commit()
    return data


def list_login_logs(db: Session, params: Mapping[str, Any]) -> dict:
    return LOGIN_SPEC.page(db, select(AdminLoginLog), params, serialize=login_log_dict)


LOGIN_EXPORT_HEADERS = ("ID", "时间", "员工ID", "账号", "因素", "成功", "原因", "IP", "UA")


def export_login_logs(db: Session, actor: Staff, params: Mapping[str, Any]) -> bytes:
    def row(r: AdminLoginLog) -> list[Any]:
        return [r.id, aware(r.created_at), r.user_id, r.username, r.factor, r.success, r.reason, r.ip, r.user_agent]

    data = b"".join(export_csv(db, LOGIN_SPEC, select(AdminLoginLog), params, LOGIN_EXPORT_HEADERS, row,
                               hard_limit=config.get_config().export_max_rows))
    audit(db, actor, "login_log.export", "admin_login_log", "-", after={"filters": _filters(params), "bytes": len(data)})
    db.commit()
    return data
