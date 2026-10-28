"""积分接口（FastAPI 路由）：查询余额与最近流水。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select

from identity.api import current_user
from luzback import get_db, ok
from membership import PointsLedger, balance

router = APIRouter(prefix="/api/v1/app/points", tags=["membership"])


@router.get("")
def points(user_id: int = Depends(current_user), db=Depends(get_db)):
    rows = db.execute(
        select(PointsLedger).where(PointsLedger.user_id == user_id).order_by(PointsLedger.id.desc()).limit(20)
    ).scalars().all()
    return ok({
        "balance": balance(db, user_id),
        "recent": [{"delta": r.delta, "reason": r.reason, "created_at": r.created_at.isoformat()} for r in rows],
    })
