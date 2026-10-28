"""优惠券接口（FastAPI 路由）：领取。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header

from identity.api import current_user
from luzback import get_db, ok, run_idempotent
from coupon import claim

router = APIRouter(prefix="/api/v1/app/coupon-templates", tags=["coupons"])


@router.post("/{template_id}/claim")
def claim_coupon(
    template_id: int,
    user_id: int = Depends(current_user),
    db=Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    def work():
        coupon = claim(db, user_id, template_id)
        return {"user_coupon_id": coupon.id, "slot": coupon.slot}

    return ok(run_idempotent(db, "coupon.claim", idempotency_key, work))
