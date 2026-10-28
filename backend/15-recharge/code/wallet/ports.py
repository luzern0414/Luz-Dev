"""外部依赖的适配层（储值 15 与会员卡 29 共用）：支付、支付事件、后台员工鉴权、会员与门店查询。

本文件是两个模块与其他模块之间唯一的对接面；对方接口变化只改这里。

## 支付（09-wechat-pay）——假定的契约

创建支付（同步，返回调起参数）：`PaymentPort.create_payment(db, biz_type, biz_id, user_id, store_id, amount_cents,
description, goods_kind, expires_at) -> {"payment_id", "out_trade_no", "pay_params"}`。
默认实现 `WechatPayPort` 调用 `wechat_pay.create_payment(db, business_type=..., business_id=..., user_id=...,
store_id=..., amount_cents=..., expires_at=..., description=..., goods_kind=...)`（CONTRACTS.md 中 wechat_pay 一行）。

支付模块经发件箱发出、本模块消费的事件（payload 字段，兼容 biz_* 与 business_* 两种写法）：

| 事件 | 字段 |
|---|---|
| `payment.succeeded` | payment_id, out_trade_no, biz_type / business_type, biz_id / business_id, user_id, amount_cents, paid_at |
| `payment.refunded` | refund_id, payment_id, biz_type, biz_id, business_ref（= 我方 refund_requested 里的 business_ref）, amount_cents |
| `payment.refund_failed` | 同上 + reason |

本模块发出、期望支付模块消费的退款请求：`<module>.refund_requested`，payload：business_ref, biz_type, biz_id,
payment_id, out_trade_no, amount_cents, reason, route（original 原路 / offline 线下）。支付模块按 business_ref 幂等创建退款，
结果以 payment.refunded / refund_failed 回传 business_ref。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from luzback import AppError, ErrorCode, decode_token, get_db, get_settings

GOODS_OFFLINE_SERVICE = "offline_service"


# ---------------------------------------------------------------- 支付端口


class PaymentPort(Protocol):
    def create_payment(self, db: Session, *, biz_type: str, biz_id: int, user_id: int, store_id: int | None,
                       amount_cents: int, description: str, goods_kind: str, expires_at: datetime) -> dict: ...


class WechatPayPort:
    """调用 09-wechat-pay 的公开函数（运行时导入，模块缺失时 5020）。"""

    def create_payment(self, db: Session, *, biz_type: str, biz_id: int, user_id: int, store_id: int | None,
                       amount_cents: int, description: str, goods_kind: str, expires_at: datetime) -> dict:
        try:
            import wechat_pay  # noqa: PLC0415
            fn = getattr(wechat_pay, "create_payment")
            result = fn(db, business_type=biz_type, business_id=biz_id, user_id=user_id, store_id=store_id,
                        amount_cents=amount_cents, expires_at=expires_at, description=description, goods_kind=goods_kind)
        except (ImportError, AttributeError, TypeError) as exc:
            raise AppError(ErrorCode.UPSTREAM_FAILED, "payment module unavailable") from exc
        if not isinstance(result, dict):
            result = {"payment_id": getattr(result, "id", None), "out_trade_no": getattr(result, "out_trade_no", None),
                      "pay_params": {}}
        return {"payment_id": str(result.get("payment_id") or ""), "out_trade_no": result.get("out_trade_no"),
                "pay_params": result.get("pay_params") or {}}


class FakePaymentPort:
    """测试替身：记录调用并返回确定的单号；fail=True 模拟上游失败。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.fail = False

    def create_payment(self, db: Session, **kwargs: Any) -> dict:
        if self.fail:
            raise AppError(ErrorCode.UPSTREAM_FAILED, "payment upstream failed")
        self.calls.append(kwargs)
        n = len(self.calls)
        return {"payment_id": f"pay-{kwargs['biz_type']}-{kwargs['biz_id']}", "out_trade_no": f"T{n:06d}",
                "pay_params": {"package": f"prepay_id=fake{n}"}}


_payment_port: PaymentPort = WechatPayPort()


def set_payment_port(port: PaymentPort) -> None:
    global _payment_port
    _payment_port = port


def payment_port() -> PaymentPort:
    return _payment_port


# ---------------------------------------------------------------- 支付事件适配（唯一的 payload 映射处）


@dataclass(frozen=True)
class PaidEvent:
    biz_type: str
    biz_id: int
    payment_id: str
    out_trade_no: str | None
    amount_cents: int
    user_id: int | None
    paid_at: datetime


@dataclass(frozen=True)
class RefundResultEvent:
    biz_type: str
    biz_id: int | None
    business_ref: str
    success: bool
    amount_cents: int
    refund_id: str | None
    reason: str | None = None


def _pick(payload: dict, *names: str) -> Any:
    for name in names:
        if payload.get(name) not in (None, ""):
            return payload[name]
    return None


def _when(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def parse_paid(payload: dict) -> PaidEvent | None:
    """payment.succeeded → PaidEvent；缺必要字段返回 None（不是本模块的单）。"""
    biz_type = _pick(payload, "biz_type", "business_type")
    biz_id = _pick(payload, "biz_id", "business_id")
    amount = _pick(payload, "amount_cents", "total_cents", "amount")
    if biz_type is None or biz_id is None or amount is None:
        return None
    user = _pick(payload, "user_id")
    return PaidEvent(biz_type=str(biz_type), biz_id=int(biz_id), payment_id=str(_pick(payload, "payment_id", "id") or ""),
                     out_trade_no=_pick(payload, "out_trade_no"), amount_cents=int(amount),
                     user_id=int(user) if user is not None else None, paid_at=_when(_pick(payload, "paid_at", "success_time")))


def parse_refund_result(topic: str, payload: dict) -> RefundResultEvent | None:
    ref = _pick(payload, "business_ref", "out_refund_no", "refund_ref")
    if ref is None:
        return None
    biz_id = _pick(payload, "biz_id", "business_id")
    return RefundResultEvent(
        biz_type=str(_pick(payload, "biz_type", "business_type") or ""),
        biz_id=int(biz_id) if biz_id is not None else None,
        business_ref=str(ref), success=topic.endswith("refunded") and not topic.endswith("refund_failed"),
        amount_cents=int(_pick(payload, "amount_cents", "refund_cents") or 0),
        refund_id=_pick(payload, "refund_id"), reason=_pick(payload, "reason", "fail_reason"))


# ---------------------------------------------------------------- 后台员工鉴权（22-admin-rbac 适配）


@dataclass(frozen=True)
class StaffContext:
    id: int
    permissions: frozenset[str] = field(default_factory=frozenset)
    store_scope: tuple[int, ...] | None = None  # None = 全部门店

    def has(self, code: str) -> bool:
        prefix = code.split(":", 1)[0]
        return "*" in self.permissions or code in self.permissions or f"{prefix}:*" in self.permissions

    def in_scope(self, store_id: int | None) -> bool:
        return self.store_scope is None or (store_id is not None and int(store_id) in self.store_scope)


def _default_grants(db: Session, staff_id: int, claims: dict) -> tuple[frozenset[str], tuple[int, ...] | None]:
    try:
        from admin_rbac import service as rbac  # noqa: PLC0415
    except ImportError:  # pragma: no cover - 未启用 22 模块时只认令牌里的 perms（开发）
        return frozenset(claims.get("perms", [])), None
    staff = rbac.load_active_staff(db, staff_id, claims.get("rv"))
    codes, stores = rbac.grants_of(db, staff)
    return frozenset(codes), (tuple(stores) if stores else None)


_grants_resolver: Callable[[Session, int, dict], tuple[frozenset[str], tuple[int, ...] | None]] = _default_grants


def set_grants_resolver(fn: Callable[[Session, int, dict], tuple[frozenset[str], tuple[int, ...] | None]] | None) -> None:
    """扩展点 / 测试替身：返回 (权限码集合, 门店范围或 None)。"""
    global _grants_resolver
    _grants_resolver = fn or _default_grants


_bearer = HTTPBearer(auto_error=False)


def current_staff(creds: HTTPAuthorizationCredentials | None = Depends(_bearer), db: Session = Depends(get_db)) -> StaffContext:
    if creds is None:
        raise AppError(ErrorCode.UNAUTHORIZED, "missing bearer token")
    s = get_settings()
    claims = decode_token(creds.credentials, s.jwt_secret, kind="admin", issuer=s.jwt_issuer)
    staff_id = int(claims["sub"])
    perms, scope = _grants_resolver(db, staff_id, claims)
    return StaffContext(id=staff_id, permissions=perms, store_scope=scope)


def require_permission(code: str) -> Callable[..., StaffContext]:
    def dependency(staff: StaffContext = Depends(current_staff)) -> StaffContext:
        if not staff.has(code):
            raise AppError(ErrorCode.FORBIDDEN, f"permission required: {code}", {"required": code})
        return staff
    return dependency


def assert_store_scope(staff: StaffContext, store_id: int | None) -> None:
    if not staff.in_scope(store_id):
        raise AppError(ErrorCode.SCOPE_FORBIDDEN, "store out of scope", {"store_id": store_id})


# ---------------------------------------------------------------- 顾客鉴权（01-identity 适配）


def _default_user_decoder(token: str) -> int:
    from identity import service as identity_service  # noqa: PLC0415
    return int(identity_service.decode_access(token)["sub"])


_user_decoder: Callable[[str], int] = _default_user_decoder


def set_user_token_decoder(fn: Callable[[str], int] | None) -> None:
    """扩展点 / 测试替身：访问令牌 → user_id；无效令牌须抛 AppError(4010)。"""
    global _user_decoder
    _user_decoder = fn or _default_user_decoder


def current_user(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> int:
    """依赖：当前顾客 id（只来自令牌）；缺失或无效 4010。"""
    if creds is None:
        raise AppError(ErrorCode.UNAUTHORIZED, "missing bearer token")
    return _user_decoder(creds.credentials)


# ---------------------------------------------------------------- 会员与门店查询（01-identity / 02-store 适配）


@dataclass(frozen=True)
class MemberRef:
    user_id: int
    name: str = ""
    phone: str | None = None


def _default_member(db: Session, user_id: int | None = None, phone: str | None = None) -> MemberRef | None:
    try:
        import identity  # noqa: PLC0415
    except ImportError:  # pragma: no cover
        return MemberRef(user_id=user_id) if user_id else None
    if user_id is None and phone:
        from sqlalchemy import select  # noqa: PLC0415
        from luzback.crypto import FieldCipher  # noqa: PLC0415
        digest = FieldCipher(get_settings().field_encryption_key).lookup_hash(phone)
        user_id = db.execute(select(identity.User.id).where(identity.User.phone_hash == digest,
                                                            identity.User.deleted_at.is_(None))).scalar_one_or_none()
        if user_id is None:
            return None
    if user_id is None:
        return None
    try:
        info = identity.profile(db, int(user_id), reveal_phone=True)
    except AppError:
        return None
    if info.get("status") not in (None, "active"):
        return None
    return MemberRef(user_id=int(user_id), name=info.get("nickname") or "", phone=info.get("phone"))


_member_resolver: Callable[..., MemberRef | None] = _default_member


def set_member_resolver(fn: Callable[..., MemberRef | None] | None) -> None:
    """扩展点：fn(db, user_id=None, phone=None) -> MemberRef | None（不存在 / 已注销返回 None）。"""
    global _member_resolver
    _member_resolver = fn or _default_member


def find_member(db: Session, user_id: int | None = None, phone: str | None = None) -> MemberRef | None:
    return _member_resolver(db, user_id=user_id, phone=phone)


_store_tz_resolver: Callable[[Session, int | None], str] | None = None


def set_store_timezone_resolver(fn: Callable[[Session, int | None], str] | None) -> None:
    """扩展点：门店时区（IANA 名）；默认取 LUZ_TIMEZONE。"""
    global _store_tz_resolver
    _store_tz_resolver = fn


def store_timezone(db: Session, store_id: int | None) -> str:
    if _store_tz_resolver is not None:
        return _store_tz_resolver(db, store_id)
    return get_settings().timezone


def phone_lookup_hash(phone: str) -> str:
    from luzback.crypto import FieldCipher  # noqa: PLC0415
    return FieldCipher(get_settings().field_encryption_key).lookup_hash(phone) or ""
