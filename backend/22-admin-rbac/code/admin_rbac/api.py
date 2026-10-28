"""admin_rbac 路由与对外依赖。业务在 auth.py / service.py；这里只做参数、鉴权依赖、幂等与响应。

对外依赖（CONTRACTS.md：其他模块的后台路由这样用）：

    from admin_rbac.api import current_staff, require_permission, require_fresh
    @router.post("/orders/{id}/refund")
    def refund(id: int, staff=Depends(require_permission("order:refund")), _=Depends(require_fresh), ...):
        assert_in_scope(staff, order.store_id); assert_not_self(staff, request.created_by, "approve")

- authenticated_staff：任何有效会话（含“首登改密 / 必须绑定 MFA”的受限会话），只给 me / 改密 / MFA 绑定 / 登出用。
- current_staff：完整会话；受限会话返回 4011（data.reason = password_change_required | mfa_enrollment_required）。
- require_permission(*codes)：全部持有才放行，否则 4030；未登记的码自动登记到注册表。
- require_fresh（别名 require_fresh_auth）：最近 fresh_auth_seconds 内完成过完整认证，否则 4011 reason=reauth_required。
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from luzback import AppError, ErrorCode, get_db, idempotency_key, ok, run_idempotent
from luzback.app import client_ip

from . import auth, service
from .access import Staff, assert_fresh, assert_permission
from .permissions import declare_permission, validate_code

router = APIRouter(prefix="/api/v1/admin", tags=["admin_rbac"])
_bearer = HTTPBearer(auto_error=False)


# ---------- 依赖 ----------

def _ip(request: Request) -> str:
    return client_ip(request)


def _ua(request: Request) -> str | None:
    return request.headers.get("User-Agent")


def authenticated_staff(request: Request, creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
                        db=Depends(get_db)) -> Staff:
    return auth.authenticate(db, creds.credentials if creds else None, ip=_ip(request), user_agent=_ua(request),
                             trace_id=getattr(request.state, "trace_id", None))


def current_staff(staff: Staff = Depends(authenticated_staff)) -> Staff:
    reason = staff.restricted_reason
    if reason:
        raise AppError(ErrorCode.MFA_REQUIRED, "complete account security setup first", data={"reason": reason})
    return staff


def require_permission(*codes: str) -> Callable[..., Staff]:
    """FastAPI 依赖工厂：要求持有全部权限码（默认拒绝）。"""
    if not codes:
        raise ValueError("require_permission needs at least one code")
    for code in codes:
        validate_code(code)
        declare_permission(code)

    def dependency(staff: Staff = Depends(current_staff)) -> Staff:
        assert_permission(staff, *codes)
        return staff

    dependency.__name__ = "require_" + "_".join(c.replace(":", "_") for c in codes)
    return dependency


def require_fresh(staff: Staff = Depends(current_staff)) -> Staff:
    assert_fresh(staff)
    return staff


require_fresh_auth = require_fresh


def _fresh_basic(staff: Staff = Depends(authenticated_staff)) -> Staff:
    assert_fresh(staff)
    return staff


def _fingerprint(body: BaseModel | dict | None) -> str:
    data = body.model_dump(mode="json") if isinstance(body, BaseModel) else (body or {})
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _idem(db, staff: Staff, scope: str, key: str, body: Any, fn: Callable[[], Any]) -> Any:
    return run_idempotent(db, scope, key, fn, actor=f"staff:{staff.id}", fingerprint=_fingerprint(body), require_key=True)


def _idem_secret(db, staff: Staff, scope: str, key: str, body: Any, fn: Callable[[], dict]) -> dict:
    """含一次性临时口令的写：口令不进幂等记录；重放时返回 temporary_password=null（需要的话重新重置）。"""
    box: dict[str, Any] = {}

    def work() -> dict:
        result = fn()
        box["temporary_password"] = result.pop("temporary_password")
        return result

    result = dict(_idem(db, staff, scope, key, body, work))
    if "temporary_password" in box:
        return {**result, "temporary_password": box["temporary_password"]}
    return {**result, "temporary_password": None, "replayed": True}


def _csv(data: bytes, filename: str) -> Response:
    return Response(content=data, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ---------- 请求模型 ----------

class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)
    captcha_token: str | None = Field(default=None, max_length=2048)


class MfaIn(BaseModel):
    mfa_token: str = Field(min_length=10, max_length=2048)
    code: str | None = Field(default=None, min_length=6, max_length=8)
    recovery_code: str | None = Field(default=None, min_length=10, max_length=20)


class ReauthIn(BaseModel):
    password: str = Field(min_length=1, max_length=1024)
    code: str | None = Field(default=None, min_length=6, max_length=8)
    recovery_code: str | None = Field(default=None, min_length=10, max_length=20)


class ChangePasswordIn(BaseModel):
    old_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=1, max_length=1024)


class CodeIn(BaseModel):
    code: str = Field(min_length=6, max_length=8)


class StaffCreateIn(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    name: str = Field(default="", max_length=32)
    phone: str | None = Field(default=None, max_length=20)
    role_ids: list[int] = Field(default_factory=list, max_length=20)
    all_stores: bool = False
    store_ids: list[int] = Field(default_factory=list, max_length=500)


class StaffUpdateIn(BaseModel):
    name: str | None = Field(default=None, max_length=32)
    phone: str | None = Field(default=None, max_length=20)
    reason: str | None = Field(default=None, max_length=200)


class AccessIn(BaseModel):
    role_ids: list[int] = Field(default_factory=list, max_length=20)
    all_stores: bool = False
    store_ids: list[int] = Field(default_factory=list, max_length=500)
    reason: str | None = Field(default=None, max_length=200)


class ReasonIn(BaseModel):
    reason: str | None = Field(default=None, max_length=200)


class RoleIn(BaseModel):
    name: str = Field(min_length=1, max_length=32)
    description: str = Field(default="", max_length=200)
    permissions: list[str] = Field(default_factory=list, max_length=500)
    mfa_required: bool = False


class RoleUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=32)
    description: str | None = Field(default=None, max_length=200)
    permissions: list[str] | None = Field(default=None, max_length=500)
    mfa_required: bool | None = None
    reason: str | None = Field(default=None, max_length=200)


# ---------- 认证 ----------

@router.post("/auth/login")
def login(body: LoginIn, request: Request, db=Depends(get_db)):
    return ok(auth.login(db, body.username, body.password, ip=_ip(request), user_agent=_ua(request),
                         captcha_token=body.captcha_token))


@router.post("/auth/mfa")
def verify_mfa(body: MfaIn, request: Request, db=Depends(get_db)):
    return ok(auth.verify_mfa(db, body.mfa_token, code=body.code, recovery_code=body.recovery_code, ip=_ip(request),
                              user_agent=_ua(request)))


@router.post("/auth/logout")
def logout(staff: Staff = Depends(authenticated_staff), db=Depends(get_db)):
    auth.logout(db, staff)
    return ok()


@router.get("/auth/me")
def me(staff: Staff = Depends(authenticated_staff), db=Depends(get_db)):
    return ok(auth.me(db, staff))


@router.post("/auth/password")
def change_password(body: ChangePasswordIn, request: Request, staff: Staff = Depends(authenticated_staff), db=Depends(get_db)):
    return ok(auth.change_password(db, staff, body.old_password, body.new_password, ip=_ip(request), user_agent=_ua(request)))


@router.post("/auth/reauth")
def reauth(body: ReauthIn, request: Request, staff: Staff = Depends(authenticated_staff), db=Depends(get_db)):
    return ok(auth.reauthenticate(db, staff, body.password, code=body.code, recovery_code=body.recovery_code,
                                  ip=_ip(request), user_agent=_ua(request)))


@router.get("/auth/sessions")
def my_sessions(staff: Staff = Depends(current_staff), db=Depends(get_db)):
    return ok({"items": auth.list_sessions(db, staff.id, staff.session_id)})


@router.post("/auth/sessions/revoke-others")
def revoke_others(staff: Staff = Depends(require_fresh), db=Depends(get_db)):
    return ok({"revoked": auth.revoke_other_sessions(db, staff)})


@router.delete("/auth/sessions/{session_id}")
def revoke_session(session_id: int, staff: Staff = Depends(require_fresh), db=Depends(get_db)):
    auth.revoke_own_session(db, staff, session_id)
    return ok()


@router.post("/auth/mfa/enroll")
def mfa_enroll(staff: Staff = Depends(_fresh_basic), db=Depends(get_db)):
    return ok(auth.start_mfa_enrollment(db, staff))


@router.post("/auth/mfa/confirm")
def mfa_confirm(body: CodeIn, request: Request, staff: Staff = Depends(authenticated_staff), db=Depends(get_db)):
    return ok(auth.confirm_mfa_enrollment(db, staff, body.code, ip=_ip(request), user_agent=_ua(request)))


@router.post("/auth/mfa/recovery-codes")
def mfa_recovery_codes(staff: Staff = Depends(require_fresh), db=Depends(get_db)):
    return ok({"recovery_codes": auth.regenerate_recovery_codes(db, staff)})


@router.post("/auth/mfa/disable")
def mfa_disable(body: CodeIn, staff: Staff = Depends(require_fresh), db=Depends(get_db)):
    return ok(auth.disable_own_mfa(db, staff, body.code))


# ---------- 员工 ----------

@router.get("/staff")
def list_staff(request: Request, staff: Staff = Depends(require_permission("staff:read")), db=Depends(get_db)):
    return ok(service.list_staff(db, staff, request.query_params))


@router.get("/staff/export")
def export_staff(request: Request, staff: Staff = Depends(require_permission("staff:read")), db=Depends(get_db)):
    return _csv(service.export_staff(db, staff, request.query_params), "staff.csv")


@router.get("/staff/{staff_id}")
def get_staff(staff_id: int, staff: Staff = Depends(require_permission("staff:read")), db=Depends(get_db)):
    return ok(service.get_staff(db, staff, staff_id))


@router.post("/staff")
def create_staff(body: StaffCreateIn, staff: Staff = Depends(require_permission("staff:write")), _f=Depends(require_fresh),
                 key: str = Depends(idempotency_key), db=Depends(get_db)):
    return ok(_idem_secret(db, staff, "staff.create", key, body, lambda: service.create_staff(
        db, staff, body.username, body.name, body.phone, body.role_ids, body.all_stores, body.store_ids)))


@router.put("/staff/{staff_id}")
def update_staff(staff_id: int, body: StaffUpdateIn, staff: Staff = Depends(require_permission("staff:write")),
                 key: str = Depends(idempotency_key), db=Depends(get_db)):
    return ok(_idem(db, staff, f"staff.update:{staff_id}", key, body, lambda: service.update_staff(
        db, staff, staff_id, name=body.name, phone=body.phone, reason=body.reason)))


@router.put("/staff/{staff_id}/access")
def set_staff_access(staff_id: int, body: AccessIn, staff: Staff = Depends(require_permission("staff:write")),
                     _f=Depends(require_fresh), key: str = Depends(idempotency_key), db=Depends(get_db)):
    return ok(_idem(db, staff, f"staff.access:{staff_id}", key, body, lambda: service.set_staff_access(
        db, staff, staff_id, body.role_ids, body.all_stores, body.store_ids, reason=body.reason)))


def _simple(action: str, fn: Callable[..., dict], fresh: bool = False):
    @router.post(f"/staff/{{staff_id}}/{action}", name=f"staff_{action.replace('-', '_').replace('/', '_')}")
    def endpoint(staff_id: int, body: ReasonIn | None = None, staff: Staff = Depends(require_permission("staff:write")),
                 key: str = Depends(idempotency_key), db=Depends(get_db)):
        if fresh:
            assert_fresh(staff)
        body = body or ReasonIn()
        return ok(_idem(db, staff, f"staff.{action}:{staff_id}", key, body,
                        lambda: fn(db, staff, staff_id, reason=body.reason)))

    return endpoint


_simple("disable", service.disable_staff)
_simple("enable", service.enable_staff)
_simple("unlock", service.unlock_staff)
_simple("reset-mfa", service.reset_mfa, fresh=True)
_simple("sessions/revoke", service.revoke_staff_sessions)


@router.post("/staff/{staff_id}/reset-password")
def reset_password(staff_id: int, body: ReasonIn | None = None, staff: Staff = Depends(require_permission("staff:write")),
                   _f=Depends(require_fresh), key: str = Depends(idempotency_key), db=Depends(get_db)):
    body = body or ReasonIn()
    return ok(_idem_secret(db, staff, f"staff.reset_password:{staff_id}", key, body,
                           lambda: service.reset_password(db, staff, staff_id, reason=body.reason)))


@router.delete("/staff/{staff_id}")
def delete_staff(staff_id: int, reason: str | None = Query(default=None, max_length=200),
                 staff: Staff = Depends(require_permission("staff:write")), _f=Depends(require_fresh),
                 key: str = Depends(idempotency_key), db=Depends(get_db)):
    return ok(_idem(db, staff, f"staff.delete:{staff_id}", key, {"reason": reason},
                    lambda: service.delete_staff(db, staff, staff_id, reason=reason)))


@router.get("/staff/{staff_id}/sessions")
def staff_sessions(staff_id: int, staff: Staff = Depends(require_permission("staff:read")), db=Depends(get_db)):
    return ok({"items": service.staff_sessions(db, staff, staff_id)})


@router.post("/staff/{staff_id}/phone/reveal")
def reveal_phone(staff_id: int, body: ReasonIn, staff: Staff = Depends(require_permission("staff:sensitive")), db=Depends(get_db)):
    return ok(service.reveal_phone(db, staff, staff_id, body.reason))


# ---------- 角色与权限码 ----------

@router.get("/roles")
def list_roles(request: Request, staff: Staff = Depends(require_permission("role:read")), db=Depends(get_db)):
    return ok(service.list_roles(db, request.query_params))


@router.get("/roles/{role_id}")
def get_role(role_id: int, staff: Staff = Depends(require_permission("role:read")), db=Depends(get_db)):
    return ok(service.get_role(db, role_id))


@router.post("/roles")
def create_role(body: RoleIn, staff: Staff = Depends(require_permission("role:write")), _f=Depends(require_fresh),
                key: str = Depends(idempotency_key), db=Depends(get_db)):
    return ok(_idem(db, staff, "role.create", key, body, lambda: service.create_role(
        db, staff, body.name, body.permissions, body.description, body.mfa_required)))


@router.put("/roles/{role_id}")
def update_role(role_id: int, body: RoleUpdateIn, staff: Staff = Depends(require_permission("role:write")),
                _f=Depends(require_fresh), key: str = Depends(idempotency_key), db=Depends(get_db)):
    return ok(_idem(db, staff, f"role.update:{role_id}", key, body, lambda: service.update_role(
        db, staff, role_id, name=body.name, permissions=body.permissions, description=body.description,
        mfa_required=body.mfa_required, reason=body.reason)))


@router.delete("/roles/{role_id}")
def delete_role(role_id: int, reason: str | None = Query(default=None, max_length=200),
                staff: Staff = Depends(require_permission("role:write")), _f=Depends(require_fresh),
                key: str = Depends(idempotency_key), db=Depends(get_db)):
    return ok(_idem(db, staff, f"role.delete:{role_id}", key, {"reason": reason},
                    lambda: service.delete_role(db, staff, role_id, reason=reason)))


@router.get("/permissions")
def list_permissions(staff: Staff = Depends(require_permission("role:read"))):
    return ok({"items": service.list_permissions()})


# ---------- 审计 ----------

@router.get("/audit-logs")
def list_audit_logs(request: Request, staff: Staff = Depends(require_permission("audit:read")), db=Depends(get_db)):
    return ok(service.list_audit_logs(db, request.query_params))


@router.get("/audit-logs/export")
def export_audit_logs(request: Request, staff: Staff = Depends(require_permission("audit:read")), db=Depends(get_db)):
    return _csv(service.export_audit_logs(db, staff, request.query_params), "audit-logs.csv")


@router.get("/audit-logs/{log_id}")
def get_audit_log(log_id: int, staff: Staff = Depends(require_permission("audit:read")), db=Depends(get_db)):
    return ok(service.get_audit_log(db, log_id))


@router.get("/login-logs")
def list_login_logs(request: Request, staff: Staff = Depends(require_permission("audit:read")), db=Depends(get_db)):
    return ok(service.list_login_logs(db, request.query_params))


@router.get("/login-logs/export")
def export_login_logs(request: Request, staff: Staff = Depends(require_permission("audit:read")), db=Depends(get_db)):
    return _csv(service.export_login_logs(db, staff, request.query_params), "login-logs.csv")
