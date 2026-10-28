"""接口依赖：会员身份、可选会员身份、后台员工权限（适配 22-admin-rbac）。

后台鉴权顺序：
1. 项目或测试用 `set_staff_authenticator(fn)` 注入：fn(request, db, permission_code) -> staff_id，失败抛 AppError。
2. 否则若 22-admin-rbac 的 `admin_rbac.service` 可用：校验员工访问令牌（role=staff）→ load_active_staff → assert_permission。
3. 否则（admin-rbac 未装配）：校验员工访问令牌，并按令牌声明 `perms` 判断（仅开发环境的退路）。
权限码通配：`coupon:*`、`*`。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from luzback import AppError, ErrorCode, decode_token, get_db, get_settings

PERMISSIONS = (
    ("coupon:read", "优惠券查看（模板、会员券、统计）"),
    ("coupon:write", "优惠券模板配置、发布、暂停、结束、作废会员券"),
    ("coupon:issue", "定向发券 / 补偿发券"),
    ("coupon:code", "兑换码生成、作废与导出"),
    ("coupon:export", "优惠券数据导出"),
)

try:  # 22-admin-rbac 的权限注册表（可选）
    from admin_rbac.permissions import declare_permissions as _declare  # type: ignore

    _declare("coupon", PERMISSIONS)
except Exception:  # noqa: BLE001 - admin-rbac 未装配时跳过
    pass

StaffAuthenticator = Callable[[Request, Session, str], int]
_staff_authenticator: StaffAuthenticator | None = None
_bearer = HTTPBearer(auto_error=False)


def set_staff_authenticator(fn: StaffAuthenticator | None) -> None:
    global _staff_authenticator
    _staff_authenticator = fn


def _token(request: Request) -> str:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer ") or len(header) < 8:
        raise AppError(ErrorCode.UNAUTHORIZED, "missing bearer token")
    return header[7:]


def _granted(perms: Any, code: str) -> bool:
    perms = set(perms or [])
    return code in perms or "*" in perms or f"{code.split(':', 1)[0]}:*" in perms


def _rbac() -> Any:
    """22-admin-rbac 的服务模块（需要 load_active_staff 与 assert_permission）；未装配返回 None。"""
    try:
        from admin_rbac import service as rbac  # type: ignore
    except Exception:  # noqa: BLE001
        return None
    return rbac if hasattr(rbac, "load_active_staff") and hasattr(rbac, "assert_permission") else None


def _default_staff_auth(request: Request, db: Session, code: str) -> int:
    s = get_settings()
    claims = decode_token(_token(request), s.jwt_secret, kind="access", issuer=s.jwt_issuer)
    if claims.get("role") != "staff":
        raise AppError(ErrorCode.UNAUTHORIZED, "staff token required")
    rbac = _rbac()
    if rbac is not None:
        staff = rbac.load_active_staff(db, int(claims["sub"]), claims.get("rv"))
        rbac.assert_permission(db, staff, code)
        return int(staff.id)
    if not _granted(claims.get("perms"), code):
        raise AppError(ErrorCode.FORBIDDEN, f"permission required: {code}", data={"required": code})
    return int(claims["sub"])


def require_permission(code: str) -> Callable[..., int]:
    def dependency(request: Request, db: Session = Depends(get_db)) -> int:
        return (_staff_authenticator or _default_staff_auth)(request, db, code)  # noqa: B023

    dependency.__name__ = f"require_{code.replace(':', '_')}"
    return dependency


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
