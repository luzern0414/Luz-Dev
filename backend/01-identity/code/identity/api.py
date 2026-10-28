"""identity 路由与依赖。业务在 service.py；这里只做参数、鉴权依赖与注入。

对外依赖（其他模块导入）：
- current_user(): 当前用户 id（4010）
- current_claims(): 完整声明（含 iat，用于 require_fresh）
- service_call(): 服务间签名校验（智能体等内部服务调用写接口时附带）
- set_code2session / set_sms_sender / set_phone_resolver: 注入外部实现
"""
from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from luzback import AppError, ErrorCode, get_db, get_settings, ok
from luzback.security import NonceStore, verify_service_signature

from . import service

router = APIRouter(prefix="/api/v1/app", tags=["identity"])

_code2session: Callable[[str], dict] | None = None
_sms_sender: Callable[[str, str], None] | None = None
_phone_resolver: Callable[[str], str] | None = None
_nonces = NonceStore()


def set_code2session(fn: Callable[[str], dict]) -> None:
    global _code2session
    _code2session = fn


def set_sms_sender(fn: Callable[[str, str], None]) -> None:
    global _sms_sender
    _sms_sender = fn


def set_phone_resolver(fn: Callable[[str], str]) -> None:
    global _phone_resolver
    _phone_resolver = fn


def _wechat(code: str) -> dict:
    if _code2session is None:
        raise AppError(ErrorCode.UPSTREAM_FAILED, "wechat client not configured")
    return _code2session(code)


def _sms(phone: str, code: str) -> None:
    if _sms_sender is None:
        raise AppError(ErrorCode.UPSTREAM_FAILED, "sms client not configured")
    _sms_sender(phone, code)


def _phone(code: str) -> str:
    if _phone_resolver is None:
        raise AppError(ErrorCode.UPSTREAM_FAILED, "wechat phone client not configured")
    return _phone_resolver(code)


_bearer = HTTPBearer(auto_error=False)


def current_claims(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> dict:
    if creds is None:
        raise AppError(ErrorCode.UNAUTHORIZED, "missing bearer token")
    return service.decode_access(creds.credentials)


def current_user(claims: dict = Depends(current_claims)) -> int:
    """依赖：当前用户 id；缺失或无效返回 4010。"""
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
        skew_seconds=s.service_signature_skew_seconds,
    )
    return name


class LoginIn(BaseModel):
    code: str = Field(min_length=1, max_length=128)


class RefreshIn(BaseModel):
    refresh_token: str = Field(min_length=10, max_length=2048)


class SmsSendIn(BaseModel):
    phone: str = Field(min_length=11, max_length=11)
    purpose: str = Field(default="bind", pattern="^(bind|login)$")


class SmsVerifyIn(BaseModel):
    phone: str = Field(min_length=11, max_length=11)
    code: str = Field(min_length=4, max_length=8)


class PhoneBindIn(BaseModel):
    phone_code: str = Field(min_length=1, max_length=256)


class ProfileIn(BaseModel):
    nickname: str | None = Field(default=None, max_length=20)
    avatar_url: str | None = Field(default=None, max_length=500)
    gender: int | None = Field(default=None, ge=0, le=2)
    birthday: str | None = Field(default=None, max_length=10)


class DeactivateIn(BaseModel):
    reason: str | None = Field(default=None, max_length=200)


@router.post("/auth/login")
def login(body: LoginIn, db=Depends(get_db)):
    return ok(service.login_with_code(db, body.code, _wechat))


@router.post("/auth/refresh")
def refresh(body: RefreshIn, db=Depends(get_db)):
    return ok(service.refresh_tokens(db, body.refresh_token))


@router.post("/auth/logout")
def logout(user_id: int = Depends(current_user), db=Depends(get_db)):
    service.logout(db, user_id)
    return ok(None)


@router.post("/auth/sms/send")
def sms_send(body: SmsSendIn, db=Depends(get_db)):
    return ok(service.send_sms_code(db, body.phone, body.purpose, _sms))


@router.post("/auth/sms/verify")
def sms_verify(body: SmsVerifyIn, user_id: int = Depends(current_user), db=Depends(get_db)):
    return ok(service.bind_phone_with_code(db, user_id, body.phone, body.code))


@router.post("/auth/phone/bind")
def phone_bind(body: PhoneBindIn, user_id: int = Depends(current_user), db=Depends(get_db)):
    return ok(service.bind_phone_from_wechat(db, user_id, body.phone_code, _phone))


@router.get("/me")
def me(user_id: int = Depends(current_user), db=Depends(get_db)):
    return ok(service.profile(db, user_id))


@router.put("/me")
def update_me(body: ProfileIn, user_id: int = Depends(current_user), db=Depends(get_db)):
    return ok(service.update_profile(db, user_id, body.nickname, body.avatar_url, body.gender, body.birthday))


@router.post("/me/deactivate")
def deactivate(body: DeactivateIn, claims: dict = Depends(current_claims), db=Depends(get_db)):
    return ok(service.deactivate(db, int(claims["sub"]), claims, body.reason))
