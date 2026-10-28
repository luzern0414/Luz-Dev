"""鉴权适配：会员令牌（kind=access）、员工令牌（kind=admin）与权限码检查。

会员：校验 identity 签发的 access 令牌（同一 LUZ_JWT_SECRET / issuer），sub 即会员 id；
不直接依赖 identity.api，避免会员模块重构时连带本模块不可导入。需要撤销检查的项目用 set_member_checker 注入。

22-admin-rbac 可导入时用它的 load_active_staff + grants_of（停用 / 撤销 / 权限缓存一致）；
不可导入时只认令牌里的 perms 声明（默认没有 → 一律 4030，失败即关闭）。项目可用 set_permission_resolver 替换。
权限码支持通配：notification:* 与 *。
"""
from __future__ import annotations

import logging
from collections.abc import Callable

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from luzback import AppError, ErrorCode, decode_token, get_db, get_settings

log = logging.getLogger("luz.notification")

PERMISSIONS = {
    "notification:read": "查看通知投递日志与统计",
    "notification:export": "导出通知投递日志",
    "notification:resend": "重发失败的通知",
    "notification:template": "管理通知场景与模板映射",
}

_bearer = HTTPBearer(auto_error=False)
PermissionResolver = Callable[[Session, int, dict], frozenset[str] | set[str]]
_resolver: PermissionResolver | None = None


def set_permission_resolver(fn: PermissionResolver | None) -> None:
    global _resolver
    _resolver = fn


def _token_kind() -> str:
    try:
        from admin_rbac.models import TOKEN_KIND_ACCESS  # noqa: PLC0415
        return TOKEN_KIND_ACCESS
    except Exception:  # noqa: BLE001
        return "admin"


def _default_permissions(db: Session, staff_id: int, claims: dict) -> frozenset[str]:
    try:
        from admin_rbac.service import grants_of, load_active_staff  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return frozenset(claims.get("perms") or ())
    staff = load_active_staff(db, staff_id, claims.get("rv"))
    codes, _stores = grants_of(db, staff)
    return frozenset(codes)


MemberChecker = Callable[[Session, dict], None]
_member_checker: MemberChecker | None = None


def set_member_checker(fn: MemberChecker | None) -> None:
    """会员令牌额外校验（如 identity 的冻结 / 注销 / refresh_version 检查），失败时抛 AppError(4010)。"""
    global _member_checker
    _member_checker = fn


def current_member(creds: HTTPAuthorizationCredentials | None = Depends(_bearer), db: Session = Depends(get_db)) -> int:
    if creds is None:
        raise AppError(ErrorCode.UNAUTHORIZED, "missing bearer token")
    s = get_settings()
    claims = decode_token(creds.credentials, s.jwt_secret, kind="access", issuer=s.jwt_issuer)
    if _member_checker is not None:
        _member_checker(db, claims)
    return int(claims["sub"])


def staff_claims(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> dict:
    if creds is None:
        raise AppError(ErrorCode.UNAUTHORIZED, "missing bearer token")
    s = get_settings()
    return decode_token(creds.credentials, s.jwt_secret, kind=_token_kind(), issuer=s.jwt_issuer)


def current_staff(claims: dict = Depends(staff_claims)) -> int:
    return int(claims["sub"])


def has_permission(granted: frozenset[str] | set[str], code: str) -> bool:
    module = code.split(":", 1)[0]
    return code in granted or f"{module}:*" in granted or "*" in granted


def require_permission(code: str):
    if code not in PERMISSIONS:
        raise ValueError(f"undeclared permission {code}")

    def dependency(claims: dict = Depends(staff_claims), db: Session = Depends(get_db)) -> int:
        staff_id = int(claims["sub"])
        resolver = _resolver or _default_permissions
        granted = frozenset(resolver(db, staff_id, claims))
        if not has_permission(granted, code):
            raise AppError(ErrorCode.FORBIDDEN, "permission denied", data={"required": code})
        return staff_id

    return dependency
