"""会员卡定时任务：fn(db) -> str，幂等可重跑；由 25-jobs 的调度器注册（JOBS）。任务体只写库与发件箱。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from luzback.scheduler import Job

from . import service


def _run(fn, db: Session, at: datetime | None, label: str) -> str:
    n = fn(db, at)
    db.commit()
    return f"{label}={n}"


def expire_cards(db: Session, at: datetime | None = None) -> str:
    return _run(service.expire_cards, db, at, "expired")


def auto_activate(db: Session, at: datetime | None = None) -> str:
    return _run(service.auto_activate, db, at, "activated")


def auto_unfreeze(db: Session, at: datetime | None = None) -> str:
    return _run(service.auto_unfreeze, db, at, "unfrozen")


def remind_expiring(db: Session, at: datetime | None = None) -> str:
    return _run(service.remind_expiring, db, at, "reminded")


def close_expired_orders(db: Session, at: datetime | None = None) -> str:
    return _run(service.close_expired_orders, db, at, "closed")


def flag_overdue_refunds(db: Session, at: datetime | None = None) -> str:
    return _run(service.flag_overdue_refunds, db, at, "overdue")


JOBS = (
    Job("member_card.close_expired_orders", interval_seconds=60, fn=close_expired_orders, lock_ttl=50),
    Job("member_card.auto_unfreeze", interval_seconds=600, fn=auto_unfreeze, lock_ttl=300),
    Job("member_card.auto_activate", interval_seconds=600, fn=auto_activate, lock_ttl=300),
    Job("member_card.expire_cards", interval_seconds=600, fn=expire_cards, lock_ttl=300),
    Job("member_card.remind_expiring", interval_seconds=3600, fn=remind_expiring, lock_ttl=600),
    Job("member_card.flag_overdue_refunds", interval_seconds=3600, fn=flag_overdue_refunds, lock_ttl=600),
)
