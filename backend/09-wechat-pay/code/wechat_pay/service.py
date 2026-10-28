"""wechat_pay 用例：支付单、回调、查单、关单、退款、发货信息、对账、后台查询。

事务约定（SPEC §6.10 禁止在事务中调用外部 HTTP）：
- 涉及渠道调用的用例分段提交：本地写入并 commit → 渠道调用（经 `resilient`）→ 条件 UPDATE 记录结果并 commit。
  因此这些函数会提交调用方会话中已有的改动；不要在另一个未提交的业务事务中间调用它们。
- 回调与查单共用同一套“事实处理”函数：验签 / 核对 → 条件 UPDATE（只有一个赢家）→ 同事务业务钩子 + 发件箱事件。
"""
from __future__ import annotations

import gzip
import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from luzback import (
    AppError, BreakerRegistry, Bulkhead, CircuitOpenError, ErrorCode, Field, ListSpec, TransientError, UpstreamError,
    enqueue_event, resilient, utcnow, write_audit,
)
from luzback.contracts import PaymentInfo, RefundInfo

from . import apiv3, rules
from .client import (
    SHIPPING_ALREADY_DONE, SHIPPING_RETRYABLE, HttpWechatPayClient, ShippingApiError, ShippingClient,
    TransientWechatPayError, WechatPayApiError, WechatPayClient, dumps,
)
from .config import get_config
from .models import PaymentRecord, PayNotifyLog, ReconBatch, ReconDiff, RefundRecord, ShippingUpload
from .rules import (
    GOODS_PHYSICAL, PAY_CLOSED, PAY_CREATED, PAY_FAILED, PAY_OPEN, PAY_PAID, PAY_PAYING, REFUND_ABNORMAL, REFUND_CLOSED,
    REFUND_FAILED, REFUND_PENDING, REFUND_PROCESSING, REFUND_SUCCEEDED, aware,
)

log = logging.getLogger("luz.wechat_pay")

PERMISSIONS = {
    "payment:read": "查看支付单、退款单、发货上传与对账结果",
    "payment:refund": "发起退款、处理异常退款",
    "payment:reconcile": "执行对账、处理对账差异、同步 / 关闭支付单",
}

# ---------------------------------------------------------------- 注册表与依赖注入


@dataclass
class Payable:
    """业务模块告诉支付模块“这笔要付多少”（金额只来自服务端业务对象）。"""

    amount_cents: int
    description: str
    expires_at: datetime
    store_id: int | None = None
    goods_kind: str = GOODS_PHYSICAL
    business_no: str = ""
    attach: str | None = None


PayableResolver = Callable[[Session, int, int], Payable]   # (db, user_id, business_id)；他人 4040，不可支付 4090
_payables: dict[str, PayableResolver] = {}
_on_paid: dict[str, Callable[[Session, PaymentInfo], None]] = {}
_on_refunded: dict[str, Callable[[Session, RefundInfo], None]] = {}
_openid_resolver: Callable[[Session, int], str | None] | None = None
_client: WechatPayClient | None = None
_shipping_client: ShippingClient | None = None
_breakers = BreakerRegistry()
_bulkhead: Bulkhead | None = None
_sleep: Callable[[float], None] = time.sleep


def register_payable(business_type: str, resolver: PayableResolver) -> None:
    _payables[business_type] = resolver


def register_on_paid(business_type: str, fn: Callable[[Session, PaymentInfo], None]) -> None:
    """支付成功同事务钩子：只做轻量条件 UPDATE（如订单 pending_payment → paid）；重活走事件。抛错 → 人工 + 自动退款。"""
    _on_paid[business_type] = fn


def register_on_refunded(key: str, fn: Callable[[Session, RefundInfo], None]) -> None:
    """退款终态钩子：key 为 business_ref 前缀（after_sales:12 → after_sales）或支付单 business_type。"""
    _on_refunded[key] = fn


def has_on_paid(business_type: str) -> bool:
    return business_type in _on_paid


def has_payable(business_type: str) -> bool:
    return business_type in _payables


def set_openid_resolver(fn: Callable[[Session, int], str | None] | None) -> None:
    global _openid_resolver
    _openid_resolver = fn


def set_client(client: WechatPayClient | None) -> None:
    global _client
    _client = client


def get_client() -> WechatPayClient:
    global _client
    if _client is None:
        cfg = get_config()
        if cfg.client_mode == "http":
            _client = HttpWechatPayClient(cfg)
        else:
            from luzback import get_settings

            if get_settings().is_prod:
                raise RuntimeError("fake wechat pay client is not allowed in prod")
            from .fake import FAKE_APIV3_KEY, FakeWechatPay

            key = cfg.apiv3_key if len(cfg.apiv3_key.encode()) == 32 else FAKE_APIV3_KEY
            _client = FakeWechatPay(appid=cfg.appid, mchid=cfg.mchid, apiv3_key=key)
    return _client


def set_shipping_client(client: ShippingClient | None) -> None:
    global _shipping_client
    _shipping_client = client


def get_shipping_client() -> ShippingClient:
    global _shipping_client
    if _shipping_client is None:
        from .fake import FakeShippingClient

        _shipping_client = FakeShippingClient()  # 未注入时只在开发环境可用；生产由启动代码注入 HttpShippingClient
    return _shipping_client


def set_breakers(registry: BreakerRegistry) -> None:
    global _breakers
    _breakers = registry


def set_sleep(fn: Callable[[float], None]) -> None:
    global _sleep
    _sleep = fn


def _get_bulkhead() -> Bulkhead:
    global _bulkhead
    if _bulkhead is None:
        _bulkhead = Bulkhead("wechat_pay", get_config().bulkhead)
    return _bulkhead


# ---------------------------------------------------------------- 小工具


def _now(now: datetime | None) -> datetime:
    return aware(now) if now is not None else utcnow()


def _call(op: str, fn: Callable[[], Any], *, timeout: float, attempts: int = 1, breaker: str = "wechat_pay") -> Any:
    """经 resilient 调用渠道；返回原始错误类型（WechatPayApiError / CircuitOpenError），超时转 TIMEOUT。"""
    try:
        return resilient(f"wechat_pay.{op}", fn, timeout_s=timeout, attempts=attempts, breaker=_breakers.get(breaker),
                         bulkhead=_get_bulkhead(), sleep=_sleep)
    except UpstreamError as exc:
        cause = exc.__cause__
        if isinstance(cause, WechatPayApiError):
            raise cause from None
        raise TransientWechatPayError("TIMEOUT", str(cause or exc)) from exc


def _upd(db: Session, model: Any, *where: Any, **values: Any) -> int:
    stmt = update(model).where(*where).values(**values).execution_options(synchronize_session=False)
    return db.execute(stmt).rowcount


def _reload(db: Session, model: Any, row_id: int) -> Any:
    return db.get(model, row_id, populate_existing=True)


def _trim_bytes(text: str, limit: int) -> str:
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[:limit].decode("utf-8", errors="ignore")


def _payment_info(p: PaymentRecord) -> PaymentInfo:
    return PaymentInfo(payment_id=p.id, business_type=p.business_type, business_id=p.business_id, user_id=p.user_id or 0,
                       amount_cents=p.amount_cents, out_trade_no=p.out_trade_no, channel_txn_id=p.channel_txn_id,
                       paid_at=aware(p.paid_at))


def _refund_info(r: RefundRecord, p: PaymentRecord) -> RefundInfo:
    return RefundInfo(refund_id=r.id, payment_id=p.id, business_type=p.business_type, business_id=p.business_id,
                      business_ref=r.business_ref, amount_cents=r.amount_cents, status=r.status, out_refund_no=r.out_refund_no)


def _payment_event(db: Session, topic: str, p: PaymentRecord, **extra: Any) -> None:
    payload = {"payment_id": p.id, "business_type": p.business_type, "business_id": p.business_id,
               "business_no": p.business_no, "user_id": p.user_id, "store_id": p.store_id, "amount_cents": p.amount_cents,
               "out_trade_no": p.out_trade_no, **extra}
    enqueue_event(db, topic, payload, "payment", p.id)


def _refund_event(db: Session, topic: str, r: RefundRecord, p: PaymentRecord, **extra: Any) -> None:
    payload = {"refund_id": r.id, "payment_id": p.id, "business_type": p.business_type, "business_id": p.business_id,
               "business_ref": r.business_ref, "amount_cents": r.amount_cents, "out_refund_no": r.out_refund_no,
               "out_trade_no": p.out_trade_no, "status": r.status, **extra}
    enqueue_event(db, topic, payload, "refund", r.id)


def _run_hook(db: Session, fn: Callable[[Session, Any], None] | None, info: Any) -> str | None:
    """在保存点里执行业务钩子；失败只回滚钩子的写入，返回错误描述。"""
    if fn is None:
        return None
    try:
        with db.begin_nested():
            fn(db, info)
        return None
    except AppError as exc:
        return f"{int(exc.code)} {exc.message}"[:150]
    except Exception as exc:  # noqa: BLE001 - 业务钩子的任何异常都不能影响支付事实落库
        log.exception("wechat_pay hook failed")
        return type(exc).__name__


def _log_notify(db: Session, *, kind: str, result: str, reason: str | None = None, envelope: Mapping | None = None,
                plain: Mapping | None = None, serial: str | None = None, verified: bool = False, source: str = "notify",
                now: datetime) -> None:
    plain = plain or {}
    db.add(PayNotifyLog(
        kind=kind, notify_id=(envelope or {}).get("id"), event_type=(envelope or {}).get("event_type"),
        out_trade_no=plain.get("out_trade_no"), out_refund_no=plain.get("out_refund_no"),
        channel_txn_id=plain.get("transaction_id"), serial=(serial or "")[:64] or None, verified=verified,
        result=result, reason=(reason or "")[:200] or None, source=source, received_at=now,
    ))
    db.commit()


def _flag_manual(db: Session, model: Any, row_id: int, reason: str) -> None:
    values = {"needs_manual": True}
    if model is PaymentRecord:
        values["manual_reason"] = reason[:200]
    _upd(db, model, model.id == row_id, **values)
    db.commit()


# ---------------------------------------------------------------- 支付：创建 / 复用 / 重新下单


def _resolve_openid(db: Session, user_id: int | None) -> str | None:
    if _openid_resolver is not None:
        return _openid_resolver(db, user_id) if user_id else None
    if not user_id:
        return None
    try:
        from identity import User  # 延迟导入：identity 是 L1
    except Exception:  # noqa: BLE001
        return None
    user = db.get(User, user_id)
    return getattr(user, "openid", None) if user else None


def _prepay_body(client: WechatPayClient, out_trade_no: str, description: str, amount: int, openid: str,
                 expires_at: datetime, attach: str | None) -> dict[str, Any]:
    cfg = get_config()
    body: dict[str, Any] = {
        "appid": client.appid, "mchid": client.mchid, "description": description, "out_trade_no": out_trade_no,
        "time_expire": rules.rfc3339(expires_at, cfg.tz), "notify_url": cfg.notify_url,
        "amount": {"total": amount, "currency": "CNY"}, "payer": {"openid": openid},
    }
    if attach:
        body["attach"] = attach
    return body


def _result(p: PaymentRecord, pay_params: dict[str, str] | None) -> dict[str, Any]:
    return {"payment_id": p.id, "out_trade_no": p.out_trade_no, "amount_cents": p.amount_cents, "status": p.status,
            "expires_at": aware(p.expires_at).isoformat(), "pay_params": pay_params}


def create_payment(db: Session, business_type: str, business_id: int, user_id: int | None, store_id: int | None,
                   amount_cents: int, expires_at: datetime | None, description: str, *, payer_openid: str | None = None,
                   goods_kind: str = GOODS_PHYSICAL, business_no: str = "", attach: str | None = None,
                   now: datetime | None = None) -> dict[str, Any]:
    """为业务对象创建或继续支付，返回小程序调起参数（CONTRACTS：dict(payment_id, out_trade_no, pay_params)）。

    1. 已有已支付单 → 4090；
    2. 有未完成单且参数完全一致、未过 time_expire：prepay_id 2 小时内复用，过期用原报文重新下单（同一 out_trade_no）；
    3. 参数变化或已过 time_expire：先查单再关单（查到已支付 → 4090），再用新 out_trade_no 下单。
    expires_at 必须是业务侧稳定的截止时间（例如订单 pay_deadline），否则每次都会被视为参数变化。
    """
    cfg = get_config()
    now = _now(now)
    rules.assert_wechat_pay_allowed(goods_kind)
    if not business_type or len(business_type) > 32 or business_id is None:
        raise AppError(ErrorCode.BAD_REQUEST, "business_type / business_id required")
    if not isinstance(amount_cents, int) or amount_cents <= 0:
        raise AppError(ErrorCode.BAD_REQUEST, "amount must be a positive integer of cents")
    description = rules.sanitize_text(description, 127)
    if not description:
        raise AppError(ErrorCode.BAD_REQUEST, "description required")
    attach = rules.sanitize_text(attach, 128) or None if attach else None
    expires_at = aware(expires_at) if expires_at else now + timedelta(minutes=cfg.default_expire_minutes)
    expires_at = expires_at.replace(microsecond=0)
    openid = payer_openid or _resolve_openid(db, user_id)
    if not openid:
        raise AppError(ErrorCode.BUSINESS_RULE, "payer openid unavailable", data={"reason": "openid_missing"})
    client = get_client()
    business = (PaymentRecord.business_type == business_type, PaymentRecord.business_id == business_id)

    for _ in range(3):
        if db.scalar(select(PaymentRecord.id).where(*business, PaymentRecord.status == PAY_PAID).limit(1)):
            raise AppError(ErrorCode.CONFLICT, "already paid", data={"reason": "already_paid"})
        current = db.scalars(select(PaymentRecord).where(*business, PaymentRecord.status.in_(PAY_OPEN))
                             .execution_options(populate_existing=True)).first()
        if current is not None:
            candidate = dumps(_prepay_body(client, current.out_trade_no, description, amount_cents, openid, expires_at, attach))
            if candidate == current.request_json and aware(current.expires_at) > now + timedelta(minutes=1):
                return _ensure_prepay(db, current, now)
            outcome = _close_channel(db, current, "replaced", now)
            if outcome == PAY_PAID:
                raise AppError(ErrorCode.CONFLICT, "already paid", data={"reason": "already_paid"})
            if outcome in PAY_OPEN:
                raise UpstreamError("微信支付暂不可用，请稍后重试", data={"payment_unavailable": True, "reason": "close_pending"})
            continue
        rules.validate_expire(expires_at, now)
        out_trade_no = rules.new_out_trade_no(cfg.out_trade_no_prefix, now, cfg.tz)
        body = _prepay_body(client, out_trade_no, description, amount_cents, openid, expires_at, attach)
        record = PaymentRecord(
            business_type=business_type, business_id=business_id, business_no=(business_no or "")[:64], user_id=user_id,
            store_id=store_id, goods_kind=goods_kind, out_trade_no=out_trade_no, amount_cents=amount_cents,
            status=PAY_CREATED, description=description, payer_openid=openid, request_json=dumps(body),
            expires_at=expires_at, next_query_at=now + timedelta(seconds=rules.next_query_delay(0, cfg.query_schedule_s)),
        )
        db.add(record)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()  # 并发创建：部分唯一索引裁决，重新读取未完成单
            continue
        return _ensure_prepay(db, record, now)
    raise AppError(ErrorCode.CONFLICT, "payment is being created, retry", data={"reason": "busy"})


def _ensure_prepay(db: Session, p: PaymentRecord, now: datetime) -> dict[str, Any]:
    cfg = get_config()
    client = get_client()
    if p.status == PAY_PAYING and p.prepay_id and p.prepay_at and aware(p.prepay_at) > now - timedelta(seconds=cfg.prepay_reuse_seconds):
        return _result(p, client.jsapi_pay_params(p.prepay_id))
    body = json.loads(p.request_json)
    db.commit()  # 结束读事务，渠道调用不在事务内
    try:
        prepay_id = _call("prepay", lambda: client.jsapi_prepay(body), timeout=cfg.timeout_prepay_s, attempts=1)
    except CircuitOpenError:
        raise
    except WechatPayApiError as exc:
        if exc.retryable:  # 支付单保持 created / paying，可用相同参数（同一单号）再次发起
            raise UpstreamError("微信支付暂不可用，请稍后重试",
                                data={"code": exc.code, "payment_unavailable": True}) from exc
        if exc.code in ("OUT_TRADE_NO_USED", "TRADE_ERROR", "ORDERPAID", "ORDER_CLOSED"):
            try:
                status = _sync_once(db, p, now)  # 以渠道事实为准（可能已支付）
            except (WechatPayApiError, CircuitOpenError, AppError):
                db.rollback()
                status = None
            if status == PAY_PAID:
                raise AppError(ErrorCode.CONFLICT, "already paid", data={"reason": "already_paid"}) from exc
        _fail_payment(db, p, exc.code, now)
        raise UpstreamError("微信支付下单失败", data={"code": exc.code}) from exc
    changed = _upd(db, PaymentRecord, PaymentRecord.id == p.id, PaymentRecord.status.in_(PAY_OPEN),
                   status=PAY_PAYING, prepay_id=prepay_id, prepay_at=now)
    db.commit()
    p = _reload(db, PaymentRecord, p.id)
    if changed != 1:
        if p.status == PAY_PAID:
            raise AppError(ErrorCode.CONFLICT, "already paid", data={"reason": "already_paid"})
        raise AppError(ErrorCode.CONFLICT, "payment status changed", data={"status": p.status})
    return _result(p, client.jsapi_pay_params(prepay_id))


def _fail_payment(db: Session, p: PaymentRecord, code: str, now: datetime) -> None:
    changed = _upd(db, PaymentRecord, PaymentRecord.id == p.id, PaymentRecord.status.in_(PAY_OPEN),
                   status=PAY_FAILED, fail_code=code[:64], next_query_at=None, closed_at=now)
    if changed == 1:
        _payment_event(db, "payment.failed", _reload(db, PaymentRecord, p.id), code=code)
    db.commit()


def pay_business(db: Session, user_id: int, business_type: str, business_id: int, now: datetime | None = None) -> dict[str, Any]:
    """顾客端“去支付 / 继续支付”：金额与截止时间由业务模块注册的解析器给出。"""
    resolver = _payables.get(business_type)
    if resolver is None:
        raise AppError(ErrorCode.NOT_FOUND, "business not payable")
    payable = resolver(db, user_id, business_id)
    return create_payment(db, business_type, business_id, user_id, payable.store_id, payable.amount_cents,
                          payable.expires_at, payable.description, goods_kind=payable.goods_kind,
                          business_no=payable.business_no, attach=payable.attach, now=now)


# ---------------------------------------------------------------- 支付事实（回调 / 查单共用）


def apply_transaction(db: Session, p: PaymentRecord, txn: Mapping[str, Any], *, source: str, now: datetime) -> str:
    """核对并应用一条交易事实，返回 processed / duplicate / 当前本地状态。核对失败抛 WechatPayError。"""
    client = get_client()
    local = apiv3.check_transaction(txn, appid=client.appid, mchid=client.mchid, out_trade_no=p.out_trade_no,
                                    amount_cents=p.amount_cents, currency=p.currency)
    if local == PAY_PAID:
        return _settle_paid(db, p, txn, source=source, now=now)
    if local == PAY_CLOSED:
        return _mark_closed(db, p, "channel_closed", now)
    if local == PAY_FAILED:
        _fail_payment(db, p, str(txn.get("trade_state")), now)
        return _reload(db, PaymentRecord, p.id).status
    _upd(db, PaymentRecord, PaymentRecord.id == p.id, trade_state=str(txn.get("trade_state"))[:16])
    db.commit()
    return _reload(db, PaymentRecord, p.id).status


def _settle_paid(db: Session, p: PaymentRecord, txn: Mapping[str, Any], *, source: str, now: datetime) -> str:
    cfg = get_config()
    txn_id = str(txn["transaction_id"])
    paid_at = rules.parse_rfc3339(txn.get("success_time")) or now
    values = dict(status=PAY_PAID, channel_txn_id=txn_id, paid_at=paid_at, trade_state=str(txn.get("trade_state"))[:16],
                  next_query_at=None)
    late = False
    try:
        won = _upd(db, PaymentRecord, PaymentRecord.id == p.id, PaymentRecord.status.in_(PAY_OPEN), **values)
        if won != 1:
            won = _upd(db, PaymentRecord, PaymentRecord.id == p.id, PaymentRecord.status == PAY_CLOSED, **values)
            late = won == 1
    except IntegrityError:  # channel_txn_id 唯一：并发的同一事实
        db.rollback()
        won = 0
    if won != 1:
        db.rollback()
        current = _reload(db, PaymentRecord, p.id)
        if current.status == PAY_PAID and current.channel_txn_id == txn_id:
            return "duplicate"
        raise apiv3.WechatPayError("state_conflict", f"payment is {current.status}")
    p = _reload(db, PaymentRecord, p.id)
    reasons: list[str] = []
    if late:
        reasons.append("paid_after_close")
    other_paid = db.scalar(select(PaymentRecord.id).where(
        PaymentRecord.business_type == p.business_type, PaymentRecord.business_id == p.business_id,
        PaymentRecord.status == PAY_PAID, PaymentRecord.id != p.id).limit(1))
    if other_paid:
        reasons.append("duplicate_payment")
    auto_refund = bool(reasons)
    if not reasons:
        hook = _on_paid.get(p.business_type)
        if hook is None:
            reasons.append("no_on_paid_handler")  # CONTRACTS：未注册 → 记成功 + 人工，不抛错
            log.warning("wechat_pay: no on_paid handler for %s", p.business_type)
        else:
            error = _run_hook(db, hook, _payment_info(p))
            if error:
                reasons.append(f"business_rejected: {error}")
                auto_refund = True
    if reasons:
        _upd(db, PaymentRecord, PaymentRecord.id == p.id, needs_manual=True, manual_reason="; ".join(reasons)[:200])
    _payment_event(db, "payment.succeeded", p, transaction_id=txn_id, paid_at=aware(paid_at).isoformat(),
                   needs_manual=bool(reasons), source=source)
    refund = None
    if auto_refund and cfg.auto_refund_orphans:
        refund = _reserve_refund(db, p.id, p.amount_cents, "支付异常自动退款", "system:auto_refund", "system", now,
                                 enforce_interval=False, commit=False)
    db.commit()
    if refund is not None:
        log.warning("wechat_pay: auto refund %s for %s (%s)", refund.out_refund_no, p.out_trade_no, reasons)
    return "processed"


def _mark_closed(db: Session, p: PaymentRecord, reason: str, now: datetime) -> str:
    changed = _upd(db, PaymentRecord, PaymentRecord.id == p.id, PaymentRecord.status.in_(PAY_OPEN),
                   status=PAY_CLOSED, closed_at=now, close_reason=reason[:32], trade_state="CLOSED", next_query_at=None)
    if changed == 1:
        _payment_event(db, "payment.closed", _reload(db, PaymentRecord, p.id), reason=reason)
    db.commit()
    return _reload(db, PaymentRecord, p.id).status


def _query(out_trade_no: str) -> dict[str, Any]:
    cfg = get_config()
    client = get_client()
    return _call("query", lambda: client.query_by_out_trade_no(out_trade_no), timeout=cfg.timeout_query_s,
                 attempts=cfg.query_attempts)


def _sync_once(db: Session, p: PaymentRecord, now: datetime) -> str:
    """查单一次并应用事实；渠道无此单返回本地状态。核对失败标人工。"""
    db.commit()
    try:
        txn = _query(p.out_trade_no)
    except WechatPayApiError as exc:
        if exc.code == "ORDER_NOT_EXIST":
            return _reload(db, PaymentRecord, p.id).status
        raise
    try:
        outcome = apply_transaction(db, p, txn, source="query", now=now)
    except apiv3.WechatPayError as exc:
        db.rollback()
        _flag_manual(db, PaymentRecord, p.id, f"query_{exc.reason}")
        _log_notify(db, kind="pay", result="rejected", reason=exc.reason, plain=txn, verified=True, source="query", now=now)
        raise AppError(ErrorCode.BUSINESS_RULE, "payment facts mismatch", data={"reason": exc.reason}) from exc
    if outcome == "processed":
        _log_notify(db, kind="pay", result="processed", plain=txn, verified=True, source="query", now=now)
    return _reload(db, PaymentRecord, p.id).status


def _close_channel(db: Session, p: PaymentRecord, reason: str, now: datetime) -> str:
    """先查单再关单（4012791911）。返回最终本地状态；渠道暂不可用时返回仍未完成的状态并登记待关单。"""
    cfg = get_config()
    client = get_client()
    if p.close_requested_at is None:
        _upd(db, PaymentRecord, PaymentRecord.id == p.id, PaymentRecord.close_requested_at.is_(None), close_requested_at=now)
    db.commit()
    try:
        txn = _query(p.out_trade_no)
    except WechatPayApiError as exc:
        if exc.code == "ORDER_NOT_EXIST":  # 渠道从未建单：本地直接关闭
            return _mark_closed(db, p, reason, now)
        return _reload(db, PaymentRecord, p.id).status
    except CircuitOpenError:
        return _reload(db, PaymentRecord, p.id).status
    try:
        status = apply_transaction(db, p, txn, source="query", now=now)
    except apiv3.WechatPayError as exc:
        db.rollback()
        _flag_manual(db, PaymentRecord, p.id, f"query_{exc.reason}")
        return _reload(db, PaymentRecord, p.id).status
    current = _reload(db, PaymentRecord, p.id)
    if status in ("processed", "duplicate") or current.status not in PAY_OPEN:
        return current.status
    try:
        _call("close", lambda: client.close(p.out_trade_no), timeout=cfg.timeout_close_s, attempts=cfg.close_attempts)
    except WechatPayApiError as exc:
        if not exc.retryable:  # 例如已支付：再查一次以事实为准
            try:
                apply_transaction(db, current, _query(p.out_trade_no), source="query", now=now)
            except (WechatPayApiError, apiv3.WechatPayError, CircuitOpenError):
                db.rollback()
        return _reload(db, PaymentRecord, p.id).status
    except CircuitOpenError:
        return current.status
    return _mark_closed(db, current, reason, now)


def close_payment(db: Session, payment_id: int, reason: str = "business_cancel", now: datetime | None = None) -> dict[str, Any]:
    """业务取消 / 超时：查单 → 关单。已支付返回 status=paid（业务方应改走退款）；渠道不可用时登记待关单由补偿任务完成。"""
    now = _now(now)
    p = db.get(PaymentRecord, payment_id)
    if p is None:
        raise AppError(ErrorCode.NOT_FOUND, "payment not found")
    if p.status not in PAY_OPEN:
        return {"payment_id": p.id, "status": p.status}
    status = _close_channel(db, p, reason[:32], now)
    return {"payment_id": p.id, "status": status if status not in PAY_OPEN else "closing"}


def query_and_reconcile(db: Session, out_trade_no: str, now: datetime | None = None) -> dict[str, Any]:
    """主动查单并按回调同一逻辑落库（CONTRACTS）。"""
    now = _now(now)
    p = db.scalar(select(PaymentRecord).where(PaymentRecord.out_trade_no == out_trade_no))
    if p is None:
        raise AppError(ErrorCode.NOT_FOUND, "payment not found")
    if p.status in PAY_OPEN or p.status == PAY_CLOSED:
        try:
            _sync_once(db, p, now)
        except WechatPayApiError as exc:
            raise UpstreamError("微信支付查询失败", data={"code": exc.code}) from exc
        _upd(db, PaymentRecord, PaymentRecord.id == p.id, last_queried_at=now)
        db.commit()
    p = _reload(db, PaymentRecord, p.id)
    return {"payment_id": p.id, "out_trade_no": p.out_trade_no, "status": p.status, "trade_state": p.trade_state}


def paid_total(db: Session, business_type: str, business_id: int) -> int:
    """该业务对象已支付金额合计（不扣退款）。"""
    return int(db.scalar(select(func.coalesce(func.sum(PaymentRecord.amount_cents), 0)).where(
        PaymentRecord.business_type == business_type, PaymentRecord.business_id == business_id,
        PaymentRecord.status == PAY_PAID)) or 0)


def get_business_payment(db: Session, business_type: str, business_id: int) -> dict[str, Any] | None:
    p = db.scalars(select(PaymentRecord).where(PaymentRecord.business_type == business_type,
                                               PaymentRecord.business_id == business_id)
                   .order_by(PaymentRecord.id.desc())).first()
    return payment_view(p) if p else None


def payment_status_for_user(db: Session, user_id: int, out_trade_no: str, now: datetime | None = None,
                            sync: bool = True) -> dict[str, Any]:
    """顾客查询支付结果：未完成时节流查单一次（渠道失败不影响返回，以服务端当前状态为准）。"""
    cfg = get_config()
    now = _now(now)
    p = db.scalar(select(PaymentRecord).where(PaymentRecord.out_trade_no == out_trade_no))
    if p is None or p.user_id != user_id:
        raise AppError(ErrorCode.NOT_FOUND, "payment not found")
    degraded = False
    if sync and p.status in PAY_OPEN and p.prepay_id:
        cutoff = now - timedelta(seconds=cfg.app_sync_min_interval_s)
        claimed = _upd(db, PaymentRecord, PaymentRecord.id == p.id,
                       or_(PaymentRecord.last_queried_at.is_(None), PaymentRecord.last_queried_at <= cutoff),
                       last_queried_at=now)
        db.commit()
        if claimed == 1:
            try:
                _sync_once(db, p, now)
            except (WechatPayApiError, CircuitOpenError, AppError):
                db.rollback()
                degraded = True
    p = _reload(db, PaymentRecord, p.id)
    return {"payment_id": p.id, "out_trade_no": p.out_trade_no, "status": p.status, "amount_cents": p.amount_cents,
            "business_type": p.business_type, "business_id": p.business_id,
            "paid_at": aware(p.paid_at).isoformat() if p.paid_at else None,
            "expires_at": aware(p.expires_at).isoformat(), "degraded": degraded}


# ---------------------------------------------------------------- 回调


@dataclass
class NotifyResult:
    http_status: int              # 204 成功；4xx / 5xx 失败（微信会重发）
    body: dict[str, str] | None   # 失败应答体 {"code":"FAIL","message":…}
    result: str                   # processed | duplicate | rejected | ignored
    reason: str | None = None


def _serial(headers: Mapping[str, str]) -> str | None:
    return next((v for k, v in headers.items() if k.lower() == "wechatpay-serial"), None)


def _verify_notification(db: Session, kind: str, headers: Mapping[str, str], body: bytes,
                         now: datetime) -> tuple[dict, dict] | NotifyResult:
    try:
        return get_client().parse_notification(headers, body, now=int(now.timestamp()))
    except apiv3.WechatPayError as exc:
        _log_notify(db, kind=kind, result="rejected", reason=exc.reason, serial=_serial(headers), now=now)
        status = 401 if exc.reason in ("sign_probe", "bad_signature", "unknown_serial", "stale_timestamp",
                                       "missing_headers", "bad_timestamp") else 400
        return NotifyResult(status, apiv3.failure_body("签名验证失败" if status == 401 else "报文无效"), "rejected", exc.reason)


def handle_pay_notify(db: Session, headers: Mapping[str, str], body: bytes, now: datetime | None = None) -> NotifyResult:
    """支付成功回调：验签 → 解密 → 核对 → 幂等落库（条件 UPDATE）→ 同事务钩子与事件 → 204。只做本地写，满足 5 秒应答。"""
    now = _now(now)
    parsed = _verify_notification(db, "pay", headers, body, now)
    if isinstance(parsed, NotifyResult):
        return parsed
    envelope, plain = parsed
    serial = _serial(headers)
    if envelope.get("event_type") != "TRANSACTION.SUCCESS":
        _log_notify(db, kind="pay", result="ignored", reason="event_type", envelope=envelope, plain=plain, serial=serial,
                    verified=True, now=now)
        return NotifyResult(204, None, "ignored")
    p = db.scalar(select(PaymentRecord).where(PaymentRecord.out_trade_no == str(plain.get("out_trade_no", ""))))
    if p is None:
        _log_notify(db, kind="pay", result="rejected", reason="unknown_order", envelope=envelope, plain=plain,
                    serial=serial, verified=True, now=now)
        return NotifyResult(404, apiv3.failure_body("订单不存在"), "rejected", "unknown_order")
    try:
        outcome = apply_transaction(db, p, plain, source="notify", now=now)
    except apiv3.WechatPayError as exc:
        db.rollback()
        _flag_manual(db, PaymentRecord, p.id, f"notify_{exc.reason}")
        _log_notify(db, kind="pay", result="rejected", reason=exc.reason, envelope=envelope, plain=plain, serial=serial,
                    verified=True, now=now)
        log.error("wechat_pay notify rejected: %s %s", p.out_trade_no, exc.reason)
        return NotifyResult(400, apiv3.failure_body("订单信息不一致"), "rejected", exc.reason)
    result = "duplicate" if outcome == "duplicate" else "processed"
    _log_notify(db, kind="pay", result=result, envelope=envelope, plain=plain, serial=serial, verified=True, now=now)
    return NotifyResult(204, None, result)


def handle_refund_notify(db: Session, headers: Mapping[str, str], body: bytes, now: datetime | None = None) -> NotifyResult:
    """退款结果回调（REFUND.SUCCESS / ABNORMAL / CLOSED）：与支付回调相同的验签、解密、幂等规则。"""
    now = _now(now)
    parsed = _verify_notification(db, "refund", headers, body, now)
    if isinstance(parsed, NotifyResult):
        return parsed
    envelope, plain = parsed
    serial = _serial(headers)
    status = rules.REFUND_EVENT_STATUS.get(str(envelope.get("event_type")))
    if status is None:
        _log_notify(db, kind="refund", result="ignored", reason="event_type", envelope=envelope, plain=plain,
                    serial=serial, verified=True, now=now)
        return NotifyResult(204, None, "ignored")
    if plain.get("mchid") != get_client().mchid:
        _log_notify(db, kind="refund", result="rejected", reason="merchant_mismatch", envelope=envelope, plain=plain,
                    serial=serial, verified=True, now=now)
        return NotifyResult(400, apiv3.failure_body("商户号不一致"), "rejected", "merchant_mismatch")
    refund = db.scalar(select(RefundRecord).where(RefundRecord.out_refund_no == str(plain.get("out_refund_no", ""))))
    if refund is None:
        _log_notify(db, kind="refund", result="rejected", reason="unknown_refund", envelope=envelope, plain=plain,
                    serial=serial, verified=True, now=now)
        return NotifyResult(404, apiv3.failure_body("退款单不存在"), "rejected", "unknown_refund")
    fact = {**plain, "status": plain.get("refund_status") or status}
    try:
        outcome = apply_refund_fact(db, refund, fact, source="notify", now=now)
    except apiv3.WechatPayError as exc:
        db.rollback()
        _flag_manual(db, RefundRecord, refund.id, exc.reason)
        _log_notify(db, kind="refund", result="rejected", reason=exc.reason, envelope=envelope, plain=plain,
                    serial=serial, verified=True, now=now)
        return NotifyResult(400, apiv3.failure_body("退款信息不一致"), "rejected", exc.reason)
    result = "duplicate" if outcome == "duplicate" else "processed"
    _log_notify(db, kind="refund", result=result, envelope=envelope, plain=plain, serial=serial, verified=True, now=now)
    return NotifyResult(204, None, result)


handle_notify = handle_pay_notify  # 兼容 v1 名称


# ---------------------------------------------------------------- 退款


def _limits() -> rules.RefundLimits:
    cfg = get_config()
    return rules.RefundLimits(cfg.refund_max_times, cfg.refund_min_interval_s, cfg.refund_window_days)


def _reserve_refund(db: Session, payment_id: int, amount: int, reason: str, business_ref: str, actor: str, now: datetime,
                    *, enforce_interval: bool = True, commit: bool = True) -> RefundRecord:
    """一条条件 UPDATE 同时裁决：已支付、累计 ≤ 实付、次数 < 50、间隔 ≥ 1 分钟、支付后 365 天内。"""
    limits = _limits()
    conditions = [
        PaymentRecord.id == payment_id, PaymentRecord.status == PAY_PAID,
        PaymentRecord.refunded_cents + amount <= PaymentRecord.amount_cents,
        PaymentRecord.refund_count < limits.max_times,
        PaymentRecord.paid_at >= now - timedelta(days=limits.window_days),
    ]
    if enforce_interval:
        conditions.append(or_(PaymentRecord.last_refund_at.is_(None),
                              PaymentRecord.last_refund_at <= now - timedelta(seconds=limits.min_interval_s)))
    changed = 0
    if amount > 0:
        changed = _upd(db, PaymentRecord, *conditions, refunded_cents=PaymentRecord.refunded_cents + amount,
                       refund_count=PaymentRecord.refund_count + 1, last_refund_at=now)
    if changed != 1:
        db.rollback()
        p = _reload(db, PaymentRecord, payment_id)
        if p is None:
            raise AppError(ErrorCode.NOT_FOUND, "payment not found")
        error = rules.refund_block_reason(
            status=p.status, amount_cents=p.amount_cents, refunded_cents=p.refunded_cents, refund_count=p.refund_count,
            last_refund_at=p.last_refund_at if enforce_interval else None, paid_at=p.paid_at, amount=amount, now=now,
            limits=limits)
        raise error or AppError(ErrorCode.CONFLICT, "refund changed concurrently, retry")
    seq, out_trade_no = db.execute(select(PaymentRecord.refund_count, PaymentRecord.out_trade_no)
                                   .where(PaymentRecord.id == payment_id)).one()
    refund = RefundRecord(payment_id=payment_id, business_ref=business_ref[:64], out_refund_no=rules.out_refund_no(out_trade_no, seq),
                          amount_cents=amount, reason=reason, status=REFUND_PENDING, requested_by=actor[:64],
                          next_attempt_at=now)
    db.add(refund)
    db.flush()
    if commit:
        db.commit()
    return refund


def create_refund(db: Session, payment_id: int, amount_cents: int, reason: str, business_ref: str, *,
                  actor: str = "system", now: datetime | None = None) -> RefundInfo:
    """申请退款（CONTRACTS）。规则违规抛 4220 / 4225 / 4090；额度占用后不再抛错：渠道结果写在返回的 status 上。

    - 可重试错误（SYSTEM_ERROR、超时、FREQUENCY_LIMITED、NOT_ENOUGH …）：保持 pending，由任务先查询再用**同一** out_refund_no 重试；
    - 明确拒绝：failed + 释放额度 + refund.failed 事件。
    """
    now = _now(now)
    if not isinstance(amount_cents, int) or amount_cents <= 0:
        raise AppError(ErrorCode.BAD_REQUEST, "refund amount must be a positive integer of cents")
    reason = _trim_bytes(rules.sanitize_text(reason, 80), 80)
    refund = _reserve_refund(db, payment_id, amount_cents, reason, (business_ref or "")[:64], actor, now)
    return _submit_refund(db, refund, now)


def _refund_body(r: RefundRecord, p: PaymentRecord) -> dict[str, Any]:
    body: dict[str, Any] = {"out_trade_no": p.out_trade_no, "out_refund_no": r.out_refund_no,
                            "notify_url": get_config().refund_notify_url,
                            "amount": {"refund": r.amount_cents, "total": p.amount_cents, "currency": "CNY"}}
    if r.reason:
        body["reason"] = r.reason
    return body


def _submit_refund(db: Session, refund: RefundRecord, now: datetime) -> RefundInfo:
    cfg = get_config()
    client = get_client()
    p = _reload(db, PaymentRecord, refund.payment_id)
    body = _refund_body(refund, p)
    db.commit()
    try:
        data = _call("refund", lambda: client.create_refund(body), timeout=cfg.timeout_refund_s, attempts=1)
    except WechatPayApiError as exc:
        if exc.retryable:
            _refund_retry_later(db, refund, exc.code, now)
        else:
            _finish_refund(db, refund, REFUND_FAILED, now, error=exc.code)
        return _refund_info(_reload(db, RefundRecord, refund.id), p)
    except (CircuitOpenError, TransientError, AppError) as exc:
        _refund_retry_later(db, refund, type(exc).__name__, now)
        return _refund_info(_reload(db, RefundRecord, refund.id), p)
    try:
        apply_refund_fact(db, refund, data, source="submit", now=now)
    except apiv3.WechatPayError as exc:
        db.rollback()
        _flag_manual(db, RefundRecord, refund.id, exc.reason)
    return _refund_info(_reload(db, RefundRecord, refund.id), _reload(db, PaymentRecord, p.id))


def _refund_retry_later(db: Session, refund: RefundRecord, code: str, now: datetime) -> None:
    current = _reload(db, RefundRecord, refund.id)
    attempts = current.attempts + 1
    values: dict[str, Any] = {"attempts": attempts, "last_error_code": code[:64],
                              "next_attempt_at": now + timedelta(seconds=rules.retry_delay(attempts))}
    if attempts >= get_config().refund_submit_max_attempts:
        values["needs_manual"] = True
    _upd(db, RefundRecord, RefundRecord.id == refund.id, RefundRecord.status == REFUND_PENDING, **values)
    db.commit()


def _release(db: Session, r: RefundRecord) -> None:
    _upd(db, PaymentRecord, PaymentRecord.id == r.payment_id, refunded_cents=PaymentRecord.refunded_cents - r.amount_cents)


def _refund_hook(db: Session, r: RefundRecord, p: PaymentRecord) -> None:
    key = r.business_ref.split(":", 1)[0] if ":" in r.business_ref else ""
    fn = _on_refunded.get(key) or _on_refunded.get(p.business_type)
    error = _run_hook(db, fn, _refund_info(r, p))
    if error:
        _upd(db, RefundRecord, RefundRecord.id == r.id, needs_manual=True)
        log.error("wechat_pay refund hook failed: %s %s", r.out_refund_no, error)


def _finish_refund(db: Session, refund: RefundRecord, target: str, now: datetime, error: str | None = None) -> bool:
    sources = rules.REFUND_MACHINE.sources_of(target)
    values: dict[str, Any] = {"status": target, "finished_at": now, "next_query_at": None, "next_attempt_at": None}
    if error:
        values["last_error_code"] = error[:64]
    changed = _upd(db, RefundRecord, RefundRecord.id == refund.id, RefundRecord.status.in_(sources), **values)
    if changed != 1:
        db.rollback()
        return False
    r = _reload(db, RefundRecord, refund.id)
    _release(db, r)
    p = _reload(db, PaymentRecord, r.payment_id)
    _refund_event(db, "refund.failed", r, p, reason=error or target)
    _refund_hook(db, r, p)
    db.commit()
    return True


def apply_refund_fact(db: Session, refund: RefundRecord, data: Mapping[str, Any], *, source: str, now: datetime) -> str:
    """应用退款渠道事实（申请应答 / 查询 / 回调），返回 processed / duplicate / ignored。金额或单号不符抛 WechatPayError。"""
    cfg = get_config()
    if str(data.get("out_refund_no")) != refund.out_refund_no:
        raise apiv3.WechatPayError("refund_mismatch", "退款单号不一致")
    amount = (data.get("amount") or {}).get("refund")
    if amount is not None and int(amount) != refund.amount_cents:
        raise apiv3.WechatPayError("amount_mismatch", "退款金额不一致")
    target = rules.REFUND_STATUS_MAP.get(str(data.get("status") or data.get("refund_status") or ""))
    if target is None:
        return "ignored"
    channel = {"channel_refund_id": data.get("refund_id"), "channel_status": str(data.get("status") or data.get("refund_status"))[:16]}
    if data.get("channel"):
        channel["refund_channel"] = str(data["channel"])[:32]
    if data.get("user_received_account"):
        channel["user_received_account"] = str(data["user_received_account"])[:64]
    channel = {k: v for k, v in channel.items() if v}
    current = _reload(db, RefundRecord, refund.id)
    if current.status == target:
        if channel:
            _upd(db, RefundRecord, RefundRecord.id == refund.id, **channel)
            db.commit()
        return "duplicate"
    sources = rules.REFUND_MACHINE.sources_of(target)
    if target == REFUND_PROCESSING:
        changed = _upd(db, RefundRecord, RefundRecord.id == refund.id, RefundRecord.status.in_(sources),
                       status=REFUND_PROCESSING, submitted_at=current.submitted_at or now, query_count=0,
                       next_query_at=now + timedelta(seconds=cfg.refund_query_fast_s), **channel)
        db.commit()
        return "processed" if changed == 1 else "duplicate"
    if target == REFUND_CLOSED:
        changed = _upd(db, RefundRecord, RefundRecord.id == refund.id, RefundRecord.status.in_(sources), **channel)
        db.commit()
        return "processed" if changed == 1 and _finish_refund(db, refund, REFUND_CLOSED, now, error="CLOSED") else "duplicate"
    values: dict[str, Any] = {"status": target, "next_query_at": None, **channel}
    if target == REFUND_SUCCEEDED:
        values.update(succeeded_at=rules.parse_rfc3339(data.get("success_time")) or now, finished_at=now,
                      submitted_at=current.submitted_at or now)
    else:  # abnormal
        values.update(needs_manual=True, submitted_at=current.submitted_at or now)
    changed = _upd(db, RefundRecord, RefundRecord.id == refund.id, RefundRecord.status.in_(sources), **values)
    if changed != 1:
        db.rollback()
        return "duplicate"
    r = _reload(db, RefundRecord, refund.id)
    if target == REFUND_SUCCEEDED:
        _upd(db, PaymentRecord, PaymentRecord.id == r.payment_id,
             refund_succeeded_cents=PaymentRecord.refund_succeeded_cents + r.amount_cents)
    p = _reload(db, PaymentRecord, r.payment_id)
    if target == REFUND_SUCCEEDED:
        _refund_event(db, "refund.succeeded", r, p, source=source)
        _refund_hook(db, r, p)
    else:
        _refund_event(db, "refund.abnormal", r, p, source=source)
    db.commit()
    return "processed"


REFUND_ACTIVE_STATUSES = (REFUND_PENDING, REFUND_PROCESSING, REFUND_SUCCEEDED, REFUND_ABNORMAL)


def find_active_refund(db: Session, business_ref: str) -> RefundRecord | None:
    """同一业务退款引用（如 `after_sales:{id}`）下仍有效的退款（pending / processing / succeeded / abnormal），最新的一笔。

    业务模块崩溃重提前先查这里：有就采纳，不再新建，避免重复退款。failed / closed 不算（可以重新发起）。
    """
    if not business_ref:
        return None
    return db.execute(select(RefundRecord).where(RefundRecord.business_ref == business_ref,
                                                 RefundRecord.status.in_(REFUND_ACTIVE_STATUSES))
                      .order_by(RefundRecord.id.desc()).limit(1)).scalar_one_or_none()


def get_refund_status(db: Session, refund_id: int) -> RefundInfo:
    r = db.get(RefundRecord, refund_id)
    if r is None:
        raise AppError(ErrorCode.NOT_FOUND, "refund not found")
    return _refund_info(r, db.get(PaymentRecord, r.payment_id))


def sync_refund(db: Session, refund: RefundRecord, now: datetime) -> str:
    """查询单笔退款并应用；返回本地状态。"""
    cfg = get_config()
    client = get_client()
    db.commit()
    data = _call("refund_query", lambda: client.query_refund(refund.out_refund_no), timeout=cfg.timeout_query_s,
                 attempts=cfg.query_attempts)
    apply_refund_fact(db, refund, data, source="query", now=now)
    return _reload(db, RefundRecord, refund.id).status


def apply_abnormal_refund(db: Session, refund_id: int, *, refund_type: str, bank_type: str | None = None,
                          bank_account: str | None = None, real_name: str | None = None, staff_id: int,
                          now: datetime | None = None) -> dict[str, Any]:
    """发起异常退款（仅 ABNORMAL）：退到用户银行卡（卡号、姓名按官方 OAEP 加密）或商户银行账户。审计不含敏感信息。"""
    cfg = get_config()
    client = get_client()
    now = _now(now)
    r = db.get(RefundRecord, refund_id)
    if r is None:
        raise AppError(ErrorCode.NOT_FOUND, "refund not found")
    if r.status != REFUND_ABNORMAL or not r.channel_refund_id:
        raise AppError(ErrorCode.CONFLICT, "refund is not abnormal", data={"status": r.status})
    body: dict[str, Any] = {"out_refund_no": r.out_refund_no, "type": refund_type}
    if refund_type == "USER_BANK_CARD":
        if not (bank_type and bank_account and real_name):
            raise AppError(ErrorCode.BAD_REQUEST, "bank_type, bank_account and real_name are required")
        body.update(bank_type=bank_type, bank_account=client.encrypt_sensitive(bank_account),
                    real_name=client.encrypt_sensitive(real_name))
    elif refund_type != "MERCHANT_BANK_CARD":
        raise AppError(ErrorCode.BAD_REQUEST, "type must be USER_BANK_CARD or MERCHANT_BANK_CARD")
    channel_refund_id = r.channel_refund_id
    db.commit()
    try:
        data = _call("abnormal_refund", lambda: client.apply_abnormal_refund(channel_refund_id, body),
                     timeout=cfg.timeout_refund_s, attempts=1)
    except WechatPayApiError as exc:
        raise UpstreamError("异常退款提交失败", data={"code": exc.code, "retryable": exc.retryable}) from exc
    apply_refund_fact(db, r, data, source="abnormal", now=now)
    r = _reload(db, RefundRecord, refund_id)
    write_audit(db, "staff", staff_id, "payment.refund.abnormal", "refund", r.id, before={"status": REFUND_ABNORMAL},
                after={"status": r.status, "type": refund_type})
    db.commit()
    return refund_view(r)


def admin_refund(db: Session, payment_id: int, amount_cents: int, reason: str, *, staff_id: int,
                 business_ref: str = "", now: datetime | None = None) -> dict[str, Any]:
    """后台发起退款（payment:refund）：与 create_refund 同规则，另写审计。"""
    now = _now(now)
    before = db.get(PaymentRecord, payment_id)
    if before is None:
        raise AppError(ErrorCode.NOT_FOUND, "payment not found")
    snapshot = {"refunded_cents": before.refunded_cents, "refund_count": before.refund_count}
    info = create_refund(db, payment_id, amount_cents, reason, business_ref or f"admin:{staff_id}",
                         actor=f"staff:{staff_id}", now=now)
    write_audit(db, "staff", staff_id, "payment.refund", "payment", payment_id, before=snapshot,
                after={"refund_id": info.refund_id, "amount_cents": amount_cents, "status": info.status}, reason=reason)
    db.commit()
    return refund_view(db.get(RefundRecord, info.refund_id))


# ---------------------------------------------------------------- 小程序发货信息管理


def request_shipping_upload(db: Session, payment_id: int, item_desc: str, *, logistics_type: int = 3,
                            tracking_no: str | None = None, express_company: str | None = None,
                            now: datetime | None = None) -> dict[str, Any]:
    """履约完成后录入发货信息（upload_shipping_info）。默认虚拟发货 logistics_type=3。幂等：每笔支付一条。"""
    cfg = get_config()
    now = _now(now)
    if not cfg.shipping_enabled:
        return {"payment_id": payment_id, "status": "skipped"}
    p = db.get(PaymentRecord, payment_id)
    if p is None:
        raise AppError(ErrorCode.NOT_FOUND, "payment not found")
    if p.status != PAY_PAID:
        raise AppError(ErrorCode.CONFLICT, "payment not paid", data={"status": p.status})
    existing = db.scalar(select(ShippingUpload).where(ShippingUpload.payment_id == payment_id))
    if existing is not None:
        return shipping_view(existing)
    desc = rules.sanitize_text(item_desc, 120)
    if not desc:
        raise AppError(ErrorCode.BAD_REQUEST, "item_desc required")
    if logistics_type not in (1, 2, 3, 4):
        raise AppError(ErrorCode.BAD_REQUEST, "logistics_type must be 1-4")
    item: dict[str, Any] = {"item_desc": desc}
    if logistics_type == 1:
        if not (tracking_no and express_company):
            raise AppError(ErrorCode.BAD_REQUEST, "tracking_no and express_company are required for express")
        item.update(tracking_no=tracking_no[:128], express_company=express_company[:64])
    order_key = ({"order_number_type": 2, "transaction_id": p.channel_txn_id} if p.channel_txn_id
                 else {"order_number_type": 1, "mchid": get_client().mchid, "out_trade_no": p.out_trade_no})
    payload = {"order_key": order_key, "logistics_type": logistics_type, "delivery_mode": 1, "shipping_list": [item],
               "payer": {"openid": p.payer_openid}}
    record = ShippingUpload(payment_id=p.id, out_trade_no=p.out_trade_no, logistics_type=logistics_type,
                            payload_json=dumps(payload), status="pending", next_attempt_at=now)
    db.add(record)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return shipping_view(db.scalar(select(ShippingUpload).where(ShippingUpload.payment_id == payment_id)))
    return upload_shipping(db, record, now)


def upload_shipping(db: Session, record: ShippingUpload, now: datetime) -> dict[str, Any]:
    cfg = get_config()
    client = get_shipping_client()
    payload = json.loads(record.payload_json)
    payload["upload_time"] = rules.rfc3339(now, cfg.tz)  # 每次上传必须比上一次晚
    db.commit()
    errcode: int | None = None
    error: str | None = None
    done = retryable = False
    try:
        resilient("wechat_shipping.upload", lambda: client.upload_shipping_info(payload), timeout_s=5, attempts=1,
                  breaker=_breakers.get("wechat_shipping"), sleep=_sleep)
        done = True
    except ShippingApiError as exc:
        errcode, error = exc.errcode, exc.errmsg
        retryable = exc.errcode in SHIPPING_RETRYABLE
        done = exc.errcode == SHIPPING_ALREADY_DONE  # 已完成发货：视为成功
    except (UpstreamError, CircuitOpenError, TransientError) as exc:
        error, retryable = type(exc).__name__, True
    current = _reload(db, ShippingUpload, record.id)
    attempts = current.attempts + 1
    if done:
        _upd(db, ShippingUpload, ShippingUpload.id == record.id, ShippingUpload.status == "pending", status="uploaded",
             attempts=attempts, uploaded_at=now, next_attempt_at=None, last_errcode=None, last_error=None)
    elif retryable and attempts < cfg.shipping_max_attempts:
        _upd(db, ShippingUpload, ShippingUpload.id == record.id, ShippingUpload.status == "pending", attempts=attempts,
             last_errcode=errcode, last_error=(error or "")[:200],
             next_attempt_at=now + timedelta(seconds=rules.retry_delay(attempts)))
    else:
        _upd(db, ShippingUpload, ShippingUpload.id == record.id, ShippingUpload.status == "pending", status="failed",
             attempts=attempts, last_errcode=errcode, last_error=(error or "")[:200], next_attempt_at=None)
        log.error("wechat_pay shipping upload failed: %s %s", record.out_trade_no, errcode)
    db.commit()
    return shipping_view(_reload(db, ShippingUpload, record.id))


def retry_shipping_upload(db: Session, upload_id: int, staff_id: int, now: datetime | None = None) -> dict[str, Any]:
    """后台人工重试失败的发货上传（重新进入 pending）。"""
    now = _now(now)
    record = db.get(ShippingUpload, upload_id)
    if record is None:
        raise AppError(ErrorCode.NOT_FOUND, "shipping upload not found")
    changed = _upd(db, ShippingUpload, ShippingUpload.id == upload_id, ShippingUpload.status == "failed",
                   status="pending", attempts=0, next_attempt_at=now)
    if changed != 1:
        raise AppError(ErrorCode.CONFLICT, "only failed uploads can be retried", data={"status": record.status})
    write_audit(db, "staff", staff_id, "payment.shipping.retry", "shipping_upload", upload_id)
    db.commit()
    return upload_shipping(db, _reload(db, ShippingUpload, upload_id), now)


# ---------------------------------------------------------------- 账单与对账


def _bill_ready_at(bill_date: date) -> datetime:
    cfg = get_config()
    return datetime.combine(bill_date + timedelta(days=1), dtime(cfg.bill_ready_hour), tzinfo=ZoneInfo(cfg.tz))


def batch_view(b: ReconBatch) -> dict[str, Any]:
    return {"id": b.id, "bill_date": b.bill_date.isoformat(), "status": b.status, "attempts": b.attempts,
            "remote_payments": b.remote_payments, "remote_refunds": b.remote_refunds, "local_payments": b.local_payments,
            "local_refunds": b.local_refunds, "diff_count": b.diff_count, "last_error": b.last_error,
            "next_attempt_at": aware(b.next_attempt_at).isoformat() if b.next_attempt_at else None,
            "finished_at": aware(b.finished_at).isoformat() if b.finished_at else None}


def reconcile_bill(db: Session, bill_date: date, now: datetime | None = None, force: bool = False) -> dict[str, Any]:
    """下载交易账单（ALL，GZIP）→ 校验 SHA1 → 解析 → 与本地支付 / 退款逐笔核对 → 差异入表。可重跑。"""
    cfg = get_config()
    client = get_client()
    now = _now(now)
    today = now.astimezone(ZoneInfo(cfg.tz)).date()
    if bill_date >= today or bill_date < today - timedelta(days=cfg.bill_max_days):
        raise AppError(ErrorCode.BUSINESS_RULE, "bill date must be within the last 3 months and before today")
    if now < _bill_ready_at(bill_date):
        raise AppError(ErrorCode.BUSINESS_RULE, f"bill is available after {cfg.bill_ready_hour}:00 next day",
                       data={"reason": "bill_not_ready"})
    batch = db.scalar(select(ReconBatch).where(ReconBatch.bill_date == bill_date))
    if batch is None:
        db.add(ReconBatch(bill_date=bill_date, status="pending"))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
        batch = db.scalar(select(ReconBatch).where(ReconBatch.bill_date == bill_date))
    if batch.status in ("done", "no_statement") and not force:
        return batch_view(batch)
    db.commit()

    def retry_later(status: str, error: str) -> dict[str, Any]:
        _upd(db, ReconBatch, ReconBatch.id == batch.id, status=status, attempts=ReconBatch.attempts + 1,
             last_error=error[:200], next_attempt_at=now + timedelta(minutes=cfg.bill_retry_minutes))
        db.commit()
        return batch_view(_reload(db, ReconBatch, batch.id))

    lines: list[rules.BillLine] = []
    final_status = "done"
    try:
        info = _call("bill", lambda: client.apply_trade_bill(bill_date.isoformat(), "ALL", "GZIP"),
                     timeout=cfg.timeout_bill_s, attempts=2)
        raw = _call("bill_download", lambda: client.download_bill(info["download_url"]), timeout=cfg.timeout_bill_s,
                    attempts=2)
    except WechatPayApiError as exc:
        if exc.code == "NO_STATEMENT_EXIST":
            final_status = "no_statement"
        elif exc.code == "STATEMENT_CREATING":
            return retry_later("creating", exc.code)
        else:
            return retry_later("pending" if exc.retryable else "failed", exc.code)
    except CircuitOpenError as exc:
        return retry_later("pending", exc.message)
    else:
        content = gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw
        expected = str(info.get("hash_value", "")).lower()
        # 官方 Java 示例对解压后的文件计算 SHA1；文档未写明压缩时针对哪一份（未核实），两者任一相符即通过
        if expected not in (apiv3.sha1_hex(content), apiv3.sha1_hex(raw)):
            log.error("wechat_pay bill hash mismatch for %s", bill_date)
            return retry_later("failed", "hash_mismatch")
        details, _summary = rules.parse_trade_bill(content.decode("utf-8"))
        lines = rules.bill_lines(details)
        _upd(db, ReconBatch, ReconBatch.id == batch.id, hash_value=expected[:64])
    return _reconcile(db, batch, bill_date, lines, final_status, now)


def _reconcile(db: Session, batch: ReconBatch, bill_date: date, lines: list[rules.BillLine], final_status: str,
               now: datetime) -> dict[str, Any]:
    cfg = get_config()
    start, end = rules.local_day_bounds(bill_date, cfg.tz)
    remote_pay = {line.out_trade_no: line for line in lines if line.kind == "payment"}
    remote_ref = {line.out_refund_no: line for line in lines if line.kind == "refund" and line.out_refund_no}
    local_pay = {p.out_trade_no: p for p in db.scalars(select(PaymentRecord).where(
        PaymentRecord.paid_at >= start, PaymentRecord.paid_at < end))}
    others = [no for no in remote_pay if no not in local_pay]
    by_no: dict[str, PaymentRecord] = {}
    for i in range(0, len(others), 500):
        by_no.update({p.out_trade_no: p for p in db.scalars(select(PaymentRecord).where(
            PaymentRecord.out_trade_no.in_(others[i:i + 500])))})
    local_ref = {r.out_refund_no: r for r in db.scalars(select(RefundRecord).where(
        RefundRecord.succeeded_at >= start, RefundRecord.succeeded_at < end))}
    ref_others = [no for no in remote_ref if no not in local_ref]
    ref_by_no: dict[str, RefundRecord] = {}
    for i in range(0, len(ref_others), 500):
        ref_by_no.update({r.out_refund_no: r for r in db.scalars(select(RefundRecord).where(
            RefundRecord.out_refund_no.in_(ref_others[i:i + 500])))})

    diffs: list[dict[str, Any]] = []

    def add(diff_type: str, ref_type: str, ref_no: str, out_trade_no: str, local_amount, remote_amount, local_status,
            remote_status) -> None:
        diffs.append(dict(diff_type=diff_type, ref_type=ref_type, ref_no=ref_no[:64], out_trade_no=(out_trade_no or "")[:32],
                          local_amount_cents=local_amount, remote_amount_cents=remote_amount,
                          local_status=local_status, remote_status=(remote_status or "")[:16] or None))

    wake: list[int] = []
    for no, line in remote_pay.items():
        p = local_pay.get(no) or by_no.get(no)
        if p is None:
            add("missing_local", "payment", no, no, None, line.amount_cents, None, line.status)
            continue
        if p.status != PAY_PAID:
            add("status_mismatch", "payment", no, no, p.amount_cents, line.amount_cents, p.status, line.status)
            if p.status in PAY_OPEN or p.status == PAY_CLOSED:
                wake.append(p.id)  # 微信成功、本地未成功：让补偿任务立即查单落库
        if line.amount_cents != p.amount_cents:
            add("amount_mismatch", "payment", no, no, p.amount_cents, line.amount_cents, p.status, line.status)
    for no, p in local_pay.items():
        if p.status == PAY_PAID and no not in remote_pay:
            add("missing_remote", "payment", no, no, p.amount_cents, None, p.status, None)
    payments_of_refunds = {p.id: p.out_trade_no for p in list(local_pay.values()) + list(by_no.values())}
    for no, line in remote_ref.items():
        r = local_ref.get(no) or ref_by_no.get(no)
        if r is None:
            add("missing_local", "refund", no, line.out_trade_no, None, line.amount_cents, None, line.status)
            continue
        remote_ok, remote_fail = line.status == "SUCCESS", line.status in ("FAIL", "CHANGE")
        if (remote_ok and r.status != REFUND_SUCCEEDED) or (remote_fail and r.status == REFUND_SUCCEEDED):
            add("status_mismatch", "refund", no, line.out_trade_no, r.amount_cents, line.amount_cents, r.status, line.status)
        if line.amount_cents != r.amount_cents:
            add("amount_mismatch", "refund", no, line.out_trade_no, r.amount_cents, line.amount_cents, r.status, line.status)
    for no, r in local_ref.items():
        if r.status == REFUND_SUCCEEDED and no not in remote_ref:
            out_trade_no = payments_of_refunds.get(r.payment_id) or db.scalar(
                select(PaymentRecord.out_trade_no).where(PaymentRecord.id == r.payment_id)) or ""
            add("missing_remote", "refund", no, out_trade_no, r.amount_cents, None, r.status, None)

    resolved = {(d.ref_type, d.ref_no, d.diff_type) for d in db.scalars(select(ReconDiff).where(
        ReconDiff.batch_id == batch.id, ReconDiff.resolved.is_(True)))}
    db.execute(delete(ReconDiff).where(ReconDiff.batch_id == batch.id, ReconDiff.resolved.is_(False)))
    for item in diffs:
        if (item["ref_type"], item["ref_no"], item["diff_type"]) not in resolved:
            db.add(ReconDiff(batch_id=batch.id, bill_date=bill_date, **item))
    if wake:
        _upd(db, PaymentRecord, PaymentRecord.id.in_(wake), next_query_at=now)
    _upd(db, ReconBatch, ReconBatch.id == batch.id, status=final_status, attempts=ReconBatch.attempts + 1,
         remote_payments=len(remote_pay), remote_refunds=len(remote_ref), local_payments=len(local_pay),
         local_refunds=len(local_ref), diff_count=len(diffs), last_error=None, finished_at=now, next_attempt_at=None)
    db.commit()
    return batch_view(_reload(db, ReconBatch, batch.id))


def resolve_diff(db: Session, diff_id: int, staff_id: int, note: str) -> dict[str, Any]:
    d = db.get(ReconDiff, diff_id)
    if d is None:
        raise AppError(ErrorCode.NOT_FOUND, "diff not found")
    changed = _upd(db, ReconDiff, ReconDiff.id == diff_id, ReconDiff.resolved.is_(False), resolved=True,
                   resolved_by=f"staff:{staff_id}", resolved_at=utcnow(), note=note[:200])
    if changed != 1:
        raise AppError(ErrorCode.CONFLICT, "diff already resolved")
    write_audit(db, "staff", staff_id, "payment.reconcile.resolve", "recon_diff", diff_id, before={"resolved": False},
                after={"resolved": True}, reason=note[:200])
    db.commit()
    return diff_view(_reload(db, ReconDiff, diff_id))


# ---------------------------------------------------------------- 视图与列表声明（后台）


def payment_view(p: PaymentRecord) -> dict[str, Any]:
    return {
        "id": p.id, "business_type": p.business_type, "business_id": p.business_id, "business_no": p.business_no,
        "user_id": p.user_id, "store_id": p.store_id, "goods_kind": p.goods_kind, "out_trade_no": p.out_trade_no,
        "transaction_id": p.channel_txn_id, "amount_cents": p.amount_cents, "currency": p.currency, "status": p.status,
        "trade_state": p.trade_state, "description": p.description, "payer_openid": rules.mask_openid(p.payer_openid),
        "refunded_cents": p.refunded_cents, "refund_succeeded_cents": p.refund_succeeded_cents,
        "refund_count": p.refund_count, "needs_manual": p.needs_manual, "manual_reason": p.manual_reason,
        "close_reason": p.close_reason, "fail_code": p.fail_code,
        "created_at": aware(p.created_at).isoformat() if p.created_at else None,
        "expires_at": aware(p.expires_at).isoformat() if p.expires_at else None,
        "paid_at": aware(p.paid_at).isoformat() if p.paid_at else None,
        "closed_at": aware(p.closed_at).isoformat() if p.closed_at else None,
    }


def refund_view(r: RefundRecord, out_trade_no: str | None = None) -> dict[str, Any]:
    return {
        "id": r.id, "payment_id": r.payment_id, "out_trade_no": out_trade_no, "out_refund_no": r.out_refund_no,
        "refund_id": r.channel_refund_id, "business_ref": r.business_ref, "amount_cents": r.amount_cents,
        "reason": r.reason, "status": r.status, "channel_status": r.channel_status, "refund_channel": r.refund_channel,
        "user_received_account": r.user_received_account, "requested_by": r.requested_by, "attempts": r.attempts,
        "last_error_code": r.last_error_code, "needs_manual": r.needs_manual,
        "created_at": aware(r.created_at).isoformat() if r.created_at else None,
        "submitted_at": aware(r.submitted_at).isoformat() if r.submitted_at else None,
        "succeeded_at": aware(r.succeeded_at).isoformat() if r.succeeded_at else None,
    }


def diff_view(d: ReconDiff) -> dict[str, Any]:
    return {"id": d.id, "batch_id": d.batch_id, "bill_date": d.bill_date.isoformat(), "diff_type": d.diff_type,
            "ref_type": d.ref_type, "ref_no": d.ref_no, "out_trade_no": d.out_trade_no,
            "local_amount_cents": d.local_amount_cents, "remote_amount_cents": d.remote_amount_cents,
            "local_status": d.local_status, "remote_status": d.remote_status, "resolved": d.resolved,
            "resolved_by": d.resolved_by, "note": d.note}


def shipping_view(s: ShippingUpload) -> dict[str, Any]:
    return {"id": s.id, "payment_id": s.payment_id, "out_trade_no": s.out_trade_no, "logistics_type": s.logistics_type,
            "status": s.status, "attempts": s.attempts, "last_errcode": s.last_errcode, "last_error": s.last_error,
            "uploaded_at": aware(s.uploaded_at).isoformat() if s.uploaded_at else None}


PAYMENT_LIST = ListSpec(
    fields={
        "status": Field(PaymentRecord.status, ops=("eq", "in"), choices=frozenset(rules.PAY_STATUSES)),
        "business_type": Field(PaymentRecord.business_type, ops=("eq", "in")),
        "business_id": Field(PaymentRecord.business_id, kind="int"),
        "goods_kind": Field(PaymentRecord.goods_kind, ops=("eq", "in"), choices=frozenset(rules.GOODS_KINDS)),
        "store_id": Field(PaymentRecord.store_id, kind="int", ops=("eq", "in")),
        "user_id": Field(PaymentRecord.user_id, kind="int"),
        "needs_manual": Field(PaymentRecord.needs_manual, kind="bool"),
        "amount_cents": Field(PaymentRecord.amount_cents, kind="int", ops=("range",), sortable=True),
        "created_at": Field(PaymentRecord.created_at, kind="datetime", ops=("range",), sortable=True),
        "paid_at": Field(PaymentRecord.paid_at, kind="datetime", ops=("range", "null"), sortable=True),
    },
    id_column=PaymentRecord.id,
    keyword_columns=(PaymentRecord.out_trade_no, PaymentRecord.channel_txn_id, PaymentRecord.business_no),
)

REFUND_LIST = ListSpec(
    fields={
        "status": Field(RefundRecord.status, ops=("eq", "in"), choices=frozenset(rules.REFUND_STATUSES)),
        "payment_id": Field(RefundRecord.payment_id, kind="int"),
        "business_type": Field(PaymentRecord.business_type, ops=("eq", "in")),
        "store_id": Field(PaymentRecord.store_id, kind="int", ops=("eq", "in")),
        "needs_manual": Field(RefundRecord.needs_manual, kind="bool"),
        "amount_cents": Field(RefundRecord.amount_cents, kind="int", ops=("range",), sortable=True),
        "created_at": Field(RefundRecord.created_at, kind="datetime", ops=("range",), sortable=True),
        "succeeded_at": Field(RefundRecord.succeeded_at, kind="datetime", ops=("range", "null"), sortable=True),
    },
    id_column=RefundRecord.id,
    keyword_columns=(RefundRecord.out_refund_no, RefundRecord.channel_refund_id, PaymentRecord.out_trade_no,
                     PaymentRecord.channel_txn_id),
)

DIFF_LIST = ListSpec(
    fields={
        "diff_type": Field(ReconDiff.diff_type, ops=("eq", "in"), choices=frozenset(
            {"missing_local", "missing_remote", "amount_mismatch", "status_mismatch"})),
        "ref_type": Field(ReconDiff.ref_type, choices=frozenset({"payment", "refund"})),
        "resolved": Field(ReconDiff.resolved, kind="bool"),
        "batch_id": Field(ReconDiff.batch_id, kind="int"),
        "bill_date": Field(ReconDiff.bill_date, kind="date", ops=("range",), sortable=True),
        "created_at": Field(ReconDiff.created_at, kind="datetime", ops=("range",), sortable=True),
    },
    id_column=ReconDiff.id,
    keyword_columns=(ReconDiff.ref_no, ReconDiff.out_trade_no),
)

BATCH_LIST = ListSpec(
    fields={
        "status": Field(ReconBatch.status, ops=("eq", "in"), choices=frozenset(
            {"pending", "creating", "done", "no_statement", "failed"})),
        "bill_date": Field(ReconBatch.bill_date, kind="date", ops=("range",), sortable=True),
        "created_at": Field(ReconBatch.created_at, kind="datetime", ops=("range",), sortable=True),
    },
    id_column=ReconBatch.id,
    default_sort="-bill_date",
)

SHIPPING_LIST = ListSpec(
    fields={
        "status": Field(ShippingUpload.status, ops=("eq", "in"), choices=frozenset({"pending", "uploaded", "failed"})),
        "payment_id": Field(ShippingUpload.payment_id, kind="int"),
        "created_at": Field(ShippingUpload.created_at, kind="datetime", ops=("range",), sortable=True),
    },
    id_column=ShippingUpload.id,
    keyword_columns=(ShippingUpload.out_trade_no,),
)


def payments_query(store_ids: list[int] | None = None):
    stmt = select(PaymentRecord)
    if store_ids is not None:
        stmt = stmt.where(PaymentRecord.store_id.in_(store_ids))
    return stmt


def refunds_query(store_ids: list[int] | None = None):
    stmt = select(RefundRecord, PaymentRecord.out_trade_no).join(PaymentRecord, PaymentRecord.id == RefundRecord.payment_id)
    if store_ids is not None:
        stmt = stmt.where(PaymentRecord.store_id.in_(store_ids))
    return stmt


def admin_payment_detail(db: Session, payment_id: int, store_ids: list[int] | None = None) -> dict[str, Any]:
    p = db.get(PaymentRecord, payment_id)
    if p is None or (store_ids is not None and p.store_id not in store_ids):
        raise AppError(ErrorCode.NOT_FOUND, "payment not found")
    refunds = db.scalars(select(RefundRecord).where(RefundRecord.payment_id == p.id).order_by(RefundRecord.id)).all()
    shipping = db.scalar(select(ShippingUpload).where(ShippingUpload.payment_id == p.id))
    logs = db.scalars(select(PayNotifyLog).where(PayNotifyLog.out_trade_no == p.out_trade_no)
                      .order_by(PayNotifyLog.id.desc()).limit(20)).all()
    return {**payment_view(p), "refunds": [refund_view(r, p.out_trade_no) for r in refunds],
            "shipping": shipping_view(shipping) if shipping else None,
            "notify_logs": [{"kind": x.kind, "result": x.result, "reason": x.reason, "source": x.source,
                             "event_type": x.event_type, "received_at": aware(x.received_at).isoformat()} for x in logs]}


def admin_refund_detail(db: Session, refund_id: int, store_ids: list[int] | None = None) -> dict[str, Any]:
    r = db.get(RefundRecord, refund_id)
    p = db.get(PaymentRecord, r.payment_id) if r else None
    if r is None or p is None or (store_ids is not None and p.store_id not in store_ids):
        raise AppError(ErrorCode.NOT_FOUND, "refund not found")
    return {**refund_view(r, p.out_trade_no), "payment": payment_view(p)}
