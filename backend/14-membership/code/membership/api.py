"""积分接口（顾客端）：余额与最近流水。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select

from identity.api import current_user
from luzback import get_db, ok

from .models import PointsLedger
from .service import account_dict, ledger_dict

router = APIRouter(prefix="/api/v1/app/points", tags=["membership"])


@router.get("")
def points(user_id: int = Depends(current_user), db=Depends(get_db)):
    rows = db.execute(select(PointsLedger).where(PointsLedger.user_id == user_id, PointsLedger.delta != 0)
                      .order_by(PointsLedger.id.desc()).limit(20)).scalars().all()
    return ok({**account_dict(db, user_id), "recent": [ledger_dict(r) for r in rows]})
