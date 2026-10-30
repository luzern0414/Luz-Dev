"""定时任务（幂等，可重跑）。

- admin_rbac.purge_sessions：删除 30 天前已撤销或已过期的会话行（有效会话不受影响）。
- admin_rbac.purge_login_logs：删除超过保留期（默认 186 天，不少于 6 个月）的登录日志。
- admin_rbac.purge_throttles：删除未锁定且长时间无失败的节流行（含不存在账号名产生的行）。

25-jobs 读取 `JOBS`（luzback.scheduler.Job 元组）。
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete, or_
from sqlalchemy.orm import Session

from luzback.scheduler import Job, JobRegistry

from . import config
from .models import AdminLoginLog, AdminLoginThrottle, AdminSession

SESSION_KEEP_DAYS = 30


def purge_sessions(db: Session) -> str:
    cutoff = config.now() - timedelta(days=SESSION_KEEP_DAYS)
    n = db.execute(delete(AdminSession).where(or_(AdminSession.revoked_at < cutoff, AdminSession.expires_at < cutoff))).rowcount
    db.commit()
    return f"deleted={n}"


def purge_login_logs(db: Session) -> str:
    cutoff = config.now() - timedelta(days=config.get_config().login_log_retention_days)
    n = db.execute(delete(AdminLoginLog).where(AdminLoginLog.created_at < cutoff)).rowcount
    db.commit()
    return f"deleted={n}"


def purge_throttles(db: Session) -> str:
    at = config.now()
    cutoff = at - timedelta(seconds=config.get_config().login_lock_max_seconds)
    T = AdminLoginThrottle
    n = db.execute(delete(T).where(or_(T.locked_until.is_(None), T.locked_until < at),
                                   or_(T.last_failure_at.is_(None), T.last_failure_at < cutoff))).rowcount
    db.commit()
    return f"deleted={n}"


JOBS: tuple[Job, ...] = (
    Job("admin_rbac.purge_sessions", interval_seconds=3600, fn=purge_sessions, lock_ttl=300),
    Job("admin_rbac.purge_login_logs", interval_seconds=86400, fn=purge_login_logs, lock_ttl=600),
    Job("admin_rbac.purge_throttles", interval_seconds=3600, fn=purge_throttles, lock_ttl=300),
)


def register_jobs(registry: JobRegistry) -> None:
    """兼容：把 JOBS 加入调度器注册表（25-jobs 也可直接读取 JOBS）。"""
    for job in JOBS:
        registry.add(Job(job.name, interval_seconds=job.interval_seconds, fn=job.fn, lock_ttl=job.lock_ttl))
