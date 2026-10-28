"""鉴权：后台用 22-admin-rbac 的正式依赖（会话、权限码、门店范围都由它校验），顾客端用 01-identity 的令牌 + 会话检查。

- require_permission(code, fresh=False)：admin_rbac.require_permission → 映射成本模块的 StaffActor（id、门店范围、请求上下文）。
  未授权 4010；缺权限 4030；fresh=True 且认证不新鲜 4011。
- current_member：identity.decode_access + assert_session_active（登出 / 停用后立即 4010）。
"""
from __future__ import annotations

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from admin_rbac import Staff, declare_permissions
from admin_rbac import require_permission as _rbac_require
from luzback import AppError, ErrorCode, get_db

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


def to_actor(staff: Staff, request: Request | None = None) -> StaffActor:
    return StaffActor(id=int(staff.id), store_scope=staff.store_scope, ip=staff.ip,
                      user_agent=staff.user_agent or (request.headers.get("User-Agent") if request else None),
                      trace_id=staff.trace_id or (getattr(request.state, "trace_id", None) if request else None))


def require_permission(code: str, fresh: bool = False):
    rbac_dep = _rbac_require(code, fresh=fresh)

    def dependency(request: Request, staff: Staff = Depends(rbac_dep)) -> StaffActor:
        return to_actor(staff, request)

    dependency.__name__ = f"lottery_require_{code.replace(':', '_')}"
    return dependency


def current_member(creds: HTTPAuthorizationCredentials | None = Depends(_bearer), db: Session = Depends(get_db)) -> int:
    """顾客端当前会员 id（4010）：identity 令牌校验 + 会话仍有效。"""
    if creds is None:
        raise AppError(ErrorCode.UNAUTHORIZED, "missing bearer token")
    from identity import service as ids  # L1 必备模块

    claims = ids.decode_access(creds.credentials)
    ids.assert_session_active(db, claims)
    return int(claims["sub"])


def declare() -> None:
    """把权限码登记到 admin_rbac 注册表。"""
    declare_permissions("lottery", PERMISSIONS)
