"""评价接口（FastAPI 路由）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field

from identity.api import current_user
from luzback import get_db, ok, run_idempotent
from review import create_review

router = APIRouter(prefix="/api/v1/app/orders", tags=["reviews"])


class ReviewIn(BaseModel):
    rating: int = Field(ge=1, le=5)
    content: str = Field(default="", max_length=500)


@router.post("/{order_id}/review")
def review(
    order_id: int,
    body: ReviewIn,
    user_id: int = Depends(current_user),
    db=Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    def work():
        r = create_review(db, user_id, order_id, body.rating, body.content)
        return {"review_id": r.id, "status": r.status}

    return ok(run_idempotent(db, "review.create", idempotency_key, work))
