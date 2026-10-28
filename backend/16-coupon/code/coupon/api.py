"""优惠券接口（薄层）：参数校验、身份与权限、幂等提交；业务全部在 service。

顾客端 `/api/v1/app/...`：领券中心（游标）、领取、我的券（状态页签 + 数量）、兑换码、购物车可用券。
后台 `/api/v1/admin/...`：模板增改发布暂停结束删除、统计、批量发放、兑换码批次与导出、会员券列表 / 导出 / 作废。
写接口一律 `Idempotency-Key`（run_idempotent 统一 commit）；后台写操作审计在 service 内同事务完成。
"""
from __future__ import annotations

import csv
import hashlib
import io
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from luzback import AppError, ErrorCode, get_db, get_settings, idempotency_key, ok, run_idempotent, utcnow, write_audit
from luzback.export import cents_to_yuan, export_csv, safe_cell

from . import service
from .config import get_cart_lines_provider
from .deps import Staff, assert_template_scope, current_member, optional_member, require_permission, store_scope
from .models import CouponCodeBatch, CouponIssueBatch, CouponIssueBatchItem, CouponTemplate, UserCoupon

app_router = APIRouter(prefix="/api/v1/app", tags=["coupon"])
admin_router = APIRouter(prefix="/api/v1/admin", tags=["coupon-admin"])


def _fingerprint(body: BaseModel | None) -> str | None:
    return hashlib.sha256(body.model_dump_json().encode()).hexdigest() if body is not None else None


def _cursor_secret() -> str:
    return f"coupon-cursor:{get_settings().jwt_secret}"


def _json(value: Any) -> Any:
    return jsonable_encoder(value)


# ================================================================ 请求模型

Channel = Literal["claim_center", "admin", "event", "lottery", "compensation", "code"]


class TemplateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=500)
    store_id: int | None = Field(default=None, ge=1)
    type: Literal["amount_off", "percent_off", "exchange"]
    amount_cents: int = Field(default=0, ge=0, le=100_000_000)
    rate_bp: int = Field(default=10000, ge=100, le=10000)
    max_discount_cents: int | None = Field(default=None, ge=1, le=100_000_000)
    exchange_qty: int = Field(default=1, ge=1, le=99)
    threshold_cents: int = Field(default=0, ge=0, le=100_000_000)
    scope_type: Literal["all", "categories", "products", "product_kinds"] = "all"
    scope_ids: list[int | str] = Field(default_factory=list, max_length=500)
    exclude_product_ids: list[int] = Field(default_factory=list, max_length=500)
    stack_group: str = Field(default="", max_length=32)
    stack_with_campaign: bool = True
    stack_with_member: bool = True
    valid_type: Literal["fixed", "relative"] = "relative"
    use_start_at: datetime | None = None
    use_end_at: datetime | None = None
    valid_days: int | None = Field(default=None, ge=1, le=3650)
    start_delay_days: int = Field(default=0, ge=0, le=365)
    claim_start_at: datetime | None = None
    claim_end_at: datetime | None = None
    total_stock: int | None = Field(default=None, ge=1, le=100_000_000)
    per_member_limit: int = Field(default=1, ge=0, le=1000)
    per_member_daily_limit: int = Field(default=0, ge=0, le=1000)
    audience: dict[str, Any] = Field(default_factory=lambda: {"type": "all"})
    channels: list[Channel] = Field(default_factory=lambda: ["claim_center"], min_length=1, max_length=6)
    trigger_topic: str | None = Field(default=None, max_length=64)


class TemplatePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)                # 乐观锁：编辑页加载时的版本
    apply_to_issued: bool = False             # 仅对顾客有利的修改可同步给未使用的券
    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=500)
    amount_cents: int | None = Field(default=None, ge=0, le=100_000_000)
    rate_bp: int | None = Field(default=None, ge=100, le=10000)
    max_discount_cents: int | None = Field(default=None, ge=1, le=100_000_000)
    exchange_qty: int | None = Field(default=None, ge=1, le=99)
    threshold_cents: int | None = Field(default=None, ge=0, le=100_000_000)
    scope_type: Literal["all", "categories", "products", "product_kinds"] | None = None
    scope_ids: list[int | str] | None = Field(default=None, max_length=500)
    exclude_product_ids: list[int] | None = Field(default=None, max_length=500)
    stack_group: str | None = Field(default=None, max_length=32)
    stack_with_campaign: bool | None = None
    stack_with_member: bool | None = None
    use_start_at: datetime | None = None
    use_end_at: datetime | None = None
    valid_days: int | None = Field(default=None, ge=1, le=3650)
    start_delay_days: int | None = Field(default=None, ge=0, le=365)
    claim_start_at: datetime | None = None
    claim_end_at: datetime | None = None
    total_stock: int | None = Field(default=None, ge=1, le=100_000_000)
    per_member_limit: int | None = Field(default=None, ge=0, le=1000)
    per_member_daily_limit: int | None = Field(default=None, ge=0, le=1000)
    audience: dict[str, Any] | None = None
    channels: list[Channel] | None = Field(default=None, min_length=1, max_length=6)
    trigger_topic: str | None = Field(default=None, max_length=64)


class IssueBatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_id: int = Field(ge=1)
    user_ids: list[int] = Field(min_length=1, max_length=10000)
    reason: str = Field(min_length=2, max_length=200)
    channel: Literal["admin", "compensation"] = "admin"
    ignore_limit: bool = False


class CodeBatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_id: int = Field(ge=1)
    count: int = Field(ge=1, le=10000)
    name: str = Field(default="", max_length=64)


class RedeemCodeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=8, max_length=64)


class ReasonBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=2, max_length=200)


# ================================================================ 顾客端


@app_router.get("/coupon-templates")
def claim_center(request: Request, user_id: int | None = Depends(optional_member), db: Session = Depends(get_db)):
    """领券中心：进行中、开放领券渠道、在领取期内的模板；登录后附带“我已领张数 / 能否再领”。"""
    now = utcnow()
    page = service.CLAIM_CENTER_SPEC.cursor(db, service.claim_center_stmt(now), request.query_params, _cursor_secret(),
                                            serialize=lambda t: (t, service.template_view(t)))
    rows = page["items"]
    mine = service.claimed_counts(db, user_id, [t.id for t, _ in rows]) if user_id else {}
    items = []
    for t, view in rows:
        if user_id:
            claimed = mine.get(t.id, 0)
            view["claimed_by_me"] = claimed
            view["can_claim"] = not view["sold_out"] and (not t.per_member_limit or claimed < t.per_member_limit)
        items.append(view)
    return ok(_json({"items": items, "next_cursor": page["next_cursor"], "has_more": page["has_more"]}))


@app_router.get("/coupon-templates/{template_id}")
def template_detail(template_id: int, db: Session = Depends(get_db)):
    t = service.get_template(db, template_id)
    if t.status == "draft":
        raise AppError(ErrorCode.NOT_FOUND, "coupon template not found")
    return ok(_json(service.template_view(t)))


@app_router.post("/coupon-templates/{template_id}/claim")
def claim(template_id: int, user_id: int = Depends(current_member), key: str = Depends(idempotency_key),
          db: Session = Depends(get_db)):
    def work():
        return _json(service.coupon_view(service.claim(db, user_id, template_id)))

    return ok(run_idempotent(db, "coupon.claim", key, work, actor=f"user:{user_id}",
                             fingerprint=f"claim:{template_id}", require_key=True))


@app_router.get("/coupons")
def my_coupons(request: Request, user_id: int = Depends(current_member), db: Session = Depends(get_db)):
    """我的券：tab=available（可用，含锁定中）| used | expired；游标分页。"""
    now = utcnow()
    tab = request.query_params.get("tab", "available")
    page = service.MY_COUPON_SPEC.cursor(db, service.my_coupons_stmt(user_id, tab, now), request.query_params,
                                         _cursor_secret(), serialize=lambda c: service.coupon_view(c, now))
    return ok(_json(page))


@app_router.get("/coupons/counts")
def my_coupon_counts(user_id: int = Depends(current_member), db: Session = Depends(get_db)):
    return ok(service.my_coupon_counts(db, user_id))


@app_router.get("/coupons/available-for-cart")
def available_for_cart(user_id: int = Depends(current_member), db: Session = Depends(get_db)):
    """确认订单页：按服务端购物车（价格来自购物车 / 商品模块）列出可用券、不可用原因与推荐券。"""
    provider = get_cart_lines_provider()
    if provider is None:
        raise AppError(ErrorCode.SERVICE_UNAVAILABLE, "cart provider not configured")
    supplied = provider(db, user_id)
    lines, promotions = supplied if isinstance(supplied, tuple) else (supplied, [])
    if not lines:
        return ok({"usable": [], "unusable": [], "best": None})
    return ok(_json(service.available_coupons(db, user_id, lines, promotions)))


@app_router.get("/coupons/{coupon_id}")
def my_coupon(coupon_id: int, user_id: int = Depends(current_member), db: Session = Depends(get_db)):
    c = db.get(UserCoupon, coupon_id)
    if c is None or c.user_id != user_id:
        raise AppError(ErrorCode.NOT_FOUND, "coupon not found")
    return ok(_json(service.coupon_view(c)))


@app_router.post("/coupons/redeem-code")
def redeem_code(body: RedeemCodeBody, user_id: int = Depends(current_member), key: str = Depends(idempotency_key),
                db: Session = Depends(get_db)):
    def work():
        return _json(service.coupon_view(service.redeem_code(db, user_id, body.code)))

    try:
        data = run_idempotent(db, "coupon.redeem_code", key, work, actor=f"user:{user_id}",
                              fingerprint=_fingerprint(body), require_key=True)
    except service.InvalidCode:
        service.record_code_failure(db, user_id)  # 业务事务已回滚；失败计数单独提交（防暴力枚举）
        db.commit()
        raise
    return ok(data)


# ================================================================ 后台：模板


def _scoped(stmt, staff: Staff, column):
    """门店员工：只看自己门店的模板与全局模板（store_id 为空）；全门店员工不加条件。"""
    scope = store_scope(staff)
    if scope is None:
        return stmt
    return stmt.where(or_(column.is_(None), column.in_(scope))) if scope else stmt.where(column.is_(None))


def _check_template(db: Session, staff: Staff, template_id: int | None, write: bool = True) -> None:
    """按模板门店校验范围（模板不存在交给 service 报 4040）。"""
    t = db.get(CouponTemplate, template_id) if template_id else None
    if t is not None:
        assert_template_scope(staff, t.store_id, write)


def _staff_write(db: Session, scope: str, key: str, staff: Staff, body: BaseModel | None, fn):
    return ok(run_idempotent(db, scope, key, lambda: _json(fn()), actor=f"staff:{staff.id}",
                             fingerprint=_fingerprint(body), require_key=True))


@admin_router.get("/coupon-templates")
def admin_templates(request: Request, staff: Staff = Depends(require_permission("coupon:read")), db: Session = Depends(get_db)):
    stmt = _scoped(select(CouponTemplate).where(CouponTemplate.deleted_at.is_(None)), staff, CouponTemplate.store_id)
    return ok(_json(service.TEMPLATE_SPEC.page(db, stmt, request.query_params,
                                               serialize=lambda t: service.template_view(t, admin=True))))


@admin_router.get("/coupon-templates/export")
def export_templates(request: Request, staff: Staff = Depends(require_permission("coupon:export")),
                     db: Session = Depends(get_db)):
    stmt = _scoped(select(CouponTemplate).where(CouponTemplate.deleted_at.is_(None)), staff, CouponTemplate.store_id)
    stream = export_csv(db, service.TEMPLATE_SPEC, stmt, request.query_params,
                        ["ID", "名称", "类型", "状态", "规则", "总量", "已发", "已用", "发布时间"],
                        lambda t: [t.id, t.name, t.type, t.status, service.template_view(t)["rules_text"],
                                   t.total_stock if t.total_stock is not None else "不限", t.issued_count, t.used_count,
                                   t.published_at])
    write_audit(db, "staff", staff.id, "coupon.template.export", "coupon_template", "*",
                after={"params": dict(request.query_params)})
    db.commit()
    return _csv_response(stream, "coupon-templates.csv")


@admin_router.post("/coupon-templates")
def create_template(body: TemplateBody, staff: Staff = Depends(require_permission("coupon:write")),
                    key: str = Depends(idempotency_key), db: Session = Depends(get_db)):
    assert_template_scope(staff, body.store_id)
    return _staff_write(db, "coupon.template.create", key, staff, body,
                        lambda: service.template_view(service.create_template(db, staff.id, body.model_dump()), admin=True))


@admin_router.get("/coupon-templates/{template_id}")
def admin_template(template_id: int, staff: Staff = Depends(require_permission("coupon:read")), db: Session = Depends(get_db)):
    _check_template(db, staff, template_id, write=False)
    return ok(_json(service.template_view(service.get_template(db, template_id), admin=True)))


@admin_router.patch("/coupon-templates/{template_id}")
def update_template(template_id: int, body: TemplatePatch, staff: Staff = Depends(require_permission("coupon:write")),
                    key: str = Depends(idempotency_key), db: Session = Depends(get_db)):
    changes = body.model_dump(exclude_unset=True, exclude={"version", "apply_to_issued"})
    _check_template(db, staff, template_id)
    if "store_id" in changes:
        assert_template_scope(staff, changes["store_id"])
    return _staff_write(db, f"coupon.template.update:{template_id}", key, staff, body,
                        lambda: service.template_view(service.update_template(
                            db, staff.id, template_id, changes, body.version, body.apply_to_issued), admin=True))


def _transition_route(action: str, fn):
    def route(template_id: int, staff: Staff = Depends(require_permission("coupon:write")),
              key: str = Depends(idempotency_key), db: Session = Depends(get_db)):
        _check_template(db, staff, template_id)
        return _staff_write(db, f"coupon.template.{action}:{template_id}", key, staff, None,
                            lambda: service.template_view(fn(db, staff.id, template_id), admin=True))

    route.__name__ = f"{action}_template"
    admin_router.post(f"/coupon-templates/{{template_id}}/{action}")(route)


_transition_route("publish", service.publish_template)
_transition_route("pause", service.pause_template)
_transition_route("resume", service.resume_template)
_transition_route("end", service.end_template)


@admin_router.delete("/coupon-templates/{template_id}")
def delete_template(template_id: int, staff: Staff = Depends(require_permission("coupon:write")),
                    key: str = Depends(idempotency_key), db: Session = Depends(get_db)):
    def work():
        service.delete_template(db, staff.id, template_id)
        return {"deleted": template_id}

    _check_template(db, staff, template_id)
    return _staff_write(db, f"coupon.template.delete:{template_id}", key, staff, None, work)


@admin_router.get("/coupon-templates/{template_id}/stats")
def template_stats(template_id: int, staff: Staff = Depends(require_permission("coupon:read")), db: Session = Depends(get_db)):
    _check_template(db, staff, template_id, write=False)
    return ok(service.template_stats(db, template_id))


# ================================================================ 后台：批量发放


@admin_router.post("/coupon-issue-batches")
def create_issue_batch(body: IssueBatchBody, staff: Staff = Depends(require_permission("coupon:issue")),
                       key: str = Depends(idempotency_key), db: Session = Depends(get_db)):
    def work():
        batch = service.create_issue_batch(db, staff.id, body.template_id, body.user_ids, body.reason, body.channel,
                                           body.ignore_limit)
        return _batch_view(batch)

    _check_template(db, staff, body.template_id)
    return _staff_write(db, "coupon.issue_batch.create", key, staff, body, work)


def _batch_view(b: CouponIssueBatch) -> dict[str, Any]:
    return {"id": b.id, "template_id": b.template_id, "channel": b.channel, "reason": b.reason,
            "ignore_limit": b.ignore_limit, "total": b.total, "issued": b.issued, "failed": b.failed,
            "status": b.status, "created_by": b.created_by, "created_at": b.created_at, "finished_at": b.finished_at}


@admin_router.get("/coupon-issue-batches")
def issue_batches(request: Request, staff: Staff = Depends(require_permission("coupon:read")), db: Session = Depends(get_db)):
    return ok(_json(service.ISSUE_BATCH_SPEC.page(db, select(CouponIssueBatch), request.query_params,
                                                  serialize=_batch_view)))


@admin_router.get("/coupon-issue-batches/{batch_id}")
def issue_batch(batch_id: int, staff: Staff = Depends(require_permission("coupon:read")), db: Session = Depends(get_db)):
    batch = db.get(CouponIssueBatch, batch_id)
    if batch is None:
        raise AppError(ErrorCode.NOT_FOUND, "issue batch not found")
    failures = db.execute(select(CouponIssueBatchItem.user_id, CouponIssueBatchItem.error).where(
        CouponIssueBatchItem.batch_id == batch.id, CouponIssueBatchItem.status == "failed")
        .order_by(CouponIssueBatchItem.id).limit(100)).all()
    return ok(_json({**_batch_view(batch), "failures": [{"user_id": u, "error": e} for u, e in failures]}))


# ================================================================ 后台：兑换码


def _code_batch_view(b: CouponCodeBatch, used: int | None = None) -> dict[str, Any]:
    return {"id": b.id, "template_id": b.template_id, "name": b.name, "count": b.count, "used": used,
            "created_by": b.created_by, "created_at": b.created_at, "exported_at": b.exported_at}


@admin_router.post("/coupon-code-batches")
def create_code_batch(body: CodeBatchBody, staff: Staff = Depends(require_permission("coupon:code")),
                      key: str = Depends(idempotency_key), db: Session = Depends(get_db)):
    _check_template(db, staff, body.template_id)
    return _staff_write(db, "coupon.codes.create", key, staff, body, lambda: _code_batch_view(
        service.create_code_batch(db, staff.id, body.template_id, body.count, body.name)))


@admin_router.get("/coupon-code-batches")
def code_batches(request: Request, staff: Staff = Depends(require_permission("coupon:read")), db: Session = Depends(get_db)):
    return ok(_json(service.CODE_BATCH_SPEC.page(db, select(CouponCodeBatch), request.query_params,
                                                 serialize=_code_batch_view)))


@admin_router.get("/coupon-code-batches/{batch_id}/export")
def export_codes(batch_id: int, staff: Staff = Depends(require_permission("coupon:code")),
                 db: Session = Depends(get_db)):
    batch = db.get(CouponCodeBatch, batch_id)
    _check_template(db, staff, batch.template_id if batch else None)
    rows = service.export_code_rows(db, staff.id, batch_id)
    db.commit()
    buffer = io.StringIO()
    buffer.write("﻿")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(["兑换码", "状态", "使用会员", "使用时间"])
    for row in rows:
        writer.writerow([safe_cell(v) for v in row])
    return _csv_response(iter([buffer.getvalue().encode("utf-8")]), f"coupon-codes-{batch_id}.csv")


@admin_router.post("/coupon-code-batches/{batch_id}/void")
def void_codes(batch_id: int, staff: Staff = Depends(require_permission("coupon:code")),
               key: str = Depends(idempotency_key), db: Session = Depends(get_db)):
    batch = db.get(CouponCodeBatch, batch_id)
    _check_template(db, staff, batch.template_id if batch else None)
    return _staff_write(db, f"coupon.codes.void:{batch_id}", key, staff, None,
                        lambda: {"voided": service.void_code_batch(db, staff.id, batch_id)})


# ================================================================ 后台：会员券


def _member_coupon_row(c: UserCoupon) -> dict[str, Any]:
    return {**service.coupon_view(c), "user_id": c.user_id, "source_ref": c.source_ref,
            "revoke_reason": c.revoke_reason, "returned_at": c.returned_at}


def _member_coupon_query(db: Session, staff: Staff, params) -> tuple[Any, dict[str, Any]]:
    """会员券查询：门店范围（按模板门店）+ 关键词是手机号时走 identity 的哈希查找（手机号不明文入库，不能 LIKE）。"""
    params = dict(params)
    stmt = select(UserCoupon)
    if store_scope(staff) is not None:
        stmt = _scoped(stmt.join(CouponTemplate, CouponTemplate.id == UserCoupon.template_id), staff,
                       CouponTemplate.store_id)
    keyword = (params.get("keyword") or "").strip()
    if service.looks_like_phone(keyword):
        params.pop("keyword")
        user_id = service.user_id_by_phone(db, keyword)
        stmt = stmt.where(UserCoupon.user_id == user_id) if user_id is not None else stmt.where(UserCoupon.id < 0)
    return stmt, params


@admin_router.get("/user-coupons")
def member_coupons(request: Request, staff: Staff = Depends(require_permission("coupon:read")), db: Session = Depends(get_db)):
    """会员券列表：status / template_id / channel / user_id / order_id 筛选，领取、使用、到期时间区间，
    keyword 匹配券名、会员 id、来源。"""
    stmt, params = _member_coupon_query(db, staff, request.query_params)
    return ok(_json(service.MEMBER_COUPON_SPEC.page(db, stmt, params, serialize=_member_coupon_row)))


@admin_router.get("/user-coupons/export")
def export_member_coupons(request: Request, staff: Staff = Depends(require_permission("coupon:export")),
                          db: Session = Depends(get_db)):
    stmt, params = _member_coupon_query(db, staff, request.query_params)
    stream = export_csv(db, service.MEMBER_COUPON_SPEC, stmt, params,
                        ["券ID", "会员ID", "模板ID", "券名", "状态", "渠道", "领取时间", "到期时间", "使用时间", "订单",
                         "抵扣(元)"],
                        lambda c: [c.id, c.user_id, c.template_id, c.name, c.status, c.channel, c.claimed_at,
                                   c.valid_to, c.used_at, c.order_id, cents_to_yuan(c.discount_cents)])
    write_audit(db, "staff", staff.id, "coupon.user_coupons.export", "user_coupon", "*",
                after={"params": dict(request.query_params)})
    db.commit()
    return _csv_response(stream, "user-coupons.csv")


@admin_router.post("/user-coupons/{coupon_id}/revoke")
def revoke_coupon(coupon_id: int, body: ReasonBody, staff: Staff = Depends(require_permission("coupon:write")),
                  key: str = Depends(idempotency_key), db: Session = Depends(get_db)):
    target = db.get(UserCoupon, coupon_id)
    _check_template(db, staff, target.template_id if target else None)
    return _staff_write(db, f"coupon.revoke:{coupon_id}", key, staff, body,
                        lambda: _member_coupon_row(service.revoke(db, staff.id, coupon_id, body.reason)))


def _csv_response(stream, filename: str) -> StreamingResponse:
    chunks = list(stream)  # 在会话关闭前生成完毕（export_csv 已限制行数）
    return StreamingResponse(iter(chunks), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# 统一出口：app.py 的 discover_routers 读取 routers；旧代码 / 集成测试读取 router
router = APIRouter()
router.include_router(app_router)
router.include_router(admin_router)
routers = [app_router, admin_router]  # app.py 逐个挂载；router 保留给只挂一个路由器的旧用法
