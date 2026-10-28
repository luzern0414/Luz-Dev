"""lottery 路由：只做参数、鉴权、幂等键与调用 service。"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict
from pydantic import Field as F
from sqlalchemy.orm import Session

from luzback import get_db, get_settings, idempotency_key, ok
from luzback.app import client_ip

from . import service
from .deps import current_member, declare, require_permission
from .service import StaffActor

declare()

app_router = APIRouter(prefix="/api/v1/app/lottery", tags=["lottery"])
admin_router = APIRouter(prefix="/api/v1/admin/lottery", tags=["lottery-admin"])
routers = [app_router, admin_router]

Kind = Literal["wheel", "grid", "scratch"]
PrizeKind = Literal["coupon", "points", "member_card_days", "physical", "thanks"]
Policy = Literal["thanks", "exclude_disclosed"]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PrizeIn(_In):
    seq: int = F(ge=1, le=99)
    name: str = F(min_length=1, max_length=64)
    kind: PrizeKind
    ref_id: int | None = F(default=None, ge=1)
    quantity: int = F(default=1, ge=0, le=1_000_000)
    value_cents: int = F(default=0, ge=0, le=1_000_000_000)  # 5 万元封顶由服务层判定（4220）
    weight_bp: int = F(ge=1, le=10_000)
    stock_total: int | None = F(default=None, ge=0, le=10_000_000)
    per_member_limit: int = F(default=0, ge=0, le=1000)
    image_url: str | None = F(default=None, max_length=255)
    description: str = F(default="", max_length=255)


class ActivityBase(_In):
    store_id: int | None = F(default=None, ge=1)
    rules_text: str = F(default="", max_length=10_000)
    audience: str = F(default="all", min_length=1, max_length=64)
    free_daily_chances: int = F(default=0, ge=0, le=100)
    limit_daily: int = F(default=0, ge=0, le=1000)
    limit_monthly: int = F(default=0, ge=0, le=10_000)
    limit_total: int = F(default=0, ge=0, le=100_000)
    earn_payment_min_cents: int = F(default=0, ge=0, le=100_000_000)
    earn_payment_chances: int = F(default=0, ge=0, le=100)
    earn_checkin_chances: int = F(default=0, ge=0, le=100)
    out_of_stock_policy: Policy = "thanks"
    budget_cents: int = F(default=0, ge=0, le=2_000_000_000)
    claim_days: int = F(default=7, ge=1, le=90)
    early_end_allowed: bool = False
    publicity_threshold_cents: int | None = F(default=None, ge=1, le=50_000)


class ActivityIn(ActivityBase):
    name: str = F(min_length=1, max_length=64)
    kind: Kind = "wheel"
    start_at: datetime
    end_at: datetime
    prizes: list[PrizeIn] | None = F(default=None, max_length=12)


class ActivityPatch(_In):
    expected_version: int | None = F(default=None, ge=1)
    store_id: int | None = F(default=None, ge=1)
    name: str | None = F(default=None, min_length=1, max_length=64)
    kind: Kind | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    rules_text: str | None = F(default=None, max_length=10_000)
    audience: str | None = F(default=None, min_length=1, max_length=64)
    free_daily_chances: int | None = F(default=None, ge=0, le=100)
    limit_daily: int | None = F(default=None, ge=0, le=1000)
    limit_monthly: int | None = F(default=None, ge=0, le=10_000)
    limit_total: int | None = F(default=None, ge=0, le=100_000)
    earn_payment_min_cents: int | None = F(default=None, ge=0, le=100_000_000)
    earn_payment_chances: int | None = F(default=None, ge=0, le=100)
    earn_checkin_chances: int | None = F(default=None, ge=0, le=100)
    out_of_stock_policy: Policy | None = None
    budget_cents: int | None = F(default=None, ge=0, le=2_000_000_000)
    claim_days: int | None = F(default=None, ge=1, le=90)
    early_end_allowed: bool | None = None
    publicity_threshold_cents: int | None = F(default=None, ge=1, le=50_000)


class PrizesIn(_In):
    prizes: list[PrizeIn] = F(min_length=1, max_length=12)


class StockIn(_In):
    add: int = F(ge=1, le=1_000_000)


class EndIn(_In):
    reason: str = F(min_length=1, max_length=200)


class SimulateIn(_In):
    n: int = F(default=100_000, ge=1, le=1_000_000)
    seed: int | None = None


class GrantIn(_In):
    member_id: int = F(ge=1)
    chances: int = F(ge=1, le=100)
    reason: str = F(min_length=1, max_length=200)


class ShipIn(_In):
    carrier: str = F(min_length=1, max_length=32)
    tracking_no: str = F(min_length=4, max_length=64, pattern=r"^[A-Za-z0-9\-]+$")


class DrawIn(_In):
    store_id: int | None = F(default=None, ge=1)


class ClaimIn(_In):
    name: str = F(min_length=1, max_length=32)
    phone: str = F(min_length=11, max_length=11)
    address: str = F(min_length=5, max_length=200)


def _secret() -> str:
    return get_settings().jwt_secret


# ================================================================ 顾客端

@app_router.get("/activities")
def app_activities(request: Request, store_id: int | None = None, db: Session = Depends(get_db)) -> dict:
    return ok(service.list_open_activities(db, request.query_params, store_id, _secret()))


@app_router.get("/activities/{activity_id}")
def app_activity(activity_id: int, db: Session = Depends(get_db)) -> dict:
    """活动详情：公示规则、奖项、概率（万分比与百分比）、数量、开奖方式、变更记录。"""
    return ok(service.activity_detail(db, activity_id))


@app_router.get("/activities/{activity_id}/winners")
def app_winners(activity_id: int, request: Request, db: Session = Depends(get_db)) -> dict:
    """公开：超过 500 元奖项的兑奖情况（脱敏）。"""
    return ok(service.winners(db, activity_id, request.query_params, _secret()))


@app_router.get("/activities/{activity_id}/chances")
def app_chances(activity_id: int, member_id: int = Depends(current_member), db: Session = Depends(get_db)) -> dict:
    return ok(service.my_chances(db, activity_id, member_id))


@app_router.post("/activities/{activity_id}/draw")
def app_draw(activity_id: int, body: DrawIn, request: Request, member_id: int = Depends(current_member),
             key: str = Depends(idempotency_key), db: Session = Depends(get_db)) -> dict:
    return ok(service.draw(db, activity_id, member_id, key, store_id=body.store_id, ip=client_ip(request)))


@app_router.get("/records")
def app_records(request: Request, member_id: int = Depends(current_member), db: Session = Depends(get_db)) -> dict:
    return ok(service.my_records(db, member_id, request.query_params, _secret()))


@app_router.get("/records/{draw_id}")
def app_record(draw_id: int, member_id: int = Depends(current_member), db: Session = Depends(get_db)) -> dict:
    return ok(service.my_record(db, member_id, draw_id))


@app_router.post("/records/{draw_id}/claim")
def app_claim(draw_id: int, body: ClaimIn, member_id: int = Depends(current_member),
              key: str = Depends(idempotency_key), db: Session = Depends(get_db)) -> dict:
    return ok(service.claim_prize(db, member_id, draw_id, body.name, body.phone, body.address, key))


# ================================================================ 后台

@admin_router.get("/activities")
def admin_activities(request: Request, staff: StaffActor = Depends(require_permission("lottery:read")),
                     db: Session = Depends(get_db)) -> dict:
    return ok(service.list_activities(db, staff, request.query_params))


@admin_router.post("/activities")
def admin_create(body: ActivityIn, staff: StaffActor = Depends(require_permission("lottery:write")),
                 key: str = Depends(idempotency_key), db: Session = Depends(get_db)) -> dict:
    data = body.model_dump(exclude={"prizes"}, exclude_none=True)
    prizes = [p.model_dump() for p in body.prizes] if body.prizes else None
    return ok(service.create_activity(db, staff, data, key, prizes))


@admin_router.get("/activities/{activity_id}")
def admin_activity(activity_id: int, staff: StaffActor = Depends(require_permission("lottery:read")),
                   db: Session = Depends(get_db)) -> dict:
    return ok(service.get_activity_admin(db, staff, activity_id))


@admin_router.patch("/activities/{activity_id}")
def admin_update(activity_id: int, body: ActivityPatch, staff: StaffActor = Depends(require_permission("lottery:write")),
                 key: str = Depends(idempotency_key), db: Session = Depends(get_db)) -> dict:
    changes = body.model_dump(exclude_unset=True)
    version = changes.pop("expected_version", None)
    return ok(service.update_activity(db, staff, activity_id, changes, key, expected_version=version))


@admin_router.delete("/activities/{activity_id}")
def admin_delete(activity_id: int, staff: StaffActor = Depends(require_permission("lottery:write")),
                 key: str = Depends(idempotency_key), db: Session = Depends(get_db)) -> dict:
    return ok(service.delete_activity(db, staff, activity_id, key))


@admin_router.put("/activities/{activity_id}/prizes")
def admin_prizes(activity_id: int, body: PrizesIn, staff: StaffActor = Depends(require_permission("lottery:write")),
                 key: str = Depends(idempotency_key), db: Session = Depends(get_db)) -> dict:
    return ok(service.set_prizes(db, staff, activity_id, [p.model_dump() for p in body.prizes], key))


@admin_router.post("/activities/{activity_id}/prizes/{prize_id}/stock")
def admin_stock(activity_id: int, prize_id: int, body: StockIn,
                staff: StaffActor = Depends(require_permission("lottery:write")),
                key: str = Depends(idempotency_key), db: Session = Depends(get_db)) -> dict:
    return ok(service.add_prize_stock(db, staff, activity_id, prize_id, body.add, key))


@admin_router.post("/activities/{activity_id}/publish")
def admin_publish(activity_id: int, staff: StaffActor = Depends(require_permission("lottery:publish")),
                  key: str = Depends(idempotency_key), db: Session = Depends(get_db)) -> dict:
    return ok(service.publish(db, staff, activity_id, key))


@admin_router.post("/activities/{activity_id}/end")
def admin_end(activity_id: int, body: EndIn, staff: StaffActor = Depends(require_permission("lottery:publish")),
              key: str = Depends(idempotency_key), db: Session = Depends(get_db)) -> dict:
    return ok(service.end_activity(db, staff, activity_id, body.reason, key))


@admin_router.post("/activities/{activity_id}/simulate")
def admin_simulate(activity_id: int, body: SimulateIn, staff: StaffActor = Depends(require_permission("lottery:read")),
                   db: Session = Depends(get_db)) -> dict:
    return ok(service.simulate(db, staff, activity_id, body.n, body.seed))


@admin_router.get("/activities/{activity_id}/stats")
def admin_stats(activity_id: int, staff: StaffActor = Depends(require_permission("lottery:read")),
                db: Session = Depends(get_db)) -> dict:
    return ok(service.stats(db, staff, activity_id))


@admin_router.post("/activities/{activity_id}/grants")
def admin_grant(activity_id: int, body: GrantIn, staff: StaffActor = Depends(require_permission("lottery:grant")),
                key: str = Depends(idempotency_key), db: Session = Depends(get_db)) -> dict:
    return ok(service.admin_grant(db, staff, activity_id, body.member_id, body.chances, body.reason, key))


@admin_router.get("/grants")
def admin_grants(request: Request, staff: StaffActor = Depends(require_permission("lottery:read")),
                 db: Session = Depends(get_db)) -> dict:
    return ok(service.list_grants(db, staff, request.query_params))


@admin_router.get("/records/export")
def admin_export(request: Request, staff: StaffActor = Depends(require_permission("lottery:export")),
                 db: Session = Depends(get_db)) -> Response:
    body = b"".join(service.export_records(db, staff, request.query_params))
    return Response(body, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="lottery-records.csv"'})


@admin_router.get("/records")
def admin_records(request: Request, staff: StaffActor = Depends(require_permission("lottery:read")),
                  db: Session = Depends(get_db)) -> dict:
    return ok(service.list_records(db, staff, request.query_params))


@admin_router.get("/records/{draw_id}")
def admin_record(draw_id: int, staff: StaffActor = Depends(require_permission("lottery:read")),
                 db: Session = Depends(get_db)) -> dict:
    return ok(service.get_record_admin(db, staff, draw_id))


@admin_router.get("/records/{draw_id}/contact")
def admin_contact(draw_id: int, staff: StaffActor = Depends(require_permission("lottery:fulfil")),
                  db: Session = Depends(get_db)) -> dict:
    return ok(service.reveal_claim_contact(db, staff, draw_id))


@admin_router.post("/records/{draw_id}/ship")
def admin_ship(draw_id: int, body: ShipIn, staff: StaffActor = Depends(require_permission("lottery:fulfil")),
               key: str = Depends(idempotency_key), db: Session = Depends(get_db)) -> dict:
    return ok(service.ship_prize(db, staff, draw_id, body.carrier, body.tracking_no, key))


@admin_router.post("/records/{draw_id}/received")
def admin_received(draw_id: int, staff: StaffActor = Depends(require_permission("lottery:fulfil")),
                   key: str = Depends(idempotency_key), db: Session = Depends(get_db)) -> dict:
    return ok(service.confirm_received(db, staff, draw_id, key))


@admin_router.post("/records/{draw_id}/retry-delivery")
def admin_retry(draw_id: int, staff: StaffActor = Depends(require_permission("lottery:fulfil")),
                key: str = Depends(idempotency_key), db: Session = Depends(get_db)) -> dict:
    return ok(service.retry_delivery(db, staff, draw_id, key))
