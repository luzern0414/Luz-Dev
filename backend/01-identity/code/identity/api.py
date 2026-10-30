"""identity 路由与依赖。业务在 service.py / admin.py；这里只做参数、鉴权依赖、幂等与注入。

对外依赖（其他模块导入）：
- current_user(): 当前用户 id（令牌无效或会话已失效 → 4010）
- current_claims(): 完整声明（含 sid、iat，用于 require_fresh）
- service_call(): 服务间签名校验（智能体等内部服务调用写接口时附带）
- 后台路由鉴权：22-admin-rbac 的 require_permission（member:read / member:write / member:reveal + 新鲜认证）
- set_mini_program / set_code2session / set_phone_resolver / set_sms_sender / set_agreement_gateway: 注入外部实现
- rate_limit_rules(settings): 登录 / 短信 / 刷新的限流声明，供 app.py 装配
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from luzback import AppError, ErrorCode, current_config, get_db, get_settings, idempotency_key, ok, run_idempotent
from luzback.app import bind_identity, client_ip
from luzback.db import get_session_factory
from luzback.ratelimit import RateLimitRule, fixed_window
from luzback.security import NonceStore, verify_service_signature

from admin_rbac import Staff, declare_permissions, require_permission

from . import admin, service
from .ports import AgreementAccept, AgreementGateway, FakeSmsSender, LegacyCode2Session, LegacySmsSender, SmsSender

declare_permissions("member", admin.PERMISSIONS)  # 权限码目录（22-admin-rbac 角色配置用）

router = APIRouter(prefix="/api/v1/app", tags=["identity"])
admin_router = APIRouter(prefix="/api/v1/admin/members", tags=["identity-admin"])
routers = [router, admin_router]

_mp_client: Any = None
_legacy_c2s: Callable[[str], dict] | None = None
_legacy_phone: Callable[[str], str] | None = None
_sms_sender: SmsSender | None = None
_agreements: AgreementGateway | None = None
_nonces = NonceStore()


# ---------------------------------------------------------------- 注入

def set_mini_program(client: Any) -> None:
    """注入 wechat_mp.MiniProgramClient（生产用 HttpMiniProgramClient，测试用 FakeMiniProgram）；None 取进程级客户端。"""
    global _mp_client
    _mp_client = client


def set_code2session(fn: Callable[[str], dict]) -> None:
    """兼容旧注入：fn(code) → {"openid", "unionid"?, "session_key"?}。"""
    global _legacy_c2s
    _legacy_c2s = fn
    set_mini_program(LegacyCode2Session(fn, _legacy_phone))


def set_phone_resolver(fn: Callable[[str], str]) -> None:
    """兼容旧注入：fn(phone_code) → 手机号。"""
    global _legacy_phone
    _legacy_phone = fn
    set_mini_program(LegacyCode2Session(_legacy_c2s or (lambda code: {}), fn))


def set_sms_sender(sender: SmsSender | Callable[[str, str], None] | None) -> None:
    global _sms_sender
    _sms_sender = sender if (sender is None or hasattr(sender, "send_code")) else LegacySmsSender(sender)  # type: ignore[arg-type]


def set_agreement_gateway(gateway: AgreementGateway | None) -> None:
    global _agreements
    _agreements = gateway


def _sms() -> SmsSender:
    if _sms_sender is None:
        raise AppError(ErrorCode.UPSTREAM_FAILED, "sms channel not configured")
    return _sms_sender


# ---------------------------------------------------------------- 鉴权依赖

_bearer = HTTPBearer(auto_error=False)


def current_claims(creds: HTTPAuthorizationCredentials | None = Depends(_bearer), db: Any = Depends(get_db),
                   request: Request = None) -> dict:  # type: ignore[assignment]
    """令牌校验 + 会话有效性。CONTRACTS 允许直接调用 current_claims(creds)：此时自开会话做会话检查。

    经 FastAPI 调用时把会员内部编号写进请求上下文（请求汇总行 http.request 带 user_ref）。
    """
    if creds is None:
        raise AppError(ErrorCode.UNAUTHORIZED, "missing bearer token")
    claims = service.decode_access(creds.credentials)
    if isinstance(db, Session):
        service.assert_session_active(db, claims)
    else:
        with get_session_factory()() as own:
            service.assert_session_active(own, claims)
    if isinstance(request, Request):
        bind_identity(request, user_ref=claims.get("sub"))
    return claims


def current_user(claims: dict = Depends(current_claims)) -> int:
    """依赖：当前用户 id；缺失、无效、会话已吊销返回 4010。"""
    return int(claims["sub"])


async def service_call(request: Request) -> str | None:
    """依赖：若带 X-Service 头则校验服务间签名并返回服务名；普通请求返回 None。"""
    name = request.headers.get("X-Service")
    if not name:
        return None
    s = get_settings()
    secret = s.service_secret(name)
    if not secret:
        raise AppError(ErrorCode.UNAUTHORIZED, "unknown service")
    body = await request.body()
    verify_service_signature(
        secret, request.method, request.url.path, request.headers.get("X-Timestamp", ""),
        request.headers.get("X-Nonce", ""), body, request.headers.get("X-Signature", ""), _nonces,
        skew_seconds=current_config().auth.service_signature_skew_s,
    )
    return name


def rate_limit_rules(settings=None) -> list[RateLimitRule]:
    """登录 / 短信 / 刷新限流声明（按 IP）；按手机号与每日上限在 service 内用数据库计数。

    次数取运行配置 [rate_limit] login_per_min、sms_send_per_min、refresh_per_min（热加载生效）；settings 参数保留兼容，不再使用。
    """
    return [
        RateLimitRule("identity_login", fixed_window(lambda cfg: cfg.rate_limit.login_per_min), path_prefix="/api/v1/app/auth/login",
                      methods=("POST",), key_fn=client_ip, retry_after=60),
        RateLimitRule("identity_sms_login", fixed_window(lambda cfg: cfg.rate_limit.login_per_min),
                      path_prefix="/api/v1/app/auth/sms/login", methods=("POST",), key_fn=client_ip, retry_after=60),
        RateLimitRule("identity_sms_send", fixed_window(lambda cfg: cfg.rate_limit.sms_send_per_min),
                      path_prefix="/api/v1/app/auth/sms/send", methods=("POST",), key_fn=client_ip, retry_after=60),
        RateLimitRule("identity_refresh", fixed_window(lambda cfg: cfg.rate_limit.refresh_per_min), path_prefix="/api/v1/app/auth/refresh",
                      methods=("POST",), key_fn=client_ip, retry_after=60),
    ]


def _fp(body: BaseModel | None) -> str:
    return hashlib.sha256((body.model_dump_json() if body is not None else "").encode()).hexdigest()


def _ua(request: Request) -> str | None:
    return request.headers.get("User-Agent")


# ---------------------------------------------------------------- 请求模型

class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AgreementIn(_In):
    document_id: int = Field(ge=1)
    version: str = Field(min_length=1, max_length=24)
    sha256: str = Field(min_length=64, max_length=64)


class LoginIn(_In):
    code: str = Field(min_length=1, max_length=128)
    agreements: list[AgreementIn] = Field(default_factory=list, max_length=10)


class RefreshIn(_In):
    refresh_token: str = Field(min_length=10, max_length=256)


class SmsSendIn(_In):
    phone: str = Field(min_length=11, max_length=11)
    purpose: str = Field(default="bind", pattern="^(bind|login)$")


class SmsLoginIn(_In):
    phone: str = Field(min_length=11, max_length=11)
    request_id: str = Field(min_length=8, max_length=32)
    code: str = Field(min_length=6, max_length=6)


class PhoneWechatIn(_In):
    phone_code: str = Field(min_length=1, max_length=256)


class ProfileIn(_In):
    nickname: str | None = Field(default=None, max_length=20)
    avatar_url: str | None = Field(default=None, max_length=500)
    gender: int | None = Field(default=None, ge=0, le=2)
    birthday: str | None = Field(default=None, max_length=10)


class DeletionIn(_In):
    reason: str | None = Field(default=None, max_length=200)


class ReasonIn(_In):
    reason: str = Field(min_length=2, max_length=200)


# ---------------------------------------------------------------- 顾客端

@router.post("/auth/login")
def login(body: LoginIn, request: Request, db=Depends(get_db)):
    """微信登录（无幂等键：code 一次性；响应含令牌，不进幂等缓存）。"""
    agreements = [AgreementAccept(a.document_id, a.version, a.sha256) for a in body.agreements]
    return ok(service.login_with_code(db, body.code, _mp_client, agreements=agreements, agreement_gateway=_agreements,
                                      ip=client_ip(request), user_agent=_ua(request)))


@router.post("/auth/refresh")
def refresh(body: RefreshIn, db=Depends(get_db)):
    return ok(service.refresh_tokens(db, body.refresh_token))


@router.post("/auth/logout")
def logout(claims: dict = Depends(current_claims), db=Depends(get_db)):
    """结束当前会话。"""
    return ok(service.logout(db, int(claims["sub"]), session_id=claims["sid"]))


@router.post("/auth/logout-all")
def logout_all(user_id: int = Depends(current_user), db=Depends(get_db)):
    """结束全部会话（所有设备）。"""
    return ok(service.logout(db, user_id))


@router.post("/auth/sms/send")
def sms_send(body: SmsSendIn, request: Request, db=Depends(get_db)):
    """发送验证码：响应与号码是否注册无关；返回 request_id，校验时必须带回。"""
    return ok(service.send_sms_code(db, body.phone, body.purpose, _sms(), ip=client_ip(request)))


@router.post("/auth/sms/login")
def sms_login(body: SmsLoginIn, request: Request, db=Depends(get_db)):
    return ok(service.login_with_sms(db, body.phone, body.request_id, body.code, ip=client_ip(request), user_agent=_ua(request)))


@router.get("/me")
def me(user_id: int = Depends(current_user), db=Depends(get_db)):
    return ok(service.profile(db, user_id))


@router.put("/me")
def update_me(body: ProfileIn, user_id: int = Depends(current_user), key: str = Depends(idempotency_key), db=Depends(get_db)):
    scope, actor, fp = "identity.me.update", f"user:{user_id}", _fp(body)
    replay = service.stored_response(db, scope, key, actor, fp)
    if replay is not None:
        return ok(replay)
    plan = service.prepare_profile_update(db, user_id, body.model_dump(exclude_unset=True), _mp_client)
    return ok(run_idempotent(db, scope, key, lambda: service.apply_profile_update(db, user_id, plan, commit=False),
                             actor=actor, fingerprint=fp, require_key=True))


@router.post("/me/phone/wechat")
def bind_phone_wechat(body: PhoneWechatIn, user_id: int = Depends(current_user), key: str = Depends(idempotency_key),
                      db=Depends(get_db)):
    """微信手机号快速验证组件的 code → 绑定（code 一次性：先查幂等重放，再调微信）。"""
    scope, actor, fp = "identity.phone.wechat", f"user:{user_id}", _fp(body)
    replay = service.stored_response(db, scope, key, actor, fp)
    if replay is not None:
        return ok(replay)
    phone = service.resolve_wechat_phone(db, user_id, body.phone_code, _mp_client)
    return ok(run_idempotent(db, scope, key, lambda: service.attach_phone(db, user_id, phone, "wechat", commit=False),
                             actor=actor, fingerprint=fp, require_key=True))


@router.post("/me/phone/sms")
def bind_phone_sms(body: SmsLoginIn, user_id: int = Depends(current_user), key: str = Depends(idempotency_key),
                   db=Depends(get_db)):
    return ok(run_idempotent(db, "identity.phone.sms", key,
                             lambda: service.bind_phone_with_sms(db, user_id, body.phone, body.request_id, body.code, commit=False),
                             actor=f"user:{user_id}", fingerprint=_fp(body), require_key=True))


@router.get("/me/deletion")
def deletion_status(user_id: int = Depends(current_user), db=Depends(get_db)):
    return ok(service.deletion_status(db, user_id))


@router.post("/me/deletion")
def request_deletion(body: DeletionIn, claims: dict = Depends(current_claims), key: str = Depends(idempotency_key),
                     db=Depends(get_db)):
    user_id = int(claims["sub"])
    return ok(run_idempotent(db, "identity.deletion.request", key,
                             lambda: service.request_deletion(db, user_id, claims, body.reason, commit=False),
                             actor=f"user:{user_id}", fingerprint=_fp(body), require_key=True))


@router.post("/me/deletion/cancel")
def cancel_deletion(user_id: int = Depends(current_user), key: str = Depends(idempotency_key), db=Depends(get_db)):
    return ok(run_idempotent(db, "identity.deletion.cancel", key, lambda: service.cancel_deletion(db, user_id, commit=False),
                             actor=f"user:{user_id}", fingerprint=_fp(None), require_key=True))


# ---------------------------------------------------------------- 后台

def _trace(request: Request) -> str | None:
    return getattr(request.state, "trace_id", None)


@admin_router.get("")
def admin_list(request: Request, staff: Staff = Depends(require_permission("member:read")), db=Depends(get_db)):
    return ok(admin.list_members(db, request.query_params))


@admin_router.get("/export")
def admin_export(request: Request, staff: Staff = Depends(require_permission("member:read")), db=Depends(get_db)):
    staff_id = staff.id
    params = request.query_params
    content = b"".join(admin.export_members(db, staff_id, params, ip=client_ip(request), trace_id=_trace(request)))
    return Response(content, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="members.csv"'})


@admin_router.get("/{user_id}")
def admin_detail(user_id: int, staff: Staff = Depends(require_permission("member:read")), db=Depends(get_db)):
    return ok(admin.member_detail(db, user_id))


@admin_router.post("/{user_id}/reveal-phone")
def admin_reveal_phone(user_id: int, body: ReasonIn, request: Request,
                       staff: Staff = Depends(require_permission("member:reveal", fresh=True)), db=Depends(get_db)):
    staff_id = staff.id
    """查看完整手机号（不缓存进幂等记录，避免明文落库）；每次查看都审计。"""
    return ok(admin.reveal_phone(db, staff_id, user_id, body.reason, ip=client_ip(request), trace_id=_trace(request)))


def _admin_write(db, staff_id: int, scope: str, key: str, body: BaseModel, fn):
    return ok(run_idempotent(db, scope, key, fn, actor=f"staff:{staff_id}", fingerprint=_fp(body), require_key=True))


@admin_router.post("/{user_id}/disable")
def admin_disable(user_id: int, body: ReasonIn, request: Request, key: str = Depends(idempotency_key),
                  staff: Staff = Depends(require_permission("member:write")), db=Depends(get_db)):
    staff_id = staff.id
    return _admin_write(db, staff_id, f"identity.admin.disable:{user_id}", key, body, lambda: admin.disable_member(
        db, staff_id, user_id, body.reason, ip=client_ip(request), trace_id=_trace(request), commit=False))


@admin_router.post("/{user_id}/enable")
def admin_enable(user_id: int, body: ReasonIn, request: Request, key: str = Depends(idempotency_key),
                 staff: Staff = Depends(require_permission("member:write")), db=Depends(get_db)):
    staff_id = staff.id
    return _admin_write(db, staff_id, f"identity.admin.enable:{user_id}", key, body, lambda: admin.enable_member(
        db, staff_id, user_id, body.reason, ip=client_ip(request), trace_id=_trace(request), commit=False))


@admin_router.post("/{user_id}/logout")
def admin_force_logout(user_id: int, body: ReasonIn, request: Request, key: str = Depends(idempotency_key),
                       staff: Staff = Depends(require_permission("member:write")), db=Depends(get_db)):
    staff_id = staff.id
    return _admin_write(db, staff_id, f"identity.admin.logout:{user_id}", key, body, lambda: admin.force_logout(
        db, staff_id, user_id, body.reason, ip=client_ip(request), trace_id=_trace(request), commit=False))


__all__ = [
    "router", "admin_router", "routers", "current_user", "current_claims", "service_call",
    "rate_limit_rules", "set_mini_program", "set_code2session", "set_phone_resolver", "set_sms_sender",
    "set_agreement_gateway", "FakeSmsSender",
]
