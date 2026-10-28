"""会员卡接口：顾客端 /api/v1/app/member-cards/*，后台 /api/v1/admin/member-cards/*。路由只做参数、鉴权、幂等与调用 service。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field as PField, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from luzback import AppError, ErrorCode, Field, ListSpec, export_csv, get_db, get_settings, idempotency_key, ok, run_idempotent, utcnow, write_audit
from luzback.export import cents_to_yuan
from wallet import StaffContext, assert_store_scope, current_user, phone_lookup_hash, require_permission

from . import service
from .models import CARD_STATUSES, KINDS, CardLedger, CardOrder, CardProduct, CardRefund, MemberCard

app_router = APIRouter(prefix="/api/v1/app/member-cards", tags=["member-card"])
admin_router = APIRouter(prefix="/api/v1/admin/member-cards", tags=["member-card-admin"])
routers = [app_router, admin_router]


def _fp(body) -> str:
    data = body.model_dump(mode="json") if isinstance(body, BaseModel) else (body or {})
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _run(db: Session, scope: str, key: str, actor: str, body, fn):
    return ok(run_idempotent(db, scope, key, lambda: jsonable_encoder(fn()), actor=actor, fingerprint=_fp(body),
                             require_key=True))


def _submit(db: Session, response: dict) -> dict:
    """退款进入处理中后（事务已提交）把原路退款交给支付模块。"""
    data = response.get("data") or {}
    if data.get("status") == "processing" and data.get("route") == "original" and data.get("id"):
        response["data"] = jsonable_encoder(service.submit_refund(db, int(data["id"])))
    return response


def _secret() -> str:
    return get_settings().jwt_secret


def _staff_card(db: Session, staff: StaffContext, card_id: int) -> MemberCard:
    card = service.get_card(db, card_id)
    if not staff.in_scope(card.store_id):
        raise AppError(ErrorCode.NOT_FOUND, "card not found")  # 越界的卡对该员工不可见
    return card


def _dt(value: str | None) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AppError(ErrorCode.BAD_REQUEST, "invalid datetime") from exc
    return service.aware(moment)


# ---------------------------------------------------------------- ListSpec 声明

PRODUCT_SPEC = ListSpec(fields={
    "status": Field(CardProduct.status, choices=frozenset({"on", "off"})),
    "kind": Field(CardProduct.kind, ops=("eq", "in"), choices=frozenset(KINDS)),
    "store_id": Field(CardProduct.store_id, "int", ops=("eq", "null")),
    "price_cents": Field(CardProduct.price_cents, "int", ops=("range",), sortable=True),
    "seq": Field(CardProduct.sort, "int", sortable=True),
    "created_at": Field(CardProduct.created_at, "datetime", ops=("range",), sortable=True),
}, id_column=CardProduct.id, keyword_columns=(CardProduct.name,), default_sort="seq")

APP_PRODUCT_SPEC = ListSpec(fields={
    "kind": Field(CardProduct.kind, ops=("eq", "in"), choices=frozenset(KINDS)),
    "seq": Field(CardProduct.sort, "int", sortable=True),
}, id_column=CardProduct.id, default_sort="seq", extra_params=frozenset({"store_id"}))

CARD_SPEC = ListSpec(fields={
    "status": Field(MemberCard.status, ops=("eq", "in"), choices=frozenset(CARD_STATUSES)),
    "kind": Field(MemberCard.kind, ops=("eq", "in"), choices=frozenset(KINDS)),
    "product_id": Field(MemberCard.product_id, "int", ops=("eq", "in")),
    "store_id": Field(MemberCard.store_id, "int", ops=("eq", "in")),
    "user_id": Field(MemberCard.user_id, "int"),
    "source": Field(MemberCard.source, choices=frozenset({"purchase", "manual", "gift"})),
    "ends_at": Field(MemberCard.ends_at, "datetime", ops=("range", "null"), sortable=True),
    "created_at": Field(MemberCard.created_at, "datetime", ops=("range",), sortable=True),
    "paid_cents": Field(MemberCard.paid_cents, "int", ops=("range",), sortable=True),
}, id_column=MemberCard.id, keyword_columns=(MemberCard.card_no, MemberCard.holder_name, MemberCard.holder_phone_masked),
    extra_params=frozenset({"expiring_within_days", "phone"}))

APP_CARD_SPEC = ListSpec(fields={
    "status": Field(MemberCard.status, ops=("eq", "in"), choices=frozenset(CARD_STATUSES)),
    "created_at": Field(MemberCard.created_at, "datetime", sortable=True),
}, id_column=MemberCard.id)

LEDGER_SPEC = ListSpec(fields={
    "kind": Field(CardLedger.kind, ops=("eq", "in")),
    "created_at": Field(CardLedger.created_at, "datetime", ops=("range",), sortable=True),
}, id_column=CardLedger.id)

REFUND_SPEC = ListSpec(fields={
    "status": Field(CardRefund.status, ops=("eq", "in"),
                    choices=frozenset({"pending_approval", "processing", "succeeded", "failed", "rejected"})),
    "cause": Field(CardRefund.cause, choices=frozenset({"customer", "merchant"})),
    "route": Field(CardRefund.route, choices=frozenset({"original", "offline"})),
    "card_id": Field(CardRefund.card_id, "int"),
    "user_id": Field(CardRefund.user_id, "int"),
    "due_at": Field(CardRefund.due_at, "datetime", ops=("range",), sortable=True),
    "created_at": Field(CardRefund.created_at, "datetime", ops=("range",), sortable=True),
}, id_column=CardRefund.id, keyword_columns=(CardRefund.refund_no,))

ORDER_SPEC = ListSpec(fields={
    "status": Field(CardOrder.status, ops=("eq", "in"), choices=frozenset({"pending", "paid", "closed"})),
    "kind": Field(CardOrder.kind, choices=frozenset({"purchase", "transfer_fee"})),
    "user_id": Field(CardOrder.user_id, "int"),
    "product_id": Field(CardOrder.product_id, "int"),
    "store_id": Field(CardOrder.store_id, "int", ops=("eq", "in")),
    "needs_manual": Field(CardOrder.needs_manual, "bool"),
    "created_at": Field(CardOrder.created_at, "datetime", ops=("range",), sortable=True),
}, id_column=CardOrder.id, keyword_columns=(CardOrder.order_no, CardOrder.out_trade_no))


def _cards_stmt(staff: StaffContext, params) -> select:
    stmt = select(MemberCard)
    if staff.store_scope is not None:
        stmt = stmt.where(MemberCard.store_id.in_(staff.store_scope))
    within = params.get("expiring_within_days")
    if within not in (None, ""):
        try:
            days = int(within)
        except ValueError as exc:
            raise AppError(ErrorCode.BAD_REQUEST, "expiring_within_days: expected integer") from exc
        if not 0 < days <= 366:
            raise AppError(ErrorCode.BAD_REQUEST, "expiring_within_days must be 1..366")
        now = utcnow()
        stmt = stmt.where(MemberCard.status == "active", MemberCard.ends_at > now, MemberCard.ends_at <= now + timedelta(days=days))
    phone = (params.get("phone") or "").strip()
    if phone:
        stmt = stmt.where(MemberCard.holder_phone_hash == phone_lookup_hash(phone))
    return stmt


# ---------------------------------------------------------------- 请求模型

Ref = PField(min_length=1, max_length=48, pattern=r"^[A-Za-z0-9:_\-.]+$")


class Window(BaseModel):
    weekdays: list[int] = PField(min_length=1, max_length=7)
    start: str = PField(pattern=r"^\d{2}:\d{2}$")
    end: str = PField(pattern=r"^\d{2}:\d{2}$")


class ProductIn(BaseModel):
    store_id: int | None = PField(default=None, ge=1)
    name: str = PField(min_length=1, max_length=64)
    kind: Literal["count", "period", "time_slot"]
    price_cents: int = PField(ge=0, le=100_000_000)
    original_price_cents: int | None = PField(default=None, ge=0, le=100_000_000)
    sessions: int = PField(default=0, ge=0, le=10_000)
    gift_sessions: int = PField(default=0, ge=0, le=10_000)
    period_days: int = PField(default=0, ge=0, le=3660)
    gift_days: int = PField(default=0, ge=0, le=3660)
    valid_days: int | None = PField(default=None, ge=1, le=3660)
    slot_windows: list[Window] = PField(default_factory=list, max_length=21)
    activation_mode: Literal["on_purchase", "on_first_use", "on_date"] = "on_purchase"
    activation_date: str | None = None
    activation_deadline_days: int | None = PField(default=None, ge=1, le=3660)
    renew_stack: bool = True
    store_scope: Literal["single", "multi", "all"] = "single"
    store_ids: list[int] = PField(default_factory=list, max_length=200)
    per_member_limit: int | None = PField(default=None, ge=1, le=1000)
    sale_start_at: str | None = None
    sale_end_at: str | None = None
    transferable: bool = True
    transfer_fee_cents: int = PField(default=0, ge=0, le=10_000_000)
    max_transfers: int = PField(default=1, ge=0, le=100)
    transfer_cooling_days: int = PField(default=0, ge=0, le=3660)
    freezable: bool = True
    freeze_min_days: int = PField(default=7, ge=1, le=3660)
    freeze_max_days: int = PField(default=30, ge=1, le=3660)
    freeze_max_times: int = PField(default=2, ge=0, le=100)
    freeze_total_max_days: int = PField(default=60, ge=0, le=3660)
    refund_fee_bp: int = PField(default=0, ge=0, le=3000)
    entry_cooldown_minutes: int = PField(default=0, ge=0, le=1440)
    terms_text: str = PField(default="", max_length=10_000)
    sort: int = 0

    def values(self) -> dict:
        data = self.model_dump()
        data["slot_windows"] = [w.model_dump() for w in self.slot_windows]
        for key in ("activation_date", "sale_start_at", "sale_end_at"):
            data[key] = _dt(data[key])
        return data


class ProductUpdateIn(ProductIn):
    pass


class OrderIn(BaseModel):
    product_id: int = PField(ge=1)
    store_id: int | None = PField(default=None, ge=1)


class FreezeIn(BaseModel):
    days: int = PField(ge=1, le=3660)
    reason: str = PField(default="", max_length=200)


class TransferIn(BaseModel):
    to_member_id: int | None = PField(default=None, ge=1)
    to_phone: str | None = PField(default=None, pattern=r"^1\d{10}$")
    reason: str = PField(default="", max_length=200)
    fee_collected_offline: bool = False

    @model_validator(mode="after")
    def _one_target(self):
        if (self.to_member_id is None) == (self.to_phone is None):
            raise ValueError("exactly one of to_member_id or to_phone is required")
        return self


class RefundRequestIn(BaseModel):
    reason: str = PField(default="", max_length=200)


class AdminRefundIn(BaseModel):
    cause: Literal["customer", "merchant"]
    reason: str = PField(min_length=1, max_length=200)


class IssueIn(BaseModel):
    product_id: int = PField(ge=1)
    user_id: int = PField(ge=1)
    store_id: int | None = PField(default=None, ge=1)
    source: Literal["manual", "gift"] = "manual"
    paid_cents: int = PField(default=0, ge=0, le=100_000_000)
    reason: str = PField(min_length=1, max_length=200)


class ConsumeIn(BaseModel):
    store_id: int | None = PField(default=None, ge=1)
    ref: str = Ref


class ConsumeByNoIn(ConsumeIn):
    card_no: str = PField(min_length=4, max_length=20)


class ReverseIn(BaseModel):
    ref: str = Ref
    reason: str = PField(min_length=1, max_length=200)


class AdjustIn(BaseModel):
    delta_sessions: int = 0
    delta_days: int = 0
    reason: str = PField(min_length=1, max_length=200)


class ReasonIn(BaseModel):
    reason: str = PField(min_length=1, max_length=200)


class OfflineCompleteIn(BaseModel):
    success: bool = True
    note: str = PField(default="", max_length=200)


# ================================================================ 顾客端


@app_router.get("/products")
def app_products(request: Request, store_id: int | None = Query(default=None, ge=1), user_id: int = Depends(current_user),
                 db: Session = Depends(get_db)):
    stmt = service.products_on_sale_stmt(store_id, utcnow())
    return ok(APP_PRODUCT_SPEC.cursor(db, stmt, request.query_params, _secret(), serialize=service.product_dict,
                                      row_values=lambda p: {"seq": p.sort, "id": p.id}))


@app_router.get("/products/{product_id}")
def app_product(product_id: int = Path(ge=1), user_id: int = Depends(current_user), db: Session = Depends(get_db)):
    product = db.execute(service.products_on_sale_stmt(None, utcnow()).where(CardProduct.id == product_id)).scalar_one_or_none()
    if product is None:
        raise AppError(ErrorCode.NOT_FOUND, "card product not found")
    return ok(service.product_dict(product))


@app_router.post("/orders")
def app_order(body: OrderIn, key: str = Depends(idempotency_key), user_id: int = Depends(current_user),
              db: Session = Depends(get_db)):
    created = run_idempotent(db, "member_card.order.create", key,
                             lambda: jsonable_encoder(service.create_order(db, user_id, body.product_id, body.store_id)),
                             actor=f"user:{user_id}", fingerprint=_fp(body), require_key=True)
    # 购卡单已提交；调起支付在独立步骤（支付模块自行提交）。重放时支付模块复用同一支付单
    return ok(jsonable_encoder(service.start_payment(db, user_id, created["id"])))


@app_router.post("/orders/{order_id}/pay")
def app_pay(order_id: int = Path(ge=1), user_id: int = Depends(current_user), db: Session = Depends(get_db)):
    """继续支付（购卡单或转让手续费单）。"""
    return ok(jsonable_encoder(service.start_payment(db, user_id, order_id)))


@app_router.get("/cards")
def app_cards(request: Request, user_id: int = Depends(current_user), db: Session = Depends(get_db)):
    stmt = select(MemberCard).where(MemberCard.user_id == user_id)
    return ok(APP_CARD_SPEC.cursor(db, stmt, request.query_params, _secret(), serialize=service.card_dict))


@app_router.get("/cards/{card_id}")
def app_card(card_id: int = Path(ge=1), user_id: int = Depends(current_user), db: Session = Depends(get_db)):
    card = service.get_card(db, card_id, user_id)
    refunds = db.execute(select(CardRefund).where(CardRefund.card_id == card.id).order_by(CardRefund.id.desc()).limit(5)).scalars()
    return ok(service.card_dict(card) | {"terms": card.terms_snapshot,
                                         "refunds": [service.refund_dict(r) for r in refunds]})


@app_router.get("/cards/{card_id}/ledger")
def app_card_ledger(request: Request, card_id: int = Path(ge=1), user_id: int = Depends(current_user),
                    db: Session = Depends(get_db)):
    card = service.get_card(db, card_id, user_id)
    stmt = select(CardLedger).where(CardLedger.card_id == card.id, CardLedger.user_id == user_id)
    return ok(LEDGER_SPEC.cursor(db, stmt, request.query_params, _secret(), serialize=service.ledger_dict))


@app_router.post("/cards/{card_id}/freeze")
def app_freeze(body: FreezeIn, card_id: int = Path(ge=1), key: str = Depends(idempotency_key),
               user_id: int = Depends(current_user), db: Session = Depends(get_db)):
    if not service.cfg().app_freeze_enabled:
        raise AppError(ErrorCode.FORBIDDEN, "请到门店办理冻结")
    return _run(db, f"member_card.freeze:{card_id}", key, f"user:{user_id}", body,
                lambda: service.freeze(db, service.get_card(db, card_id, user_id), body.days, body.reason, f"user:{user_id}"))


@app_router.post("/cards/{card_id}/unfreeze")
def app_unfreeze(card_id: int = Path(ge=1), key: str = Depends(idempotency_key), user_id: int = Depends(current_user),
                 db: Session = Depends(get_db)):
    return _run(db, f"member_card.unfreeze:{card_id}", key, f"user:{user_id}", None,
                lambda: service.unfreeze(db, service.get_card(db, card_id, user_id), f"user:{user_id}"))


@app_router.get("/cards/{card_id}/refund-quote")
def app_refund_quote(card_id: int = Path(ge=1), user_id: int = Depends(current_user), db: Session = Depends(get_db)):
    return ok(service.quote_refund(db, service.get_card(db, card_id, user_id), "customer"))


@app_router.post("/cards/{card_id}/refund-request")
def app_refund_request(body: RefundRequestIn, card_id: int = Path(ge=1), key: str = Depends(idempotency_key),
                       user_id: int = Depends(current_user), db: Session = Depends(get_db)):
    return _submit(db, _run(db, f"member_card.refund.request:{card_id}", key, f"user:{user_id}", body,
                            lambda: service.request_refund(db, service.get_card(db, card_id, user_id), "customer",
                                                           body.reason, f"user:{user_id}", user_id=user_id)))


@app_router.post("/cards/{card_id}/transfer")
def app_transfer(body: TransferIn, card_id: int = Path(ge=1), key: str = Depends(idempotency_key),
                 user_id: int = Depends(current_user), db: Session = Depends(get_db)):
    if not service.cfg().app_transfer_enabled:
        raise AppError(ErrorCode.FORBIDDEN, "请到门店办理转让")
    response = _run(db, f"member_card.transfer:{card_id}", key, f"user:{user_id}", body,
                    lambda: service.transfer(db, service.get_card(db, card_id, user_id), to_user_id=body.to_member_id,
                                             to_phone=body.to_phone, operator=f"user:{user_id}", reason=body.reason))
    data = response["data"]
    if data.get("status") == "awaiting_fee_payment":  # 手续费单已提交，再调起支付
        data["order"] = jsonable_encoder(service.start_payment(db, user_id, data["order"]["id"]))
    return response


@app_router.post("/cards/{card_id}/report-lost")
def app_report_lost(card_id: int = Path(ge=1), key: str = Depends(idempotency_key), user_id: int = Depends(current_user),
                    db: Session = Depends(get_db)):
    return _run(db, f"member_card.lost:{card_id}", key, f"user:{user_id}", None,
                lambda: service.report_lost(db, service.get_card(db, card_id, user_id), f"user:{user_id}"))


# ================================================================ 后台：卡种


@admin_router.get("/products")
def admin_products(request: Request, staff: StaffContext = Depends(require_permission("member_card:read")),
                   db: Session = Depends(get_db)):
    stmt = select(CardProduct).where(CardProduct.deleted_at.is_(None))
    return ok(PRODUCT_SPEC.page(db, stmt, request.query_params, serialize=service.product_dict))


@admin_router.post("/products")
def admin_create_product(body: ProductIn, key: str = Depends(idempotency_key),
                         staff: StaffContext = Depends(require_permission("member_card:product")),
                         db: Session = Depends(get_db)):
    for store_id in [body.store_id, *body.store_ids]:
        if store_id:
            assert_store_scope(staff, store_id)
    return _run(db, "member_card.product.create", key, f"staff:{staff.id}", body,
                lambda: service.create_product(db, staff.id, body.values()))


@admin_router.get("/products/{product_id}")
def admin_product(product_id: int = Path(ge=1), staff: StaffContext = Depends(require_permission("member_card:read")),
                  db: Session = Depends(get_db)):
    return ok(service.product_dict(service.get_product(db, product_id)))


@admin_router.put("/products/{product_id}")
def admin_update_product(body: ProductUpdateIn, product_id: int = Path(ge=1), key: str = Depends(idempotency_key),
                         staff: StaffContext = Depends(require_permission("member_card:product")),
                         db: Session = Depends(get_db)):
    return _run(db, f"member_card.product.update:{product_id}", key, f"staff:{staff.id}", body,
                lambda: service.update_product(db, staff.id, product_id, body.values()))


@admin_router.post("/products/{product_id}/{action}")
def admin_product_status(product_id: int = Path(ge=1), action: Literal["on-sale", "off-sale"] = Path(),
                         key: str = Depends(idempotency_key),
                         staff: StaffContext = Depends(require_permission("member_card:product")),
                         db: Session = Depends(get_db)):
    status = "on" if action == "on-sale" else "off"
    return _run(db, f"member_card.product.{status}:{product_id}", key, f"staff:{staff.id}", None,
                lambda: service.set_product_status(db, staff.id, product_id, status))


@admin_router.delete("/products/{product_id}")
def admin_delete_product(product_id: int = Path(ge=1), key: str = Depends(idempotency_key),
                         staff: StaffContext = Depends(require_permission("member_card:product")),
                         db: Session = Depends(get_db)):
    return _run(db, f"member_card.product.delete:{product_id}", key, f"staff:{staff.id}", None,
                lambda: service.delete_product(db, staff.id, product_id))


# ================================================================ 后台：卡列表、导出、详情

CARD_HEADERS = ("卡号", "会员ID", "持卡人", "手机号", "卡名", "类型", "状态", "门店ID", "实付(元)", "剩余次数", "开始", "到期",
                "冻结次数", "转让次数", "来源", "开卡时间")


def _card_row(c: MemberCard) -> list:
    return [c.card_no, c.user_id, c.holder_name, c.holder_phone_masked, c.name, c.kind, c.status, c.store_id,
            cents_to_yuan(c.paid_cents), c.sessions_remaining if c.kind == "count" else "", service.aware(c.starts_at),
            service.aware(c.ends_at), c.freeze_count, c.transfer_count, c.source, service.aware(c.created_at)]


@admin_router.get("")
def admin_cards(request: Request, staff: StaffContext = Depends(require_permission("member_card:read")),
                db: Session = Depends(get_db)):
    params = request.query_params
    return ok(CARD_SPEC.page(db, _cards_stmt(staff, params), params, serialize=service.card_dict))


@admin_router.get("/export")
def admin_cards_export(request: Request, staff: StaffContext = Depends(require_permission("member_card:export")),
                       db: Session = Depends(get_db)):
    params = request.query_params
    stream = export_csv(db, CARD_SPEC, _cards_stmt(staff, params), params, CARD_HEADERS, _card_row)
    write_audit(db, "staff", staff.id, "member_card.export", "member_card", "-", after={"query": str(params)})
    db.commit()
    return StreamingResponse(stream, media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": 'attachment; filename="member-cards.csv"'})


@admin_router.get("/orders")
def admin_orders(request: Request, staff: StaffContext = Depends(require_permission("member_card:read")),
                 db: Session = Depends(get_db)):
    stmt = select(CardOrder)
    if staff.store_scope is not None:
        stmt = stmt.where(CardOrder.store_id.in_(staff.store_scope))
    return ok(ORDER_SPEC.page(db, stmt, request.query_params, serialize=service.order_dict))


@admin_router.get("/refunds")
def admin_refunds(request: Request, staff: StaffContext = Depends(require_permission("member_card:read")),
                  db: Session = Depends(get_db)):
    return ok(REFUND_SPEC.page(db, select(CardRefund), request.query_params, serialize=service.refund_dict))


@admin_router.post("/refunds/{refund_id}/approve")
def admin_approve(refund_id: int = Path(ge=1), key: str = Depends(idempotency_key),
                  staff: StaffContext = Depends(require_permission("member_card:refund", fresh=True)), db: Session = Depends(get_db)):
    return _submit(db, _run(db, f"member_card.refund.approve:{refund_id}", key, f"staff:{staff.id}", None,
                            lambda: service.approve_refund(db, refund_id, staff.id)))


@admin_router.post("/refunds/{refund_id}/reject")
def admin_reject(body: ReasonIn, refund_id: int = Path(ge=1), key: str = Depends(idempotency_key),
                 staff: StaffContext = Depends(require_permission("member_card:refund", fresh=True)), db: Session = Depends(get_db)):
    return _run(db, f"member_card.refund.reject:{refund_id}", key, f"staff:{staff.id}", body,
                lambda: service.reject_refund(db, refund_id, staff.id, body.reason))


@admin_router.post("/refunds/{refund_id}/offline-complete")
def admin_offline_complete(body: OfflineCompleteIn, refund_id: int = Path(ge=1), key: str = Depends(idempotency_key),
                           staff: StaffContext = Depends(require_permission("member_card:refund", fresh=True)),
                           db: Session = Depends(get_db)):
    return _run(db, f"member_card.refund.offline:{refund_id}", key, f"staff:{staff.id}", body,
                lambda: service.complete_offline_refund(db, refund_id, staff.id, body.success, body.note))


@admin_router.post("/issue")
def admin_issue(body: IssueIn, key: str = Depends(idempotency_key),
                staff: StaffContext = Depends(require_permission("member_card:issue", fresh=True)), db: Session = Depends(get_db)):
    if body.store_id:
        assert_store_scope(staff, body.store_id)
    return _run(db, "member_card.issue", key, f"staff:{staff.id}", body,
                lambda: service.issue_manual(db, staff.id, body.product_id, body.user_id, body.store_id, body.source,
                                             body.paid_cents, body.reason, f"{body.source}:{staff.id}:{key}"))


@admin_router.post("/consume-by-no")
def admin_consume_by_no(body: ConsumeByNoIn, key: str = Depends(idempotency_key),
                        staff: StaffContext = Depends(require_permission("member_card:consume")),
                        db: Session = Depends(get_db)):
    if body.store_id:
        assert_store_scope(staff, body.store_id)
    return _run(db, "member_card.consume", key, f"staff:{staff.id}", body,
                lambda: service.consume(db, store_id=body.store_id, ref=body.ref, card_no=body.card_no,
                                        operator=f"staff:{staff.id}"))


@admin_router.post("/usages/reverse")
def admin_reverse_use(body: ReverseIn, key: str = Depends(idempotency_key),
                      staff: StaffContext = Depends(require_permission("member_card:consume", fresh=True)),
                      db: Session = Depends(get_db)):
    def work():
        data = service.reverse_use(db, body.ref, body.reason, f"staff:{staff.id}")
        write_audit(db, "staff", staff.id, "member_card.use.reverse", "member_card", data["card_id"], after=data,
                    reason=body.reason)
        return data
    return _run(db, "member_card.use.reverse", key, f"staff:{staff.id}", body, work)


@admin_router.get("/{card_id}")
def admin_card(card_id: int = Path(ge=1), staff: StaffContext = Depends(require_permission("member_card:read")),
               db: Session = Depends(get_db)):
    card = _staff_card(db, staff, card_id)
    ledger = db.execute(select(CardLedger).where(CardLedger.card_id == card.id).order_by(CardLedger.id.desc()).limit(50)).scalars()
    refunds = db.execute(select(CardRefund).where(CardRefund.card_id == card.id).order_by(CardRefund.id.desc())).scalars()
    return ok(service.card_dict(card) | {"terms": card.terms_snapshot, "product_snapshot": json.loads(card.product_snapshot_json),
                                         "ledger": [service.ledger_dict(e) for e in ledger],
                                         "refunds": [service.refund_dict(r) for r in refunds]})


@admin_router.get("/{card_id}/ledger")
def admin_card_ledger(request: Request, card_id: int = Path(ge=1),
                      staff: StaffContext = Depends(require_permission("member_card:read")), db: Session = Depends(get_db)):
    card = _staff_card(db, staff, card_id)
    return ok(LEDGER_SPEC.page(db, select(CardLedger).where(CardLedger.card_id == card.id), request.query_params,
                               serialize=service.ledger_dict))


def _card_action(db, staff, card_id, scope, key, body, fn):
    return _run(db, f"member_card.{scope}:{card_id}", key, f"staff:{staff.id}", body,
                lambda: fn(_staff_card(db, staff, card_id)))


@admin_router.post("/{card_id}/consume")
def admin_consume(body: ConsumeIn, card_id: int = Path(ge=1), key: str = Depends(idempotency_key),
                  staff: StaffContext = Depends(require_permission("member_card:consume")), db: Session = Depends(get_db)):
    if body.store_id:
        assert_store_scope(staff, body.store_id)
    return _card_action(db, staff, card_id, "consume", key, body,
                        lambda card: service.consume(db, store_id=body.store_id, ref=body.ref, card_id=card.id,
                                                     operator=f"staff:{staff.id}"))


@admin_router.post("/{card_id}/adjust")
def admin_adjust(body: AdjustIn, card_id: int = Path(ge=1), key: str = Depends(idempotency_key),
                 staff: StaffContext = Depends(require_permission("member_card:adjust", fresh=True)), db: Session = Depends(get_db)):
    return _card_action(db, staff, card_id, "adjust", key, body,
                        lambda card: service.adjust(db, staff.id, card, body.delta_sessions, body.delta_days, body.reason,
                                                    f"adjust:{staff.id}:{key}"))


def _audited(db, staff, action, card, reason, data):
    write_audit(db, "staff", staff.id, action, "member_card", card.id, after={"status": card.status}, reason=reason or None)
    return data


@admin_router.post("/{card_id}/freeze")
def admin_freeze(body: FreezeIn, card_id: int = Path(ge=1), key: str = Depends(idempotency_key),
                 staff: StaffContext = Depends(require_permission("member_card:freeze")), db: Session = Depends(get_db)):
    return _card_action(db, staff, card_id, "freeze", key, body, lambda card: _audited(
        db, staff, "member_card.freeze", card, body.reason,
        service.freeze(db, card, body.days, body.reason, f"staff:{staff.id}")))


@admin_router.post("/{card_id}/unfreeze")
def admin_unfreeze(card_id: int = Path(ge=1), key: str = Depends(idempotency_key),
                   staff: StaffContext = Depends(require_permission("member_card:freeze")), db: Session = Depends(get_db)):
    return _card_action(db, staff, card_id, "unfreeze", key, None, lambda card: _audited(
        db, staff, "member_card.unfreeze", card, None, service.unfreeze(db, card, f"staff:{staff.id}")))


@admin_router.post("/{card_id}/transfer")
def admin_transfer(body: TransferIn, card_id: int = Path(ge=1), key: str = Depends(idempotency_key),
                   staff: StaffContext = Depends(require_permission("member_card:transfer")), db: Session = Depends(get_db)):
    return _card_action(db, staff, card_id, "transfer", key, body, lambda card: _audited(
        db, staff, "member_card.transfer", card, body.reason,
        service.transfer(db, card, to_user_id=body.to_member_id, to_phone=body.to_phone, operator=f"staff:{staff.id}",
                         by_staff=True, fee_collected_offline=body.fee_collected_offline, reason=body.reason)))


@admin_router.post("/{card_id}/report-lost")
def admin_report_lost(body: ReasonIn, card_id: int = Path(ge=1), key: str = Depends(idempotency_key),
                      staff: StaffContext = Depends(require_permission("member_card:lost")), db: Session = Depends(get_db)):
    return _card_action(db, staff, card_id, "lost", key, body, lambda card: _audited(
        db, staff, "member_card.lost", card, body.reason, service.report_lost(db, card, f"staff:{staff.id}")))


@admin_router.post("/{card_id}/cancel-lost")
def admin_cancel_lost(body: ReasonIn, card_id: int = Path(ge=1), key: str = Depends(idempotency_key),
                      staff: StaffContext = Depends(require_permission("member_card:lost")), db: Session = Depends(get_db)):
    return _card_action(db, staff, card_id, "cancel_lost", key, body, lambda card: _audited(
        db, staff, "member_card.cancel_lost", card, body.reason, service.cancel_lost(db, card, f"staff:{staff.id}")))


@admin_router.post("/{card_id}/replace")
def admin_replace(body: ReasonIn, card_id: int = Path(ge=1), key: str = Depends(idempotency_key),
                  staff: StaffContext = Depends(require_permission("member_card:lost")), db: Session = Depends(get_db)):
    return _card_action(db, staff, card_id, "replace", key, body,
                        lambda card: service.replace_card(db, staff.id, card, body.reason))


@admin_router.post("/{card_id}/disable")
def admin_disable(body: ReasonIn, card_id: int = Path(ge=1), key: str = Depends(idempotency_key),
                  staff: StaffContext = Depends(require_permission("member_card:disable")), db: Session = Depends(get_db)):
    return _card_action(db, staff, card_id, "disable", key, body,
                        lambda card: service.disable_card(db, staff.id, card, body.reason))


@admin_router.post("/{card_id}/enable")
def admin_enable(body: ReasonIn, card_id: int = Path(ge=1), key: str = Depends(idempotency_key),
                 staff: StaffContext = Depends(require_permission("member_card:disable")), db: Session = Depends(get_db)):
    return _card_action(db, staff, card_id, "enable", key, body,
                        lambda card: service.enable_card(db, staff.id, card, body.reason))


@admin_router.get("/{card_id}/refund-quote")
def admin_refund_quote(card_id: int = Path(ge=1), cause: Literal["customer", "merchant"] = Query(default="customer"),
                       staff: StaffContext = Depends(require_permission("member_card:refund")),
                       db: Session = Depends(get_db)):
    return ok(service.quote_refund(db, _staff_card(db, staff, card_id), cause))


@admin_router.post("/{card_id}/refunds")
def admin_create_refund(body: AdminRefundIn, card_id: int = Path(ge=1), key: str = Depends(idempotency_key),
                        staff: StaffContext = Depends(require_permission("member_card:refund", fresh=True)),
                        db: Session = Depends(get_db)):
    return _submit(db, _card_action(db, staff, card_id, "refund", key, body,
                                    lambda card: service.request_refund(db, card, body.cause, body.reason,
                                                                        f"staff:{staff.id}", staff_id=staff.id)))
