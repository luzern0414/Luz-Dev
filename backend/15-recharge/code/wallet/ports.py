"""外部依赖的适配层（储值 15 与会员卡 29 共用）：支付与退款、支付事件、后台员工鉴权、顾客鉴权、会员与门店查询。

本文件是两个模块与其他模块之间唯一的对接面；对方接口变化只改这里。

## 支付（09-wechat-pay，契约见其 MODULE.md §1 / §7）

- 创建支付：`wechat_pay.create_payment(db, business_type, business_id, user_id, store_id, amount_cents, expires_at,
  description, goods_kind=..., business_no=...) -> {payment_id, out_trade_no, pay_params}`。**该函数在渠道调用前后自行
  commit**，所以本模块先提交业务单，再在独立步骤里调用（`start_payment`），绝不放进未提交的事务。
- 发起退款：`wechat_pay.create_refund(db, payment_id, amount_cents, reason, business_ref) -> RefundInfo`（同样自行提交，
  渠道调用在内）；business_ref = `<前缀>:<退款单号>`（wallet_refund / member_card_refund），每笔只提交一次（条件占位）。
- 同事务钩子：`register_on_paid(business_type, fn(db, PaymentInfo))`（抛错 → 支付模块标人工并自动退款）、
  `register_on_refunded(前缀, fn(db, RefundInfo))`、`register_payable(business_type, fn(db, user_id, business_id) -> Payable)`。
- 事件（发件箱，本模块订阅作为补偿通道，幂等）：
  - `payment.succeeded`：payment_id, business_type, business_id, business_no, user_id, store_id, amount_cents,
    out_trade_no, transaction_id, paid_at, needs_manual, source（needs_manual=true 表示支付模块已转人工 / 自动退款，不履约）
  - `refund.succeeded` / `refund.failed` / `refund.abnormal`：refund_id, payment_id, business_type, business_id,
    business_ref, amount_cents, out_refund_no, out_trade_no, status（failed 另含 reason）
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from luzback import AppError, ErrorCode, get_settings

GOODS_OFFLINE_SERVICE = "offline_service"


# ---------------------------------------------------------------- 支付端口


class PaymentPort(Protocol):
    def create_payment(self, db: Session, *, biz_type: str, biz_id: int, user_id: int, store_id: int | None,
                       amount_cents: int, description: str, goods_kind: str, expires_at: datetime,
                       business_no: str = "") -> dict: ...

    def create_refund(self, db: Session, *, payment_id: str, amount_cents: int, reason: str, business_ref: str) -> dict: ...


class WechatPayPort:
    """调用 09-wechat-pay 的公开函数（运行时导入）。两者都会自行 commit。"""

    def create_payment(self, db: Session, *, biz_type: str, biz_id: int, user_id: int, store_id: int | None,
                       amount_cents: int, description: str, goods_kind: str, expires_at: datetime,
                       business_no: str = "") -> dict:
        import wechat_pay  # noqa: PLC0415
        result = wechat_pay.create_payment(db, biz_type, biz_id, user_id, store_id, amount_cents, expires_at, description,
                                           goods_kind=goods_kind, business_no=business_no)
        return {"payment_id": str(result.get("payment_id") or ""), "out_trade_no": result.get("out_trade_no"),
                "pay_params": result.get("pay_params") or {}}

    def create_refund(self, db: Session, *, payment_id: str, amount_cents: int, reason: str, business_ref: str) -> dict:
        import wechat_pay  # noqa: PLC0415
        info = wechat_pay.create_refund(db, int(payment_id), amount_cents, reason, business_ref)
        return {"refund_id": str(getattr(info, "refund_id", "") or ""), "status": getattr(info, "status", "")}


class FakePaymentPort:
    """测试替身：记录调用并返回确定的单号；fail / refund_fail 模拟上游失败，refund_reject 模拟规则拒绝。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.refunds: list[dict] = []
        self.fail = False
        self.refund_fail = False
        self.refund_reject = False
        self._issued: dict[tuple, dict] = {}

    def create_payment(self, db: Session, **kwargs: Any) -> dict:
        if self.fail:
            raise AppError(ErrorCode.UPSTREAM_FAILED, "payment upstream failed")
        self.calls.append(kwargs)
        db.commit()  # 与真实实现一致：自行提交
        key = (kwargs["biz_type"], kwargs["biz_id"], kwargs["amount_cents"])
        if key not in self._issued:  # 同一业务单参数不变 → 复用（与支付模块的 prepay 复用一致）
            n = len(self._issued) + 1
            self._issued[key] = {"payment_id": str(1000 + kwargs["biz_id"]), "out_trade_no": f"T{n:06d}",
                                 "pay_params": {"package": f"prepay_id=fake{n}"}}
        return dict(self._issued[key])

    def create_refund(self, db: Session, **kwargs: Any) -> dict:
        if self.refund_fail:
            raise AppError(ErrorCode.UPSTREAM_FAILED, "refund upstream failed")
        if self.refund_reject:
            raise AppError(ErrorCode.BUSINESS_RULE, "refund window exceeded")
        self.refunds.append(kwargs)
        db.commit()
        return {"refund_id": str(len(self.refunds)), "status": "pending"}


_payment_port: PaymentPort = WechatPayPort()


def set_payment_port(port: PaymentPort) -> None:
    global _payment_port
    _payment_port = port


def payment_port() -> PaymentPort:
    return _payment_port


def register_payment_hooks(business_type: str, on_paid: Callable[[Session, "PaidEvent"], Any], refund_prefix: str,
                           on_refund: Callable[[Session, "RefundResultEvent"], Any],
                           payable: Callable[[Session, int, int], dict] | None = None) -> bool:
    """向 09-wechat-pay 注册同事务钩子；未部署支付模块时返回 False（只走事件通道）。"""
    try:
        import wechat_pay  # noqa: PLC0415
        register_paid, register_refunded = wechat_pay.register_on_paid, wechat_pay.register_on_refunded
    except (ImportError, AttributeError):
        return False

    def paid_hook(db: Session, info: Any) -> None:
        on_paid(db, PaidEvent(biz_type=info.business_type, biz_id=int(info.business_id), payment_id=str(info.payment_id),
                              out_trade_no=info.out_trade_no, amount_cents=int(info.amount_cents),
                              user_id=info.user_id or None, paid_at=_when(info.paid_at), from_hook=True))

    def refund_hook(db: Session, info: Any) -> None:
        status = str(getattr(info, "status", ""))
        if status not in ("succeeded", "failed", "closed", "abnormal"):
            return
        on_refund(db, RefundResultEvent(biz_type=info.business_type, biz_id=info.business_id,
                                        business_ref=info.business_ref, success=status == "succeeded",
                                        amount_cents=int(info.amount_cents), refund_id=str(info.refund_id),
                                        abnormal=status == "abnormal", reason=status))

    register_paid(business_type, paid_hook)
    register_refunded(refund_prefix, refund_hook)
    if payable is not None and hasattr(wechat_pay, "register_payable"):
        def payable_hook(db: Session, user_id: int, business_id: int):
            return wechat_pay.Payable(**payable(db, user_id, business_id))
        wechat_pay.register_payable(business_type, payable_hook)
    return True


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
    needs_manual: bool = False
    from_hook: bool = False


@dataclass(frozen=True)
class RefundResultEvent:
    biz_type: str
    biz_id: int | None
    business_ref: str
    success: bool
    amount_cents: int
    refund_id: str | None
    reason: str | None = None
    abnormal: bool = False

    @property
    def local_ref(self) -> str:
        """business_ref 去掉“前缀:”，即本模块的退款单号。"""
        return self.business_ref.split(":", 1)[1] if ":" in self.business_ref else self.business_ref


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
    biz_type = _pick(payload, "business_type", "biz_type")
    biz_id = _pick(payload, "business_id", "biz_id")
    amount = _pick(payload, "amount_cents")
    if biz_type is None or biz_id is None or amount is None:
        return None
    user = _pick(payload, "user_id")
    return PaidEvent(biz_type=str(biz_type), biz_id=int(biz_id), payment_id=str(_pick(payload, "payment_id") or ""),
                     out_trade_no=_pick(payload, "out_trade_no"), amount_cents=int(amount),
                     user_id=int(user) if user is not None else None, paid_at=_when(_pick(payload, "paid_at")),
                     needs_manual=bool(payload.get("needs_manual")))


REFUND_TOPICS = ("refund.succeeded", "refund.failed", "refund.abnormal")


def parse_refund_result(topic: str, payload: dict) -> RefundResultEvent | None:
    ref = _pick(payload, "business_ref")
    if ref is None or topic not in REFUND_TOPICS:
        return None
    biz_id = _pick(payload, "business_id", "biz_id")
    return RefundResultEvent(
        biz_type=str(_pick(payload, "business_type", "biz_type") or ""),
        biz_id=int(biz_id) if biz_id is not None else None, business_ref=str(ref),
        success=topic == "refund.succeeded", abnormal=topic == "refund.abnormal",
        amount_cents=int(_pick(payload, "amount_cents") or 0), refund_id=_pick(payload, "refund_id"),
        reason=_pick(payload, "reason") if topic == "refund.failed" else _pick(payload, "status"))  # 只有 failed 带 reason


# ---------------------------------------------------------------- 后台员工鉴权（22-admin-rbac）


@dataclass(frozen=True)
class StaffContext:
    """本模块使用的员工视图。store_scope：None = 全部门店；() = 没有任何门店（不是“全部”）。"""

    id: int
    permissions: frozenset[str] = field(default_factory=frozenset)
    store_scope: tuple[int, ...] | None = None

    def has(self, code: str) -> bool:
        from admin_rbac import grants  # noqa: PLC0415

        return grants(self.permissions, code)

    def in_scope(self, store_id: int | None) -> bool:
        return self.store_scope is None or (store_id is not None and int(store_id) in self.store_scope)


def staff_context(staff: Any) -> StaffContext:
    """admin_rbac.Staff → StaffContext。Staff.store_scope：None = 全部门店（all_stores），[] = 无门店。"""
    scope = staff.store_scope
    return StaffContext(id=int(staff.id), permissions=frozenset(staff.permissions),
                        store_scope=None if scope is None else tuple(int(s) for s in scope))


def require_permission(code: str, fresh: bool = False) -> Callable[..., StaffContext]:
    """后台依赖（薄别名）：admin_rbac.require_permission(code, fresh=...)（完整会话、UA 绑定、受限会话 4011、默认拒绝 4030；
    fresh=True 要求 fresh_auth_seconds 内完成完整认证，否则 4011 reauth_required），再转成本模块的 StaffContext。"""
    import admin_rbac  # noqa: PLC0415  (22-admin-rbac 是后台必备模块；未部署即默认拒绝——导入失败启动报错)

    base = admin_rbac.require_permission(code, fresh=fresh)

    def dependency(staff=Depends(base)) -> StaffContext:
        return staff_context(staff)

    dependency.__name__ = f"wallet_require_{code.replace(':', '_')}{'_fresh' if fresh else ''}"
    return dependency


def declare_permissions(module: str, items) -> None:
    import admin_rbac  # noqa: PLC0415

    admin_rbac.declare_permissions(module, [(code, desc) for code, desc in items])


def assert_store_scope(staff: StaffContext, store_id: int | None) -> None:
    if not staff.in_scope(store_id):
        raise AppError(ErrorCode.SCOPE_FORBIDDEN, "store out of scope", {"store_id": store_id})


# ---------------------------------------------------------------- 顾客鉴权（01-identity 适配）

_bearer = HTTPBearer(auto_error=False)


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
        finder = getattr(identity, "find_user_by_phone", None)
        if finder is not None:  # 01-identity 的哈希查找（规范化手机号 + 盲索引），唯一入口
            user = finder(db, phone)
            user_id = user.id if user is not None and getattr(user, "deleted_at", None) is None else None
        else:  # pragma: no cover - 旧版 identity
            from sqlalchemy import select  # noqa: PLC0415
            user_id = db.execute(select(identity.User.id).where(identity.User.phone_hash == phone_lookup_hash(phone),
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
