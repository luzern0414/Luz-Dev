"""membership 定时任务（25-jobs scheduler 加载）：积分到期清零、每日对账。逐批幂等、可重跑。"""
from __future__ import annotations

from luzback.scheduler import Job

from .service import expire_points, reconcile


def _expire(db):
    n = expire_points(db)
    db.commit()
    return n


def _reconcile(db):
    n = reconcile(db)
    db.commit()
    return n


JOBS = (
    Job("membership.expire_points", 3600, _expire, lock_ttl=600),
    Job("membership.reconcile", 86400, _reconcile, lock_ttl=1800),
)
