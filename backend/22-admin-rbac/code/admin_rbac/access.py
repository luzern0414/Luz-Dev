"""当前员工上下文与授权助手（其他模块通过 admin_rbac 导入使用）。

- Staff：请求内的员工快照（权限码、门店范围、会话信息），每请求从数据库加载，不缓存，角色变更立即生效。
- 默认拒绝：没有明确授予的权限码一律 4030；超级管理员持有通配 "*"；角色可授予模块通配 `module:*`（前缀匹配）。
- 门店范围：all_stores=True → None（全部）；否则为列表（可能为空 = 无门店）。越界 4031。
- 职责分离：assert_not_self（例如不能审批自己发起的退款）。
- 新鲜认证：assert_fresh（敏感操作要求最近 N 秒内完成过完整认证，否则 4011 reason=reauth_required）。
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import Select, false, select
from sqlalchemy.orm import Session

from luzback import AppError, ErrorCode, write_audit
from luzback import log_events as ev
from luzback.logging import log_event

from . import config
from .models import STATUS_ACTIVE, AdminRole, AdminRolePermission, AdminStoreScope, AdminUser, AdminUserRole
from .permissions import WILDCARD, grants, registered_codes
from .rules import aware, is_fresh
from .seed import SUPER_ADMIN

log = logging.getLogger("admin_rbac.access")

REASON_PASSWORD_CHANGE = "password_change_required"
REASON_MFA_ENROLL = "mfa_enrollment_required"
REASON_REAUTH = "reauth_required"


@dataclass(frozen=True)
class Staff:
    id: int
    username: str
    name: str = ""
    permissions: frozenset[str] = frozenset()
    all_stores: bool = False
    store_ids: tuple[int, ...] = ()
    role_ids: tuple[int, ...] = ()
    is_super_admin: bool = False
    mfa_enabled: bool = False
    mfa_required: bool = False
    must_change_password: bool = False
    session_id: int | None = None
    auth_at: datetime | None = None
    amr: str | None = None
    ip: str | None = None
    user_agent: str | None = None
    trace_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict, compare=False)

    def has(self, code: str) -> bool:
        return grants(self.permissions, code)

    @property
    def store_scope(self) -> list[int] | None:
        return None if self.all_stores else list(self.store_ids)

    @property
    def restricted_reason(self) -> str | None:
        """受限会话：首登改密 / 必须绑定 MFA 前，只能访问 me、改密、MFA 绑定、登出。"""
        if self.must_change_password:
            return REASON_PASSWORD_CHANGE
        if self.mfa_required and not self.mfa_enabled:
            return REASON_MFA_ENROLL
        return None


def mfa_required_for(roles: Iterable[AdminRole], codes: Iterable[str]) -> bool:
    cfg = config.get_config()
    if cfg.mfa_required_all:
        return True
    if any(r.mfa_required for r in roles):
        return True
    granted = frozenset(codes)
    return any(grants(granted, c) for c in cfg.mfa_required_permissions)


def load_grants(db: Session, staff_id: int) -> tuple[frozenset[str], list[AdminRole], list[int]]:
    """返回 (权限码, 角色列表, 门店列表)。超级管理员 = 注册表全集 + "*"。"""
    roles = list(db.execute(select(AdminRole).join(AdminUserRole, AdminUserRole.role_id == AdminRole.id)
                            .where(AdminUserRole.user_id == staff_id).order_by(AdminRole.id)).scalars().all())
    if any(r.builtin_code == SUPER_ADMIN for r in roles):
        codes = frozenset(registered_codes() | {WILDCARD})
    else:
        role_ids = [r.id for r in roles]
        codes = frozenset(db.execute(select(AdminRolePermission.code).where(AdminRolePermission.role_id.in_(role_ids)))
                          .scalars().all()) if role_ids else frozenset()
    stores = list(db.execute(select(AdminStoreScope.store_id).where(AdminStoreScope.user_id == staff_id)
                             .order_by(AdminStoreScope.store_id)).scalars().all())
    return codes, roles, stores


def build_staff(db: Session, user: AdminUser, *, session_id: int | None = None, auth_at: datetime | None = None,
                amr: str | None = None, ip: str | None = None, user_agent: str | None = None,
                trace_id: str | None = None) -> Staff:
    codes, roles, stores = load_grants(db, user.id)
    return Staff(
        id=user.id, username=user.username, name=user.name, permissions=codes, all_stores=bool(user.all_stores),
        store_ids=tuple(stores), role_ids=tuple(r.id for r in roles),
        is_super_admin=any(r.builtin_code == SUPER_ADMIN for r in roles), mfa_enabled=bool(user.mfa_enabled),
        mfa_required=mfa_required_for(roles, codes), must_change_password=bool(user.must_change_password),
        session_id=session_id, auth_at=aware(auth_at), amr=amr, ip=ip, user_agent=user_agent, trace_id=trace_id,
    )


def load_staff(db: Session, staff_id: int, **context: Any) -> Staff:
    """按 id 加载在职员工（任务、测试、服务间调用用）；不存在 / 已删除 / 停用 → 4010。"""
    user = db.get(AdminUser, staff_id)
    if user is None or user.deleted_at is not None or user.status != STATUS_ACTIVE:
        raise AppError(ErrorCode.UNAUTHORIZED, "session expired, please sign in again")
    return build_staff(db, user, **context)


# ---------- 授权助手 ----------

def assert_permission(*args: Any) -> None:
    """assert_permission(staff, *codes)：全部码都需持有，否则 4030。

    兼容旧签名 assert_permission(db, staff, code)（staff 可为 Staff 或 AdminUser）。
    """
    if args and isinstance(args[0], Staff):
        staff, codes = args[0], tuple(args[1:])
    elif len(args) >= 3 and isinstance(args[0], Session):
        staff = args[1] if isinstance(args[1], Staff) else build_staff(args[0], args[1])
        codes = tuple(args[2:])
    else:
        raise TypeError("assert_permission(staff, *codes) or assert_permission(db, staff, code)")
    if not codes:
        raise TypeError("at least one permission code is required")
    missing = [c for c in codes if not staff.has(c)]
    if missing:
        log_event(log, ev.SECURITY_AUTHZ_FAIL, logging.WARNING, outcome="failure", reason="permission_missing",
                  staff_ref=f"staff:{staff.id}", required=missing, source_ip=staff.ip)
        raise AppError(ErrorCode.FORBIDDEN, "permission denied", data={"required": missing})


def store_scope(staff: Staff) -> list[int] | None:
    """None = 全部门店；列表 = 可访问的门店 id（可能为空）。"""
    return staff.store_scope


def assert_in_scope(staff: Staff, store_id: int | None) -> None:
    """门店越界 4031。store_id 为空（全局对象）只允许全门店员工。"""
    scope = staff.store_scope
    if scope is None:
        return
    if not store_id or int(store_id) not in scope:
        log_event(log, ev.SECURITY_AUTHZ_FAIL, logging.WARNING, outcome="failure", reason="store_out_of_scope",
                  staff_ref=f"staff:{staff.id}", target_store_id=store_id, source_ip=staff.ip)
        raise AppError(ErrorCode.SCOPE_FORBIDDEN, "store out of scope", data={"store_id": store_id})


def apply_store_scope(stmt: Select, staff: Staff, column: Any) -> Select:
    """给查询追加门店范围条件：全部门店不加条件；空范围返回永假条件。"""
    scope = staff.store_scope
    if scope is None:
        return stmt
    if not scope:
        return stmt.where(false())
    return stmt.where(column.in_(scope))


def assert_not_self(staff: Staff, other_staff_id: int | str | None, action: str = "approve") -> None:
    """职责分离：发起人与审批人不能是同一人（例如退款发起与审批）。"""
    if other_staff_id is not None and str(other_staff_id) == str(staff.id):
        raise AppError(ErrorCode.FORBIDDEN, f"separation of duties: you cannot {action} your own request",
                       data={"reason": "separation_of_duties"})


def assert_fresh(staff: Staff, now: datetime | None = None, window_seconds: int | None = None) -> None:
    window = window_seconds if window_seconds is not None else config.get_config().fresh_auth_seconds
    if not is_fresh(now or config.now(), staff.auth_at, window):
        raise AppError(ErrorCode.MFA_REQUIRED, "please verify your identity again to continue",
                       data={"reason": REASON_REAUTH, "max_age_seconds": window})


def audit(db: Session, staff: Staff, action: str, resource: str, resource_id: Any, before: Any = None, after: Any = None,
          reason: str | None = None) -> None:
    write_audit(db, "staff", staff.id, action, resource, resource_id, before=before, after=after, reason=reason,
                ip=staff.ip, user_agent=staff.user_agent, trace_id=staff.trace_id)


def log_sensitive_read(db: Session, staff: Staff, resource: str, resource_id: Any, fields: Iterable[str],
                       reason: str | None = None) -> None:
    """敏感查看留痕（手机号明文、证件号等）：动作 `<resource>.sensitive_read`，与查询同事务，由调用方 commit。"""
    audit(db, staff, f"{resource}.sensitive_read", resource, resource_id, after={"fields": sorted(fields)}, reason=reason)
