"""定时任务（注册到 25-jobs 的 JobRegistry）。每个函数 fn(db) -> str，幂等可重跑，多实例由调度锁 + 条件 UPDATE 保证。"""
from __future__ import annotations

from sqlalchemy.orm import Session

from luzback.scheduler import Job, JobRegistry

from . import service


def dispatch_deliveries(db: Session) -> str:
    """投递到期的通知（订阅消息 / 短信）；静默时段与频控在投递时判断。"""
    counts = service.dispatch_due(db)
    return ",".join(f"{k}={v}" for k, v in sorted(counts.items())) or "idle"


def reclaim_stuck_deliveries(db: Session) -> str:
    """sending 超时（进程崩溃）的投递退回 pending。"""
    return f"reclaimed={service.reclaim_stuck(db)}"


def purge_notifications(db: Session) -> str:
    """清理过期频控计数与超过保留期的站内信。"""
    result = service.purge_old(db)
    return ",".join(f"{k}={v}" for k, v in sorted(result.items()))


JOBS = (
    Job("notification.dispatch", 15, dispatch_deliveries, lock_ttl=120),
    Job("notification.reclaim", 60, reclaim_stuck_deliveries, lock_ttl=55),
    Job("notification.purge", 86400, purge_notifications, lock_ttl=600),
)


def register(registry: JobRegistry) -> None:
    for job in JOBS:
        registry.add(Job(job.name, job.interval_seconds, job.fn, lock_ttl=job.lock_ttl))
