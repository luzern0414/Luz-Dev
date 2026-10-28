"""order 用例：试算、下单、支付（微信 / 余额）、支付成功、取消、超时关单、履约、确认收货、自动完成、退款额度、查询。

事务约定：
- 用例函数不 commit（由接口层 run_idempotent、任务、事件 worker 提交），例外是需要先调用支付渠道的用例
  （`pay`、`cancel`、`admin_close`、`close_expired`）：渠道调用经 09-wechat-pay 完成并会提交会话，
  所以先“查单 → 关单”（会提交），再在新的事务里改订单（SPEC §6.10 渠道调用不在业务事务内）。
- 每次状态变化：一条条件 UPDATE（StateMachine.advance）+ 状态日志 + 发件箱事件，同一事务。
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import case, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from luzback import AppError, ErrorCode, Field, ListSpec, UpstreamError, enqueue_event, run_idempotent, utcnow, write_audit
from luzback.security import mask_address, mask_name, mask_phone

from . import ports
from .config import get_config
from .kinds import ResolvedLine, get_line_kind, snapshot_json
from .models import Order, OrderItem, OrderNote, OrderQuote, OrderStatusLog, PurchaseCounter
from .rules import (
    ACTIVE_AFTER_PAY, CANCEL_ADMIN, CANCEL_TIMEOUT, CANCEL_USER, CANCELLED, CLOSED, COMPLETED, FULFILLED, FULFILLING,
    GOODS_KINDS, GOODS_PHYSICAL, ORDER_FLOW, PAID, PAY_BALANCE, PAY_FREE, PAY_METHODS, PAY_WECHAT, PENDING, REFUND_DONE,
    REFUND_NONE, REFUND_STATUSES, REFUNDED, REFUNDING, PARTIALLY_REFUNDED, STATUSES, TABS, aware, max_refund_for_qty,
    new_order_no, order_goods_kind, request_hash,
)

log = logging.getLogger("luz.order")

PERMISSIONS = {
    "order:read": "查看订单列表与详情",
    "order:export": "导出订单",
    "order:remark": "添加后台备注",
    "order:close": "后台关闭未支付订单",
    "order:fulfil": "发货 / 标记履约完成",
}


def _now(now: datetime | None) -> datetime:
    return aware(now) or utcnow()


def _rule(message: str, reason: str, **data: Any) -> AppError:
    return AppError(ErrorCode.BUSINESS_RULE, message, data={"reason": reason, **data})


# ================================================================ 解析与计价


def _normalize_items(items: Any) -> list[dict[str, Any]]:
    """合并同一商品的多行；数量与行数上限；不接受客户端价格字段。"""
    cfg = get_config()
    if not isinstance(items, list) or not items:
        raise AppError(ErrorCode.BAD_REQUEST, "items required")
    merged: dict[tuple, dict[str, Any]] = {}
    for raw in items:
        kind = str(raw.get("kind") or "goods")
        qty = int(raw.get("qty") or 0)
        if not 1 <= qty <= cfg.max_qty_per_line:
            raise AppError(ErrorCode.BAD_REQUEST, f"qty must be 1..{cfg.max_qty_per_line}")
        key = (kind, int(raw.get("sku_id") or 0), int(raw.get("product_id") or 0))
        if key in merged:
            merged[key]["qty"] += qty
            if merged[key]["qty"] > cfg.max_qty_per_line:
                raise AppError(ErrorCode.BAD_REQUEST, f"qty must be 1..{cfg.max_qty_per_line}")
        else:
            merged[key] = {"kind": kind, "sku_id": key[1] or None, "product_id": key[2] or None, "qty": qty}
    if len(merged) > cfg.max_lines:
        raise AppError(ErrorCode.BAD_REQUEST, f"at most {cfg.max_lines} lines")
    return [merged[k] for k in sorted(merged, key=lambda k: (k[0], k[1], k[2]))]


def _resolve(db: Session, user_id: int, store_id: int | None, items: list[dict[str, Any]], now: datetime) -> list[ResolvedLine]:
    lines = []
    for item in items:
        line = get_line_kind(item["kind"]).resolve(db, user_id, store_id, item, now)
        if line.goods_kind not in GOODS_KINDS or line.unit_price_cents < 0:
            raise AppError(ErrorCode.CONFLICT, "invalid product data", data={"product_id": line.product_id})
        lines.append(line)
    return lines


@dataclass
class Priced:
    lines: list[ResolvedLine]
    campaign: list[int]
    coupon: list[int]
    coupons: list[dict[str, Any]] = field(default_factory=list)
    promotions_applied: list[dict[str, Any]] = field(default_factory=list)
    promotions_rejected: list[dict[str, Any]] = field(default_factory=list)
    promos: list[Any] = field(default_factory=list)
    coupon_ids: list[int] = field(default_factory=list)

    @property
    def paid(self) -> list[int]:
        return [ln.gross_cents - c - k for ln, c, k in zip(self.lines, self.campaign, self.coupon)]

    @property
    def items_cents(self) -> int:
        return sum(ln.gross_cents for ln in self.lines)

    @property
    def campaign_cents(self) -> int:
        return sum(self.campaign)

    @property
    def coupon_cents(self) -> int:
        return sum(self.coupon)

    @property
    def total_cents(self) -> int:
        return sum(self.paid)

    def totals(self) -> dict[str, int]:
        return {"items_cents": self.items_cents, "campaign_discount_cents": self.campaign_cents,
                "coupon_cents": self.coupon_cents, "total_cents": self.total_cents}

    def fingerprint(self) -> dict[str, Any]:
        """报价比对口径：总额、每行单价 / 数量 / 实付、券。"""
        return {"total_cents": self.total_cents, "coupon_ids": sorted(self.coupon_ids),
                "lines": [[ln.kind, ln.sku_id, ln.product_id, ln.unit_price_cents, ln.qty, p]
                          for ln, p in zip(self.lines, self.paid)]}


def _price(db: Session, user_id: int, store_id: int | None, lines: list[ResolvedLine], coupon_ids: list[int],
           now: datetime, order_id: int | None = None) -> Priced:
    """服务端计价：17-campaign 活动（promotions 提供者）→ 16-coupon 券（在活动后金额上计算）→ 最大余数法按行分摊。"""
    promos = ports.promotions(db, user_id, store_id, lines, now)
    cp = ports.coupon_port()
    if cp.enabled:
        quote, line_coupon, coupons = cp.price(db, user_id, coupon_ids, lines, promos, now, order_id)
    else:
        if coupon_ids:
            raise _rule("coupons are not enabled", "coupon_disabled")
        import pricing
        quote = pricing.quote([pricing.Line(sku_id=int(ln.sku_id or ln.product_id), qty=ln.qty,
                                            unit_price_cents=ln.unit_price_cents,
                                            category="" if ln.category_id is None else str(ln.category_id))
                               for ln in lines], promos)
        line_coupon, coupons = [0] * len(lines), []
    campaign = [quote.line_discount_cents[i] - line_coupon[i] for i in range(len(lines))]
    coupon_keys = {f"coupon:{c['user_coupon_id']}" for c in coupons}
    applied = [{"promotion_id": d["promotion_id"], "amount_cents": d["amount_cents"]}
               for d in quote.discounts if d["promotion_id"] not in coupon_keys]
    rejected = [r for r in quote.rejected if r["promotion_id"] not in coupon_keys]
    return Priced(lines=lines, campaign=campaign, coupon=list(line_coupon), coupons=coupons, promotions_applied=applied,
                  promotions_rejected=rejected, promos=promos, coupon_ids=sorted({int(i) for i in coupon_ids}))


def _line_view(ln: ResolvedLine, campaign: int, coupon: int, paid: int) -> dict[str, Any]:
    return {"kind": ln.kind, "product_id": ln.product_id, "sku_id": ln.sku_id, "name": ln.name, "spec": ln.spec,
            "image": ln.image, "unit_price_cents": ln.unit_price_cents, "qty": ln.qty, "gross_cents": ln.gross_cents,
            "campaign_alloc_cents": campaign, "coupon_alloc_cents": coupon, "paid_cents": paid,
            "goods_kind": ln.goods_kind, "returnable": ln.returnable}


def _purchased(db: Session, user_id: int, keys: list[str]) -> dict[str, int]:
    if not keys:
        return {}
    rows = db.execute(select(PurchaseCounter.product_key, PurchaseCounter.qty)
                      .where(PurchaseCounter.user_id == user_id, PurchaseCounter.product_key.in_(keys))).all()
    return {k: q for k, q in rows}


def preview(db: Session, user_id: int, body: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """试算（电商法第五十条：提交前可更正）：服务端价、活动、券（可用 / 不可用原因）、库存与限购提示；返回 quote_token。

    本用例写一条报价记录，调用方 commit。
    """
    now = _now(now)
    cfg = get_config()
    store_id = body.get("store_id")
    items = _normalize_items(body.get("items"))
    coupon_ids = [int(i) for i in (body.get("coupon_ids") or [])]
    lines = _resolve(db, user_id, store_id, items, now)
    issues: list[dict[str, Any]] = []
    for ln in lines:
        if ln.stock_qty is not None and ln.stock_qty < ln.qty:
            issues.append({"reason": "out_of_stock", "product_id": ln.product_id, "sku_id": ln.sku_id,
                           "available": max(0, ln.stock_qty)})
    bought = _purchased(db, user_id, [ln.product_key for ln in lines if ln.limit_per_user])
    for ln in lines:
        if ln.limit_per_user and bought.get(ln.product_key, 0) + ln.qty > ln.limit_per_user:
            issues.append({"reason": "purchase_limit", "product_id": ln.product_id, "limit": ln.limit_per_user,
                           "bought": bought.get(ln.product_key, 0)})
    coupon_error = None
    try:
        priced = _price(db, user_id, store_id, lines, coupon_ids, now)
    except AppError as exc:
        if not coupon_ids or exc.code not in (ErrorCode.BUSINESS_RULE, ErrorCode.NOT_FOUND):
            raise
        coupon_error = {"code": int(exc.code), "message": exc.message, **(exc.data or {})}
        priced = _price(db, user_id, store_id, lines, [], now)
    coupons_view: dict[str, Any] = {"applied": priced.coupons, "usable": [], "unusable": [], "best": None}
    cp = ports.coupon_port()
    if cp.enabled:
        avail = cp.available(db, user_id, lines, priced.promos, now)
        coupons_view.update(usable=avail["usable"], unusable=avail["unusable"], best=avail["best"])
    request = {"store_id": store_id, "items": items, "coupon_ids": priced.coupon_ids}
    result = {"fingerprint": priced.fingerprint(), "totals": priced.totals()}
    token = uuid.uuid4().hex
    expires_at = now + timedelta(seconds=cfg.quote_ttl_seconds)
    db.add(OrderQuote(token=token, user_id=user_id, store_id=store_id, request_json=json.dumps(request),
                      request_hash=request_hash(request), result_json=json.dumps(result), total_cents=priced.total_cents,
                      expires_at=expires_at, created_at=now))
    db.flush()
    return {
        "quote_token": token, "expires_at": expires_at.isoformat(), "store_id": store_id,
        "lines": [_line_view(ln, c, k, p) for ln, c, k, p in zip(lines, priced.campaign, priced.coupon, priced.paid)],
        **priced.totals(), "promotions": priced.promotions_applied, "promotions_rejected": priced.promotions_rejected,
        "coupons": coupons_view, "coupon_error": coupon_error, "issues": issues,
        "goods_kind": order_goods_kind([ln.goods_kind for ln in lines]),
        "terms": {"required": cfg.terms_required, "version": cfg.terms_version},
        "submittable": not issues,
    }


# ================================================================ 下单


def _log(db: Session, order_id: int, source: str | None, target: str, actor_type: str, actor_id: Any = "",
         reason: str | None = None, now: datetime | None = None) -> None:
    db.add(OrderStatusLog(order_id=order_id, from_status=source, to_status=target, actor_type=actor_type,
                          actor_id=str(actor_id or ""), reason=(reason or None) and reason[:200], created_at=_now(now)))


def _event(db: Session, topic: str, order: Order, **extra: Any) -> None:
    payload = {"order_id": order.id, "order_no": order.order_no, "user_id": order.user_id, "store_id": order.store_id,
               "status": order.status, "total_cents": order.total_cents, **extra}
    enqueue_event(db, topic, payload, "order", order.id)


def _bump_limit(db: Session, user_id: int, key: str, qty: int, limit: int) -> None:
    """限购：条件自增 qty + n <= limit；首次插入计数行（唯一约束裁决并发首行）。"""
    for _ in range(2):
        changed = db.execute(update(PurchaseCounter).where(
            PurchaseCounter.user_id == user_id, PurchaseCounter.product_key == key,
            PurchaseCounter.qty + qty <= limit).values(qty=PurchaseCounter.qty + qty)).rowcount
        if changed == 1:
            return
        exists = db.scalar(select(PurchaseCounter.id).where(PurchaseCounter.user_id == user_id,
                                                            PurchaseCounter.product_key == key))
        if exists:
            break
        if qty > limit:
            break
        try:
            with db.begin_nested():
                db.add(PurchaseCounter(user_id=user_id, product_key=key, qty=qty))
            return
        except IntegrityError:
            continue
    raise AppError(ErrorCode.LIMIT_REACHED, "purchase limit reached", data={"reason": "purchase_limit",
                                                                           "product_key": key, "limit": limit})


def _unbump_limits(db: Session, order: Order, items: list[OrderItem]) -> None:
    for it in items:
        snap = json.loads(it.snapshot_json or "{}")
        if snap.get("limit_per_user"):
            db.execute(update(PurchaseCounter).where(
                PurchaseCounter.user_id == order.user_id, PurchaseCounter.product_key == f"{it.kind}:{it.product_id}",
                PurchaseCounter.qty >= it.qty).values(qty=PurchaseCounter.qty - it.qty))


def _insert_order(db: Session, values: dict[str, Any], now: datetime) -> Order:
    cfg = get_config()
    for _ in range(3):
        order = Order(order_no=new_order_no(now, cfg.timezone, cfg.order_no_prefix), **values)
        try:
            with db.begin_nested():
                db.add(order)
            return order
        except IntegrityError:  # 单号碰撞（唯一约束兜底）：换号重试
            continue
    raise AppError(ErrorCode.DUPLICATE_REQUEST, "order number collision, retry", retry_after=1)


def _create(db: Session, user_id: int, request: dict[str, Any], now: datetime, *, quote: OrderQuote | None = None,
            terms_version: str | None = None, remark: str = "", address_id: int | None = None,
            expected_total_cents: int | None = None) -> dict[str, Any]:
    cfg = get_config()
    store_id = request.get("store_id")
    items = _normalize_items(request.get("items"))
    lines = _resolve(db, user_id, store_id, items, now)
    priced = _price(db, user_id, store_id, lines, [int(i) for i in request.get("coupon_ids") or []], now)
    if quote is not None:
        expected = json.loads(quote.result_json)["fingerprint"]
        if priced.fingerprint() != expected:
            raise AppError(ErrorCode.QUOTE_EXPIRED, "price changed, please confirm again",
                           data={"reason": "price_changed", "quoted_total_cents": quote.total_cents,
                                 "total_cents": priced.total_cents})
        used = db.execute(update(OrderQuote).where(OrderQuote.id == quote.id, OrderQuote.used_at.is_(None))
                          .values(used_at=now)).rowcount
        if used != 1:
            raise AppError(ErrorCode.CONFLICT, "quote already used", data={"reason": "quote_used"})
    if expected_total_cents is not None and expected_total_cents != priced.total_cents:
        raise AppError(ErrorCode.QUOTE_EXPIRED, "price changed, please confirm again",
                       data={"reason": "price_changed", "total_cents": priced.total_cents})
    phone_masked, phone_hash = _buyer(db, user_id)
    address_json = None
    if address_id:
        address_json = ports.encrypt_json(json.dumps(ports.address_snapshot(db, user_id, address_id), ensure_ascii=False,
                                                     default=str))
    kinds = sorted({ln.kind for ln in lines})
    order = _insert_order(db, dict(
        user_id=user_id, store_id=store_id, status=PENDING, refund_status=REFUND_NONE,
        kind=kinds[0] if len(kinds) == 1 else "mixed", goods_kind=order_goods_kind([ln.goods_kind for ln in lines]),
        items_cents=priced.items_cents, campaign_discount_cents=priced.campaign_cents, coupon_cents=priced.coupon_cents,
        total_cents=priced.total_cents, coupon_ids_json=json.dumps(priced.coupon_ids),
        pay_deadline_at=now + timedelta(minutes=cfg.pay_timeout_minutes), remark=(remark or "")[:200],
        address_snapshot_json=address_json, buyer_phone_masked=phone_masked, buyer_phone_hash=phone_hash,
        terms_version=terms_version, terms_accepted_at=now if terms_version else None,
        quote_token=quote.token if quote else None, created_at=now, updated_at=now), now)
    if quote is not None:
        db.execute(update(OrderQuote).where(OrderQuote.id == quote.id).values(order_id=order.id))
    rows: list[OrderItem] = []
    for ln, campaign, coupon, paid in zip(lines, priced.campaign, priced.coupon, priced.paid):
        snap = {"snapshot": ln.snapshot, "category_id": ln.category_id, "limit_per_user": ln.limit_per_user}
        row = OrderItem(order_id=order.id, user_id=user_id, kind=ln.kind, goods_kind=ln.goods_kind,
                        product_id=ln.product_id, sku_id=ln.sku_id, name=ln.name[:128], spec=ln.spec[:128],
                        image=ln.image[:500], unit_price_cents=ln.unit_price_cents, qty=ln.qty,
                        gross_cents=ln.gross_cents, campaign_alloc_cents=campaign, coupon_alloc_cents=coupon,
                        paid_cents=paid, returnable=ln.returnable, snapshot_json=snapshot_json(snap))
        db.add(row)
        rows.append(row)
    db.flush()
    limits: dict[str, tuple[int, int]] = {}
    for ln in lines:
        if ln.limit_per_user:
            qty, _ = limits.get(ln.product_key, (0, ln.limit_per_user))
            limits[ln.product_key] = (qty + ln.qty, ln.limit_per_user)
    for key in sorted(limits):
        _bump_limit(db, user_id, key, *limits[key])
    for kind in kinds:
        get_line_kind(kind).reserve(db, order, [r for r in rows if r.kind == kind], now)
    if priced.coupon_ids:
        ports.coupon_port().lock(db, user_id, order.id, priced.coupon_ids, lines, priced.promos, now)
    _log(db, order.id, None, PENDING, "user", user_id, now=now)
    _event(db, "order.created", order, kind=order.kind, coupon_ids=priced.coupon_ids)
    if order.total_cents == 0:  # 0 元单不能走微信支付：提交即成交
        _settle_paid(db, order, method=PAY_FREE, amount_cents=0, paid_at=now, actor=("system", "free"))
    db.flush()
    return order_summary(order)


def _buyer(db: Session, user_id: int) -> tuple[str | None, str | None]:
    try:
        with db.begin_nested():
            return ports.buyer_contact(db, user_id)
    except Exception:  # noqa: BLE001 - 联系方式只是检索辅助，取不到不影响下单
        log.warning("order: buyer contact unavailable for user %s", user_id)
        return None, None


def submit(db: Session, user_id: int, body: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """提交订单（电商法第四十九条：提交成功即合同成立）。必须带有效 quote_token；服务端重新计价，变化 → 4223。不 commit。"""
    now = _now(now)
    token = str(body.get("quote_token") or "")
    quote = db.scalar(select(OrderQuote).where(OrderQuote.token == token)) if token else None
    if quote is None or quote.user_id != user_id:
        raise AppError(ErrorCode.QUOTE_EXPIRED, "quote not found, please preview again", data={"reason": "quote_missing"})
    if aware(quote.expires_at) <= now:
        raise AppError(ErrorCode.QUOTE_EXPIRED, "quote expired, please preview again", data={"reason": "quote_expired"})
    if quote.used_at is not None:
        raise AppError(ErrorCode.CONFLICT, "quote already used", data={"reason": "quote_used", "order_id": quote.order_id})
    terms_version = ports.check_terms(db, user_id, bool(body.get("terms_accepted")), body.get("terms_version"))
    return _create(db, user_id, json.loads(quote.request_json), now, quote=quote, terms_version=terms_version,
                   remark=str(body.get("remark") or ""), address_id=body.get("address_id"),
                   expected_total_cents=body.get("expected_total_cents"))


def order_summary(order: Order) -> dict[str, Any]:
    return {"order_id": order.id, "order_no": order.order_no, "status": order.status, "total_cents": order.total_cents,
            "pay_deadline_at": aware(order.pay_deadline_at).isoformat(), "goods_kind": order.goods_kind}


# ================================================================ 支付成功


def _items(db: Session, order_id: int) -> list[OrderItem]:
    return list(db.execute(select(OrderItem).where(OrderItem.order_id == order_id).order_by(OrderItem.id)).scalars())


def _advance(db: Session, order: Order, target: str, actor: tuple[str, Any], reason: str | None = None,
             now: datetime | None = None, extra_where: tuple = (), **values: Any) -> Order:
    source = order.status
    ORDER_FLOW.advance(db, Order, order.id, source, target, extra_where=extra_where, version=Order.version + 1,
                       updated_at=_now(now), **values)
    _log(db, order.id, source, target, actor[0], actor[1], reason, now)
    db.refresh(order)
    return order


def _settle_paid(db: Session, order: Order, *, method: str, amount_cents: int, paid_at: datetime,
                 actor: tuple[str, Any], payment_id: int | None = None, out_trade_no: str | None = None) -> Order:
    """pending_payment → paid：核对金额 → 条件 UPDATE → 核销券 → 确认库存 → 履约钩子 → order.paid。不 commit。"""
    if amount_cents != order.total_cents:
        raise AppError(ErrorCode.CONFLICT, "paid amount mismatch",
                       data={"reason": "amount_mismatch", "expected": order.total_cents, "paid": amount_cents})
    paid_at = aware(paid_at) or utcnow()
    _advance(db, order, PAID, actor, now=paid_at, paid_cents=amount_cents, paid_at=paid_at, pay_method=method,
             payment_id=payment_id, out_trade_no=out_trade_no)
    items = _items(db, order.id)
    if json.loads(order.coupon_ids_json or "[]"):
        ports.coupon_port().redeem(db, order.id, paid_at)
    for kind in sorted({it.kind for it in items}):
        get_line_kind(kind).confirm(db, order, [it for it in items if it.kind == kind], paid_at)
    for it in items:
        ref = get_line_kind(it.kind).fulfil(db, order, it, paid_at)
        if ref:
            db.execute(update(OrderItem).where(OrderItem.id == it.id, OrderItem.fulfil_status == "pending")
                       .values(fulfil_status="fulfilled", fulfil_ref=ref[:64], fulfilled_at=paid_at))
    _event(db, "order.paid", order, pay_method=method, payment_id=payment_id, paid_cents=amount_cents, kind=order.kind)
    pending = db.scalar(select(func.count()).select_from(OrderItem).where(OrderItem.order_id == order.id,
                                                                          OrderItem.fulfil_status == "pending"))
    if not pending:  # 全部行已即时交付（会员卡等）：直接进入已履约
        _advance(db, order, FULFILLED, ("system", "auto_fulfil"), now=paid_at, fulfilled_at=paid_at)
        _event(db, "order.fulfilled", order)
    return order


def on_payment_paid(db: Session, info: Any) -> None:
    """09-wechat-pay 支付成功钩子（同事务、保存点内）。抛错 → 支付模块记 needs_manual 并自动全额退款。"""
    order = db.get(Order, info.business_id)
    if order is None:
        raise AppError(ErrorCode.NOT_FOUND, "order not found")
    if info.user_id and order.user_id != info.user_id:
        raise AppError(ErrorCode.CONFLICT, "payer mismatch", data={"reason": "payer_mismatch"})
    if order.status != PENDING:
        if order.payment_id == info.payment_id and order.status in ACTIVE_AFTER_PAY:
            return
        raise AppError(ErrorCode.CONFLICT, "order is not awaiting payment", data={"status": order.status})
    _settle_paid(db, order, method=PAY_WECHAT, amount_cents=int(info.amount_cents),
                 paid_at=info.paid_at or utcnow(), actor=("payment", info.out_trade_no),
                 payment_id=info.payment_id, out_trade_no=info.out_trade_no)


def resolve_payable(db: Session, user_id: int, order_id: int) -> Any:
    """09-wechat-pay 支付解析器：金额 = 订单应付（服务端），截止时间 = pay_deadline_at（稳定，可复用 prepay_id）。"""
    import wechat_pay
    order = db.get(Order, order_id)
    if order is None or order.user_id != user_id:
        raise AppError(ErrorCode.NOT_FOUND, "order not found")
    if order.status != PENDING:
        raise AppError(ErrorCode.CONFLICT, "order is not awaiting payment", data={"status": order.status})
    first = db.scalar(select(OrderItem.name).where(OrderItem.order_id == order.id).order_by(OrderItem.id).limit(1))
    return wechat_pay.Payable(amount_cents=order.total_cents, description=f"{first or '订单'} {order.order_no}"[:127],
                              expires_at=aware(order.pay_deadline_at), store_id=order.store_id,
                              goods_kind=order.goods_kind, business_no=order.order_no)


def install_payment_hooks() -> bool:
    """向 09-wechat-pay 注册订单的支付解析器与支付成功钩子（导入本模块时自动执行；测试重置注册表后可再调用）。"""
    try:
        import wechat_pay
    except ImportError:
        return False
    wechat_pay.register_payable("order", resolve_payable)
    wechat_pay.register_on_paid("order", on_payment_paid)
    return True


# ================================================================ 去支付


def get_owned(db: Session, user_id: int, order_id: int) -> Order:
    order = db.get(Order, order_id)
    if order is None or order.user_id != user_id:
        raise AppError(ErrorCode.NOT_FOUND, "order not found")
    return order


def _release_payment(db: Session, order: Order, reason: str, now: datetime) -> str:
    """取消 / 关单前处理微信支付单（官方：先查单再关单）。返回 none | closed | paid | closing。会提交会话。"""
    info = ports.payment_gateway().open_payment(db, order.id)
    if info is None:
        return "none"
    if info["status"] == "paid":
        return "paid"
    if info["status"] in ("closed", "failed"):
        return "closed"
    status = ports.payment_gateway().close(db, int(info["id"]), reason, now)
    db.refresh(order)
    if status == "paid":
        return "paid"
    if status in ("closed", "failed"):
        return "closed"
    return "closing"


def pay(db: Session, user_id: int, order_id: int, method: str, now: datetime | None = None) -> dict[str, Any]:
    """去支付：wechat → 09 下单 / 复用（返回调起参数）；balance → 余额按行扣款并同事务成交。会提交会话。"""
    now = _now(now)
    if method not in PAY_METHODS:
        raise AppError(ErrorCode.BAD_REQUEST, "method must be wechat or balance")
    order = get_owned(db, user_id, order_id)
    if order.status != PENDING:
        raise AppError(ErrorCode.CONFLICT, "order is not awaiting payment", data={"status": order.status})
    if aware(order.pay_deadline_at) <= now:
        raise AppError(ErrorCode.CONFLICT, "order payment window has ended", data={"reason": "pay_expired"})
    if method == PAY_WECHAT:
        return {"method": PAY_WECHAT, "order_id": order.id, **ports.payment_gateway().pay(db, user_id, order.id, now)}
    if not get_config().balance_payment_enabled:
        raise _rule("balance payment disabled", "balance_disabled")
    outcome = _release_payment(db, order, "replaced", now)
    if outcome == "paid":
        db.refresh(order)
        raise AppError(ErrorCode.CONFLICT, "order already paid", data={"reason": "already_paid", "status": order.status})
    if outcome == "closing":
        raise UpstreamError("微信支付单正在关闭，请稍后再试", data={"reason": "payment_closing"})
    db.refresh(order)
    if order.status != PENDING:
        raise AppError(ErrorCode.CONFLICT, "order is not awaiting payment", data={"status": order.status})
    items = _items(db, order.id)
    for it in items:
        if it.paid_cents > 0:
            ref = f"order:{order.id}:item:{it.id}"
            ports.wallet_port().consume(db, user_id, ref, it.paid_cents, order.store_id)
            db.execute(update(OrderItem).where(OrderItem.id == it.id).values(pay_ref=ref))
    _settle_paid(db, order, method=PAY_BALANCE, amount_cents=order.total_cents, paid_at=now, actor=("user", user_id))
    return {"method": PAY_BALANCE, "order_id": order.id, "status": order.status, "paid_cents": order.paid_cents}


# ================================================================ 取消 / 关单


def _close_order(db: Session, order: Order, target: str, cancel_type: str, reason: str | None,
                 actor: tuple[str, Any], now: datetime) -> Order:
    """pending_payment → cancelled | closed：释放库存、解锁券（已过期的券变 expired）、回退限购计数、order.cancelled。"""
    _advance(db, order, target, actor, reason, now, cancelled_at=now, cancel_type=cancel_type,
             cancel_reason=(reason or None) and reason[:200])
    items = _items(db, order.id)
    for kind in sorted({it.kind for it in items}):
        get_line_kind(kind).release(db, order, [it for it in items if it.kind == kind], now)
    if json.loads(order.coupon_ids_json or "[]"):
        ports.coupon_port().unlock(db, order.id, now)
    _unbump_limits(db, order, items)
    _event(db, "order.cancelled", order, cancel_type=cancel_type, reason=reason)
    return order


def cancel(db: Session, user_id: int, order_id: int, reason: str = "", now: datetime | None = None) -> dict[str, Any]:
    """用户取消：仅未支付。先查单再关单（已支付 → 4090；渠道暂不可用 → 5020 稍后重试）。会提交会话；调用方提交最终事务。"""
    now = _now(now)
    order = get_owned(db, user_id, order_id)
    if order.status != PENDING:
        raise AppError(ErrorCode.CONFLICT, "only unpaid orders can be cancelled; paid orders go through after-sales",
                       data={"status": order.status})
    _cancel_with_payment(db, order, CANCELLED, CANCEL_USER, reason, ("user", user_id), now)
    return order_summary(order)


def _cancel_with_payment(db: Session, order: Order, target: str, cancel_type: str, reason: str | None,
                         actor: tuple[str, Any], now: datetime) -> Order:
    outcome = _release_payment(db, order, "business_cancel" if cancel_type != CANCEL_TIMEOUT else "expired", now)
    db.refresh(order)
    if outcome == "paid" and order.status != PENDING:
        raise AppError(ErrorCode.CONFLICT, "order already paid", data={"reason": "already_paid", "status": order.status})
    if outcome == "closing":
        raise UpstreamError("支付结果确认中，请稍后再试", data={"reason": "payment_closing"})
    if order.status != PENDING:
        raise AppError(ErrorCode.CONFLICT, "order status changed", data={"status": order.status})
    if outcome == "paid":
        # 渠道已扣款但订单钩子拒绝（例如金额不符）：支付模块已登记人工 + 自动退款；订单关闭并标记人工
        db.execute(update(Order).where(Order.id == order.id).values(needs_manual=True,
                                                                     manual_reason="paid_payment_rejected"))
    return _close_order(db, order, target, cancel_type, reason, actor, now)


def admin_close(db: Session, staff_id: int, order_id: int, reason: str, store_ids: list[int] | None = None,
                now: datetime | None = None) -> dict[str, Any]:
    now = _now(now)
    if not (reason or "").strip():
        raise AppError(ErrorCode.BAD_REQUEST, "reason is required")
    order = admin_get(db, order_id, store_ids)
    if order.status != PENDING:
        raise AppError(ErrorCode.CONFLICT, "only unpaid orders can be closed", data={"status": order.status})
    _cancel_with_payment(db, order, CANCELLED, CANCEL_ADMIN, reason, ("staff", staff_id), now)
    write_audit(db, "staff", staff_id, "order.close", "order", order.id, before={"status": PENDING},
                after={"status": order.status}, reason=reason[:200])
    return order_summary(order)


def close_expired(db: Session, now: datetime | None = None, limit: int | None = None) -> dict[str, list[int]]:
    """超时关单任务：pending_payment 且 pay_deadline_at 已过 → 先查单（已付 → 由支付钩子成交）→ 关单 → 关闭订单。

    每单独立提交；渠道不可用的本轮跳过（保留库存与券，下轮再查，R7 §2.3 规则 2）；重复执行无副作用。
    """
    now = _now(now)
    ids = db.execute(select(Order.id).where(Order.status == PENDING, Order.pay_deadline_at <= now)
                     .order_by(Order.pay_deadline_at, Order.id).limit(limit or get_config().close_batch)).scalars().all()
    report: dict[str, list[int]] = {"closed": [], "paid": [], "retry": [], "skipped": []}
    db.commit()
    for order_id in ids:
        try:
            order = db.get(Order, order_id)
            db.refresh(order)
            if order.status != PENDING:
                report["skipped"].append(order_id)
                continue
            _cancel_with_payment(db, order, CLOSED, CANCEL_TIMEOUT, "payment timeout", ("system", "timeout"), now)
            db.commit()
            report["closed"].append(order_id)
        except UpstreamError:
            db.rollback()
            report["retry"].append(order_id)
        except AppError as exc:
            db.rollback()
            current = db.get(Order, order_id)
            if current is not None and current.status in ACTIVE_AFTER_PAY:
                report["paid"].append(order_id)
            else:
                log.warning("order close failed %s: %s", order_id, exc.message)
                report["retry"].append(order_id)
        except Exception:  # noqa: BLE001 - 单个订单失败不影响其他订单
            db.rollback()
            log.exception("order close failed %s", order_id)
            report["retry"].append(order_id)
    return report


# ================================================================ 履约 / 收货 / 完成


def _post_commit_shipping(db: Session, order: Order, logistics_type: int) -> None:
    """微信小程序发货信息录入（交易类小程序必需）。必须在业务事务提交之后调用；失败由 shipping_sync 任务补录。"""
    if not get_config().shipping_upload_enabled or order.pay_method != PAY_WECHAT or not order.payment_id:
        return
    first = db.scalar(select(OrderItem.name).where(OrderItem.order_id == order.id).order_by(OrderItem.id).limit(1))
    try:
        ports.payment_gateway().shipping(db, order.payment_id, first or order.order_no, logistics_type=logistics_type,
                                         tracking_no=order.tracking_no, express_company=order.express_company)
        db.execute(update(Order).where(Order.id == order.id, Order.shipping_uploaded_at.is_(None))
                   .values(shipping_uploaded_at=utcnow()))
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        log.warning("order shipping upload deferred for %s", order.id)


def ship(db: Session, staff_id: int, order_id: int, express_company: str, tracking_no: str,
         store_ids: list[int] | None = None, now: datetime | None = None) -> Order:
    """实物发货：paid → fulfilling（记录快递公司与单号）。不 commit。"""
    now = _now(now)
    if not express_company.strip() or not tracking_no.strip():
        raise AppError(ErrorCode.BAD_REQUEST, "express_company and tracking_no are required")
    order = admin_get(db, order_id, store_ids)
    if order.status != PAID:
        raise AppError(ErrorCode.CONFLICT, "only paid orders can be shipped", data={"status": order.status})
    if order.refund_status == REFUNDING:
        raise AppError(ErrorCode.CONFLICT, "after-sale in progress", data={"reason": "refunding"})
    _advance(db, order, FULFILLING, ("staff", staff_id), now=now, fulfilling_at=now,
             express_company=express_company.strip()[:64], tracking_no=tracking_no.strip()[:64])
    write_audit(db, "staff", staff_id, "order.ship", "order", order.id, after={"tracking_no": order.tracking_no})
    _event(db, "order.fulfilling", order, express_company=order.express_company, tracking_no=order.tracking_no)
    return order


def mark_fulfilled(db: Session, staff_id: int, order_id: int, store_ids: list[int] | None = None,
                   now: datetime | None = None) -> Order:
    """履约完成（送达 / 到店服务完成 / 核销）：paid | fulfilling → fulfilled；待交付的行全部标记已交付。不 commit。"""
    now = _now(now)
    order = admin_get(db, order_id, store_ids)
    if order.status not in (PAID, FULFILLING):
        raise AppError(ErrorCode.CONFLICT, "order cannot be fulfilled now", data={"status": order.status})
    db.execute(update(OrderItem).where(OrderItem.order_id == order.id, OrderItem.fulfil_status == "pending")
               .values(fulfil_status="fulfilled", fulfilled_at=now))
    _advance(db, order, FULFILLED, ("staff", staff_id), now=now, fulfilled_at=now)
    write_audit(db, "staff", staff_id, "order.fulfil", "order", order.id, after={"status": FULFILLED})
    _event(db, "order.fulfilled", order)
    return order


def confirm_receipt(db: Session, user_id: int, order_id: int, now: datetime | None = None) -> Order:
    """顾客确认收货：fulfilling | fulfilled → completed（签收时间记为交付时间，电商法第五十一条）。不 commit。"""
    now = _now(now)
    order = get_owned(db, user_id, order_id)
    if order.status not in (FULFILLING, FULFILLED):
        raise AppError(ErrorCode.CONFLICT, "order cannot be confirmed now", data={"status": order.status})
    values: dict[str, Any] = {"completed_at": now}
    if order.fulfilled_at is None:
        values["fulfilled_at"] = now
        db.execute(update(OrderItem).where(OrderItem.order_id == order.id, OrderItem.fulfil_status == "pending")
                   .values(fulfil_status="fulfilled", fulfilled_at=now))
    _advance(db, order, COMPLETED, ("user", user_id), now=now, **values)
    _event(db, "order.completed", order, by="user")
    return order


def auto_complete(db: Session, now: datetime | None = None, limit: int | None = None) -> dict[str, list[int]]:
    """自动完成：fulfilled（或已发货 fulfilling）超过 ORDER_AUTO_COMPLETE_DAYS 天且无处理中售后 → completed。逐单提交。"""
    now = _now(now)
    cfg = get_config()
    cutoff = now - timedelta(days=cfg.auto_complete_days)
    ids = db.execute(select(Order.id).where(
        Order.refund_status != REFUNDING,
        ((Order.status == FULFILLED) & (Order.fulfilled_at <= cutoff))
        | ((Order.status == FULFILLING) & (Order.fulfilling_at <= cutoff)),
    ).order_by(Order.id).limit(limit or cfg.complete_batch)).scalars().all()
    report: dict[str, list[int]] = {"completed": [], "skipped": []}
    for order_id in ids:
        order = db.get(Order, order_id)
        try:
            values: dict[str, Any] = {"completed_at": now}
            if order.fulfilled_at is None:
                values["fulfilled_at"] = now
            _advance(db, order, COMPLETED, ("system", "auto_complete"), now=now,
                     extra_where=(Order.refund_status != REFUNDING,), **values)
            _event(db, "order.completed", order, by="system")
            db.commit()
            report["completed"].append(order_id)
        except AppError:
            db.rollback()
            report["skipped"].append(order_id)
    return report


def shipping_sync(db: Session, now: datetime | None = None, limit: int = 100) -> int:
    """补录发货信息：已发货 / 已履约的微信支付订单，发货信息尚未录入成功的。"""
    rows = db.execute(select(Order).where(Order.pay_method == PAY_WECHAT, Order.shipping_uploaded_at.is_(None),
                                          Order.status.in_((FULFILLING, FULFILLED, COMPLETED)))
                      .order_by(Order.id).limit(limit)).scalars().all()
    db.commit()
    done = 0
    for order in rows:
        _post_commit_shipping(db, order, 1 if order.tracking_no else (3 if order.goods_kind != GOODS_PHYSICAL else 4))
        db.refresh(order)
        done += 1 if order.shipping_uploaded_at else 0
    return done


def after_commit_shipping(db: Session, order_id: int) -> None:
    """接口层在发货 / 履约用例提交后调用（同步尝试一次，失败交给任务）。"""
    order = db.get(Order, order_id)
    if order is not None:
        _post_commit_shipping(db, order, 1 if order.tracking_no else (3 if order.goods_kind != GOODS_PHYSICAL else 4))


# ================================================================ 退款额度（供 10-after-sales 调用，不 commit）


def refund_quote(db: Session, order_id: int, item_id: int, qty: int, now: datetime | None = None) -> dict[str, Any]:
    """本行本次最多可退：按数量的累计公式；退完剩余数量时 = 剩余实付；行种类可再设上限（会员卡按已使用量）。"""
    now = _now(now)
    order = db.get(Order, order_id)
    item = db.get(OrderItem, item_id)
    if order is None or item is None or item.order_id != order_id:
        raise AppError(ErrorCode.NOT_FOUND, "order line not found")
    remaining_qty = item.qty - item.refund_reserved_qty
    remaining_cents = item.paid_cents - item.refund_reserved_cents
    if qty < 0 or qty > remaining_qty:
        raise _rule("quantity exceeds refundable", "qty_exceeds", item_id=item_id, refundable_qty=remaining_qty)
    if qty == remaining_qty:
        max_cents = remaining_cents
    else:
        max_cents = min(remaining_cents, max_refund_for_qty(item.paid_cents, item.qty, item.refund_reserved_qty, qty))
    if qty == 0:
        max_cents = remaining_cents
    cap = get_line_kind(item.kind).refund_cap(db, order, item, qty, now)
    if cap is not None:
        max_cents = max(0, min(max_cents, cap))
    return {"order_id": order_id, "item_id": item_id, "qty": qty, "max_cents": max(0, max_cents),
            "refundable_qty": remaining_qty, "refundable_cents": remaining_cents, "pay_method": order.pay_method,
            "kind": item.kind, "returnable": item.returnable, "fulfil_status": item.fulfil_status}


def reserve_refund(db: Session, order_id: int, lines: list[tuple[int, int, int]]) -> None:
    """占用退款额度 lines=[(item_id, qty, amount)]：条件 UPDATE 保证 Σ占用 ≤ 行实付、数量 ≤ 行数量（并发安全）。

    余额支付的订单只支持“整行剩余全部退回”（钱包按消费流水冲回，见 MODULE.md §13）。
    """
    order = db.get(Order, order_id)
    if order is None:
        raise AppError(ErrorCode.NOT_FOUND, "order not found")
    if order.status not in ACTIVE_AFTER_PAY:
        raise AppError(ErrorCode.CONFLICT, "order is not refundable", data={"status": order.status})
    total = 0
    for item_id, qty, amount in sorted(lines):
        if qty < 0 or amount <= 0:
            raise AppError(ErrorCode.BAD_REQUEST, "invalid refund line")
        conditions = [OrderItem.id == item_id, OrderItem.order_id == order_id,
                      OrderItem.refund_reserved_qty + qty <= OrderItem.qty,
                      OrderItem.refund_reserved_cents + amount <= OrderItem.paid_cents]
        changed = db.execute(update(OrderItem).where(*conditions).values(
            refund_reserved_qty=OrderItem.refund_reserved_qty + qty,
            refund_reserved_cents=OrderItem.refund_reserved_cents + amount)).rowcount
        if changed != 1:
            raise AppError(ErrorCode.CONFLICT, "refundable amount changed, refresh and retry",
                           data={"reason": "exceeds_refundable", "item_id": item_id})
        total += amount
    changed = db.execute(update(Order).where(Order.id == order_id, Order.status.in_(ACTIVE_AFTER_PAY),
                                             Order.refund_reserved_cents + total <= Order.paid_cents)
                         .values(refund_reserved_cents=Order.refund_reserved_cents + total,
                                 refund_status=REFUNDING, version=Order.version + 1)).rowcount
    if changed != 1:
        raise AppError(ErrorCode.CONFLICT, "order refund state changed", data={"reason": "exceeds_refundable"})


def _sync_refund_status(db: Session, order_id: int) -> None:
    open_cents = Order.refund_reserved_cents - Order.refunded_cents
    db.execute(update(Order).where(Order.id == order_id).values(refund_status=case(
        (open_cents > 0, REFUNDING),
        ((Order.paid_cents > 0) & (Order.refunded_cents >= Order.paid_cents), REFUND_DONE),
        (Order.refunded_cents > 0, PARTIALLY_REFUNDED),
        else_=REFUND_NONE)))


def release_refund(db: Session, order_id: int, lines: list[tuple[int, int, int]]) -> None:
    """释放占用（售后驳回 / 撤销 / 关闭 / 退款失败放弃）。"""
    total = 0
    for item_id, qty, amount in sorted(lines):
        changed = db.execute(update(OrderItem).where(
            OrderItem.id == item_id, OrderItem.order_id == order_id,
            OrderItem.refund_reserved_qty - qty >= OrderItem.refunded_qty,
            OrderItem.refund_reserved_cents - amount >= OrderItem.refunded_cents,
        ).values(refund_reserved_qty=OrderItem.refund_reserved_qty - qty,
                 refund_reserved_cents=OrderItem.refund_reserved_cents - amount)).rowcount
        if changed != 1:
            raise AppError(ErrorCode.CONFLICT, "refund reservation not found", data={"item_id": item_id})
        total += amount
    db.execute(update(Order).where(Order.id == order_id, Order.refund_reserved_cents - total >= Order.refunded_cents)
               .values(refund_reserved_cents=Order.refund_reserved_cents - total, version=Order.version + 1))
    _sync_refund_status(db, order_id)


def refund_balance(db: Session, order_id: int, lines: list[int] | list[tuple[int, int]], ref: str | None = None) -> None:
    """余额支付订单：按行冲回消费流水（原路退回，七日办法第十四条）。

    lines 为 [(item_id, amount_cents)] 时按金额部分冲回，幂等键 `{ref}:item:{item_id}`（ref 必填，通常是售后单 business_ref）；
    为 [item_id] 时冲回该行剩余全部（兼容旧调用）。wallet.reverse 保证累计冲回不超过原消费。
    """
    order = db.get(Order, order_id)
    if order is None or order.pay_method != PAY_BALANCE:
        raise AppError(ErrorCode.CONFLICT, "order was not paid by balance")
    pairs = sorted((ln, None) if isinstance(ln, int) else (int(ln[0]), int(ln[1])) for ln in lines)
    for item_id, amount in pairs:
        item = db.get(OrderItem, item_id)
        if item is None or not item.pay_ref:
            continue
        if amount is None:
            ports.wallet_port().reverse(db, order.user_id, item.pay_ref)
        elif amount > 0:
            if not ref:
                raise ValueError("partial balance refund needs ref")
            ports.wallet_port().reverse(db, order.user_id, item.pay_ref, amount_cents=amount,
                                        reversal_ref=f"{ref}:item:{item_id}")


def settle_refund(db: Session, order_id: int, lines: list[tuple[int, int, int, bool]], ref: str,
                  now: datetime | None = None) -> dict[str, Any]:
    """退款成功：lines=[(item_id, qty, amount, restock)] 计入已退 → 行种类钩子（回库存 / 终止权益）→ 按券策略退券 →
    全部退完时订单 → refunded。不 commit；按售后单状态机保证每单只调用一次。"""
    now = _now(now)
    order = db.get(Order, order_id)
    if order is None:
        raise AppError(ErrorCode.NOT_FOUND, "order not found")
    total = 0
    for item_id, qty, amount, restock in sorted(lines):
        changed = db.execute(update(OrderItem).where(
            OrderItem.id == item_id, OrderItem.order_id == order_id,
            OrderItem.refunded_qty + qty <= OrderItem.refund_reserved_qty,
            OrderItem.refunded_cents + amount <= OrderItem.refund_reserved_cents,
        ).values(refunded_qty=OrderItem.refunded_qty + qty, refunded_cents=OrderItem.refunded_cents + amount)).rowcount
        if changed != 1:
            raise AppError(ErrorCode.CONFLICT, "refund exceeds reservation", data={"item_id": item_id})
        item = db.get(OrderItem, item_id)
        get_line_kind(item.kind).on_refunded(db, order, item, qty, amount, restock, now)
        total += amount
    changed = db.execute(update(Order).where(Order.id == order_id, Order.refunded_cents + total <= Order.paid_cents)
                         .values(refunded_cents=Order.refunded_cents + total, version=Order.version + 1)).rowcount
    if changed != 1:
        raise AppError(ErrorCode.CONFLICT, "refund exceeds paid amount")
    _sync_refund_status(db, order_id)
    db.refresh(order)
    full = order.paid_cents > 0 and order.refunded_cents >= order.paid_cents
    coupons = {}
    if json.loads(order.coupon_ids_json or "[]"):
        coupons = ports.coupon_port().return_on_refund(db, order.id, full, now)
    if full and order.status in ACTIVE_AFTER_PAY:
        _advance(db, order, REFUNDED, ("system", ref), "fully refunded", now)
    _event(db, "order.refunded", order, amount_cents=total, refunded_cents=order.refunded_cents, full=full, ref=ref)
    return {"order_id": order.id, "refunded_cents": order.refunded_cents, "full": full, "coupons": coupons,
            "status": order.status, "refund_status": order.refund_status}


def mark_needs_manual(db: Session, order_id: int, reason: str) -> None:
    db.execute(update(Order).where(Order.id == order_id).values(needs_manual=True, manual_reason=reason[:200]))


# ================================================================ 查询


def _iso(value: datetime | None) -> str | None:
    value = aware(value)
    return value.isoformat() if value else None


def item_view(it: OrderItem) -> dict[str, Any]:
    return {"id": it.id, "kind": it.kind, "goods_kind": it.goods_kind, "product_id": it.product_id, "sku_id": it.sku_id,
            "name": it.name, "spec": it.spec, "image": it.image, "unit_price_cents": it.unit_price_cents, "qty": it.qty,
            "gross_cents": it.gross_cents, "campaign_alloc_cents": it.campaign_alloc_cents,
            "coupon_alloc_cents": it.coupon_alloc_cents, "paid_cents": it.paid_cents, "returnable": it.returnable,
            "fulfil_status": it.fulfil_status, "fulfil_ref": it.fulfil_ref, "refunded_qty": it.refunded_qty,
            "refunded_cents": it.refunded_cents, "refund_reserved_qty": it.refund_reserved_qty,
            "refund_reserved_cents": it.refund_reserved_cents}


def order_view(order: Order, items: list[OrderItem] | None = None, *, admin: bool = False) -> dict[str, Any]:
    data = {
        "id": order.id, "order_no": order.order_no, "user_id": order.user_id if admin else None,
        "store_id": order.store_id, "status": order.status, "refund_status": order.refund_status, "kind": order.kind,
        "goods_kind": order.goods_kind, "items_cents": order.items_cents,
        "campaign_discount_cents": order.campaign_discount_cents, "coupon_cents": order.coupon_cents,
        "total_cents": order.total_cents, "paid_cents": order.paid_cents, "refunded_cents": order.refunded_cents,
        "pay_method": order.pay_method, "out_trade_no": order.out_trade_no,
        "pay_deadline_at": _iso(order.pay_deadline_at), "paid_at": _iso(order.paid_at),
        "fulfilling_at": _iso(order.fulfilling_at), "fulfilled_at": _iso(order.fulfilled_at),
        "completed_at": _iso(order.completed_at), "cancelled_at": _iso(order.cancelled_at),
        "cancel_type": order.cancel_type, "cancel_reason": order.cancel_reason,
        "express_company": order.express_company, "tracking_no": order.tracking_no, "remark": order.remark,
        "created_at": _iso(order.created_at),
    }
    if admin:
        data.update(buyer_phone=order.buyer_phone_masked, needs_manual=order.needs_manual,
                    manual_reason=order.manual_reason, terms_version=order.terms_version,
                    terms_accepted_at=_iso(order.terms_accepted_at), payment_id=order.payment_id)
    else:
        data.pop("user_id")
    if items is not None:
        data["items"] = [item_view(it) for it in items]
    return data


def _address(order: Order, masked: bool) -> dict[str, Any] | None:
    raw = ports.decrypt_json(order.address_snapshot_json)
    if not raw:
        return None
    snap = json.loads(raw)
    if masked:
        snap["phone"] = mask_phone(snap.get("phone"))
        snap["contact"] = mask_name(snap.get("contact"))
        snap["detail"] = mask_address(snap.get("detail"), keep=4)
        snap.pop("house_no", None)
    return snap


def _timeline(db: Session, order_id: int) -> list[dict[str, Any]]:
    rows = db.execute(select(OrderStatusLog).where(OrderStatusLog.order_id == order_id)
                      .order_by(OrderStatusLog.id)).scalars().all()
    return [{"from": r.from_status, "to": r.to_status, "actor_type": r.actor_type, "reason": r.reason,
             "at": _iso(r.created_at)} for r in rows]


def app_detail(db: Session, user_id: int, order_id: int) -> dict[str, Any]:
    order = get_owned(db, user_id, order_id)
    data = order_view(order, _items(db, order.id))
    data["address"] = _address(order, masked=False)  # 本人查看自己的收货信息
    data["timeline"] = _timeline(db, order.id)
    data["can_cancel"] = order.status == PENDING
    data["can_pay"] = order.status == PENDING and aware(order.pay_deadline_at) > utcnow()
    data["can_confirm"] = order.status in (FULFILLING, FULFILLED)
    return data


APP_LIST = ListSpec(
    fields={"created_at": Field(Order.created_at, kind="datetime", ops=("range",), sortable=True)},
    id_column=Order.id, default_sort="-created_at", extra_params=frozenset({"tab"}),
)


def app_list(db: Session, user_id: int, params: dict[str, Any], secret: str) -> dict[str, Any]:
    tab = params.get("tab") or "all"
    if tab not in TABS:
        raise AppError(ErrorCode.BAD_REQUEST, f"tab must be one of {', '.join(TABS)}")
    stmt = select(Order).where(Order.user_id == user_id, Order.status.in_(TABS[tab]))
    page = APP_LIST.cursor(db, stmt, params, secret, serialize=lambda o: _list_row(db, o))
    page["counts"] = tab_counts(db, user_id)
    return page


def _list_row(db: Session, order: Order) -> dict[str, Any]:
    data = order_view(order)
    first = db.execute(select(OrderItem).where(OrderItem.order_id == order.id).order_by(OrderItem.id).limit(3)).scalars()
    data["preview_items"] = [{"name": it.name, "image": it.image, "qty": it.qty} for it in first]
    data["item_count"] = db.scalar(select(func.coalesce(func.sum(OrderItem.qty), 0)).where(OrderItem.order_id == order.id))
    return data


def tab_counts(db: Session, user_id: int) -> dict[str, int]:
    rows = dict(db.execute(select(Order.status, func.count()).where(Order.user_id == user_id)
                           .group_by(Order.status)).all())
    return {tab: sum(rows.get(s, 0) for s in statuses) for tab, statuses in TABS.items()}


ADMIN_LIST = ListSpec(
    fields={
        "status": Field(Order.status, ops=("eq", "in"), choices=frozenset(STATUSES)),
        "refund_status": Field(Order.refund_status, ops=("eq", "in"), choices=frozenset(REFUND_STATUSES)),
        "kind": Field(Order.kind, ops=("eq", "in")),
        "goods_kind": Field(Order.goods_kind, ops=("eq", "in"), choices=frozenset(GOODS_KINDS)),
        "pay_method": Field(Order.pay_method, ops=("eq", "in"), choices=frozenset((PAY_WECHAT, PAY_BALANCE, PAY_FREE))),
        "store_id": Field(Order.store_id, kind="int", ops=("eq", "in")),
        "user_id": Field(Order.user_id, kind="int"),
        "needs_manual": Field(Order.needs_manual, kind="bool"),
        "total_cents": Field(Order.total_cents, kind="int", ops=("range",), sortable=True),
        "created_at": Field(Order.created_at, kind="datetime", ops=("range",), sortable=True),
        "paid_at": Field(Order.paid_at, kind="datetime", ops=("range", "null"), sortable=True),
    },
    id_column=Order.id,
    keyword_columns=(Order.order_no, Order.buyer_phone_masked),
    default_sort="-created_at",
)


def admin_query(params: dict[str, Any], store_ids: list[int] | None) -> tuple[Any, dict[str, Any]]:
    """后台查询语句 + 处理后的参数：关键词是完整手机号时按哈希精确匹配（不做明文模糊搜索）。"""
    stmt = select(Order)
    if store_ids is not None:
        stmt = stmt.where(Order.store_id.in_(store_ids))
    params = dict(params)
    keyword = (params.get("keyword") or "").strip()
    if keyword.isdigit() and len(keyword) == 11:
        stmt = stmt.where(Order.buyer_phone_hash == ports.phone_hash(keyword))
        params.pop("keyword")
    return stmt, params


def admin_list(db: Session, params: dict[str, Any], store_ids: list[int] | None) -> dict[str, Any]:
    stmt, params = admin_query(params, store_ids)
    return ADMIN_LIST.page(db, stmt, params, serialize=lambda o: order_view(o, admin=True))


def admin_get(db: Session, order_id: int, store_ids: list[int] | None = None) -> Order:
    order = db.get(Order, order_id)
    if order is None or (store_ids is not None and order.store_id not in store_ids):
        raise AppError(ErrorCode.NOT_FOUND, "order not found")
    return order


def admin_detail(db: Session, order_id: int, store_ids: list[int] | None = None) -> dict[str, Any]:
    order = admin_get(db, order_id, store_ids)
    data = order_view(order, _items(db, order.id), admin=True)
    data["address"] = _address(order, masked=True)
    data["timeline"] = _timeline(db, order.id)
    data["notes"] = [{"id": n.id, "staff_id": n.staff_id, "text": n.text, "created_at": _iso(n.created_at)}
                     for n in db.execute(select(OrderNote).where(OrderNote.order_id == order.id)
                                         .order_by(OrderNote.id)).scalars()]
    return data


def add_note(db: Session, staff_id: int, order_id: int, text: str, store_ids: list[int] | None = None) -> dict[str, Any]:
    text = (text or "").strip()
    if not 1 <= len(text) <= 500:
        raise AppError(ErrorCode.BAD_REQUEST, "note must be 1-500 chars")
    order = admin_get(db, order_id, store_ids)
    note = OrderNote(order_id=order.id, staff_id=staff_id, text=text)
    db.add(note)
    db.flush()
    write_audit(db, "staff", staff_id, "order.note", "order", order.id, after={"note_id": note.id})
    return {"id": note.id, "order_id": order.id, "text": text}


EXPORT_HEADERS = ["订单ID", "订单号", "门店", "种类", "状态", "退款状态", "商品金额(元)", "活动优惠(元)", "券抵扣(元)",
                  "应付(元)", "已退(元)", "支付方式", "下单人手机", "下单时间", "支付时间"]


def export_row(o: Order) -> list[Any]:
    from luzback import cents_to_yuan
    return [o.id, o.order_no, o.store_id, o.kind, o.status, o.refund_status, cents_to_yuan(o.items_cents),
            cents_to_yuan(o.campaign_discount_cents), cents_to_yuan(o.coupon_cents), cents_to_yuan(o.total_cents),
            cents_to_yuan(o.refunded_cents), o.pay_method, o.buyer_phone_masked, aware(o.created_at), aware(o.paid_at)]


# ================================================================ v1 兼容（19-review 等调用）


def create_order(db: Session, user_id: int, lines: list[tuple[int, int]], idempotency_key: str | None,
                 now: datetime | None = None) -> dict[str, Any]:
    """v1：直接按 SKU 下实物单（无报价令牌、无券）。新代码请走 preview → submit。会提交。"""
    now = _now(now)
    request = {"store_id": None, "items": [{"kind": "goods", "sku_id": s, "qty": q} for s, q in lines], "coupon_ids": []}
    return run_idempotent(db, "order.create", idempotency_key, lambda: _create(db, user_id, request, now),
                          actor=f"user:{user_id}")


def mark_paid(db: Session, order_id: int, now: datetime | None = None) -> Order:
    """v1 兼容：按订单应付记一次成交（线下 / 测试）。不 commit。"""
    order = db.get(Order, order_id)
    if order is None:
        raise AppError(ErrorCode.NOT_FOUND, "order not found")
    return _settle_paid(db, order, method=PAY_WECHAT, amount_cents=order.total_cents, paid_at=_now(now),
                        actor=("system", "mark_paid"))


def cancel_order(db: Session, order_id: int, now: datetime | None = None) -> Order:
    """v1 兼容：取消未支付订单（不处理渠道支付单；新代码请用 cancel / close_expired）。不 commit。"""
    order = db.get(Order, order_id)
    if order is None:
        raise AppError(ErrorCode.NOT_FOUND, "order not found")
    return _close_order(db, order, CANCELLED, CANCEL_ADMIN, None, ("system", "v1"), _now(now))
