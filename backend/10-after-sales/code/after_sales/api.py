"""售后接口（薄层）。

顾客端：identity.api.current_user（4010；他人售后 / 订单 4040）。
后台：admin_rbac.require_permission（4010 / 4030）；同意、重试退款、线下结清还要求近期二次验证（require_permission(..., fresh=True)，4011）；
门店范围外按不存在处理（4040）。写接口一律 Idempotency-Key（8–128 位）。
退款需要调用支付渠道：审批 / 收货 / 重试的事务提交后再调用 submit_refund（失败由 after_sales.submit_refunds 任务补偿）。
"""
from __future__ import annotations

import hashlib
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field as PField
from sqlalchemy.orm import Session

from admin_rbac import Staff, declare_permissions, require_permission, store_scope
from identity.api import current_user
from luzback import export_csv, get_db, get_settings, idempotency_key, ok, run_idempotent, write_audit

from . import service

declare_permissions("after_sales", service.PERMISSIONS.items())  # 权限码目录（22-admin-rbac 角色配置用）
from .rules import REFUNDING

app_router = APIRouter(prefix="/api/v1/app/after-sales", tags=["after-sales"])
admin_router = APIRouter(prefix="/api/v1/admin/after-sales", tags=["after-sales-admin"])
routers = [app_router, admin_router]
router = app_router


def _fp(body: BaseModel | None) -> str:
    return hashlib.sha256((body.model_dump_json() if body else "").encode()).hexdigest()


def _idem(db: Session, scope: str, key: str, actor: str, body: BaseModel | None, fn) -> Any:
    return run_idempotent(db, scope, key, fn, actor=actor, fingerprint=_fp(body), require_key=True)


def _then_refund(db: Session, result: dict[str, Any]) -> dict[str, Any]:
    """审批 / 收货 / 重试提交后立即尝试原路退款；返回最新状态。"""
    if result.get("status") == REFUNDING:
        service.submit_refund(db, int(result["id"]))
        result = service.case_view(db, service.get_case(db, int(result["id"])))
    return result


# ---------------------------------------------------------------- 顾客端


class LineIn(BaseModel):
    item_id: int = PField(ge=1)
    qty: int = PField(ge=0, le=999)
    amount_cents: int | None = PField(default=None, ge=1)


class ApplyIn(BaseModel):
    order_id: int = PField(ge=1)
    type: Literal["refund_only", "return_and_refund"]
    reason_code: str = PField(min_length=2, max_length=24, pattern=r"^[a-z_]+$")
    reason_text: str = PField(default="", max_length=200)
    evidence: list[int] = PField(default_factory=list, max_length=9)
    lines: list[LineIn] = PField(min_length=1, max_length=50)


class ShipIn(BaseModel):
    company: str = PField(min_length=1, max_length=64)
    tracking_no: str = PField(min_length=4, max_length=64, pattern=r"^[A-Za-z0-9-]+$")


@app_router.get("/orders/{order_id}/refundable")
def refundable(order_id: int, user_id: int = Depends(current_user), db: Session = Depends(get_db)):
    """申请页：每行可退数量 / 金额、七日无理由与售后截止时间。"""
    return ok(service.refundable(db, user_id, order_id))


@app_router.post("")
def apply(body: ApplyIn, user_id: int = Depends(current_user), db: Session = Depends(get_db),
          key: str = Depends(idempotency_key)):
    return ok(_idem(db, "after_sale.apply", key, f"user:{user_id}", body,
                    lambda: service.apply(db, user_id, body.model_dump())))


@app_router.get("")
def my_cases(request: Request, user_id: int = Depends(current_user), db: Session = Depends(get_db)):
    """我的售后：tab=all|processing|done，游标分页，附页签数量。"""
    return ok(service.app_list(db, user_id, request.query_params, get_settings().jwt_secret))


@app_router.get("/{case_id}")
def detail(case_id: int, user_id: int = Depends(current_user), db: Session = Depends(get_db)):
    return ok(service.case_view(db, service.get_case(db, case_id, user_id)))


@app_router.post("/{case_id}/cancel")
def cancel(case_id: int, user_id: int = Depends(current_user), db: Session = Depends(get_db),
           key: str = Depends(idempotency_key)):
    return ok(_idem(db, f"after_sale.cancel:{case_id}", key, f"user:{user_id}", None,
                    lambda: service.cancel(db, user_id, case_id)))


@app_router.post("/{case_id}/return-shipment")
def ship_return(case_id: int, body: ShipIn, user_id: int = Depends(current_user), db: Session = Depends(get_db),
                key: str = Depends(idempotency_key)):
    return ok(_idem(db, f"after_sale.ship:{case_id}", key, f"user:{user_id}", body,
                    lambda: service.ship_return(db, user_id, case_id, body.company, body.tracking_no)))


# ---------------------------------------------------------------- 后台


class AdminApplyIn(ApplyIn):
    pass


class ReasonIn(BaseModel):
    reason: str = PField(min_length=2, max_length=200)


class ApproveIn(BaseModel):
    note: str | None = PField(default=None, max_length=200)


class SettleIn(BaseModel):
    note: str = PField(min_length=4, max_length=200)


@admin_router.get("")
def admin_list(request: Request, staff: Staff = Depends(require_permission("after_sale:read")),
               db: Session = Depends(get_db)):
    """筛选：status、type、reason_code、route、store_id、order_id、user_id、amount_cents_min/max、created_at_from/to、
    refunded_at_from/to；keyword=售后单号 / 订单号 / 退货快递单号 / 退款单号；排序 amount_cents / created_at / refunded_at。"""
    return ok(service.admin_list(db, request.query_params, store_scope(staff)))


@admin_router.get("/export")
def admin_export(request: Request, staff: Staff = Depends(require_permission("after_sale:export")),
                 db: Session = Depends(get_db)):
    stream = export_csv(db, service.ADMIN_LIST, service.admin_query(store_scope(staff)), request.query_params,
                        service.EXPORT_HEADERS, service.export_row)
    write_audit(db, "staff", staff.id, "after_sale.export", "export", "after_sales",
                after={"params": dict(request.query_params)})
    db.commit()
    return StreamingResponse(stream, media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": 'attachment; filename="after_sales.csv"'})


@admin_router.get("/{case_id}")
def admin_detail(case_id: int, staff: Staff = Depends(require_permission("after_sale:read")),
                 db: Session = Depends(get_db)):
    return ok(service.case_view(db, service.get_case(db, case_id, store_ids=store_scope(staff)), admin=True))


@admin_router.post("")
def admin_apply(body: AdminApplyIn, staff: Staff = Depends(require_permission("after_sale:create")),
                db: Session = Depends(get_db), key: str = Depends(idempotency_key)):
    """代客发起（电话 / 到店）：审批须由其他员工完成（职责分离）。"""
    return ok(_idem(db, "after_sale.admin_apply", key, f"staff:{staff.id}", body,
                    lambda: service.apply(db, 0, body.model_dump(), staff_id=staff.id, store_ids=store_scope(staff))))


@admin_router.post("/{case_id}/approve")
def approve(case_id: int, body: ApproveIn | None = None, staff: Staff = Depends(require_permission("after_sale:approve", fresh=True)),
            db: Session = Depends(get_db), key: str = Depends(idempotency_key)):
    body = body or ApproveIn()
    result = _idem(db, f"after_sale.approve:{case_id}", key, f"staff:{staff.id}", body,
                   lambda: service.approve(db, staff.id, case_id, store_ids=store_scope(staff), note=body.note))
    return ok(_then_refund(db, result))


@admin_router.post("/{case_id}/reject")
def reject(case_id: int, body: ReasonIn, staff: Staff = Depends(require_permission("after_sale:approve")),
           db: Session = Depends(get_db), key: str = Depends(idempotency_key)):
    return ok(_idem(db, f"after_sale.reject:{case_id}", key, f"staff:{staff.id}", body,
                    lambda: service.reject(db, staff.id, case_id, body.reason, store_ids=store_scope(staff))))


@admin_router.post("/{case_id}/receive")
def receive(case_id: int, staff: Staff = Depends(require_permission("after_sale:receive")),
            db: Session = Depends(get_db), key: str = Depends(idempotency_key)):
    result = _idem(db, f"after_sale.receive:{case_id}", key, f"staff:{staff.id}", None,
                   lambda: service.receive_return(db, staff.id, case_id, store_ids=store_scope(staff)))
    return ok(_then_refund(db, result))


@admin_router.post("/{case_id}/retry-refund")
def retry_refund(case_id: int, staff: Staff = Depends(require_permission("after_sale:refund", fresh=True)),
                 db: Session = Depends(get_db), key: str = Depends(idempotency_key)):
    result = _idem(db, f"after_sale.retry:{case_id}", key, f"staff:{staff.id}", None,
                   lambda: service.retry_refund(db, staff.id, case_id, store_ids=store_scope(staff)))
    return ok(_then_refund(db, result))


@admin_router.post("/{case_id}/manual-settle")
def manual_settle(case_id: int, body: SettleIn, staff: Staff = Depends(require_permission("after_sale:refund", fresh=True)),
                  db: Session = Depends(get_db), key: str = Depends(idempotency_key)):
    return ok(_idem(db, f"after_sale.settle:{case_id}", key, f"staff:{staff.id}", body,
                    lambda: service.manual_settle(db, staff.id, case_id, body.note, store_ids=store_scope(staff))))


@admin_router.post("/{case_id}/close")
def close(case_id: int, body: ReasonIn, staff: Staff = Depends(require_permission("after_sale:refund")),
          db: Session = Depends(get_db), key: str = Depends(idempotency_key)):
    return ok(_idem(db, f"after_sale.close:{case_id}", key, f"staff:{staff.id}", body,
                    lambda: service.admin_close(db, staff.id, case_id, body.reason, store_ids=store_scope(staff))))
