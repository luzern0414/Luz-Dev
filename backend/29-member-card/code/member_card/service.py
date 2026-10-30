"""会员卡用例：卡种管理、购卡（支付事件发卡）、后台发卡 / 赠卡、使用与冲正、冻结（请假）、转让、挂失补办、
退卡（报价 → 申请 → 审批 → 原路退款事件 → 结果回写）、后台调整、停用；以及定时任务主体。

事务约定：用例只 flush 不 commit——接口层经 run_idempotent 提交；事件消费 handle_event 与 jobs.py 自己提交。
每次卡状态 / 次数 / 有效期变化都是带条件的 UPDATE（状态、剩余次数、version）+ rowcount 检查，并写一条只追加的卡流水。
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy import update as _sa_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from luzback import AppError, ErrorCode, enqueue_event, utcnow, write_audit
from luzback.business import business_value
from luzback.security import mask_phone
from wallet import (
    CATEGORY_COUNT, CATEGORY_PERIOD, PaidEvent, RefundResultEvent, find_member, parse_paid, parse_refund_result,
    payment_port, phone_lookup_hash, policy_for, register_outstanding_provider, register_payment_hooks, store_timezone,
)

from . import config as _config
from .models import (
    ACT_ON_DATE, ACT_ON_FIRST_USE, ACT_ON_PURCHASE, ACTIVATIONS, KIND_COUNT, KIND_PERIOD, KIND_TIME_SLOT, KINDS,
    O_CLOSED, O_PAID, O_PENDING, ORDER_PURCHASE, ORDER_TRANSFER_FEE, R_FAILED, R_PENDING, R_PROCESSING, R_REJECTED,
    R_SUCCEEDED, S_ACTIVE, S_DISABLED, S_EXPIRED, S_FROZEN, S_INACTIVE, S_LOST, S_REFUNDED, S_REFUNDING, S_USED_UP,
    SCOPE_ALL, SCOPE_MULTI, SCOPE_SINGLE, SRC_GIFT, SRC_MANUAL, SRC_PURCHASE, CardFreeze, CardLedger, CardOrder,
    CardProduct, CardRefund, CardReminder, CardTransfer, CardUsage, MemberCard,
)
from .rules import (
    CAUSE_CUSTOMER, CAUSE_MERCHANT, CAUSES, RULE_COOLING_OFF, aware, ceil_days, in_windows, local_midnight_after,
    period_used_days, quote_card_refund, validate_windows,
)

BIZ_TYPE = "member_card"
CONSUMER = "member_card"
REFUND_TOPIC = "member_card.refund_requested"
REFUND_PREFIX = "member_card_refund"
GOODS_KIND = "offline_service"
LIVE = (S_INACTIVE, S_ACTIVE, S_FROZEN, S_LOST, S_USED_UP, S_EXPIRED, S_REFUNDING, S_DISABLED)

# 使用被拒的原因（data.reason），前端据此提示
REASONS = {
    S_FROZEN: (ErrorCode.BUSINESS_RULE, "card_frozen", "卡已冻结（请假中）"),
    S_EXPIRED: (ErrorCode.BUSINESS_RULE, "card_expired", "卡已过期"),
    S_LOST: (ErrorCode.BUSINESS_RULE, "card_lost", "卡已挂失"),
    S_DISABLED: (ErrorCode.BUSINESS_RULE, "card_disabled", "卡已停用"),
    S_REFUNDING: (ErrorCode.BUSINESS_RULE, "card_refunding", "卡正在退款"),
    S_REFUNDED: (ErrorCode.BUSINESS_RULE, "card_refunded", "卡已退"),
    S_USED_UP: (ErrorCode.INSUFFICIENT_BALANCE, "card_used_up", "次数已用完"),
}


def update(model):
    """ORM UPDATE 不在会话内求值同步（SQLite 读回的时间是 naive），调用方需要时显式 refresh。"""
    return _sa_update(model).execution_options(synchronize_session=False)


def cfg() -> _config.CardConfig:
    return _config.get_config()


def _no(prefix: str) -> str:
    return f"{prefix}{utcnow():%y%m%d%H%M%S}{secrets.token_hex(4).upper()}"


def _card_no() -> str:
    return "MC" + "".join(secrets.choice("0123456789") for _ in range(14))


def _err(code: ErrorCode, reason: str, message: str, **data: Any) -> AppError:
    return AppError(code, message, {"reason": reason, **data})


def _category(kind: str) -> str:
    return CATEGORY_COUNT if kind == KIND_COUNT else CATEGORY_PERIOD


# ================================================================ 卡种


PRODUCT_FIELDS = (
    "store_id", "name", "kind", "price_cents", "original_price_cents", "sessions", "gift_sessions", "period_days",
    "gift_days", "valid_days", "activation_mode", "activation_date", "activation_deadline_days", "renew_stack",
    "store_scope", "per_member_limit", "sale_start_at", "sale_end_at", "transferable", "transfer_fee_cents",
    "max_transfers", "transfer_cooling_days", "freezable", "freeze_min_days", "freeze_max_days", "freeze_max_times",
    "freeze_total_max_days", "refund_fee_bp", "entry_cooldown_minutes", "terms_text", "sort",
)
SNAPSHOT_FIELDS = PRODUCT_FIELDS + ("id",)


def _units(values: dict) -> tuple[int, int]:
    if values["kind"] == KIND_COUNT:
        return int(values.get("sessions") or 0), int(values.get("gift_sessions") or 0)
    return int(values.get("period_days") or 0), int(values.get("gift_days") or 0)


def _validate_product(values: dict) -> dict:
    kind = values.get("kind")
    if kind not in KINDS:
        raise AppError(ErrorCode.BAD_REQUEST, "kind must be count, period or time_slot (stored value is the wallet module)")
    if values.get("price_cents") is None or values["price_cents"] < 0:
        raise AppError(ErrorCode.BAD_REQUEST, "price_cents must be >= 0")
    if values.get("original_price_cents") is None:
        values["original_price_cents"] = values["price_cents"]
    if values["original_price_cents"] < values["price_cents"]:
        raise AppError(ErrorCode.BAD_REQUEST, "original_price_cents must be >= price_cents")
    paid, gift = _units(values)
    if paid < 1 or gift < 0:
        raise AppError(ErrorCode.BAD_REQUEST, "sessions / period_days must be >= 1 and gifts >= 0")
    if kind == KIND_COUNT and (values.get("period_days") or values.get("gift_days")):
        raise AppError(ErrorCode.BAD_REQUEST, "count card uses sessions, not period_days")
    if kind != KIND_COUNT and (values.get("sessions") or values.get("gift_sessions") or values.get("valid_days")):
        raise AppError(ErrorCode.BAD_REQUEST, "period card uses period_days, not sessions")
    windows = values.get("slot_windows") or []
    if kind == KIND_TIME_SLOT:
        values["slot_windows_json"] = json.dumps(validate_windows(windows))
    elif windows:
        raise AppError(ErrorCode.BAD_REQUEST, "slot_windows only for time_slot cards")
    mode = values.get("activation_mode") or ACT_ON_PURCHASE
    if mode not in ACTIVATIONS:
        raise AppError(ErrorCode.BAD_REQUEST, "invalid activation_mode")
    if mode == ACT_ON_DATE and not values.get("activation_date"):
        raise AppError(ErrorCode.BAD_REQUEST, "activation_date is required for on_date")
    scope = values.get("store_scope") or SCOPE_SINGLE
    store_ids = sorted({int(s) for s in (values.get("store_ids") or [])})
    if scope == SCOPE_SINGLE:
        store_ids = store_ids or ([int(values["store_id"])] if values.get("store_id") else [])
        if len(store_ids) != 1:
            raise AppError(ErrorCode.BAD_REQUEST, "single-store card needs exactly one store")
    elif scope == SCOPE_MULTI:
        if not store_ids:
            raise AppError(ErrorCode.BAD_REQUEST, "multi-store card needs store_ids")
    elif scope == SCOPE_ALL:
        store_ids = []
    else:
        raise AppError(ErrorCode.BAD_REQUEST, "store_scope must be single, multi or all")
    values["store_ids_json"] = json.dumps(store_ids)
    values["store_ids_key"] = "," + ",".join(str(s) for s in store_ids) + "," if store_ids else ""
    start, end = values.get("sale_start_at"), values.get("sale_end_at")
    if start and end and aware(start) >= aware(end):
        raise AppError(ErrorCode.BAD_REQUEST, "sale_start_at must be earlier than sale_end_at")
    fmin, fmax = values.get("freeze_min_days", 7), values.get("freeze_max_days", 30)
    if fmin is not None and fmax is not None and (fmin < 1 or fmin > fmax):
        raise AppError(ErrorCode.BAD_REQUEST, "freeze_min_days must be between 1 and freeze_max_days")
    policy = policy_for(values.get("store_id"))
    policy.check_card(_category(kind), values["price_cents"], paid + gift)
    policy.check_gift(paid, gift)
    return values


def product_dict(p: CardProduct) -> dict:
    return {"id": p.id, "store_id": p.store_id, "name": p.name, "kind": p.kind, "price_cents": p.price_cents,
            "original_price_cents": p.original_price_cents, "sessions": p.sessions, "gift_sessions": p.gift_sessions,
            "period_days": p.period_days, "gift_days": p.gift_days, "valid_days": p.valid_days,
            "slot_windows": json.loads(p.slot_windows_json or "[]"), "activation_mode": p.activation_mode,
            "activation_date": aware(p.activation_date), "activation_deadline_days": p.activation_deadline_days,
            "renew_stack": p.renew_stack, "store_scope": p.store_scope, "store_ids": json.loads(p.store_ids_json or "[]"),
            "per_member_limit": p.per_member_limit, "sale_start_at": aware(p.sale_start_at),
            "sale_end_at": aware(p.sale_end_at), "status": p.status, "goods_kind": p.goods_kind,
            "transferable": p.transferable, "transfer_fee_cents": p.transfer_fee_cents, "max_transfers": p.max_transfers,
            "transfer_cooling_days": p.transfer_cooling_days, "freezable": p.freezable,
            "freeze_min_days": p.freeze_min_days, "freeze_max_days": p.freeze_max_days,
            "freeze_max_times": p.freeze_max_times, "freeze_total_max_days": p.freeze_total_max_days,
            "refund_fee_bp": p.refund_fee_bp, "entry_cooldown_minutes": p.entry_cooldown_minutes,
            "terms_text": p.terms_text, "sort": p.sort, "invoice": {"tax_code": "601", "note": "预付卡销售和充值 不征税"}}


def _apply_product(product: CardProduct, values: dict) -> None:
    for key in PRODUCT_FIELDS + ("slot_windows_json", "store_ids_json", "store_ids_key"):
        if key in values:
            setattr(product, key, values[key])


def create_product(db: Session, staff_id: int, values: dict) -> dict:
    values = _validate_product(dict(values))
    product = CardProduct(status="off", created_by=staff_id, updated_by=staff_id, goods_kind=GOODS_KIND)
    _apply_product(product, values)
    db.add(product)
    db.flush()
    write_audit(db, "staff", staff_id, "member_card.product.create", "member_card_product", product.id,
                after=product_dict(product))
    return product_dict(product)


def get_product(db: Session, product_id: int) -> CardProduct:
    product = db.get(CardProduct, product_id)
    if product is None or product.deleted_at is not None:
        raise AppError(ErrorCode.NOT_FOUND, "card product not found")
    return product


def update_product(db: Session, staff_id: int, product_id: int, values: dict) -> dict:
    product = get_product(db, product_id)
    before = product_dict(product)
    merged = {k: getattr(product, k) for k in PRODUCT_FIELDS} | {
        "slot_windows": json.loads(product.slot_windows_json or "[]"), "store_ids": json.loads(product.store_ids_json or "[]"),
    } | values
    merged = _validate_product(merged)
    _apply_product(product, merged)
    product.updated_by = staff_id
    db.flush()
    write_audit(db, "staff", staff_id, "member_card.product.update", "member_card_product", product_id, before=before,
                after=product_dict(product))
    return product_dict(product)


def set_product_status(db: Session, staff_id: int, product_id: int, status: str) -> dict:
    if status not in ("on", "off"):
        raise AppError(ErrorCode.BAD_REQUEST, "status must be on or off")
    product = get_product(db, product_id)
    before = product.status
    db.execute(update(CardProduct).where(CardProduct.id == product_id).values(status=status, updated_by=staff_id))
    db.refresh(product)
    write_audit(db, "staff", staff_id, f"member_card.product.{status}", "member_card_product", product_id,
                before={"status": before}, after={"status": status})
    return product_dict(product)


def delete_product(db: Session, staff_id: int, product_id: int) -> dict:
    product = get_product(db, product_id)
    db.execute(update(CardProduct).where(CardProduct.id == product_id).values(deleted_at=utcnow(), status="off"))
    write_audit(db, "staff", staff_id, "member_card.product.delete", "member_card_product", product_id,
                before=product_dict(product))
    return {"id": product_id, "deleted": True}


def products_on_sale_stmt(store_id: int | None, at: datetime):
    stmt = select(CardProduct).where(CardProduct.deleted_at.is_(None), CardProduct.status == "on",
                                     or_(CardProduct.sale_start_at.is_(None), CardProduct.sale_start_at <= at),
                                     or_(CardProduct.sale_end_at.is_(None), CardProduct.sale_end_at > at))
    if store_id is not None:
        stmt = stmt.where(or_(CardProduct.store_scope == SCOPE_ALL, CardProduct.store_ids_key.like(f"%,{int(store_id)},%")))
    return stmt


def _applies_to(store_scope: str, store_ids_json: str, store_id: int | None) -> bool:
    if store_scope == SCOPE_ALL:
        return True
    return store_id is not None and int(store_id) in json.loads(store_ids_json or "[]")


# ================================================================ 卡：读取、流水、状态推进


def get_card(db: Session, card_id: int, user_id: int | None = None) -> MemberCard:
    card = db.get(MemberCard, card_id)
    if card is None or (user_id is not None and card.user_id != user_id):
        raise AppError(ErrorCode.NOT_FOUND, "card not found")
    return card


def get_card_by_no(db: Session, card_no: str) -> MemberCard:
    card = db.execute(select(MemberCard).where(MemberCard.card_no == card_no)).scalar_one_or_none()
    if card is None:
        raise AppError(ErrorCode.NOT_FOUND, "card not found")
    return card


def _ledger(db: Session, card: MemberCard, kind: str, ref: str, *, delta_sessions: int = 0, delta_days: int = 0,
            operator: str = "system", note: str | None = None, store_id: int | None = None) -> CardLedger:
    db.refresh(card)
    entry = CardLedger(card_id=card.id, user_id=card.user_id, kind=kind, delta_sessions=delta_sessions,
                       delta_days=delta_days, sessions_after=card.sessions_remaining, ends_at_after=card.ends_at,
                       status_after=card.status, ref=ref[:64], store_id=store_id, operator=operator[:32],
                       note=(note or None) and note[:200])
    db.add(entry)
    try:
        db.flush()
    except IntegrityError as exc:
        raise AppError(ErrorCode.DUPLICATE_REQUEST, "same operation is being processed", retry_after=1) from exc
    return entry


def _cas(db: Session, card: MemberCard, conditions: tuple = (), **values: Any) -> None:
    """按 version 乐观并发更新卡；失败 4090。"""
    result = db.execute(update(MemberCard).where(MemberCard.id == card.id, MemberCard.version == card.version, *conditions)
                        .values(version=MemberCard.version + 1, updated_at=utcnow(), **values))
    if result.rowcount != 1:
        raise AppError(ErrorCode.CONFLICT, "card changed concurrently, refresh and retry")
    db.refresh(card)


def _snapshot(card: MemberCard) -> dict:
    return json.loads(card.product_snapshot_json or "{}")


def _remaining_days(card: MemberCard, at: datetime) -> int | None:
    if card.ends_at is None:
        return None
    return ceil_days((aware(card.ends_at) - max(at, aware(card.starts_at) or at)).total_seconds())


def card_dict(card: MemberCard, at: datetime | None = None) -> dict:
    at = at or utcnow()
    return {"id": card.id, "card_no": card.card_no, "user_id": card.user_id, "product_id": card.product_id,
            "store_id": card.store_id, "kind": card.kind, "name": card.name, "status": card.status,
            "source": card.source, "paid_cents": card.paid_cents, "paid_units": card.paid_units,
            "gift_units": card.gift_units, "total_units": card.total_units,
            "sessions_remaining": card.sessions_remaining if card.kind == KIND_COUNT else None,
            "sessions_used": card.sessions_used if card.kind == KIND_COUNT else None,
            "remaining_days": _remaining_days(card, at), "use_count": card.use_count,
            "starts_at": aware(card.starts_at), "ends_at": aware(card.ends_at), "activated_at": aware(card.activated_at),
            "activation_deadline_at": aware(card.activation_deadline_at), "store_scope": card.store_scope,
            "store_ids": json.loads(card.store_ids_json or "[]"), "slot_windows": json.loads(card.slot_windows_json or "[]"),
            "freeze_count": card.freeze_count, "frozen_days_total": card.frozen_days_total,
            "transfer_count": card.transfer_count, "last_used_at": aware(card.last_used_at),
            "holder_name": card.holder_name, "holder_phone": card.holder_phone_masked, "paid_at": aware(card.paid_at),
            "created_at": aware(card.created_at)}


def ledger_dict(e: CardLedger) -> dict:
    return {"id": e.id, "card_id": e.card_id, "user_id": e.user_id, "kind": e.kind, "delta_sessions": e.delta_sessions,
            "delta_days": e.delta_days, "sessions_after": e.sessions_after, "ends_at_after": aware(e.ends_at_after),
            "status_after": e.status_after, "ref": e.ref, "store_id": e.store_id, "operator": e.operator,
            "note": e.note, "created_at": aware(e.created_at)}


def usage_dict(u: CardUsage) -> dict:
    return {"id": u.id, "card_id": u.card_id, "ref": u.ref, "store_id": u.store_id, "units": u.units,
            "sessions_after": u.sessions_after, "status": u.status, "used_at": aware(u.used_at),
            "reversed_at": aware(u.reversed_at)}


# ================================================================ 购卡与发卡


def order_dict(o: CardOrder, pay_params: dict | None = None) -> dict:
    data = {"id": o.id, "order_no": o.order_no, "kind": o.kind, "user_id": o.user_id, "store_id": o.store_id,
            "product_id": o.product_id, "card_id": o.card_id, "amount_cents": o.amount_cents, "status": o.status,
            "payment_id": o.payment_id, "paid_at": aware(o.paid_at), "expires_at": aware(o.expires_at),
            "needs_manual": o.needs_manual, "goods_kind": o.goods_kind,
            "invoice": {"tax_code": o.invoice_tax_code, "note": o.invoice_note, "special_invoice_allowed": False},
            "created_at": aware(o.created_at)}
    if pay_params is not None:
        data["pay_params"] = pay_params
    return data


def _holder_units(db: Session, user_id: int, kind: str, at: datetime) -> int:
    """该会员同类卡的未兑付单位（续约后上限校验）：次卡剩余次数，期限卡剩余天数。"""
    cards = db.execute(select(MemberCard).where(MemberCard.user_id == user_id,
                                                MemberCard.status.in_((S_INACTIVE, S_ACTIVE, S_FROZEN, S_LOST))))\
        .scalars().all()
    total = 0
    for card in cards:
        if _category(card.kind) != _category(kind):
            continue
        if card.kind == KIND_COUNT:
            total += card.sessions_remaining
        elif card.status == S_INACTIVE or card.ends_at is None:
            total += card.total_units
        else:
            total += _remaining_days(card, at) or 0
    return total


def _check_purchase(db: Session, product: CardProduct, user_id: int, store_id: int | None, amount: int,
                    at: datetime) -> None:
    if product.per_member_limit:
        held = db.execute(select(func.count()).select_from(MemberCard).where(
            MemberCard.user_id == user_id, MemberCard.product_id == product.id,
            MemberCard.status.notin_((S_REFUNDED,)))).scalar_one()
        pending = db.execute(select(func.count()).select_from(CardOrder).where(
            CardOrder.user_id == user_id, CardOrder.product_id == product.id, CardOrder.kind == ORDER_PURCHASE,
            CardOrder.status == O_PENDING, CardOrder.expires_at > at)).scalar_one()
        if held + pending >= product.per_member_limit:
            raise _err(ErrorCode.LIMIT_REACHED, "per_member_limit", "已达该卡种购买上限", limit=product.per_member_limit)
    policy = policy_for(store_id)
    paid, gift = (product.sessions, product.gift_sessions) if product.kind == KIND_COUNT else (product.period_days, product.gift_days)
    policy.check_card(_category(product.kind), product.price_cents, paid + gift)
    policy.check_card_after_renewal(_category(product.kind), _holder_units(db, user_id, product.kind, at), paid + gift)
    if amount > 0:
        policy.check_member_total(db, user_id, amount)


def create_order(db: Session, user_id: int, product_id: int, store_id: int | None, at: datetime | None = None) -> dict:
    """顾客购卡：校验在售、门店、限购、预付限额 → 购卡单 + 支付单（biz_type=member_card，goods_kind=offline_service）。"""
    at = at or utcnow()
    if cfg().stop_prepaid:
        raise _err(ErrorCode.BUSINESS_RULE, "prepaid_stopped", "暂停收取预付款")
    product = db.execute(products_on_sale_stmt(None, at).where(CardProduct.id == product_id)).scalar_one_or_none()
    if product is None:
        raise AppError(ErrorCode.NOT_FOUND, "card product not found or not on sale")
    if product.store_scope == SCOPE_SINGLE and store_id is None:
        store_id = json.loads(product.store_ids_json)[0]
    if store_id is not None and not _applies_to(product.store_scope, product.store_ids_json, store_id):
        raise _err(ErrorCode.BUSINESS_RULE, "store_not_applicable", "该门店不售此卡")
    if product.price_cents <= 0:
        raise _err(ErrorCode.BUSINESS_RULE, "free_product", "免费卡种只能由门店发放")
    _check_purchase(db, product, user_id, store_id, product.price_cents, at)
    order = CardOrder(order_no=_no("MO"), kind=ORDER_PURCHASE, user_id=user_id, store_id=store_id, product_id=product.id,
                      amount_cents=product.price_cents, status=O_PENDING,
                      expires_at=at + timedelta(minutes=cfg().pay_timeout_minutes),
                      product_snapshot_json=json.dumps(product_dict(product), default=str), goods_kind=GOODS_KIND,
                      invoice_tax_code="601", invoice_note="预付卡销售和充值 不征税")
    db.add(order)
    db.flush()
    return order_dict(order)


def payable(db: Session, user_id: int, order_id: int) -> dict:
    """支付模块的 Payable 解析：金额只来自购卡单 / 转让手续费单。他人 4040，不可支付 4090。"""
    order = db.get(CardOrder, order_id)
    if order is None or order.user_id != user_id:
        raise AppError(ErrorCode.NOT_FOUND, "order not found")
    if order.status != O_PENDING or (aware(order.expires_at) and aware(order.expires_at) <= utcnow()):
        raise AppError(ErrorCode.CONFLICT, "order is not payable", {"status": order.status})
    name = json.loads(order.product_snapshot_json or "{}").get("name", "")
    desc = "会员卡转让手续费" if order.kind == ORDER_TRANSFER_FEE else f"会员卡 {name}".strip()
    return {"amount_cents": order.amount_cents, "description": desc[:120], "expires_at": aware(order.expires_at),
            "store_id": order.store_id, "goods_kind": GOODS_KIND, "business_no": order.order_no}


def start_payment(db: Session, user_id: int, order_id: int) -> dict:
    """为已提交的购卡单 / 手续费单调起支付。支付模块自行 commit（含渠道调用），所以必须在业务事务提交后调用；
    本函数提交支付单号回写。可重复调用（支付模块按业务单复用 / 换单）。"""
    data = payable(db, user_id, order_id)
    db.commit()
    pay = payment_port().create_payment(db, biz_type=BIZ_TYPE, biz_id=order_id, user_id=user_id, store_id=data["store_id"],
                                        amount_cents=data["amount_cents"], description=data["description"],
                                        goods_kind=GOODS_KIND, expires_at=data["expires_at"], business_no=data["business_no"])
    db.execute(update(CardOrder).where(CardOrder.id == order_id)
               .values(payment_id=pay.get("payment_id") or None, out_trade_no=pay.get("out_trade_no")))
    db.commit()
    return order_dict(db.get(CardOrder, order_id), pay.get("pay_params") or {})


def _start_for(db: Session, product: CardProduct, user_id: int, at: datetime) -> datetime:
    start = at
    if product.activation_mode == ACT_ON_DATE and product.activation_date and aware(product.activation_date) > at:
        start = aware(product.activation_date)
    if product.renew_stack and product.kind != KIND_COUNT:
        latest = db.execute(select(func.max(MemberCard.ends_at)).where(
            MemberCard.user_id == user_id, MemberCard.product_id == product.id,
            MemberCard.status.in_((S_ACTIVE, S_FROZEN)))).scalar_one_or_none()
        if latest is not None and aware(latest) > start:
            start = aware(latest)
    return start


def _ends_for(kind: str, start: datetime, total_days: int, valid_days: int | None, tz: str) -> datetime | None:
    if kind == KIND_COUNT:
        return local_midnight_after(start, valid_days, tz) if valid_days else None
    return local_midnight_after(start, total_days, tz)


def issue_card(db: Session, product: CardProduct, user_id: int, *, store_id: int | None, source: str, issue_ref: str,
               paid_cents: int, paid_at: datetime | None = None, order_id: int | None = None,
               payment_id: str | None = None, operator: str = "system", note: str | None = None,
               at: datetime | None = None) -> MemberCard:
    """发卡（幂等：issue_ref 唯一）。激活方式：购买即生效 / 首次使用激活 / 指定日期；期限卡续购接在最晚到期之后。"""
    at = at or utcnow()
    found = db.execute(select(MemberCard).where(MemberCard.issue_ref == issue_ref)).scalar_one_or_none()
    if found is not None:
        return found
    paid, gift = (product.sessions, product.gift_sessions) if product.kind == KIND_COUNT else (product.period_days, product.gift_days)
    tz = store_timezone(db, store_id)
    member = find_member(db, user_id=user_id)
    phone = member.phone if member else None
    snapshot = product_dict(product)
    card = MemberCard(card_no=_card_no(), user_id=user_id, product_id=product.id, store_id=store_id, kind=product.kind,
                      name=product.name, source=source, issue_ref=issue_ref, order_id=order_id, payment_id=payment_id,
                      paid_cents=paid_cents, original_price_cents=product.original_price_cents if paid_cents else 0,
                      paid_at=paid_at, lpr_bp=cfg().lpr_1y_bp, paid_units=paid, gift_units=gift, total_units=paid + gift,
                      sessions_remaining=paid + gift if product.kind == KIND_COUNT else 0,
                      store_scope=product.store_scope, store_ids_json=product.store_ids_json,
                      slot_windows_json=product.slot_windows_json, entry_cooldown_minutes=product.entry_cooldown_minutes,
                      holder_name=(member.name if member else "")[:64], holder_phone_masked=mask_phone(phone) or "",
                      holder_phone_hash=phone_lookup_hash(phone) if phone else None,
                      product_snapshot_json=json.dumps(snapshot, default=str), terms_snapshot=product.terms_text)
    if product.store_scope == SCOPE_SINGLE and store_id is None:
        card.store_id = json.loads(product.store_ids_json)[0]
    if product.activation_mode == ACT_ON_FIRST_USE:
        card.status = S_INACTIVE
        if product.activation_deadline_days:
            card.activation_deadline_at = at + timedelta(days=product.activation_deadline_days)
    else:
        start = _start_for(db, product, user_id, at)
        card.status = S_ACTIVE
        card.starts_at = start
        card.activated_at = at
        card.ends_at = _ends_for(product.kind, start, paid + gift, product.valid_days, tz)
    db.add(card)
    try:
        db.flush()
    except IntegrityError as exc:
        raise AppError(ErrorCode.DUPLICATE_REQUEST, "card is being issued", retry_after=1) from exc
    _ledger(db, card, "issue", issue_ref, delta_sessions=card.sessions_remaining,
            delta_days=(paid + gift) if product.kind != KIND_COUNT else 0, operator=operator, note=note, store_id=store_id)
    enqueue_event(db, "member_card.issued", {"card_id": card.id, "user_id": user_id, "product_id": product.id,
                                             "source": source, "store_id": card.store_id, "paid_cents": paid_cents},
                  "member_card", card.id)
    return card


def issue_manual(db: Session, staff_id: int, product_id: int, user_id: int, store_id: int | None, source: str,
                 paid_cents: int, reason: str, ref: str, at: datetime | None = None) -> dict:
    """后台发卡（线下收款 manual）/ 赠卡（gift，实付 0）。原因必填，写审计。"""
    at = at or utcnow()
    if source not in (SRC_MANUAL, SRC_GIFT):
        raise AppError(ErrorCode.BAD_REQUEST, "source must be manual or gift")
    if not reason.strip():
        raise AppError(ErrorCode.BAD_REQUEST, "reason is required")
    if source == SRC_GIFT and paid_cents:
        raise AppError(ErrorCode.BAD_REQUEST, "gift cards have no payment")
    product = get_product(db, product_id)
    if find_member(db, user_id=user_id) is None:
        raise AppError(ErrorCode.NOT_FOUND, "member not found")
    if store_id is not None and not _applies_to(product.store_scope, product.store_ids_json, store_id):
        raise _err(ErrorCode.BUSINESS_RULE, "store_not_applicable", "该门店不适用此卡")
    if paid_cents > 0:
        if cfg().stop_prepaid:
            raise _err(ErrorCode.BUSINESS_RULE, "prepaid_stopped", "暂停收取预付款")
        _check_purchase(db, product, user_id, store_id, paid_cents, at)
    card = issue_card(db, product, user_id, store_id=store_id, source=source, issue_ref=ref, paid_cents=paid_cents,
                      paid_at=at if paid_cents else None, operator=f"staff:{staff_id}", note=reason, at=at)
    write_audit(db, "staff", staff_id, f"member_card.issue.{source}", "member_card", card.id,
                after={"product_id": product_id, "user_id": user_id, "paid_cents": paid_cents}, reason=reason)
    return card_dict(card, at)


def issue_gift(db: Session, user_id: int, product_id: int, ref: str, store_id: int | None = None) -> MemberCard:
    """供活动模块调用的赠卡（幂等 ref）。"""
    return issue_card(db, get_product(db, product_id), user_id, store_id=store_id, source=SRC_GIFT, issue_ref=f"gift:{ref}",
                      paid_cents=0)


def on_payment_succeeded(db: Session, event: PaidEvent) -> str:
    if event.biz_type != BIZ_TYPE:
        return "ignored"
    if event.needs_manual:  # 支付模块已转人工 / 自动退款（关单后支付、重复支付、钩子拒绝）：不发卡
        return "manual"
    order = db.get(CardOrder, event.biz_id)
    if order is None:
        return "unknown"
    if order.status == O_PAID:
        return "duplicate"
    if event.amount_cents != order.amount_cents:
        db.execute(update(CardOrder).where(CardOrder.id == order.id).values(needs_manual=True))
        enqueue_event(db, "member_card.payment_mismatch", {"order_id": order.id, "expected_cents": order.amount_cents,
                                                           "paid_cents": event.amount_cents}, "member_card_order", order.id)
        return "mismatch"
    result = db.execute(update(CardOrder).where(CardOrder.id == order.id, CardOrder.status.in_((O_PENDING, O_CLOSED)))
                        .values(status=O_PAID, paid_at=event.paid_at, payment_id=event.payment_id or order.payment_id,
                                out_trade_no=event.out_trade_no or order.out_trade_no))
    if result.rowcount != 1:
        return "duplicate"
    db.refresh(order)
    if order.kind == ORDER_TRANSFER_FEE:
        return _transfer_after_fee(db, order, strict=event.from_hook)
    product = db.get(CardProduct, order.product_id)
    issue_card(db, product, order.user_id, store_id=order.store_id, source=SRC_PURCHASE, issue_ref=f"order:{order.id}",
               paid_cents=order.amount_cents, paid_at=event.paid_at, order_id=order.id, payment_id=order.payment_id,
               at=event.paid_at)
    return "issued"


def first_time(db: Session, consumer: str, event_id: str | None) -> bool:
    """事件去重（不回滚会话）：已处理返回 False；否则登记并返回 True。并发重复由唯一约束裁决（抛错 → worker 重试）。"""
    if not event_id:
        return True
    from luzback.outbox import ProcessedEvent  # noqa: PLC0415
    seen = db.execute(select(ProcessedEvent.id).where(ProcessedEvent.consumer == consumer,
                                                      ProcessedEvent.event_id == event_id)).scalar_one_or_none()
    if seen is not None:
        return False
    db.add(ProcessedEvent(consumer=consumer, event_id=event_id))
    db.flush()
    return True


def on_payment_event(db: Session, payload: dict, event_id: str | None = None) -> str:
    """订阅 payment.succeeded（不提交）。"""
    paid = parse_paid(payload)
    if paid is None or paid.biz_type != BIZ_TYPE:
        return "ignored"
    if not first_time(db, f"{CONSUMER}.paid", event_id):
        return "duplicate"
    return on_payment_succeeded(db, paid)


def _on_refund_event(topic: str):
    def handler(db: Session, payload: dict, event_id: str | None = None) -> str:
        result = parse_refund_result(topic, payload)
        if result is None or (result.biz_type and result.biz_type != BIZ_TYPE):
            return "ignored"
        if not first_time(db, f"{CONSUMER}.{topic}", event_id):
            return "duplicate"
        return on_refund_result(db, result)
    return handler


on_refund_succeeded_event = _on_refund_event("refund.succeeded")
on_refund_failed_event = _on_refund_event("refund.failed")
on_refund_abnormal_event = _on_refund_event("refund.abnormal")
EVENT_HANDLERS = {"payment.succeeded": on_payment_event, "refund.succeeded": on_refund_succeeded_event,
                  "refund.failed": on_refund_failed_event, "refund.abnormal": on_refund_abnormal_event}


def handle_event(db: Session, topic: str, payload: dict, event_id: str | None = None) -> str:
    """便捷入口（测试 / 手工重放）：分发到订阅处理函数并提交事务。worker 走 events.SUBSCRIPTIONS（不提交）。"""
    handler = EVENT_HANDLERS.get(topic)
    if handler is None:
        return "ignored"
    try:
        outcome = handler(db, payload, event_id)
        db.commit()
        return outcome
    except Exception:
        db.rollback()
        raise


def close_expired_orders(db: Session, at: datetime | None = None, limit: int = 500) -> int:
    at = at or utcnow()
    ids = db.execute(select(CardOrder.id).where(CardOrder.status == O_PENDING, CardOrder.expires_at <= at)
                     .order_by(CardOrder.id).limit(limit)).scalars().all()
    closed = 0
    for order_id in ids:
        if db.execute(update(CardOrder).where(CardOrder.id == order_id, CardOrder.status == O_PENDING)
                      .values(status=O_CLOSED)).rowcount == 1:
            closed += 1
            enqueue_event(db, "member_card.order_closed", {"order_id": order_id, "biz_type": BIZ_TYPE},
                          "member_card_order", order_id)
    return closed


# ================================================================ 使用与冲正


def _activate(db: Session, card: MemberCard, at: datetime, operator: str, ref: str) -> None:
    snap = _snapshot(card)
    tz = store_timezone(db, card.store_id)
    ends = _ends_for(card.kind, at, card.total_units, snap.get("valid_days"), tz)
    result = db.execute(update(MemberCard).where(MemberCard.id == card.id, MemberCard.status == S_INACTIVE)
                        .values(status=S_ACTIVE, starts_at=at, ends_at=ends, activated_at=at,
                                version=MemberCard.version + 1, updated_at=utcnow()))
    db.refresh(card)
    if result.rowcount == 1:
        _ledger(db, card, "activate", ref, delta_days=card.total_units if card.kind != KIND_COUNT else 0, operator=operator)
        enqueue_event(db, "member_card.activated", {"card_id": card.id, "user_id": card.user_id}, "member_card", card.id)


def check_usable(db: Session, card: MemberCard, store_id: int | None, at: datetime) -> None:
    """按顺序检查：状态 → 未到开始时间 → 过期 → 门店 → 时段 → 防借卡间隔。每种失败有独立的 data.reason。"""
    if card.status in REASONS:
        code, reason, message = REASONS[card.status]
        raise _err(code, reason, message)
    if card.status == S_ACTIVE and card.starts_at is not None and aware(card.starts_at) > at:
        raise _err(ErrorCode.BUSINESS_RULE, "card_not_activated", "卡尚未到生效时间", starts_at=aware(card.starts_at))
    if card.ends_at is not None and aware(card.ends_at) <= at:
        raise _err(ErrorCode.BUSINESS_RULE, "card_expired", "卡已过期")
    if not _applies_to(card.store_scope, card.store_ids_json, store_id):
        raise _err(ErrorCode.BUSINESS_RULE, "store_not_applicable", "该卡不适用于本门店")
    if card.kind == KIND_TIME_SLOT and not in_windows(at, card.slot_windows_json, store_timezone(db, store_id)):
        raise _err(ErrorCode.STORE_CLOSED, "outside_time_slot", "不在该卡可用时段")
    if card.kind != KIND_COUNT and card.entry_cooldown_minutes and card.last_used_at is not None:
        if aware(card.last_used_at) > at - timedelta(minutes=card.entry_cooldown_minutes):
            raise _err(ErrorCode.BUSINESS_RULE, "entry_cooldown", "使用过于频繁", minutes=card.entry_cooldown_minutes)


def consume(db: Session, *, store_id: int | None, ref: str, card_id: int | None = None, card_no: str | None = None,
            user_id: int | None = None, operator: str = "system", at: datetime | None = None) -> dict:
    """使用一次（入场 / 上课）。幂等：同 ref 返回首次结果；次卡条件扣减不为负；期限卡检查有效期与时段。"""
    at = at or utcnow()
    existing = db.execute(select(CardUsage).where(CardUsage.ref == ref)).scalar_one_or_none()
    card = get_card(db, card_id, user_id) if card_id is not None else get_card_by_no(db, card_no or "")
    if user_id is not None and card.user_id != user_id:
        raise AppError(ErrorCode.NOT_FOUND, "card not found")
    if existing is not None:
        if existing.card_id != card.id:
            raise AppError(ErrorCode.CONFLICT, "ref already used by another card")
        return usage_dict(existing) | {"replayed": True}
    if card.status == S_INACTIVE:
        if not _applies_to(card.store_scope, card.store_ids_json, store_id):
            raise _err(ErrorCode.BUSINESS_RULE, "store_not_applicable", "该卡不适用于本门店")
        _activate(db, card, at, operator, f"activate:{card.id}")
    check_usable(db, card, store_id, at)
    common = (MemberCard.id == card.id, MemberCard.status == S_ACTIVE,
              or_(MemberCard.starts_at.is_(None), MemberCard.starts_at <= at),
              or_(MemberCard.ends_at.is_(None), MemberCard.ends_at > at))
    values = dict(use_count=MemberCard.use_count + 1, last_used_at=at, last_used_store_id=store_id,
                  version=MemberCard.version + 1, updated_at=utcnow())
    if card.kind == KIND_COUNT:
        result = db.execute(update(MemberCard).where(*common, MemberCard.sessions_remaining >= 1).values(
            sessions_remaining=MemberCard.sessions_remaining - 1, sessions_used=MemberCard.sessions_used + 1, **values))
    else:
        conditions = common
        if card.entry_cooldown_minutes:
            conditions += (or_(MemberCard.last_used_at.is_(None),
                               MemberCard.last_used_at <= at - timedelta(minutes=card.entry_cooldown_minutes)),)
        result = db.execute(update(MemberCard).where(*conditions).values(**values))
    if result.rowcount != 1:
        db.refresh(card)
        check_usable(db, card, store_id, at)
        raise _err(ErrorCode.CONFLICT, "card_changed", "卡状态已变化，请重试")
    db.refresh(card)
    if card.kind == KIND_COUNT and card.sessions_remaining == 0:
        db.execute(update(MemberCard).where(MemberCard.id == card.id, MemberCard.status == S_ACTIVE,
                                            MemberCard.sessions_remaining == 0).values(status=S_USED_UP))
        db.refresh(card)
    usage = CardUsage(card_id=card.id, user_id=card.user_id, store_id=store_id, ref=ref,
                      units=1 if card.kind == KIND_COUNT else 0, sessions_after=card.sessions_remaining,
                      operator=operator[:32], used_at=at)
    db.add(usage)
    try:
        db.flush()
    except IntegrityError as exc:  # 同 ref 并发：本事务整体回滚
        raise AppError(ErrorCode.DUPLICATE_REQUEST, "same ref is being processed", retry_after=1) from exc
    _ledger(db, card, "use", ref, delta_sessions=-usage.units, operator=operator, store_id=store_id)
    enqueue_event(db, "member_card.used", {"card_id": card.id, "user_id": card.user_id, "store_id": store_id,
                                           "ref": ref, "sessions_remaining": card.sessions_remaining},
                  "member_card", card.id)
    return usage_dict(usage)


def reverse_use(db: Session, ref: str, reason: str, operator: str = "system", at: datetime | None = None) -> dict:
    """冲正一次使用（如闸机开门失败）。幂等：已冲正返回原记录。退款中 / 已退的卡不可冲正。"""
    at = at or utcnow()
    usage = db.execute(select(CardUsage).where(CardUsage.ref == ref)).scalar_one_or_none()
    if usage is None:
        raise AppError(ErrorCode.NOT_FOUND, "usage not found")
    if usage.status == "reversed":
        return usage_dict(usage) | {"replayed": True}
    card = get_card(db, usage.card_id)
    if card.status in (S_REFUNDING, S_REFUNDED):
        raise _err(ErrorCode.CONFLICT, "card_refunding", "退款中或已退的卡不能冲正")
    if db.execute(update(CardUsage).where(CardUsage.id == usage.id, CardUsage.status == "done")
                  .values(status="reversed", reversed_at=at, reverse_reason=reason[:200])).rowcount != 1:
        db.refresh(usage)
        return usage_dict(usage) | {"replayed": True}
    values = dict(use_count=MemberCard.use_count - 1, version=MemberCard.version + 1, updated_at=utcnow())
    if usage.units:
        values |= dict(sessions_remaining=MemberCard.sessions_remaining + usage.units,
                       sessions_used=MemberCard.sessions_used - usage.units)
    db.execute(update(MemberCard).where(MemberCard.id == card.id, MemberCard.status.notin_((S_REFUNDING, S_REFUNDED)))
               .values(**values))
    db.execute(update(MemberCard).where(MemberCard.id == card.id, MemberCard.status == S_USED_UP,
                                        MemberCard.sessions_remaining > 0).values(status=S_ACTIVE))
    db.refresh(usage)
    _ledger(db, card, "reverse", ref, delta_sessions=usage.units, operator=operator, note=reason, store_id=usage.store_id)
    enqueue_event(db, "member_card.use_reversed", {"card_id": card.id, "ref": ref}, "member_card", card.id)
    return usage_dict(usage)


# ================================================================ 冻结（请假）


def freeze_dict(f: CardFreeze) -> dict:
    return {"id": f.id, "card_id": f.card_id, "start_at": aware(f.start_at), "planned_end_at": aware(f.planned_end_at),
            "planned_days": f.planned_days, "actual_end_at": aware(f.actual_end_at), "actual_days": f.actual_days,
            "status": f.status, "reason": f.reason, "operator": f.operator}


def freeze(db: Session, card: MemberCard, days: int, reason: str, operator: str, at: datetime | None = None) -> dict:
    """冻结：天数在 [最小, 最大] 内、次数与累计天数不超过上限；冻结期间不可使用，到期日顺延冻结天数。"""
    at = at or utcnow()
    snap = _snapshot(card)
    if not snap.get("freezable", True):
        raise _err(ErrorCode.BUSINESS_RULE, "freeze_not_allowed", "该卡不支持冻结")
    if card.status != S_ACTIVE:
        raise _err(ErrorCode.CONFLICT, "card_status", "当前状态不能冻结", status=card.status)
    if card.ends_at is None:
        raise _err(ErrorCode.BUSINESS_RULE, "freeze_not_applicable", "无有效期的卡无需冻结")
    if card.starts_at is not None and aware(card.starts_at) > at:
        raise _err(ErrorCode.BUSINESS_RULE, "card_not_activated", "卡尚未生效")
    if aware(card.ends_at) <= at:
        raise _err(ErrorCode.BUSINESS_RULE, "card_expired", "卡已过期")
    fmin, fmax = snap.get("freeze_min_days", 7), snap.get("freeze_max_days", 30)
    if days < fmin or days > fmax:
        raise _err(ErrorCode.BUSINESS_RULE, "freeze_days_out_of_range", "冻结天数不在允许范围", min=fmin, max=fmax)
    max_times, max_total = snap.get("freeze_max_times", 2), snap.get("freeze_total_max_days", 60)
    if card.freeze_count >= max_times:
        raise _err(ErrorCode.LIMIT_REACHED, "freeze_times_exceeded", "冻结次数已达上限", max=max_times)
    if card.frozen_days_total + days > max_total:
        raise _err(ErrorCode.LIMIT_REACHED, "freeze_days_exceeded", "累计冻结天数超过上限", max=max_total,
                   used=card.frozen_days_total)
    new_ends = aware(card.ends_at) + timedelta(days=days)
    _cas(db, card, (MemberCard.status == S_ACTIVE, MemberCard.freeze_count < max_times,
                    MemberCard.frozen_days_total + days <= max_total),
         status=S_FROZEN, prev_status=S_ACTIVE, freeze_count=MemberCard.freeze_count + 1,
         frozen_days_total=MemberCard.frozen_days_total + days, ends_at=new_ends)
    record = CardFreeze(card_id=card.id, user_id=card.user_id, start_at=at, planned_end_at=at + timedelta(days=days),
                        planned_days=days, reason=reason[:200], operator=operator[:32])
    db.add(record)
    db.flush()
    _ledger(db, card, "freeze", f"freeze:{record.id}", delta_days=days, operator=operator, note=reason)
    enqueue_event(db, "member_card.frozen", {"card_id": card.id, "user_id": card.user_id, "days": days,
                                             "until": record.planned_end_at}, "member_card", card.id)
    return freeze_dict(record) | {"card": card_dict(card, at)}


def unfreeze(db: Session, card: MemberCard, operator: str, at: datetime | None = None, auto: bool = False) -> dict:
    """解冻：按实际冻结天数（不足一天按一天，至少 1 天）重算到期日，提前解冻退回未用的冻结天数。"""
    at = at or utcnow()
    if card.status != S_FROZEN:
        raise _err(ErrorCode.CONFLICT, "card_status", "卡不在冻结中", status=card.status)
    record = db.execute(select(CardFreeze).where(CardFreeze.card_id == card.id, CardFreeze.status == "active")
                        .order_by(CardFreeze.id.desc())).scalars().first()
    if record is None:
        raise AppError(ErrorCode.CONFLICT, "no active freeze")
    planned_end = aware(record.planned_end_at)
    if auto or at >= planned_end:
        actual, end_at = record.planned_days, min(at, planned_end) if not auto else planned_end
    else:
        actual, end_at = max(1, min(record.planned_days, ceil_days((at - aware(record.start_at)).total_seconds()))), at
    unused = record.planned_days - actual
    if db.execute(update(CardFreeze).where(CardFreeze.id == record.id, CardFreeze.status == "active")
                  .values(status="ended", actual_end_at=end_at, actual_days=actual)).rowcount != 1:
        raise AppError(ErrorCode.CONFLICT, "freeze changed concurrently")
    restore = card.prev_status or S_ACTIVE
    _cas(db, card, (MemberCard.status == S_FROZEN,), status=restore, prev_status=None,
         ends_at=aware(card.ends_at) - timedelta(days=unused), frozen_days_total=MemberCard.frozen_days_total - unused)
    _ledger(db, card, "unfreeze", f"unfreeze:{record.id}", delta_days=-unused, operator=operator,
            note="auto" if auto else None)
    enqueue_event(db, "member_card.unfrozen", {"card_id": card.id, "user_id": card.user_id, "actual_days": actual,
                                               "auto": auto}, "member_card", card.id)
    db.refresh(record)
    return freeze_dict(record) | {"card": card_dict(card, at)}


# ================================================================ 转让


def transfer_dict(t: CardTransfer) -> dict:
    return {"id": t.id, "card_id": t.card_id, "from_user_id": t.from_user_id, "to_user_id": t.to_user_id,
            "fee_cents": t.fee_cents, "fee_collected": t.fee_collected, "sessions_remaining": t.sessions_remaining,
            "ends_at": aware(t.ends_at), "operator": t.operator, "created_at": aware(t.created_at)}


def _check_transfer(db: Session, card: MemberCard, to_user_id: int, at: datetime) -> dict:
    snap = _snapshot(card)
    if not snap.get("transferable", True):
        raise _err(ErrorCode.BUSINESS_RULE, "transfer_not_allowed", "该卡不支持转让")
    if card.status not in (S_ACTIVE, S_INACTIVE):
        raise _err(ErrorCode.CONFLICT, "card_status", "当前状态不能转让", status=card.status)
    if card.ends_at is not None and aware(card.ends_at) <= at:
        raise _err(ErrorCode.BUSINESS_RULE, "card_expired", "卡已过期")
    max_transfers = snap.get("max_transfers", 1)
    if card.transfer_count >= max_transfers:
        raise _err(ErrorCode.LIMIT_REACHED, "transfer_times_exceeded", "转让次数已达上限", max=max_transfers)
    cooling = snap.get("transfer_cooling_days", 0)
    since = aware(card.last_transfer_at) or aware(card.paid_at) or aware(card.created_at)
    if cooling and since and at < since + timedelta(days=cooling):
        raise _err(ErrorCode.BUSINESS_RULE, "transfer_cooling", "转让冷却期内", available_at=since + timedelta(days=cooling))
    if to_user_id == card.user_id:
        raise _err(ErrorCode.BUSINESS_RULE, "transfer_to_self", "不能转让给自己")
    return snap


def _outstanding_value(card: MemberCard, at: datetime) -> int:
    if not card.paid_cents or not card.total_units:
        return 0
    if card.kind == KIND_COUNT:
        remaining = card.sessions_remaining
    elif card.status == S_INACTIVE or card.ends_at is None:
        remaining = card.total_units
    else:
        remaining = min(card.total_units, _remaining_days(card, at) or 0)
    return card.paid_cents * remaining // card.total_units


def _execute_transfer(db: Session, card: MemberCard, from_user: int, to_user: int, *, fee: int, fee_collected: str,
                      operator: str, reason: str | None, at: datetime, fee_order_id: int | None = None) -> dict:
    member = find_member(db, user_id=to_user)
    if member is None:
        raise AppError(ErrorCode.NOT_FOUND, "member not found")
    snap = _snapshot(card)
    phone = member.phone
    result = db.execute(update(MemberCard).where(
        MemberCard.id == card.id, MemberCard.user_id == from_user, MemberCard.status.in_((S_ACTIVE, S_INACTIVE)),
        MemberCard.transfer_count < snap.get("max_transfers", 1),
    ).values(user_id=to_user, transfer_count=MemberCard.transfer_count + 1, last_transfer_at=at,
             holder_name=member.name[:64], holder_phone_masked=mask_phone(phone) or "",
             holder_phone_hash=phone_lookup_hash(phone) if phone else None, version=MemberCard.version + 1,
             updated_at=utcnow()))
    if result.rowcount != 1:
        raise _err(ErrorCode.CONFLICT, "card_changed", "卡状态已变化，请重试")
    db.refresh(card)
    record = CardTransfer(card_id=card.id, from_user_id=from_user, to_user_id=to_user, fee_cents=fee,
                          fee_order_id=fee_order_id, fee_collected=fee_collected, sessions_remaining=card.sessions_remaining,
                          ends_at=card.ends_at, operator=operator[:32], reason=(reason or None) and reason[:200], created_at=at)
    db.add(record)
    db.flush()
    _ledger(db, card, "transfer", f"transfer:{record.id}", operator=operator, note=f"{from_user}->{to_user}")
    enqueue_event(db, "member_card.transferred", {"card_id": card.id, "from_user_id": from_user, "to_user_id": to_user,
                                                  "fee_cents": fee}, "member_card", card.id)
    return transfer_dict(record) | {"card": card_dict(card, at)}


def transfer(db: Session, card: MemberCard, *, to_user_id: int | None = None, to_phone: str | None = None,
             operator: str, by_staff: bool = False, fee_collected_offline: bool = False, reason: str | None = None,
             at: datetime | None = None) -> dict:
    """转让给已注册会员。顾客发起且有手续费 → 生成手续费支付单，支付成功后完成；后台发起手续费线下收取。"""
    at = at or utcnow()
    member = find_member(db, user_id=to_user_id, phone=to_phone)
    if member is None:
        raise AppError(ErrorCode.NOT_FOUND, "member not found")
    snap = _check_transfer(db, card, member.user_id, at)
    policy_for(card.store_id).check_member_total(db, member.user_id, _outstanding_value(card, at))
    fee = int(snap.get("transfer_fee_cents") or 0)
    if fee > 0 and not by_staff:
        order = CardOrder(order_no=_no("MT"), kind=ORDER_TRANSFER_FEE, user_id=card.user_id, store_id=card.store_id,
                          product_id=card.product_id, card_id=card.id, target_user_id=member.user_id, amount_cents=fee,
                          status=O_PENDING, expires_at=at + timedelta(minutes=cfg().pay_timeout_minutes),
                          goods_kind=GOODS_KIND, invoice_tax_code=None, invoice_note="转让手续费（应税服务，按实际业务开票）")
        db.add(order)
        db.flush()
        # 手续费单提交后由接口层调用 start_payment 调起支付；支付成功后完成转让
        return {"status": "awaiting_fee_payment", "order": order_dict(order)}
    collected = "offline" if fee and fee_collected_offline else ("none" if not fee else "waived")
    data = _execute_transfer(db, card, card.user_id, member.user_id, fee=fee if fee_collected_offline else 0,
                             fee_collected=collected, operator=operator, reason=reason, at=at)
    return {"status": "transferred", "transfer": data}


def _transfer_after_fee(db: Session, order: CardOrder, strict: bool = False) -> str:
    card = db.get(MemberCard, order.card_id)
    try:
        if card is None or card.user_id != order.user_id:
            raise AppError(ErrorCode.CONFLICT, "card changed")
        _check_transfer(db, card, order.target_user_id, aware(order.paid_at) or utcnow())
        _execute_transfer(db, card, order.user_id, order.target_user_id, fee=order.amount_cents, fee_collected="online",
                          operator=f"user:{order.user_id}", reason=None, at=aware(order.paid_at) or utcnow(),
                          fee_order_id=order.id)
        return "transferred"
    except AppError as exc:
        if strict:
            # 支付模块同事务钩子：抛错 → 支付模块标人工并自动全额退回手续费
            raise
        # 事件通道（无钩子）：已收手续费但无法转让，标人工；手续费由后台在支付模块发起退款
        db.execute(update(CardOrder).where(CardOrder.id == order.id).values(needs_manual=True))
        enqueue_event(db, "member_card.transfer_fee_orphan", {"order_id": order.id, "card_id": order.card_id,
                                                              "payment_id": order.payment_id, "amount_cents": order.amount_cents,
                                                              "reason": exc.message}, "member_card_order", order.id)
        return "transfer_failed"


# ================================================================ 挂失、补办、停用


def report_lost(db: Session, card: MemberCard, operator: str, at: datetime | None = None) -> dict:
    if card.status not in (S_ACTIVE, S_INACTIVE, S_USED_UP, S_EXPIRED):
        raise _err(ErrorCode.CONFLICT, "card_status", "当前状态不能挂失", status=card.status)
    _cas(db, card, (MemberCard.status == card.status,), status=S_LOST, prev_status=card.status)
    _ledger(db, card, "lost", f"lost:{card.id}:{card.version}", operator=operator)
    enqueue_event(db, "member_card.lost", {"card_id": card.id, "user_id": card.user_id}, "member_card", card.id)
    return card_dict(card, at)


def cancel_lost(db: Session, card: MemberCard, operator: str, at: datetime | None = None) -> dict:
    if card.status != S_LOST:
        raise _err(ErrorCode.CONFLICT, "card_status", "卡未挂失", status=card.status)
    _cas(db, card, (MemberCard.status == S_LOST,), status=card.prev_status or S_ACTIVE, prev_status=None)
    _ledger(db, card, "cancel_lost", f"cancel_lost:{card.id}:{card.version}", operator=operator)
    return card_dict(card, at)


def replace_card(db: Session, staff_id: int, card: MemberCard, reason: str, at: datetime | None = None) -> dict:
    """补办：挂失中的记名卡换新卡号，权益（次数、有效期、冻结记录、流水）全部保留。"""
    if card.status != S_LOST:
        raise _err(ErrorCode.CONFLICT, "card_status", "补办前需先挂失", status=card.status)
    old_no = card.card_no
    _cas(db, card, (MemberCard.status == S_LOST,), status=card.prev_status or S_ACTIVE, prev_status=None, card_no=_card_no())
    _ledger(db, card, "replace", f"replace:{card.id}:{card.version}", operator=f"staff:{staff_id}",
            note=f"{old_no} -> {card.card_no}; fee={business_value(db, 'member_card.replace_fee_cents', card.store_id)}")
    write_audit(db, "staff", staff_id, "member_card.replace", "member_card", card.id, before={"card_no": old_no},
                after={"card_no": card.card_no}, reason=reason)
    enqueue_event(db, "member_card.replaced", {"card_id": card.id, "user_id": card.user_id}, "member_card", card.id)
    return card_dict(card, at)


def disable_card(db: Session, staff_id: int, card: MemberCard, reason: str) -> dict:
    if card.status in (S_REFUNDED, S_REFUNDING, S_DISABLED, S_FROZEN):
        raise _err(ErrorCode.CONFLICT, "card_status", "当前状态不能停用", status=card.status)
    before = card.status
    _cas(db, card, (MemberCard.status == before,), status=S_DISABLED, prev_status=before)
    _ledger(db, card, "disable", f"disable:{card.id}:{card.version}", operator=f"staff:{staff_id}", note=reason)
    write_audit(db, "staff", staff_id, "member_card.disable", "member_card", card.id, before={"status": before},
                after={"status": S_DISABLED}, reason=reason)
    return card_dict(card)


def enable_card(db: Session, staff_id: int, card: MemberCard, reason: str) -> dict:
    if card.status != S_DISABLED:
        raise _err(ErrorCode.CONFLICT, "card_status", "卡未停用", status=card.status)
    _cas(db, card, (MemberCard.status == S_DISABLED,), status=card.prev_status or S_ACTIVE, prev_status=None)
    _ledger(db, card, "enable", f"enable:{card.id}:{card.version}", operator=f"staff:{staff_id}", note=reason)
    write_audit(db, "staff", staff_id, "member_card.enable", "member_card", card.id, before={"status": S_DISABLED},
                after={"status": card.status}, reason=reason)
    return card_dict(card)


# ================================================================ 后台调整


def adjust(db: Session, staff_id: int, card: MemberCard, delta_sessions: int, delta_days: int, reason: str, ref: str,
           at: datetime | None = None) -> dict:
    """增减次数 / 天数（计入赠送部分），原因必填，审计；不得为负，不得调整已退 / 退款中的卡。"""
    at = at or utcnow()
    if not reason.strip():
        raise AppError(ErrorCode.BAD_REQUEST, "reason is required")
    if (delta_sessions and card.kind != KIND_COUNT) or (delta_days and card.kind == KIND_COUNT and card.ends_at is None):
        raise AppError(ErrorCode.BAD_REQUEST, "adjustment does not match card kind")
    if not delta_sessions and not delta_days:
        raise AppError(ErrorCode.BAD_REQUEST, "nothing to adjust")
    if abs(delta_sessions) > cfg().adjust_max_sessions or abs(delta_days) > cfg().adjust_max_days:
        raise _err(ErrorCode.LIMIT_REACHED, "adjust_cap", "单次调整超过上限")
    if card.status in (S_REFUNDING, S_REFUNDED, S_INACTIVE) or (delta_days and card.ends_at is None):
        raise _err(ErrorCode.CONFLICT, "card_status", "当前状态不能调整", status=card.status)
    found = db.execute(select(CardLedger).where(CardLedger.ref == ref, CardLedger.kind == "adjust")).scalar_one_or_none()
    if found is not None:
        return card_dict(card, at)
    before = card_dict(card, at)
    values: dict[str, Any] = {"gift_units": MemberCard.gift_units + (delta_sessions or delta_days),
                              "total_units": MemberCard.total_units + (delta_sessions or delta_days)}
    conditions: tuple = (MemberCard.status == card.status,)
    status = card.status
    if delta_sessions:
        new_remaining = card.sessions_remaining + delta_sessions
        if new_remaining < 0:
            raise _err(ErrorCode.INSUFFICIENT_BALANCE, "sessions_negative", "调整后次数不能为负")
        values["sessions_remaining"] = MemberCard.sessions_remaining + delta_sessions
        conditions += (MemberCard.sessions_remaining + delta_sessions >= 0,)
        if status == S_USED_UP and new_remaining > 0:
            status = S_ACTIVE
        elif status == S_ACTIVE and new_remaining == 0:
            status = S_USED_UP
    if delta_days:
        new_ends = aware(card.ends_at) + timedelta(days=delta_days)
        values["ends_at"] = new_ends
        if status == S_EXPIRED and new_ends > at:
            status = S_ACTIVE if (card.kind != KIND_COUNT or card.sessions_remaining > 0) else S_USED_UP
        elif status in (S_ACTIVE, S_USED_UP) and new_ends <= at:
            status = S_EXPIRED
    _cas(db, card, conditions, status=status, **values)
    _ledger(db, card, "adjust", ref, delta_sessions=delta_sessions, delta_days=delta_days, operator=f"staff:{staff_id}",
            note=reason)
    write_audit(db, "staff", staff_id, "member_card.adjust", "member_card", card.id, before=before,
                after=card_dict(card, at), reason=reason)
    return card_dict(card, at)


# ================================================================ 退卡


def refund_dict(r: CardRefund) -> dict:
    return {"id": r.id, "refund_no": r.refund_no, "card_id": r.card_id, "user_id": r.user_id, "cause": r.cause,
            "rule": r.rule, "refund_cents": r.refund_cents, "interest_cents": r.interest_cents, "fee_cents": r.fee_cents,
            "route": r.route, "status": r.status, "reason": r.reason, "requested_by": r.requested_by,
            "approved_by": r.approved_by, "due_at": aware(r.due_at), "finished_at": aware(r.finished_at),
            "fail_reason": r.fail_reason, "submitted_at": aware(r.submitted_at), "channel_refund_id": r.channel_refund_id,
            "submit_error": r.submit_error, "breakdown": json.loads(r.breakdown_json or "{}"),
            "created_at": aware(r.created_at)}


def _route(card: MemberCard, at: datetime) -> str:
    if card.source != SRC_PURCHASE or not card.payment_id or card.transfer_count > 0:
        return "offline"
    if card.paid_at and (at - aware(card.paid_at)).days >= cfg().original_refund_max_days:
        return "offline"
    return "original"


def _used_units(db: Session, card: MemberCard, at: datetime) -> int:
    if card.kind == KIND_COUNT:
        return card.total_units - card.sessions_remaining
    if card.status == S_INACTIVE:
        return 0
    pending = 0
    if card.status == S_FROZEN:
        record = db.execute(select(CardFreeze).where(CardFreeze.card_id == card.id, CardFreeze.status == "active")).scalars().first()
        if record is not None:
            pending = ceil_days((aware(record.planned_end_at) - at).total_seconds())
    return period_used_days(total_units=card.total_units, starts_at=card.starts_at, ends_at=card.ends_at, at=at,
                            pending_freeze_days=pending)


REFUNDABLE = (S_ACTIVE, S_INACTIVE, S_FROZEN, S_USED_UP, S_EXPIRED, S_LOST)


def quote_refund(db: Session, card: MemberCard, cause: str = CAUSE_CUSTOMER, at: datetime | None = None) -> dict:
    at = at or utcnow()
    if cause not in CAUSES:
        raise AppError(ErrorCode.BAD_REQUEST, "cause must be customer or merchant")
    if card.status not in REFUNDABLE:
        raise _err(ErrorCode.CONFLICT, "card_status", "当前状态不能退卡", status=card.status)
    if card.status == S_EXPIRED and cause == CAUSE_CUSTOMER:
        # 法释〔2025〕4号 第二十二条：消费者自身原因未在履行期限内要求提供服务，请求返还的不予支持
        raise _err(ErrorCode.BUSINESS_RULE, "expired_customer_no_refund", "卡已过期，消费者原因不予退款")
    snap = _snapshot(card)
    quote = quote_card_refund(
        paid_cents=card.paid_cents, original_price_cents=card.original_price_cents or card.paid_cents,
        paid_units=card.paid_units, total_units=card.total_units, used_units=_used_units(db, card, at),
        untouched=card.use_count == 0 and card.sessions_used == 0, cause=cause, paid_at=card.paid_at, at=at,
        cooling_off_days=policy_for(card.store_id).cooling_off_days,
        fee_bp=int(snap.get("refund_fee_bp") or 0) if cause == CAUSE_CUSTOMER else 0, lpr_bp=card.lpr_bp)
    quote["card_id"] = card.id
    quote["route"] = _route(card, at)
    return quote


def request_refund(db: Session, card: MemberCard, cause: str, reason: str, actor: str, *, user_id: int | None = None,
                   staff_id: int | None = None, at: datetime | None = None) -> dict:
    """申请退卡：冻结中的卡先按今天结束冻结 → 报价 → 卡进入 refunding（不可使用）→ 待审批；后台发起直接进入处理。"""
    at = at or utcnow()
    if user_id is not None and cause != CAUSE_CUSTOMER:
        raise AppError(ErrorCode.BAD_REQUEST, "customers can only request customer-caused refunds")
    quote = quote_refund(db, card, cause, at)
    if quote["refund_cents"] <= 0 and quote["interest_cents"] <= 0:
        raise _err(ErrorCode.BUSINESS_RULE, "nothing_refundable", "无可退金额", quote=quote)
    if card.status == S_FROZEN:  # 退卡不受冻结限制（不得限制消费者解除合同）：先结束冻结
        unfreeze(db, card, actor, at)
        quote = quote_refund(db, card, cause, at)
    before = card.status
    _cas(db, card, (MemberCard.status == before,), status=S_REFUNDING, prev_status=before)
    if quote["refund_cents"] <= 0:
        quote["route"] = "offline"
    refund = CardRefund(refund_no=_no("MR"), card_id=card.id, user_id=card.user_id, cause=cause, rule=quote["rule"],
                        refund_cents=quote["refund_cents"], interest_cents=quote["interest_cents"],
                        fee_cents=quote["fee_cents"], route=quote["route"], status=R_PENDING,
                        breakdown_json=json.dumps(quote, default=str), reason=reason[:200], requested_by=actor[:32])
    policy = policy_for(card.store_id)
    refund.due_at = at + timedelta(days=policy.cooling_off_refund_days if quote["rule"] == RULE_COOLING_OFF
                                   else policy.refund_deadline_days)
    db.add(refund)
    db.flush()
    _ledger(db, card, "refund_request", refund.refund_no, operator=actor, note=reason)
    enqueue_event(db, "member_card.refund_created", {"refund_id": refund.id, "card_id": card.id, "user_id": card.user_id,
                                                     "refund_cents": refund.refund_cents}, "member_card_refund", refund.id)
    if staff_id is not None:
        write_audit(db, "staff", staff_id, "member_card.refund.create", "member_card_refund", refund.id,
                    after=refund_dict(refund), reason=reason)
        return approve_refund(db, refund.id, staff_id, audit=False)
    if not cfg().refund_requires_approval:
        return _start_refund(db, refund, None)
    return refund_dict(refund)


def _start_refund(db: Session, refund: CardRefund, staff_id: int | None) -> dict:
    if db.execute(update(CardRefund).where(CardRefund.id == refund.id, CardRefund.status == R_PENDING)
                  .values(status=R_PROCESSING, approved_by=staff_id)).rowcount != 1:
        raise AppError(ErrorCode.CONFLICT, "refund status changed", {"status": refund.status})
    db.refresh(refund)
    card = db.get(MemberCard, refund.card_id)
    enqueue_event(db, REFUND_TOPIC, {
        "business_ref": f"{REFUND_PREFIX}:{refund.refund_no}", "biz_type": BIZ_TYPE, "biz_id": card.order_id, "card_id": card.id,
        "refund_id": refund.id, "payment_id": card.payment_id, "amount_cents": refund.refund_cents,
        "interest_cents": refund.interest_cents, "route": refund.route, "reason": refund.reason,
        "user_id": refund.user_id}, "member_card_refund", refund.id)
    return refund_dict(refund)


def get_refund(db: Session, refund_id: int) -> CardRefund:
    refund = db.get(CardRefund, refund_id)
    if refund is None:
        raise AppError(ErrorCode.NOT_FOUND, "refund not found")
    return refund


def approve_refund(db: Session, refund_id: int, staff_id: int, audit: bool = True) -> dict:
    refund = get_refund(db, refund_id)
    data = _start_refund(db, refund, staff_id)
    if audit:
        write_audit(db, "staff", staff_id, "member_card.refund.approve", "member_card_refund", refund_id,
                    before={"status": R_PENDING}, after={"status": R_PROCESSING})
    return data


def _restore_after_refund(db: Session, refund: CardRefund, operator: str, kind: str) -> None:
    card = db.get(MemberCard, refund.card_id)
    db.execute(update(MemberCard).where(MemberCard.id == card.id, MemberCard.status == S_REFUNDING)
               .values(status=card.prev_status or S_ACTIVE, prev_status=None, version=MemberCard.version + 1))
    _ledger(db, card, kind, refund.refund_no, operator=operator)


def reject_refund(db: Session, refund_id: int, staff_id: int, reason: str) -> dict:
    refund = get_refund(db, refund_id)
    if db.execute(update(CardRefund).where(CardRefund.id == refund_id, CardRefund.status == R_PENDING)
                  .values(status=R_REJECTED, fail_reason=reason[:200], approved_by=staff_id, finished_at=utcnow())).rowcount != 1:
        raise AppError(ErrorCode.CONFLICT, "refund status changed")
    db.refresh(refund)
    _restore_after_refund(db, refund, f"staff:{staff_id}", "refund_rejected")
    write_audit(db, "staff", staff_id, "member_card.refund.reject", "member_card_refund", refund_id,
                before={"status": R_PENDING}, after={"status": R_REJECTED}, reason=reason)
    enqueue_event(db, "member_card.refund_rejected", {"refund_id": refund_id, "card_id": refund.card_id},
                  "member_card_refund", refund_id)
    return refund_dict(refund)


def _finish(db: Session, refund: CardRefund, success: bool, reason: str | None, operator: str = "system") -> str:
    target = R_SUCCEEDED if success else R_FAILED
    if db.execute(update(CardRefund).where(CardRefund.id == refund.id, CardRefund.status == R_PROCESSING)
                  .values(status=target, finished_at=utcnow(),
                          fail_reason=None if success else (reason or "")[:200])).rowcount != 1:
        return "duplicate"
    db.refresh(refund)
    if not success:
        _restore_after_refund(db, refund, operator, "refund_failed")
        enqueue_event(db, "member_card.refund_failed", {"refund_id": refund.id, "card_id": refund.card_id,
                                                        "reason": reason}, "member_card_refund", refund.id)
        return "failed"
    card = db.get(MemberCard, refund.card_id)
    remaining = card.sessions_remaining
    db.execute(update(MemberCard).where(MemberCard.id == card.id, MemberCard.status == S_REFUNDING)
               .values(status=S_REFUNDED, prev_status=None, sessions_remaining=0, version=MemberCard.version + 1))
    _ledger(db, card, "refund", refund.refund_no, delta_sessions=-remaining, operator=operator)
    enqueue_event(db, "member_card.refunded", {"refund_id": refund.id, "card_id": card.id, "user_id": card.user_id,
                                               "refund_cents": refund.refund_cents, "interest_cents": refund.interest_cents,
                                               "invoice_action": "red_reverse"}, "member_card_refund", refund.id)
    return "refunded"


def on_refund_result(db: Session, event: RefundResultEvent) -> str:
    refund = db.execute(select(CardRefund).where(CardRefund.refund_no == event.local_ref)).scalar_one_or_none()
    if refund is None:
        return "unknown"
    if event.abnormal:  # 原路退回失败：支付模块后台人工处理异常退款，本单保持处理中
        db.execute(update(CardRefund).where(CardRefund.id == refund.id).values(submit_error="abnormal"))
        enqueue_event(db, "member_card.refund_abnormal", {"refund_id": refund.id, "card_id": refund.card_id},
                      "member_card_refund", refund.id)
        return "abnormal"
    return _finish(db, refund, event.success, event.reason)


def submit_refund(db: Session, refund_id: int) -> dict:
    """把已审批的原路退款交给支付模块（create_refund 自行提交并调渠道，所以必须在业务事务提交后调用）。
    每笔只提交一次：先条件占位并提交；上游暂时失败清除占位等任务重试；规则拒绝 → 退款失败、卡恢复原状态。"""
    refund = db.get(CardRefund, refund_id)
    if refund is None or refund.status != R_PROCESSING or refund.route != "original" or refund.submitted_at:
        return refund_dict(refund) if refund else {}
    claimed = db.execute(update(CardRefund).where(CardRefund.id == refund_id, CardRefund.status == R_PROCESSING,
                                                  CardRefund.submitted_at.is_(None)).values(submitted_at=utcnow()))
    db.commit()
    if claimed.rowcount != 1:
        db.refresh(refund)
        return refund_dict(refund)
    card = db.get(MemberCard, refund.card_id)
    try:
        result = payment_port().create_refund(db, payment_id=card.payment_id, amount_cents=refund.refund_cents,
                                              reason=(refund.reason or "会员卡退卡")[:80],
                                              business_ref=f"{REFUND_PREFIX}:{refund.refund_no}")
    except AppError as exc:
        db.rollback()
        if exc.code in (ErrorCode.UPSTREAM_FAILED, ErrorCode.SERVICE_UNAVAILABLE):
            db.execute(update(CardRefund).where(CardRefund.id == refund_id)
                       .values(submitted_at=None, submit_error=exc.message[:200]))
        else:
            db.refresh(refund)
            _finish(db, refund, False, f"rejected: {exc.message}"[:200])
        db.commit()
        db.refresh(refund)
        return refund_dict(refund)
    db.execute(update(CardRefund).where(CardRefund.id == refund_id)
               .values(channel_refund_id=str(result.get("refund_id") or ""), submit_error=None))
    db.commit()
    db.refresh(refund)
    return refund_dict(refund)


def submit_pending_refunds(db: Session, limit: int = 100) -> int:
    ids = db.execute(select(CardRefund.id).where(CardRefund.status == R_PROCESSING, CardRefund.route == "original",
                                                 CardRefund.submitted_at.is_(None)).order_by(CardRefund.id)
                     .limit(limit)).scalars().all()
    for refund_id in ids:
        submit_refund(db, refund_id)
    return len(ids)


def complete_offline_refund(db: Session, refund_id: int, staff_id: int, success: bool = True, note: str = "") -> dict:
    refund = get_refund(db, refund_id)
    if refund.route != "offline":
        raise AppError(ErrorCode.CONFLICT, "only offline refunds can be confirmed manually")
    if _finish(db, refund, success, note or None, f"staff:{staff_id}") == "duplicate":
        raise AppError(ErrorCode.CONFLICT, "refund status changed")
    write_audit(db, "staff", staff_id, "member_card.refund.offline_complete", "member_card_refund", refund_id,
                after={"status": refund.status}, reason=note or None)
    return refund_dict(refund)


# ================================================================ 定时任务主体


def expire_cards(db: Session, at: datetime | None = None, limit: int = 500) -> int:
    at = at or utcnow()
    rows = db.execute(select(MemberCard).where(MemberCard.status.in_((S_ACTIVE, S_USED_UP)), MemberCard.ends_at.is_not(None),
                                               MemberCard.ends_at <= at).order_by(MemberCard.id).limit(limit)).scalars().all()
    done = 0
    for card in rows:
        before = card.status
        if db.execute(update(MemberCard).where(MemberCard.id == card.id, MemberCard.status == before,
                                               MemberCard.ends_at <= at)
                      .values(status=S_EXPIRED, version=MemberCard.version + 1)).rowcount != 1:
            continue
        _ledger(db, card, "expire", f"expire:{card.id}:{aware(card.ends_at).isoformat()}")
        enqueue_event(db, "member_card.expired", {"card_id": card.id, "user_id": card.user_id}, "member_card", card.id)
        done += 1
    return done


def auto_activate(db: Session, at: datetime | None = None, limit: int = 500) -> int:
    at = at or utcnow()
    rows = db.execute(select(MemberCard).where(MemberCard.status == S_INACTIVE, MemberCard.activation_deadline_at.is_not(None),
                                               MemberCard.activation_deadline_at <= at)
                      .order_by(MemberCard.id).limit(limit)).scalars().all()
    for card in rows:
        _activate(db, card, aware(card.activation_deadline_at), "system", f"activate:{card.id}")
    return len(rows)


def auto_unfreeze(db: Session, at: datetime | None = None, limit: int = 500) -> int:
    at = at or utcnow()
    card_ids = db.execute(select(CardFreeze.card_id).where(CardFreeze.status == "active", CardFreeze.planned_end_at <= at)
                          .order_by(CardFreeze.id).limit(limit)).scalars().all()
    done = 0
    for card_id in card_ids:
        card = db.get(MemberCard, card_id)
        if card is None or card.status != S_FROZEN:
            continue
        try:
            unfreeze(db, card, "system", at, auto=True)
            done += 1
        except AppError:
            continue
    return done


def remind_expiring(db: Session, at: datetime | None = None, limit: int = 1000) -> int:
    """到期提醒：剩余天数落入 7 / 3 / 1 天档时发 member_card.expiring（每张卡每档每个到期日一次）。"""
    at = at or utcnow()
    thresholds = sorted(cfg().reminder_days)
    if not thresholds:
        return 0
    horizon = at + timedelta(days=max(thresholds))
    rows = db.execute(select(MemberCard).where(MemberCard.status == S_ACTIVE, MemberCard.ends_at > at,
                                               MemberCard.ends_at <= horizon).order_by(MemberCard.id).limit(limit)).scalars().all()
    sent = 0
    for card in rows:
        left = ceil_days((aware(card.ends_at) - at).total_seconds())
        tier = min((d for d in thresholds if left <= d), default=None)
        if tier is None:
            continue
        key = aware(card.ends_at).isoformat()[:32]
        exists = db.execute(select(CardReminder.id).where(CardReminder.card_id == card.id, CardReminder.days_before == tier,
                                                          CardReminder.ends_at_key == key)).scalar_one_or_none()
        if exists is not None:
            continue
        db.add(CardReminder(card_id=card.id, days_before=tier, ends_at_key=key))
        enqueue_event(db, "member_card.expiring", {"card_id": card.id, "user_id": card.user_id, "days_left": left,
                                                   "tier": tier, "ends_at": aware(card.ends_at)}, "member_card", card.id)
        sent += 1
    db.flush()
    return sent


def flag_overdue_refunds(db: Session, at: datetime | None = None) -> int:
    at = at or utcnow()
    ids = db.execute(select(CardRefund.id).where(CardRefund.status.in_((R_PENDING, R_PROCESSING)), CardRefund.due_at <= at,
                                                 CardRefund.overdue_notified.is_(False)).limit(500)).scalars().all()
    for refund_id in ids:
        if db.execute(update(CardRefund).where(CardRefund.id == refund_id, CardRefund.overdue_notified.is_(False))
                      .values(overdue_notified=True)).rowcount == 1:
            enqueue_event(db, "member_card.refund_overdue", {"refund_id": refund_id}, "member_card_refund", refund_id)
    return len(ids)


# ================================================================ 预付限额：会员卡未兑付价值


def outstanding_cents(db: Session, user_id: int) -> int:
    at = utcnow()
    cards = db.execute(select(MemberCard).where(MemberCard.user_id == user_id, MemberCard.paid_cents > 0,
                                                MemberCard.status.in_((S_INACTIVE, S_ACTIVE, S_FROZEN, S_LOST, S_REFUNDING))))\
        .scalars().all()
    return sum(_outstanding_value(card, at) for card in cards)


register_outstanding_provider("member_card", outstanding_cents)


# ================================================================ 抽奖奖品：延长会员卡天数（18-lottery）


PRIZE_KIND_DAYS = "member_card_days"


def _ack_prize(db: Session, payload: dict, ok_: bool, ref: str | None, error: str | None) -> None:
    try:
        import lottery  # noqa: PLC0415
        ack = getattr(lottery, "ack_delivery")
    except (ImportError, AttributeError):
        ack = None
    if ack is not None:
        ack(db, payload.get("draw_id"), ok_, ref, error)
        return
    enqueue_event(db, "lottery.delivery_result", {"draw_id": payload.get("draw_id"),
                                                  "delivery_key": payload.get("delivery_key"), "ok": ok_, "ref": ref,
                                                  "error": error}, "lottery_draw", payload.get("draw_id") or "")


def _prize_card(db: Session, member_id: int, ref_id: Any) -> MemberCard | None:
    if ref_id not in (None, ""):
        try:
            card = db.get(MemberCard, int(ref_id))
        except (TypeError, ValueError):
            card = None
        if card is not None and card.user_id == member_id and card.status in (S_ACTIVE, S_FROZEN, S_INACTIVE):
            return card
        return None
    # 未指定卡：取该会员当前可用的、有有效期的卡中到期最晚的一张；没有则取未激活的期限卡
    return db.execute(select(MemberCard).where(
        MemberCard.user_id == member_id, or_(
            (MemberCard.status.in_((S_ACTIVE, S_FROZEN))) & MemberCard.ends_at.is_not(None),
            (MemberCard.status == S_INACTIVE) & (MemberCard.kind != KIND_COUNT)),
    ).order_by(MemberCard.ends_at.desc().nulls_last(), MemberCard.id.desc())).scalars().first()


def on_prize_won(db: Session, payload: dict, event_id: str | None = None) -> str:
    """lottery.prize_won（prize_kind=member_card_days）：给 ref_id 指定的卡（或会员当前卡）延长 quantity 天。

    幂等：按 delivery_key 去重（成功时写卡流水 kind=prize ref=delivery_key；失败按事件去重）。不提交。
    """
    if payload.get("prize_kind") != PRIZE_KIND_DAYS:
        return "ignored"
    key = str(payload.get("delivery_key") or "")[:64]
    if not key:
        return "ignored"
    done = db.execute(select(CardLedger).where(CardLedger.ref == key, CardLedger.kind == "prize")).scalar_one_or_none()
    if done is not None:
        _ack_prize(db, payload, True, str(done.card_id), None)
        return "duplicate"
    if not first_time(db, f"{CONSUMER}.prize", event_id):
        return "duplicate"
    days = int(payload.get("quantity") or 0)
    member_id = int(payload.get("member_id") or 0)
    card = _prize_card(db, member_id, payload.get("ref_id")) if days > 0 and member_id else None
    if card is None:
        _ack_prize(db, payload, False, None, "no_eligible_card" if days > 0 else "invalid_quantity")
        return "no_card"
    values: dict[str, Any] = {"gift_units": MemberCard.gift_units + days, "total_units": MemberCard.total_units + days}
    if card.ends_at is not None:
        values["ends_at"] = aware(card.ends_at) + timedelta(days=days)
    _cas(db, card, (MemberCard.status == card.status,), **values)
    _ledger(db, card, "prize", key, delta_days=days, operator="system:lottery",
            note=f"draw {payload.get('draw_id')} {payload.get('prize_name') or ''}"[:200])
    enqueue_event(db, "member_card.extended", {"card_id": card.id, "user_id": card.user_id, "days": days,
                                               "source": "lottery", "draw_id": payload.get("draw_id")},
                  "member_card", card.id)
    _ack_prize(db, payload, True, str(card.id), None)
    return "extended"


EVENT_HANDLERS["lottery.prize_won"] = on_prize_won


# ================================================================ 订单售后退款（08-order 售卡行 → 10-after-sales）

ORDER_REFUND_KIND = "order_refund"


def on_after_sale_completed(db: Session, payload: dict, event_id: str | None = None) -> str:
    """after_sale.completed：订单里买的卡（行 fulfil_ref = member_card:{id}）已由售后原路退款 → 终止该卡。

    外部退款已完成（售后在 refund.succeeded / 余额冲回后才发此事件），这里只做权益终止：
    状态 → refunded（终态）、剩余次数清零、卡流水 kind=order_refund ref=after_sale:{售后单}:{卡}、发 member_card.terminated。
    卡正在走本模块自己的退卡流程（refunding）时不动它，发 member_card.order_refund_conflict 交人工。
    幂等：事件去重 + 卡流水 (ref, kind) 唯一 + 状态条件 UPDATE。不提交。
    """
    lines = [ln for ln in (payload.get("lines") or [])
             if str(ln.get("fulfil_ref") or "").startswith("member_card:")]
    if not lines:
        return "ignored"
    if not first_time(db, f"{CONSUMER}.after_sale_completed", event_id):
        return "duplicate"
    case_id = payload.get("after_sale_id")
    outcome = "terminated"
    for ln in lines:
        try:
            card_id = int(str(ln["fulfil_ref"]).split(":", 1)[1])
        except (TypeError, ValueError):
            continue
        card = db.get(MemberCard, card_id)
        if card is None:
            continue
        ref = f"after_sale:{case_id}:{card_id}"
        if db.execute(select(CardLedger.id).where(CardLedger.ref == ref, CardLedger.kind == ORDER_REFUND_KIND)
                      ).scalar_one_or_none() is not None or card.status == S_REFUNDED:
            continue
        if card.status == S_REFUNDING:
            enqueue_event(db, "member_card.order_refund_conflict", {"card_id": card.id, "user_id": card.user_id,
                                                                    "after_sale_id": case_id,
                                                                    "order_id": payload.get("order_id")},
                          "member_card", card.id)
            outcome = "conflict"
            continue
        remaining = card.sessions_remaining
        before = card.status
        if db.execute(update(MemberCard).where(MemberCard.id == card.id, MemberCard.status == before)
                      .values(status=S_REFUNDED, prev_status=None, sessions_remaining=0,
                              version=MemberCard.version + 1)).rowcount != 1:
            raise AppError(ErrorCode.CONFLICT, "card changed concurrently, retry")
        _ledger(db, card, ORDER_REFUND_KIND, ref, delta_sessions=-(remaining or 0), operator="system:after_sales",
                note=f"order {payload.get('order_id')} after-sale {payload.get('after_sale_no') or case_id}")
        enqueue_event(db, "member_card.terminated", {
            "card_id": card.id, "user_id": card.user_id, "reason": "order_refund", "after_sale_id": case_id,
            "order_id": payload.get("order_id"), "order_item_id": ln.get("order_item_id"),
            "refund_cents": int(ln.get("amount_cents") or 0), "status_before": before}, "member_card", card.id)
    return outcome


EVENT_HANDLERS["after_sale.completed"] = on_after_sale_completed


# 向 09-wechat-pay 注册同事务钩子（支付成功即发卡 / 完成转让；退款终态即回写）与 Payable 解析；事件订阅为补偿通道。
register_payment_hooks(BIZ_TYPE, on_payment_succeeded, REFUND_PREFIX, on_refund_result, payable=payable)
