"""鉴权适配：顾客端用 identity.api.current_user（CONTRACTS）；后台按 CONTRACTS 应来自 admin_rbac.api.require_permission。

admin_rbac 尚未导出稳定的 FastAPI 依赖，这里提供本地适配器：
- 默认解析器：校验 kind=admin 的访问令牌 → 通过 `admin_rbac.load_staff(db, staff_id)`（包导出）加载员工与权限；
  admin_rbac 不可用时一律 4010（默认拒绝）。
- `set_staff_resolver(fn)` 可替换（项目接入 admin_rbac 正式依赖、测试注入）。
"""
from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Any

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from luzback import AppError, ErrorCode, decode_token, get_db, get_settings
from luzback.app import client_ip

from .service import StaffActor

PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("lottery:read", "抽奖活动 / 记录 / 统计查看"),
    ("lottery:write", "抽奖活动与奖池编辑"),
    ("lottery:publish", "抽奖活动发布 / 结束"),
    ("lottery:grant", "抽奖次数发放"),
    ("lottery:fulfil", "中奖发货 / 重试发奖 / 查看收货信息（写审计）"),
    ("lottery:export", "抽奖记录导出"),
)

_bearer = HTTPBearer(auto_error=False)

StaffResolver = Callable[[Request, Session, str, str], StaffActor]  # (request, db, token, permission) -> StaffActor


def _default_resolver(request: Request, db: Session, token: str, code: str) -> StaffActor:
    s = get_settings()
    claims = decode_token(token, s.jwt_secret, kind="admin", issuer=s.jwt_issuer)
    try:
        rbac = importlib.import_module("admin_rbac")
        load_staff = getattr(rbac, "load_staff")
    except (ImportError, AttributeError) as exc:
        raise AppError(ErrorCode.UNAUTHORIZED, "admin authentication is not configured") from exc
    staff: Any = load_staff(db, int(claims["sub"]))
    if not staff.has(code):
        raise AppError(ErrorCode.FORBIDDEN, "permission denied", data={"required": [code]})
    return StaffActor(id=int(staff.id), store_scope=staff.store_scope, ip=client_ip(request),
                      user_agent=request.headers.get("User-Agent"), trace_id=getattr(request.state, "trace_id", None))


_resolver: StaffResolver = _default_resolver


def set_staff_resolver(fn: StaffResolver | None) -> None:
    global _resolver
    _resolver = fn or _default_resolver


def require_permission(code: str):
    def dependency(request: Request, creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
                   db: Session = Depends(get_db)) -> StaffActor:
        if creds is None:
            raise AppError(ErrorCode.UNAUTHORIZED, "missing bearer token")
        return _resolver(request, db, creds.credentials, code)

    return dependency


def current_member(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> int:
    """顾客端当前会员 id（4010）。与 identity.api.current_user 同一令牌规则。"""
    if creds is None:
        raise AppError(ErrorCode.UNAUTHORIZED, "missing bearer token")
    try:
        from identity import current_user_id
    except ImportError:  # pragma: no cover - identity 是 L1 必备模块
        s = get_settings()
        return int(decode_token(creds.credentials, s.jwt_secret, kind="access", issuer=s.jwt_issuer)["sub"])
    return current_user_id(creds.credentials)


def declare() -> None:
    """把权限码登记到 admin_rbac 注册表（可用时）。"""
    try:
        rbac = importlib.import_module("admin_rbac")
        getattr(rbac, "declare_permissions")("lottery", PERMISSIONS)
    except (ImportError, AttributeError):
        pass
