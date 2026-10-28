"""接口依赖：会员身份、可选会员身份、后台员工权限。

后台：直接用 22-admin-rbac 的正式依赖 `admin_rbac.require_permission`（服务端会话、空闲 / 绝对超时、停用即失效、
权限码含 `coupon:*` 通配、门店范围）。没有本地替换钩子，令牌里自带的权限声明一律不认（失败即关闭）。
会员：默认 01-identity（decode_access + 会话仍有效）；`set_member_resolver(fn)` 可替换（测试 / 项目）。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from admin_rbac import Staff, assert_in_scope, declare_permissions, store_scope
from admin_rbac import require_permission as _rbac_require
from luzback import AppError, ErrorCode, decode_token, get_db, get_settings

PERMISSIONS = (
    ("coupon:read", "优惠券查看（模板、会员券、统计）"),
    ("coupon:write", "优惠券模板配置、发布、暂停、结束、作废会员券"),
    ("coupon:issue", "定向发券 / 补偿发券"),
    ("coupon:code", "兑换码生成、作废与导出"),
    ("coupon:export", "优惠券数据导出"),
)
declare_permissions("coupon", PERMISSIONS)

_bearer = HTTPBearer(auto_error=False)


def require_permission(code: str, fresh: bool = False) -> Callable[..., Staff]:
    """= admin_rbac.require_permission(code, fresh=fresh)，返回 admin_rbac.Staff（.id / .store_scope）。"""
    return _rbac_require(code, fresh=fresh)


def assert_template_scope(staff: Staff, store_id: int | None, write: bool = True) -> None:
    """门店范围：写操作按 admin_rbac.assert_in_scope（全局模板只允许全门店员工，越界 4031）；
    读操作额外允许门店员工查看全局模板（store_id 为空）。"""
    if not write and store_id is None:
        return
    assert_in_scope(staff, store_id)


__all__ = ["PERMISSIONS", "Staff", "assert_template_scope", "current_member", "identity_member_resolver",
           "jwt_member_resolver", "optional_member", "require_permission", "set_member_resolver", "store_scope"]


MemberResolver = Callable[[str, Session], int]
_member_resolver: MemberResolver | None = None


def set_member_resolver(fn: MemberResolver | None) -> None:
    """注入会员令牌解析：fn(token, db) -> user_id（失败抛 4010）。默认用 01-identity。"""
    global _member_resolver
    _member_resolver = fn


def jwt_member_resolver(token: str, db: Session) -> int:
    """只校验令牌签名与类型（identity 未装配时的退路；测试也用它）。"""
    s = get_settings()
    claims = decode_token(token, s.jwt_secret, kind="access", issuer=s.jwt_issuer)
    if claims.get("role") == "staff" or not str(claims.get("sub", "")).isdigit():
        raise AppError(ErrorCode.UNAUTHORIZED, "member token required")
    return int(claims["sub"])


def identity_member_resolver(token: str, db: Session) -> int:
    """01-identity：decode_access（aud / sid / jti）+ 会话仍有效（登出、停用后立即失效）。"""
    from identity import service as ids  # type: ignore

    claims = ids.decode_access(token)
    if claims.get("role") == "staff":
        raise AppError(ErrorCode.UNAUTHORIZED, "member token required")
    if hasattr(ids, "assert_session_active"):
        ids.assert_session_active(db, claims)
    return int(claims["sub"])


def _resolve_member(token: str, db: Session) -> int:
    if _member_resolver is not None:
        return _member_resolver(token, db)
    try:
        import identity.service  # type: ignore  # noqa: F401
    except Exception:  # noqa: BLE001
        return jwt_member_resolver(token, db)
    return identity_member_resolver(token, db)


def current_member(creds: HTTPAuthorizationCredentials | None = Depends(_bearer), db: Session = Depends(get_db)) -> int:
    """会员 id（只来自令牌）。缺失或无效 4010。"""
    if creds is None:
        raise AppError(ErrorCode.UNAUTHORIZED, "missing bearer token")
    return _resolve_member(creds.credentials, db)


def optional_member(creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
                    db: Session = Depends(get_db)) -> int | None:
    """领券中心可匿名浏览；带了令牌就校验（无效令牌仍然 4010，避免静默降级）。"""
    if creds is None:
        return None
    return _resolve_member(creds.credentials, db)
