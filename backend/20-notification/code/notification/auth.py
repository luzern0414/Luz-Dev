"""鉴权：会员走 01-identity（令牌 + 会话仍有效），员工走 22-admin-rbac 正式依赖（服务端会话 + 权限码，含 notification:* 通配）。

- current_member：identity.decode_access + assert_session_active（登出 / 停用后立即 4010）；identity 未装配时只校验令牌签名。
  项目可用 set_member_checker(fn(db, claims)) 追加检查。
- current_staff：admin_rbac.current_staff → 员工 id（受限会话 4011）。
- require_permission(code)：admin_rbac.require_permission(code) → 员工 id；令牌自带的权限声明一律不认（失败即关闭）。
"""
from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from admin_rbac import Staff, declare_permissions
from admin_rbac import current_staff as _rbac_current_staff
from admin_rbac import require_permission as _rbac_require
from luzback import AppError, ErrorCode, decode_token, get_db, get_settings

PERMISSIONS = {
    "notification:read": "查看通知投递日志与统计",
    "notification:export": "导出通知投递日志",
    "notification:resend": "重发失败的通知",
    "notification:template": "管理通知场景与模板映射",
}
declare_permissions("notification", PERMISSIONS.items())

_bearer = HTTPBearer(auto_error=False)

MemberChecker = Callable[[Session, dict], None]
_member_checker: MemberChecker | None = None


def set_member_checker(fn: MemberChecker | None) -> None:
    """会员令牌额外校验（identity 之外的项目规则），失败时抛 AppError(4010)。"""
    global _member_checker
    _member_checker = fn


def _member_claims(token: str, db: Session) -> dict:
    try:
        from identity import service as ids  # noqa: PLC0415
    except ImportError:  # pragma: no cover - identity 是 L1 必备模块
        s = get_settings()
        return decode_token(token, s.jwt_secret, kind="access", issuer=s.jwt_issuer)
    claims = ids.decode_access(token)
    ids.assert_session_active(db, claims)
    return claims


def current_member(creds: HTTPAuthorizationCredentials | None = Depends(_bearer), db: Session = Depends(get_db)) -> int:
    if creds is None:
        raise AppError(ErrorCode.UNAUTHORIZED, "missing bearer token")
    claims = _member_claims(creds.credentials, db)
    if _member_checker is not None:
        _member_checker(db, claims)
    return int(claims["sub"])


def current_staff(staff: Staff = Depends(_rbac_current_staff)) -> int:
    return int(staff.id)


def has_permission(granted, code: str) -> bool:
    """兼容旧导出：与 admin_rbac.grants 同规则（`*`、`notification:*`、精确码）。"""
    from admin_rbac import grants  # noqa: PLC0415

    return grants(frozenset(granted), code)


def require_permission(code: str, fresh: bool = False):
    if code not in PERMISSIONS:
        raise ValueError(f"undeclared permission {code}")
    rbac_dep = _rbac_require(code, fresh=fresh)

    def dependency(staff: Staff = Depends(rbac_dep)) -> int:
        return int(staff.id)

    dependency.__name__ = f"notification_require_{code.replace(':', '_')}"
    return dependency
