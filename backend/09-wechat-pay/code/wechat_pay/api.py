"""支付接口（薄层）：顾客端支付 / 查询、微信回调、后台查询 / 退款 / 对账 / 导出。业务全部在 service。

鉴权：
- 顾客端：identity.api.current_user（4010）；他人支付单 4040。
- 回调：公开，但必须通过 APIv3 验签；不做登录态校验（4012075420）。应答为渠道格式（SPEC §4 例外）。
- 后台：Bearer 员工令牌 + 权限码（payment:read / payment:refund / payment:reconcile）；授权器可替换（set_staff_authorizer）。
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field as PField
from sqlalchemy.orm import Session

from luzback import (
    AppError, ErrorCode, decode_token, export_csv, get_db, get_settings, idempotency_key, ok, run_idempotent, write_audit,
)
from luzback.export import cents_to_yuan

from . import adapters, apiv3, service
from .models import RefundRecord

router = APIRouter(tags=["payment"])
VERIFY_FAILED_METRIC = "luz_payment_notify_verify_failed_total"
routers = [router]

adapters.install_defaults()

# ---------------------------------------------------------------- 顾客鉴权


def current_user(creds: HTTPAuthorizationCredentials | None = Depends(HTTPBearer(auto_error=False))) -> int:
    """顾客 id：委托 identity.api（CONTRACTS L1）；identity 不可导入时退化为框架令牌校验（同一密钥、kind=access）。"""
    if creds is None:
        raise AppError(ErrorCode.UNAUTHORIZED, "missing bearer token")
    try:
        from identity import api as identity_api
    except Exception:  # noqa: BLE001
        settings = get_settings()
        return int(decode_token(creds.credentials, settings.jwt_secret, kind="access", issuer=settings.jwt_issuer)["sub"])
    return identity_api.current_user(identity_api.current_claims(creds))


# ---------------------------------------------------------------- 后台鉴权


@dataclass
class StaffContext:
    id: int
    store_ids: list[int] | None = None  # None = 全部门店


StaffAuthorizer = Callable[[Session, dict, str], StaffContext]
_bearer = HTTPBearer(auto_error=False)
_staff_authorizer: StaffAuthorizer | None = None


def set_staff_authorizer(fn: StaffAuthorizer | None) -> None:
    """替换后台授权器：fn(db, 令牌声明, 权限码) -> StaffContext；无权限抛 4030。"""
    global _staff_authorizer
    _staff_authorizer = fn


def _rbac_authorizer(db: Session, claims: dict, code: str) -> StaffContext:
    """默认：委托 22-admin-rbac（停用 / 版本校验、权限码、门店范围）。"""
    try:
        from admin_rbac import service as rbac
    except Exception as exc:  # noqa: BLE001
        raise AppError(ErrorCode.FORBIDDEN, "admin authorization unavailable") from exc
    staff = rbac.load_active_staff(db, int(claims["sub"]), claims.get("rv"))
    rbac.assert_permission(db, staff, code)
    return StaffContext(id=staff.id, store_ids=rbac.store_scope(staff))


def require_staff(code: str):
    def dependency(creds: HTTPAuthorizationCredentials | None = Depends(_bearer), db: Session = Depends(get_db)) -> StaffContext:
        if creds is None:
            raise AppError(ErrorCode.UNAUTHORIZED, "missing bearer token")
        settings = get_settings()
        claims = decode_token(creds.credentials, settings.jwt_secret, kind="admin", issuer=settings.jwt_issuer)
        return (_staff_authorizer or _rbac_authorizer)(db, claims, code)

    return dependency


def _fingerprint(body: BaseModel | None) -> str:
    return hashlib.sha256((body.model_dump_json() if body else "").encode()).hexdigest()


# ---------------------------------------------------------------- 顾客端


class PayIn(BaseModel):
    business_type: str = PField(min_length=1, max_length=32, pattern=r"^[a-z_]+$")
    business_id: int = PField(ge=1)


@router.post("/api/v1/app/orders/{order_id}/pay")
def pay_order(order_id: int, user_id: int = Depends(current_user), db: Session = Depends(get_db),
              key: str = Depends(idempotency_key)):
    """去支付 / 继续支付订单：服务端决定复用 prepay_id、原单号重新下单或关单换号。"""
    result = run_idempotent(db, f"payment.pay:order:{order_id}", key,
                            lambda: service.pay_business(db, user_id, "order", order_id),
                            actor=f"user:{user_id}", fingerprint=_fingerprint(None), require_key=True)
    return ok(result)


@router.post("/api/v1/app/payments")
def pay(body: PayIn, user_id: int = Depends(current_user), db: Session = Depends(get_db),
        key: str = Depends(idempotency_key)):
    """通用业务支付（订单、会员卡、充值、预约订金…），金额由业务模块注册的解析器给出。"""
    result = run_idempotent(db, "payment.pay", key,
                            lambda: service.pay_business(db, user_id, body.business_type, body.business_id),
                            actor=f"user:{user_id}", fingerprint=_fingerprint(body), require_key=True)
    return ok(result)


@router.get("/api/v1/app/payments/{out_trade_no}")
def payment_status(out_trade_no: str, user_id: int = Depends(current_user), db: Session = Depends(get_db)):
    """支付结果确认页轮询：以服务端状态为准；未完成时节流查单。"""
    return ok(service.payment_status_for_user(db, user_id, out_trade_no))


# ---------------------------------------------------------------- 微信回调


def _notify_response(result: service.NotifyResult) -> Response:
    if result.http_status in (200, 204):
        return Response(status_code=204)
    return JSONResponse(status_code=result.http_status, content=result.body or apiv3.failure_body())


async def _notify(request: Request, db: Session, handler) -> Response:
    body = await request.body()  # 原始字节验签，不能先解析再序列化
    headers = dict(request.headers)
    try:
        result = await run_in_threadpool(handler, db, headers, body)
    except Exception:  # noqa: BLE001 - 任何内部错误都按渠道格式应答失败，微信会重发
        service.log.exception("wechat pay notify failed")
        return JSONResponse(status_code=500, content=apiv3.failure_body("系统繁忙"))
    if result.http_status == 401:  # 验签失败（含签名探测）：告警与回滚触发器依赖此指标名
        metrics = getattr(request.app.state, "metrics", None)
        if metrics is not None:
            metrics.inc(VERIFY_FAILED_METRIC)
    return _notify_response(result)


@router.post("/api/v1/webhooks/wechat/pay", include_in_schema=False)
async def pay_notify(request: Request, db: Session = Depends(get_db)):
    return await _notify(request, db, service.handle_pay_notify)


@router.post("/api/v1/payments/notify/wechat", include_in_schema=False)
async def pay_notify_legacy(request: Request, db: Session = Depends(get_db)):
    """兼容 v1 回调地址；新部署用 /api/v1/webhooks/wechat/pay。"""
    return await _notify(request, db, service.handle_pay_notify)


@router.post("/api/v1/webhooks/wechat/refund", include_in_schema=False)
async def refund_notify(request: Request, db: Session = Depends(get_db)):
    return await _notify(request, db, service.handle_refund_notify)


# ---------------------------------------------------------------- 后台：支付单


@router.get("/api/v1/admin/payments")
def admin_payments(request: Request, staff: StaffContext = Depends(require_staff("payment:read")),
                   db: Session = Depends(get_db)):
    return ok(service.PAYMENT_LIST.page(db, service.payments_query(staff.store_ids), request.query_params,
                                        serialize=service.payment_view))


PAYMENT_EXPORT_HEADERS = ["支付单ID", "业务类型", "业务单号", "门店", "商户订单号", "微信支付单号", "金额(元)", "状态",
                          "已退款(元)", "需人工", "创建时间", "支付时间"]


def _payment_row(p) -> list[Any]:
    return [p.id, p.business_type, p.business_no or p.business_id, p.store_id, p.out_trade_no, p.channel_txn_id,
            cents_to_yuan(p.amount_cents), p.status, cents_to_yuan(p.refund_succeeded_cents), p.needs_manual,
            p.created_at, p.paid_at]


def _csv(db: Session, staff: StaffContext, action: str, spec, stmt, params, headers, row, filename: str) -> StreamingResponse:
    stream = export_csv(db, spec, stmt, params, headers, row)
    write_audit(db, "staff", staff.id, action, "export", filename, after={"params": dict(params)})
    db.commit()
    return StreamingResponse(stream, media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{filename}.csv"'})


@router.get("/api/v1/admin/payments/export")
def admin_payments_export(request: Request, staff: StaffContext = Depends(require_staff("payment:read")),
                          db: Session = Depends(get_db)):
    return _csv(db, staff, "payment.export", service.PAYMENT_LIST, service.payments_query(staff.store_ids),
                request.query_params, PAYMENT_EXPORT_HEADERS, _payment_row, "payments")


@router.get("/api/v1/admin/payments/{payment_id}")
def admin_payment_detail(payment_id: int, staff: StaffContext = Depends(require_staff("payment:read")),
                         db: Session = Depends(get_db)):
    return ok(service.admin_payment_detail(db, payment_id, staff.store_ids))


def _own_payment(db: Session, payment_id: int, staff: StaffContext) -> None:
    service.admin_payment_detail(db, payment_id, staff.store_ids)  # 4040：不存在或不在门店范围


@router.post("/api/v1/admin/payments/{payment_id}/sync")
def admin_payment_sync(payment_id: int, staff: StaffContext = Depends(require_staff("payment:reconcile")),
                       db: Session = Depends(get_db), key: str = Depends(idempotency_key)):
    """人工查单同步（与回调同一处理逻辑）。"""
    _own_payment(db, payment_id, staff)
    out_trade_no = service.admin_payment_detail(db, payment_id)["out_trade_no"]

    def work():
        result = service.query_and_reconcile(db, out_trade_no)
        write_audit(db, "staff", staff.id, "payment.sync", "payment", payment_id, after={"status": result["status"]})
        return result

    return ok(run_idempotent(db, f"payment.sync:{payment_id}", key, work, actor=f"staff:{staff.id}", require_key=True))


@router.post("/api/v1/admin/payments/{payment_id}/close")
def admin_payment_close(payment_id: int, staff: StaffContext = Depends(require_staff("payment:reconcile")),
                        db: Session = Depends(get_db), key: str = Depends(idempotency_key)):
    """人工关单：先查单，未支付才关单。"""
    _own_payment(db, payment_id, staff)

    def work():
        result = service.close_payment(db, payment_id, "admin_close")
        write_audit(db, "staff", staff.id, "payment.close", "payment", payment_id, after=result)
        return result

    return ok(run_idempotent(db, f"payment.close:{payment_id}", key, work, actor=f"staff:{staff.id}", require_key=True))


class RefundIn(BaseModel):
    amount_cents: int = PField(ge=1, le=100_000_000)
    reason: str = PField(min_length=1, max_length=80)


@router.post("/api/v1/admin/payments/{payment_id}/refunds")
def admin_create_refund(payment_id: int, body: RefundIn, staff: StaffContext = Depends(require_staff("payment:refund")),
                        db: Session = Depends(get_db), key: str = Depends(idempotency_key)):
    """后台直接退款（售后流程之外的人工退款）。规则：≤ 可退、≤ 50 次、间隔 ≥ 1 分钟、365 天内。"""
    _own_payment(db, payment_id, staff)
    result = run_idempotent(
        db, f"payment.refund:{payment_id}", key,
        lambda: service.admin_refund(db, payment_id, body.amount_cents, body.reason, staff_id=staff.id),
        actor=f"staff:{staff.id}", fingerprint=_fingerprint(body), require_key=True)
    return ok(result)


# ---------------------------------------------------------------- 后台：退款单


@router.get("/api/v1/admin/refunds")
def admin_refunds(request: Request, staff: StaffContext = Depends(require_staff("payment:read")),
                  db: Session = Depends(get_db)):
    return ok(service.REFUND_LIST.page(db, service.refunds_query(staff.store_ids), request.query_params,
                                       serialize=lambda row: service.refund_view(row[0], row[1])))


REFUND_EXPORT_HEADERS = ["退款单ID", "支付单ID", "商户订单号", "商户退款单号", "微信退款单号", "金额(元)", "状态", "原因",
                         "发起人", "需人工", "创建时间", "成功时间"]


def _refund_row(row) -> list[Any]:
    r, out_trade_no = row[0], row[1]
    return [r.id, r.payment_id, out_trade_no, r.out_refund_no, r.channel_refund_id, cents_to_yuan(r.amount_cents), r.status,
            r.reason, r.requested_by, r.needs_manual, r.created_at, r.succeeded_at]


@router.get("/api/v1/admin/refunds/export")
def admin_refunds_export(request: Request, staff: StaffContext = Depends(require_staff("payment:read")),
                         db: Session = Depends(get_db)):
    return _csv(db, staff, "refund.export", service.REFUND_LIST, service.refunds_query(staff.store_ids),
                request.query_params, REFUND_EXPORT_HEADERS, _refund_row, "refunds")


@router.get("/api/v1/admin/refunds/{refund_id}")
def admin_refund_detail(refund_id: int, staff: StaffContext = Depends(require_staff("payment:read")),
                        db: Session = Depends(get_db)):
    return ok(service.admin_refund_detail(db, refund_id, staff.store_ids))


class AbnormalIn(BaseModel):
    type: Literal["USER_BANK_CARD", "MERCHANT_BANK_CARD"]
    bank_type: str | None = PField(default=None, max_length=16)
    bank_account: str | None = PField(default=None, max_length=32, pattern=r"^\d{8,32}$")
    real_name: str | None = PField(default=None, max_length=32)


@router.post("/api/v1/admin/refunds/{refund_id}/abnormal")
def admin_abnormal_refund(refund_id: int, body: AbnormalIn, staff: StaffContext = Depends(require_staff("payment:refund")),
                          db: Session = Depends(get_db), key: str = Depends(idempotency_key)):
    """ABNORMAL 退款发起异常退款；银行卡号与姓名只用于加密提交，不落库、不进审计。"""
    service.admin_refund_detail(db, refund_id, staff.store_ids)
    result = run_idempotent(
        db, f"payment.refund.abnormal:{refund_id}", key,
        lambda: service.apply_abnormal_refund(db, refund_id, refund_type=body.type, bank_type=body.bank_type,
                                              bank_account=body.bank_account, real_name=body.real_name, staff_id=staff.id),
        actor=f"staff:{staff.id}", fingerprint=_fingerprint(body), require_key=True)
    return ok(result)


@router.post("/api/v1/admin/refunds/{refund_id}/sync")
def admin_refund_sync(refund_id: int, staff: StaffContext = Depends(require_staff("payment:refund")),
                      db: Session = Depends(get_db), key: str = Depends(idempotency_key)):
    service.admin_refund_detail(db, refund_id, staff.store_ids)

    def work():
        refund = db.get(RefundRecord, refund_id)
        try:
            status = service.sync_refund(db, refund, service.utcnow())
        except service.WechatPayApiError as exc:
            raise AppError(ErrorCode.UPSTREAM_FAILED, "退款查询失败", data={"code": exc.code}) from exc
        write_audit(db, "staff", staff.id, "payment.refund.sync", "refund", refund_id, after={"status": status})
        return {"refund_id": refund_id, "status": status}

    return ok(run_idempotent(db, f"payment.refund.sync:{refund_id}", key, work, actor=f"staff:{staff.id}", require_key=True))


# ---------------------------------------------------------------- 后台：对账与发货


@router.get("/api/v1/admin/reconciliation/batches")
def admin_recon_batches(request: Request, staff: StaffContext = Depends(require_staff("payment:read")),
                        db: Session = Depends(get_db)):
    from sqlalchemy import select

    from .models import ReconBatch

    return ok(service.BATCH_LIST.page(db, select(ReconBatch), request.query_params, serialize=service.batch_view))


@router.get("/api/v1/admin/reconciliation/diffs")
def admin_recon_diffs(request: Request, staff: StaffContext = Depends(require_staff("payment:read")),
                      db: Session = Depends(get_db)):
    from sqlalchemy import select

    from .models import ReconDiff

    return ok(service.DIFF_LIST.page(db, select(ReconDiff), request.query_params, serialize=service.diff_view))


DIFF_EXPORT_HEADERS = ["差异ID", "账单日期", "差异类型", "对象", "单号", "商户订单号", "本地金额(元)", "微信金额(元)",
                       "本地状态", "微信状态", "已处理", "备注"]


def _diff_row(d) -> list[Any]:
    return [d.id, d.bill_date.isoformat(), d.diff_type, d.ref_type, d.ref_no, d.out_trade_no,
            cents_to_yuan(d.local_amount_cents), cents_to_yuan(d.remote_amount_cents), d.local_status, d.remote_status,
            d.resolved, d.note]


@router.get("/api/v1/admin/reconciliation/diffs/export")
def admin_recon_diffs_export(request: Request, staff: StaffContext = Depends(require_staff("payment:reconcile")),
                             db: Session = Depends(get_db)):
    from sqlalchemy import select

    from .models import ReconDiff

    return _csv(db, staff, "reconcile.export", service.DIFF_LIST, select(ReconDiff), request.query_params,
                DIFF_EXPORT_HEADERS, _diff_row, "reconciliation_diffs")


class ReconcileIn(BaseModel):
    bill_date: date
    force: bool = False


@router.post("/api/v1/admin/reconciliation/run")
def admin_recon_run(body: ReconcileIn, staff: StaffContext = Depends(require_staff("payment:reconcile")),
                    db: Session = Depends(get_db), key: str = Depends(idempotency_key)):
    def work():
        result = service.reconcile_bill(db, body.bill_date, force=body.force)
        write_audit(db, "staff", staff.id, "payment.reconcile.run", "recon_batch", body.bill_date.isoformat(),
                    after={"status": result["status"], "diff_count": result["diff_count"]})
        return result

    return ok(run_idempotent(db, "payment.reconcile.run", key, work, actor=f"staff:{staff.id}",
                             fingerprint=_fingerprint(body), require_key=True))


class ResolveIn(BaseModel):
    note: str = PField(min_length=1, max_length=200)


@router.post("/api/v1/admin/reconciliation/diffs/{diff_id}/resolve")
def admin_recon_resolve(diff_id: int, body: ResolveIn, staff: StaffContext = Depends(require_staff("payment:reconcile")),
                        db: Session = Depends(get_db), key: str = Depends(idempotency_key)):
    return ok(run_idempotent(db, f"payment.reconcile.resolve:{diff_id}", key,
                             lambda: service.resolve_diff(db, diff_id, staff.id, body.note),
                             actor=f"staff:{staff.id}", fingerprint=_fingerprint(body), require_key=True))


@router.get("/api/v1/admin/payment-shipping")
def admin_shipping(request: Request, staff: StaffContext = Depends(require_staff("payment:read")),
                   db: Session = Depends(get_db)):
    from sqlalchemy import select

    from .models import ShippingUpload

    return ok(service.SHIPPING_LIST.page(db, select(ShippingUpload), request.query_params, serialize=service.shipping_view))


@router.post("/api/v1/admin/payment-shipping/{upload_id}/retry")
def admin_shipping_retry(upload_id: int, staff: StaffContext = Depends(require_staff("payment:reconcile")),
                         db: Session = Depends(get_db), key: str = Depends(idempotency_key)):
    return ok(run_idempotent(db, f"payment.shipping.retry:{upload_id}", key,
                             lambda: service.retry_shipping_upload(db, upload_id, staff.id),
                             actor=f"staff:{staff.id}", require_key=True))
