"""给其他模块测试用的员工令牌工厂：走真实的 admin_rbac 会话与授权（不是伪造声明）。

    from admin_rbac.testing import staff_headers, staff_actor
    db = memory_session("admin_rbac.models", "wallet.models", ...)
    h = staff_headers(db, "wallet:read", "wallet:adjust", store_ids=[1])   # Authorization 头
    client.get("/api/v1/admin/wallet/accounts", headers=h)

- codes：授予的权限码（精确码或 `module:*`）；未登记的精确码会自动登记。
- store_ids：None = 全部门店；列表 = 只能访问这些门店（[] = 无门店）。
- 员工 MFA 视为已绑定（不进入“必须绑定 MFA”的受限会话），会话刚完成认证（满足 require_fresh_auth）。
- user_agent 默认 "testclient"（与 fastapi.testclient 一致；会话绑定 UA）。
"""
from __future__ import annotations

import itertools
import threading
from collections.abc import Iterable

from sqlalchemy.orm import Session

from . import auth, config
from .access import Staff, build_staff
from .models import AMR_OTP, STAGE_ACTIVE, AdminRole, AdminRolePermission, AdminStoreScope, AdminUser, AdminUserRole
from .permissions import declare_permission, is_module_wildcard

_seq = itertools.count(1)
_lock = threading.Lock()
TEST_UA = "testclient"


def _next() -> int:
    with _lock:
        return next(_seq)


def make_staff(db: Session, *codes: str, store_ids: Iterable[int] | None = None, username: str | None = None,
               name: str = "") -> AdminUser:
    """建一个持有 codes 的员工（独立角色）；提交事务。"""
    n = _next()
    granted = sorted(set(codes))
    for code in granted:
        if code != "*" and not is_module_wildcard(code):
            declare_permission(code)
    user = AdminUser(username=username or f"t.staff{n}", password_hash="!test-only", name=name or f"测试员工{n}",
                     all_stores=store_ids is None, must_change_password=False, mfa_enabled=True, created_by="test")
    db.add(user)
    db.flush()
    if granted:
        role = AdminRole(name=f"测试角色{n}-{user.id}"[:32], description="test", created_by="test")
        db.add(role)
        db.flush()
        for code in granted:
            db.add(AdminRolePermission(role_id=role.id, code=code))
        db.add(AdminUserRole(user_id=user.id, role_id=role.id))
    for sid in sorted(set(store_ids or ())):
        db.add(AdminStoreScope(user_id=user.id, store_id=int(sid)))
    db.commit()
    return user


def staff_token(db: Session, *codes: str, store_ids: Iterable[int] | None = None, username: str | None = None,
                user_agent: str = TEST_UA) -> str:
    """建员工并开一个有效的后台会话，返回 Bearer 令牌。"""
    user = make_staff(db, *codes, store_ids=store_ids, username=username)
    return session_token(db, user, user_agent=user_agent)


def session_token(db: Session, user: AdminUser, user_agent: str = TEST_UA) -> str:
    token, _sess = auth._create_session(db, user, STAGE_ACTIVE, AMR_OTP, config.now(), "127.0.0.1", user_agent)
    db.commit()
    return token


def staff_headers(db: Session, *codes: str, store_ids: Iterable[int] | None = None, username: str | None = None,
                  user_agent: str = TEST_UA) -> dict[str, str]:
    return {"Authorization": f"Bearer {staff_token(db, *codes, store_ids=store_ids, username=username, user_agent=user_agent)}"}


def staff_actor(db: Session, *codes: str, store_ids: Iterable[int] | None = None, username: str | None = None) -> Staff:
    """服务层测试用的 Staff 快照（无会话，auth_at=now 视为新鲜认证）。"""
    user = make_staff(db, *codes, store_ids=store_ids, username=username)
    return build_staff(db, user, auth_at=config.now())


__all__ = ["TEST_UA", "make_staff", "session_token", "staff_actor", "staff_headers", "staff_token"]
