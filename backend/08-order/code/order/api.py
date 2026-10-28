"""订单接口（薄层）：顾客端试算 / 下单 / 列表 / 详情 / 取消 / 支付 / 确认收货；后台列表 / 导出 / 详情 / 备注 / 关单 / 发货 / 履约。

鉴权：顾客端 identity.api.current_user（4010；他人订单 4040）；后台 admin_rbac.require_permission（4010 / 4030），
门店范围外的订单按不存在处理（4040）。写接口一律 Idempotency-Key（8–128 位）。
微信支付的调起也可直接用 09 的 `POST /api/v1/app/orders/{id}/pay`（同一解析器）。
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

declare_permissions("order", service.PERMISSIONS.items())  # 权限码目录（22-admin-rbac 角色配置用）

app_router = APIRouter(prefix="/api/v1/app/orders", tags=["orders"])
admin_router = APIRouter(prefix="/api/v1/admin/orders", tags=["orders-admin"])
routers = [app_router, admin_router]
router = app_router  # v1 名称


def _fp(body: BaseModel | None) -> str:
    return hashlib.sha256((body.model_dump_json() if body else "").encode()).hexdigest()


def _idem(db: Session, scope: str, key: str, actor: str, body: BaseModel | None, fn) -> Any:
    return run_idempotent(db, scope, key, fn, actor=actor, fingerprint=_fp(body), require_key=True)


# ---------------------------------------------------------------- 顾客端


class ItemIn(BaseModel):
    kind: str = PField(default="goods", min_length=1, max_length=24, pattern=r"^[a-z_]+$")
    sku_id: int | None = PField(default=None, ge=1)
    product_id: int | None = PField(default=None, ge=1)
    qty: int = PField(ge=1, le=999)


class PreviewIn(BaseModel):
    store_id: int | None = PField(default=None, ge=1)
    items: list[ItemIn] = PField(min_length=1, max_length=100)
    coupon_ids: list[int] = PField(default_factory=list, max_length=5)


class SubmitIn(BaseModel):
    quote_token: str = PField(min_length=8, max_length=64)
    terms_accepted: bool = False
    terms_version: str | None = PField(default=None, max_length=32)
    remark: str = PField(default="", max_length=200)
    address_id: int | None = PField(default=None, ge=1)
    expected_total_cents: int | None = PField(default=None, ge=0)


class CancelIn(BaseModel):
    reason: str = PField(default="", max_length=200)


class PayIn(BaseModel):
    method: Literal["wechat", "balance"] = "wechat"


@app_router.post("/preview")
def preview(body: PreviewIn, user_id: int = Depends(current_user), db: Session = Depends(get_db)):
    """试算：服务端价格、活动、券（可用 / 不可用原因）、库存与限购提示；返回 quote_token（ORDER_QUOTE_TTL 秒内有效）。"""
    result = service.preview(db, user_id, body.model_dump())
    db.commit()
    return ok(result)


@app_router.post("")
def submit(body: SubmitIn, user_id: int = Depends(current_user), db: Session = Depends(get_db),
           key: str = Depends(idempotency_key)):
    """提交订单：重新计价，与报价不一致 → 4223；同一报价只能下一单。"""
    return ok(_idem(db, "order.submit", key, f"user:{user_id}", body,
                    lambda: service.submit(db, user_id, body.model_dump())))


@app_router.get("")
def my_orders(request: Request, user_id: int = Depends(current_user), db: Session = Depends(get_db)):
    """我的订单：tab=all|unpaid|to_fulfil|to_receive|completed|closed，游标分页，附各页签数量。"""
    return ok(service.app_list(db, user_id, request.query_params, get_settings().jwt_secret))


@app_router.get("/{order_id}")
def detail(order_id: int, user_id: int = Depends(current_user), db: Session = Depends(get_db)):
    return ok(service.app_detail(db, user_id, order_id))


@app_router.post("/{order_id}/cancel")
def cancel(order_id: int, body: CancelIn | None = None, user_id: int = Depends(current_user),
           db: Session = Depends(get_db), key: str = Depends(idempotency_key)):
    body = body or CancelIn()
    return ok(_idem(db, f"order.cancel:{order_id}", key, f"user:{user_id}", body,
                    lambda: service.cancel(db, user_id, order_id, body.reason)))


@app_router.post("/{order_id}/payments")
def pay(order_id: int, body: PayIn, user_id: int = Depends(current_user), db: Session = Depends(get_db),
        key: str = Depends(idempotency_key)):
    """去支付：wechat 返回 wx.requestPayment 参数；balance 余额扣款并立即成交。"""
    return ok(_idem(db, f"order.pay:{order_id}", key, f"user:{user_id}", body,
                    lambda: service.pay(db, user_id, order_id, body.method)))


@app_router.post("/{order_id}/confirm-receipt")
def confirm_receipt(order_id: int, user_id: int = Depends(current_user), db: Session = Depends(get_db),
                    key: str = Depends(idempotency_key)):
    return ok(_idem(db, f"order.confirm:{order_id}", key, f"user:{user_id}", None,
                    lambda: service.order_summary(service.confirm_receipt(db, user_id, order_id))))


# ---------------------------------------------------------------- 后台


class NoteIn(BaseModel):
    text: str = PField(min_length=1, max_length=500)


class CloseIn(BaseModel):
    reason: str = PField(min_length=2, max_length=200)


class ShipIn(BaseModel):
    express_company: str = PField(min_length=1, max_length=64)
    tracking_no: str = PField(min_length=4, max_length=64, pattern=r"^[A-Za-z0-9-]+$")


@admin_router.get("")
def admin_list(request: Request, staff: Staff = Depends(require_permission("order:read")),
               db: Session = Depends(get_db)):
    """筛选：status、refund_status、kind、goods_kind、pay_method、store_id、user_id、needs_manual、
    total_cents_min/max、created_at_from/to、paid_at_from/to；keyword=订单号 / 脱敏手机号片段 / 完整手机号（哈希匹配）。"""
    return ok(service.admin_list(db, request.query_params, store_scope(staff)))


@admin_router.get("/export")
def admin_export(request: Request, staff: Staff = Depends(require_permission("order:export")),
                 db: Session = Depends(get_db)):
    stmt, params = service.admin_query(request.query_params, store_scope(staff))
    stream = export_csv(db, service.ADMIN_LIST, stmt, params, service.EXPORT_HEADERS, service.export_row)
    write_audit(db, "staff", staff.id, "order.export", "export", "orders", after={"params": dict(request.query_params)})
    db.commit()
    return StreamingResponse(stream, media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": 'attachment; filename="orders.csv"'})


@admin_router.get("/{order_id}")
def admin_detail(order_id: int, staff: Staff = Depends(require_permission("order:read")),
                 db: Session = Depends(get_db)):
    return ok(service.admin_detail(db, order_id, store_scope(staff)))


@admin_router.post("/{order_id}/notes")
def admin_note(order_id: int, body: NoteIn, staff: Staff = Depends(require_permission("order:remark")),
               db: Session = Depends(get_db), key: str = Depends(idempotency_key)):
    return ok(_idem(db, f"order.note:{order_id}", key, f"staff:{staff.id}", body,
                    lambda: service.add_note(db, staff.id, order_id, body.text, store_scope(staff))))


@admin_router.post("/{order_id}/close")
def admin_close(order_id: int, body: CloseIn, staff: Staff = Depends(require_permission("order:close")),
                db: Session = Depends(get_db), key: str = Depends(idempotency_key)):
    return ok(_idem(db, f"order.close:{order_id}", key, f"staff:{staff.id}", body,
                    lambda: service.admin_close(db, staff.id, order_id, body.reason, store_scope(staff))))


@admin_router.post("/{order_id}/ship")
def admin_ship(order_id: int, body: ShipIn, staff: Staff = Depends(require_permission("order:fulfil")),
               db: Session = Depends(get_db), key: str = Depends(idempotency_key)):
    result = _idem(db, f"order.ship:{order_id}", key, f"staff:{staff.id}", body, lambda: service.order_summary(
        service.ship(db, staff.id, order_id, body.express_company, body.tracking_no, store_scope(staff))))
    service.after_commit_shipping(db, order_id)
    return ok(result)


@admin_router.post("/{order_id}/fulfil")
def admin_fulfil(order_id: int, staff: Staff = Depends(require_permission("order:fulfil")),
                 db: Session = Depends(get_db), key: str = Depends(idempotency_key)):
    result = _idem(db, f"order.fulfil:{order_id}", key, f"staff:{staff.id}", None, lambda: service.order_summary(
        service.mark_fulfilled(db, staff.id, order_id, store_scope(staff))))
    service.after_commit_shipping(db, order_id)
    return ok(result)
