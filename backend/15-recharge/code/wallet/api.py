"""储值接口：顾客端 /api/v1/app/wallet/*，后台 /api/v1/admin/wallet/*。路由只做参数、鉴权、幂等与调用 service。"""
from __future__ import annotations

import hashlib
import json
from typing import Literal

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field as PField
from sqlalchemy import select
from sqlalchemy.orm import Session

from luzback import AppError, ErrorCode, Field, ListSpec, export_csv, get_db, get_settings, idempotency_key, ok, run_idempotent, utcnow, write_audit
from luzback.export import cents_to_yuan

from . import service
from .models import RechargeBatch, RechargePlan, WalletAccount, WalletLedger, WalletMismatch, WalletRefund
from .ports import StaffContext, assert_store_scope, current_user, require_permission

app_router = APIRouter(prefix="/api/v1/app/wallet", tags=["wallet"])
admin_router = APIRouter(prefix="/api/v1/admin/wallet", tags=["wallet-admin"])
routers = [app_router, admin_router]


def _fp(body: BaseModel | dict | None) -> str:
    data = body.model_dump(mode="json") if isinstance(body, BaseModel) else (body or {})
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _run(db: Session, scope: str, key: str, actor: str, body, fn):
    return ok(run_idempotent(db, scope, key, lambda: jsonable_encoder(fn()), actor=actor, fingerprint=_fp(body),
                             require_key=True))


def _secret() -> str:
    return get_settings().jwt_secret


def _scoped(stmt, column, staff: StaffContext):
    if staff.store_scope is not None:
        stmt = stmt.where(column.in_(staff.store_scope))
    return stmt


# ---------------------------------------------------------------- ListSpec 声明

STATUSES = frozenset({"pending", "paid", "closed", "refunding", "refunded", "exhausted"})
SOURCES = frozenset({"payment", "offline", "adjust", "gift", "direct"})
LEDGER_KINDS = frozenset({"recharge", "consume", "reverse", "adjust", "gift", "refund_hold", "refund_release",
                          "refund_done", "bonus_expire", "card_refund"})
REFUND_STATUSES = frozenset({"pending_approval", "processing", "succeeded", "failed", "rejected"})

PLAN_SPEC = ListSpec(fields={
    "status": Field(RechargePlan.status, ops=("eq",), choices=frozenset({"on", "off"})),
    "store_id": Field(RechargePlan.store_id, "int", ops=("eq", "null")),
    "amount_cents": Field(RechargePlan.amount_cents, "int", ops=("range",), sortable=True),
    "seq": Field(RechargePlan.sort, "int", sortable=True),
    "created_at": Field(RechargePlan.created_at, "datetime", ops=("range",), sortable=True),
}, id_column=RechargePlan.id, keyword_columns=(RechargePlan.name,), default_sort="seq")

APP_PLAN_SPEC = ListSpec(fields={"seq": Field(RechargePlan.sort, "int", sortable=True)}, id_column=RechargePlan.id,
                         default_sort="seq", extra_params=frozenset({"store_id"}))

BATCH_SPEC = ListSpec(fields={
    "status": Field(RechargeBatch.status, ops=("eq", "in"), choices=STATUSES),
    "source": Field(RechargeBatch.source, ops=("eq", "in"), choices=SOURCES),
    "user_id": Field(RechargeBatch.user_id, "int"),
    "store_id": Field(RechargeBatch.store_id, "int", ops=("eq", "in")),
    "plan_id": Field(RechargeBatch.plan_id, "int"),
    "needs_manual": Field(RechargeBatch.needs_manual, "bool"),
    "amount_cents": Field(RechargeBatch.amount_cents, "int", ops=("range",), sortable=True),
    "created_at": Field(RechargeBatch.created_at, "datetime", ops=("range",), sortable=True),
    "paid_at": Field(RechargeBatch.paid_at, "datetime", ops=("range", "null"), sortable=True),
}, id_column=RechargeBatch.id, keyword_columns=(RechargeBatch.order_no, RechargeBatch.out_trade_no))

APP_BATCH_SPEC = ListSpec(fields={
    "status": Field(RechargeBatch.status, ops=("eq", "in"), choices=STATUSES),
    "created_at": Field(RechargeBatch.created_at, "datetime", sortable=True),
}, id_column=RechargeBatch.id)

LEDGER_SPEC = ListSpec(fields={
    "kind": Field(WalletLedger.kind, ops=("eq", "in"), choices=LEDGER_KINDS),
    "user_id": Field(WalletLedger.user_id, "int"),
    "batch_id": Field(WalletLedger.batch_id, "int"),
    "store_id": Field(WalletLedger.store_id, "int", ops=("eq", "in")),
    "operator_id": Field(WalletLedger.operator_id, "int"),
    "created_at": Field(WalletLedger.created_at, "datetime", ops=("range",), sortable=True),
}, id_column=WalletLedger.id, keyword_columns=(WalletLedger.ref, WalletLedger.note), max_range_days=None)

APP_LEDGER_SPEC = ListSpec(fields={
    "kind": Field(WalletLedger.kind, ops=("eq", "in"), choices=LEDGER_KINDS),
    "created_at": Field(WalletLedger.created_at, "datetime", sortable=True),
}, id_column=WalletLedger.id)

ACCOUNT_SPEC = ListSpec(fields={
    "status": Field(WalletAccount.status, choices=frozenset({"active", "closed"})),
    "user_id": Field(WalletAccount.user_id, "int"),
    "principal_cents": Field(WalletAccount.principal_cents, "int", ops=("range",), sortable=True),
    "bonus_cents": Field(WalletAccount.bonus_cents, "int", ops=("range",), sortable=True),
    "updated_at": Field(WalletAccount.updated_at, "datetime", ops=("range",), sortable=True),
}, id_column=WalletAccount.id, default_sort="-updated_at")

REFUND_SPEC = ListSpec(fields={
    "status": Field(WalletRefund.status, ops=("eq", "in"), choices=REFUND_STATUSES),
    "cause": Field(WalletRefund.cause, choices=frozenset({"customer", "merchant"})),
    "route": Field(WalletRefund.route, choices=frozenset({"original", "offline"})),
    "user_id": Field(WalletRefund.user_id, "int"),
    "batch_id": Field(WalletRefund.batch_id, "int"),
    "due_at": Field(WalletRefund.due_at, "datetime", ops=("range",), sortable=True),
    "created_at": Field(WalletRefund.created_at, "datetime", ops=("range",), sortable=True),
    "refund_cents": Field(WalletRefund.refund_cents, "int", ops=("range",), sortable=True),
}, id_column=WalletRefund.id, keyword_columns=(WalletRefund.refund_no,))

APP_REFUND_SPEC = ListSpec(fields={"created_at": Field(WalletRefund.created_at, "datetime", sortable=True)},
                           id_column=WalletRefund.id)

MISMATCH_SPEC = ListSpec(fields={
    "resolved": Field(WalletMismatch.resolved, "bool"),
    "check_date": Field(WalletMismatch.check_date),
    "user_id": Field(WalletMismatch.user_id, "int"),
    "created_at": Field(WalletMismatch.created_at, "datetime", sortable=True),
}, id_column=WalletMismatch.id)


def _mismatch_dict(row: WalletMismatch) -> dict:
    return {"id": row.id, "account_id": row.account_id, "user_id": row.user_id, "check_date": row.check_date,
            "detail": json.loads(row.detail_json), "resolved": row.resolved, "created_at": service.aware(row.created_at)}


# ---------------------------------------------------------------- 请求模型


class RechargeOrderIn(BaseModel):
    plan_id: int | None = PField(default=None, ge=1)
    amount_cents: int | None = PField(default=None, ge=1, le=100_000_000)
    store_id: int | None = PField(default=None, ge=1)


class RefundRequestIn(BaseModel):
    reason: str = PField(default="", max_length=200)


class AdminRefundIn(BaseModel):
    cause: Literal["customer", "merchant"]
    reason: str = PField(min_length=1, max_length=200)


class PlanIn(BaseModel):
    store_id: int | None = PField(default=None, ge=1)
    name: str = PField(min_length=1, max_length=64)
    amount_cents: int = PField(ge=1, le=100_000_000)
    bonus_cents: int = PField(default=0, ge=0, le=100_000_000)
    bonus_valid_days: int | None = PField(default=None, ge=1, le=3650)
    sort: int = 0
    sale_start_at: str | None = None
    sale_end_at: str | None = None
    per_user_daily_limit: int | None = PField(default=None, ge=1, le=100)
    terms_text: str = PField(default="", max_length=5000)


class PlanUpdateIn(BaseModel):
    name: str | None = PField(default=None, min_length=1, max_length=64)
    amount_cents: int | None = PField(default=None, ge=1, le=100_000_000)
    bonus_cents: int | None = PField(default=None, ge=0, le=100_000_000)
    bonus_valid_days: int | None = PField(default=None, ge=1, le=3650)
    sort: int | None = None
    per_user_daily_limit: int | None = PField(default=None, ge=1, le=100)
    terms_text: str | None = PField(default=None, max_length=5000)


class AdjustIn(BaseModel):
    principal_delta: int = 0
    bonus_delta: int = 0
    reason: str = PField(min_length=1, max_length=200)


class OfflineRechargeIn(BaseModel):
    store_id: int | None = PField(default=None, ge=1)
    amount_cents: int = PField(ge=1, le=100_000_000)
    bonus_cents: int = PField(default=0, ge=0, le=100_000_000)
    note: str = PField(default="", max_length=200)


class CounterConsumeIn(BaseModel):
    store_id: int | None = PField(default=None, ge=1)
    amount_cents: int = PField(ge=1, le=100_000_000)
    ref: str = PField(min_length=1, max_length=48, pattern=r"^[A-Za-z0-9:_\-.]+$")
    note: str = PField(default="", max_length=200)


class CounterReverseIn(BaseModel):
    ref: str = PField(min_length=1, max_length=48, pattern=r"^[A-Za-z0-9:_\-.]+$")
    reason: str = PField(min_length=1, max_length=200)


class RejectIn(BaseModel):
    reason: str = PField(min_length=1, max_length=200)


class OfflineCompleteIn(BaseModel):
    success: bool = True
    note: str = PField(default="", max_length=200)


def _dt(value: str | None):
    if value in (None, ""):
        return None
    from datetime import datetime  # noqa: PLC0415
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AppError(ErrorCode.BAD_REQUEST, "invalid datetime") from exc
    return service.aware(moment)


# ================================================================ 顾客端


@app_router.get("/plans")
def app_plans(request: Request, store_id: int | None = Query(default=None, ge=1), user_id: int = Depends(current_user),
              db: Session = Depends(get_db)):
    stmt = service.plans_on_sale_stmt(store_id, utcnow())
    return ok(APP_PLAN_SPEC.cursor(db, stmt, request.query_params, _secret(), serialize=service.plan_dict,
                                   row_values=lambda p: {"seq": p.sort, "id": p.id}))


@app_router.post("/recharge-orders")
def app_create_order(body: RechargeOrderIn, key: str = Depends(idempotency_key), user_id: int = Depends(current_user),
                     db: Session = Depends(get_db)):
    return _run(db, "wallet.recharge_order.create", key, f"user:{user_id}", body,
                lambda: service.create_recharge_order(db, user_id, body.store_id, body.plan_id, body.amount_cents))


@app_router.get("/account")
def app_account(user_id: int = Depends(current_user), db: Session = Depends(get_db)):
    return ok(service.account_dict(db, user_id))


@app_router.get("/ledger")
def app_ledger(request: Request, user_id: int = Depends(current_user), db: Session = Depends(get_db)):
    stmt = select(WalletLedger).where(WalletLedger.user_id == user_id)
    return ok(APP_LEDGER_SPEC.cursor(db, stmt, request.query_params, _secret(), serialize=service.ledger_dict))


@app_router.get("/recharge-orders")
def app_orders(request: Request, user_id: int = Depends(current_user), db: Session = Depends(get_db)):
    stmt = select(RechargeBatch).where(RechargeBatch.user_id == user_id)
    return ok(APP_BATCH_SPEC.cursor(db, stmt, request.query_params, _secret(), serialize=service.batch_dict))


@app_router.get("/recharge-orders/{batch_id}")
def app_order(batch_id: int = Path(ge=1), user_id: int = Depends(current_user), db: Session = Depends(get_db)):
    return ok(service.batch_dict(service._batch_for(db, batch_id, user_id)))


@app_router.get("/recharge-orders/{batch_id}/refund-quote")
def app_refund_quote(batch_id: int = Path(ge=1), user_id: int = Depends(current_user), db: Session = Depends(get_db)):
    return ok(service.quote_refund(db, batch_id, "customer", user_id))


@app_router.post("/recharge-orders/{batch_id}/refund-request")
def app_refund_request(body: RefundRequestIn, batch_id: int = Path(ge=1), key: str = Depends(idempotency_key),
                       user_id: int = Depends(current_user), db: Session = Depends(get_db)):
    return _run(db, f"wallet.refund.request:{batch_id}", key, f"user:{user_id}", body,
                lambda: service.request_refund(db, batch_id, "customer", body.reason, f"user:{user_id}", user_id=user_id))


@app_router.get("/refunds")
def app_refunds(request: Request, user_id: int = Depends(current_user), db: Session = Depends(get_db)):
    stmt = select(WalletRefund).where(WalletRefund.user_id == user_id)
    return ok(APP_REFUND_SPEC.cursor(db, stmt, request.query_params, _secret(), serialize=service.refund_dict))


# ================================================================ 后台：档位


@admin_router.get("/plans")
def admin_plans(request: Request, staff: StaffContext = Depends(require_permission("wallet:read")),
                db: Session = Depends(get_db)):
    stmt = select(RechargePlan).where(RechargePlan.deleted_at.is_(None))
    return ok(PLAN_SPEC.page(db, stmt, request.query_params, serialize=service.plan_dict))


@admin_router.post("/plans")
def admin_create_plan(body: PlanIn, key: str = Depends(idempotency_key),
                      staff: StaffContext = Depends(require_permission("wallet:config")), db: Session = Depends(get_db)):
    assert_store_scope(staff, body.store_id) if body.store_id else None
    values = body.model_dump() | {"sale_start_at": _dt(body.sale_start_at), "sale_end_at": _dt(body.sale_end_at)}
    return _run(db, "wallet.plan.create", key, f"staff:{staff.id}", body, lambda: service.create_plan(db, staff.id, values))


@admin_router.put("/plans/{plan_id}")
def admin_update_plan(body: PlanUpdateIn, plan_id: int = Path(ge=1), key: str = Depends(idempotency_key),
                      staff: StaffContext = Depends(require_permission("wallet:config")), db: Session = Depends(get_db)):
    values = body.model_dump(exclude_unset=True)
    return _run(db, f"wallet.plan.update:{plan_id}", key, f"staff:{staff.id}", body,
                lambda: service.update_plan(db, staff.id, plan_id, values))


@admin_router.post("/plans/{plan_id}/{action}")
def admin_plan_status(plan_id: int = Path(ge=1), action: Literal["on-sale", "off-sale"] = Path(),
                      key: str = Depends(idempotency_key), staff: StaffContext = Depends(require_permission("wallet:config")),
                      db: Session = Depends(get_db)):
    status = "on" if action == "on-sale" else "off"
    return _run(db, f"wallet.plan.{status}:{plan_id}", key, f"staff:{staff.id}", None,
                lambda: service.set_plan_status(db, staff.id, plan_id, status))


@admin_router.delete("/plans/{plan_id}")
def admin_delete_plan(plan_id: int = Path(ge=1), key: str = Depends(idempotency_key),
                      staff: StaffContext = Depends(require_permission("wallet:config")), db: Session = Depends(get_db)):
    return _run(db, f"wallet.plan.delete:{plan_id}", key, f"staff:{staff.id}", None,
                lambda: service.delete_plan(db, staff.id, plan_id))


# ================================================================ 后台：充值单、流水、账户（列表 + 导出）

BATCH_HEADERS = ("充值单号", "会员ID", "门店ID", "来源", "实付(元)", "赠送(元)", "剩余本金(元)", "剩余赠送(元)", "已退(元)",
                 "状态", "支付时间", "发票编码", "创建时间")


def _batch_row(b: RechargeBatch) -> list:
    return [b.order_no, b.user_id, b.store_id, b.source, cents_to_yuan(b.amount_cents), cents_to_yuan(b.bonus_cents),
            cents_to_yuan(b.principal_remaining_cents), cents_to_yuan(b.bonus_remaining_cents),
            cents_to_yuan(b.refunded_cents), b.status, service.aware(b.paid_at), b.invoice_tax_code,
            service.aware(b.created_at)]


@admin_router.get("/recharge-orders")
def admin_orders(request: Request, staff: StaffContext = Depends(require_permission("wallet:read")),
                 db: Session = Depends(get_db)):
    stmt = _scoped(select(RechargeBatch), RechargeBatch.store_id, staff)
    return ok(BATCH_SPEC.page(db, stmt, request.query_params, serialize=service.batch_dict))


@admin_router.get("/recharge-orders/export")
def admin_orders_export(request: Request, staff: StaffContext = Depends(require_permission("wallet:export")),
                        db: Session = Depends(get_db)):
    stmt = _scoped(select(RechargeBatch), RechargeBatch.store_id, staff)
    stream = export_csv(db, BATCH_SPEC, stmt, request.query_params, BATCH_HEADERS, _batch_row)
    write_audit(db, "staff", staff.id, "wallet.export.recharge_orders", "wallet_batch", "-",
                after={"query": str(request.query_params)})
    db.commit()
    return StreamingResponse(stream, media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": 'attachment; filename="recharge-orders.csv"'})


@admin_router.get("/recharge-orders/{batch_id}")
def admin_order(batch_id: int = Path(ge=1), staff: StaffContext = Depends(require_permission("wallet:read")),
                db: Session = Depends(get_db)):
    batch = service._batch_for(db, batch_id, None)
    assert_store_scope(staff, batch.store_id) if batch.store_id else None
    return ok(service.batch_dict(batch))


LEDGER_HEADERS = ("流水ID", "会员ID", "类型", "本金变动(元)", "赠送变动(元)", "冻结本金变动(元)", "本金余额(元)", "赠送余额(元)",
                  "业务号", "批次", "门店ID", "操作人", "备注", "时间")


def _ledger_row(e: WalletLedger) -> list:
    return [e.id, e.user_id, e.kind, cents_to_yuan(e.principal_delta), cents_to_yuan(e.bonus_delta),
            cents_to_yuan(e.frozen_principal_delta), cents_to_yuan(e.principal_after), cents_to_yuan(e.bonus_after),
            e.ref, e.batch_id, e.store_id, e.operator_id, e.note, service.aware(e.created_at)]


@admin_router.get("/ledger")
def admin_ledger(request: Request, staff: StaffContext = Depends(require_permission("wallet:read")),
                 db: Session = Depends(get_db)):
    return ok(LEDGER_SPEC.page(db, select(WalletLedger), request.query_params, serialize=service.ledger_dict))


@admin_router.get("/ledger/export")
def admin_ledger_export(request: Request, staff: StaffContext = Depends(require_permission("wallet:export")),
                        db: Session = Depends(get_db)):
    stream = export_csv(db, LEDGER_SPEC, select(WalletLedger), request.query_params, LEDGER_HEADERS, _ledger_row)
    write_audit(db, "staff", staff.id, "wallet.export.ledger", "wallet_ledger", "-", after={"query": str(request.query_params)})
    db.commit()
    return StreamingResponse(stream, media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": 'attachment; filename="wallet-ledger.csv"'})


@admin_router.get("/accounts")
def admin_accounts(request: Request, staff: StaffContext = Depends(require_permission("wallet:read")),
                   db: Session = Depends(get_db)):
    return ok(ACCOUNT_SPEC.page(db, select(WalletAccount), request.query_params, serialize=service.account_row_dict))


@admin_router.get("/accounts/{user_id}")
def admin_account(user_id: int = Path(ge=1), staff: StaffContext = Depends(require_permission("wallet:read")),
                  db: Session = Depends(get_db)):
    recent = db.execute(select(WalletLedger).where(WalletLedger.user_id == user_id).order_by(WalletLedger.id.desc())
                        .limit(20)).scalars().all()
    return ok(service.account_dict(db, user_id) | {"recent_ledger": [service.ledger_dict(e) for e in recent]})


@admin_router.post("/accounts/{user_id}/adjust")
def admin_adjust(body: AdjustIn, user_id: int = Path(ge=1), key: str = Depends(idempotency_key),
                 staff: StaffContext = Depends(require_permission("wallet:adjust")), db: Session = Depends(get_db)):
    return _run(db, f"wallet.adjust:{user_id}", key, f"staff:{staff.id}", body,
                lambda: service.adjust(db, staff.id, user_id, body.principal_delta, body.bonus_delta, body.reason,
                                       f"adjust:{staff.id}:{key}"))


@admin_router.post("/accounts/{user_id}/offline-recharge")
def admin_offline_recharge(body: OfflineRechargeIn, user_id: int = Path(ge=1), key: str = Depends(idempotency_key),
                           staff: StaffContext = Depends(require_permission("wallet:recharge")),
                           db: Session = Depends(get_db)):
    assert_store_scope(staff, body.store_id) if body.store_id else None
    return _run(db, f"wallet.offline_recharge:{user_id}", key, f"staff:{staff.id}", body,
                lambda: service.offline_recharge(db, staff.id, user_id, body.store_id, body.amount_cents, body.bonus_cents,
                                                 f"offline:{staff.id}:{key}", body.note))


@admin_router.post("/accounts/{user_id}/consume")
def admin_consume(body: CounterConsumeIn, user_id: int = Path(ge=1), key: str = Depends(idempotency_key),
                  staff: StaffContext = Depends(require_permission("wallet:consume")), db: Session = Depends(get_db)):
    assert_store_scope(staff, body.store_id) if body.store_id else None
    return _run(db, f"wallet.consume:{user_id}", key, f"staff:{staff.id}", body,
                lambda: service.counter_consume(db, staff.id, user_id, body.store_id, body.amount_cents,
                                                f"counter:{body.ref}", body.note))


@admin_router.post("/accounts/{user_id}/reverse")
def admin_reverse(body: CounterReverseIn, user_id: int = Path(ge=1), key: str = Depends(idempotency_key),
                  staff: StaffContext = Depends(require_permission("wallet:consume")), db: Session = Depends(get_db)):
    return _run(db, f"wallet.reverse:{user_id}", key, f"staff:{staff.id}", body,
                lambda: service.counter_reverse(db, staff.id, user_id, f"counter:{body.ref}", body.reason))


# ================================================================ 后台：退款


@admin_router.get("/recharge-orders/{batch_id}/refund-quote")
def admin_refund_quote(batch_id: int = Path(ge=1), cause: Literal["customer", "merchant"] = Query(default="customer"),
                       staff: StaffContext = Depends(require_permission("wallet:refund")), db: Session = Depends(get_db)):
    return ok(service.quote_refund(db, batch_id, cause))


@admin_router.post("/recharge-orders/{batch_id}/refunds")
def admin_create_refund(body: AdminRefundIn, batch_id: int = Path(ge=1), key: str = Depends(idempotency_key),
                        staff: StaffContext = Depends(require_permission("wallet:refund")), db: Session = Depends(get_db)):
    return _run(db, f"wallet.refund.create:{batch_id}", key, f"staff:{staff.id}", body,
                lambda: service.request_refund(db, batch_id, body.cause, body.reason, f"staff:{staff.id}",
                                               staff_id=staff.id))


@admin_router.get("/refunds")
def admin_refunds(request: Request, staff: StaffContext = Depends(require_permission("wallet:read")),
                  db: Session = Depends(get_db)):
    return ok(REFUND_SPEC.page(db, select(WalletRefund), request.query_params, serialize=service.refund_dict))


@admin_router.post("/refunds/{refund_id}/approve")
def admin_approve(refund_id: int = Path(ge=1), key: str = Depends(idempotency_key),
                  staff: StaffContext = Depends(require_permission("wallet:refund")), db: Session = Depends(get_db)):
    return _run(db, f"wallet.refund.approve:{refund_id}", key, f"staff:{staff.id}", None,
                lambda: service.approve_refund(db, refund_id, staff.id))


@admin_router.post("/refunds/{refund_id}/reject")
def admin_reject(body: RejectIn, refund_id: int = Path(ge=1), key: str = Depends(idempotency_key),
                 staff: StaffContext = Depends(require_permission("wallet:refund")), db: Session = Depends(get_db)):
    return _run(db, f"wallet.refund.reject:{refund_id}", key, f"staff:{staff.id}", body,
                lambda: service.reject_refund(db, refund_id, staff.id, body.reason))


@admin_router.post("/refunds/{refund_id}/offline-complete")
def admin_offline_complete(body: OfflineCompleteIn, refund_id: int = Path(ge=1), key: str = Depends(idempotency_key),
                           staff: StaffContext = Depends(require_permission("wallet:refund")),
                           db: Session = Depends(get_db)):
    return _run(db, f"wallet.refund.offline:{refund_id}", key, f"staff:{staff.id}", body,
                lambda: service.complete_offline_refund(db, refund_id, staff.id, body.success, body.note))


# ================================================================ 后台：对账


@admin_router.post("/reconcile")
def admin_reconcile(key: str = Depends(idempotency_key), staff: StaffContext = Depends(require_permission("wallet:reconcile")),
                    db: Session = Depends(get_db)):
    def work():
        result = service.reconcile(db)
        write_audit(db, "staff", staff.id, "wallet.reconcile", "wallet", "-", after=result)
        return result
    return _run(db, "wallet.reconcile", key, f"staff:{staff.id}", None, work)


@admin_router.get("/mismatches")
def admin_mismatches(request: Request, staff: StaffContext = Depends(require_permission("wallet:reconcile")),
                     db: Session = Depends(get_db)):
    return ok(MISMATCH_SPEC.page(db, select(WalletMismatch), request.query_params, serialize=_mismatch_dict))
