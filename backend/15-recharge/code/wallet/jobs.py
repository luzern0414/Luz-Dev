"""储值定时任务：fn(db) -> str，幂等可重跑；由 25-jobs 的调度器注册（JOBS）。任务体只写库与发件箱；
唯一例外是 submit_refunds：委托 09-wechat-pay.create_refund（其内部经 resilient 调渠道、同一退款单号幂等重试）。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from luzback.scheduler import Job

from . import service


def close_expired_orders(db: Session, at: datetime | None = None) -> str:
    n = service.close_expired_orders(db, at)
    db.commit()
    return f"closed={n}"


def expire_bonus(db: Session, at: datetime | None = None) -> str:
    n = service.expire_bonus(db, at)
    db.commit()
    return f"expired={n}"


def reconcile(db: Session, at: datetime | None = None) -> str:
    result = service.reconcile(db, at)
    db.commit()
    return f"checked={result['checked']} mismatched={result['mismatched']}"


def flag_overdue_refunds(db: Session, at: datetime | None = None) -> str:
    n = service.flag_overdue_refunds(db, at)
    db.commit()
    return f"overdue={n}"


def submit_pending_refunds(db: Session, at: datetime | None = None) -> str:
    return f"submitted={service.submit_pending_refunds(db)}"


JOBS = (
    Job("wallet.submit_refunds", interval_seconds=60, fn=submit_pending_refunds, lock_ttl=55),
    Job("wallet.close_expired_orders", interval_seconds=60, fn=close_expired_orders, lock_ttl=50),
    Job("wallet.expire_bonus", interval_seconds=3600, fn=expire_bonus, lock_ttl=600),
    Job("wallet.reconcile", interval_seconds=86400, fn=reconcile, lock_ttl=3000),
    Job("wallet.flag_overdue_refunds", interval_seconds=3600, fn=flag_overdue_refunds, lock_ttl=600),
)
