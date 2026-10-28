"""identity 定时任务（幂等可重跑；任务体内不发外部请求）。注册到 25-jobs：`register(registry)`。

- identity.anonymize_due：冷静期到期的注销申请 → 匿名化（每用户单独提交，一个失败不影响其他）。
- identity.expire_content_checks：超时未回推的头像检测记为 timeout（头像不生效）。
- identity.purge_auth_records：清理过期验证码、过期刷新令牌、超过保留期的已吊销会话。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from luzback import utcnow
from luzback.scheduler import Job, JobRegistry

from .config import get_config
from .models import (
    CHECK_PENDING, CHECK_TIMEOUT, SESSION_REVOKED, STATUS_DELETING, TOKEN_ACTIVE, ContentCheck, RefreshToken, SmsCode, User,
    UserSession,
)
from .service import anonymize_member


def anonymize_due(db: Session, now: datetime | None = None, limit: int = 100) -> str:
    now = now or utcnow()
    ids = db.execute(select(User.id).where(User.status == STATUS_DELETING, User.deletion_due_at <= now)
                     .order_by(User.id).limit(limit)).scalars().all()
    db.rollback()
    counts = {"deleted": 0, "held": 0, "skipped": 0}
    for user_id in ids:
        try:
            counts[anonymize_member(db, user_id, now)] += 1
        except Exception:  # noqa: BLE001 单个失败不影响其他；下次重跑
            db.rollback()
            counts["skipped"] += 1
    return f"deleted={counts['deleted']} held={counts['held']} skipped={counts['skipped']}"


def expire_content_checks(db: Session, now: datetime | None = None) -> str:
    now = now or utcnow()
    cutoff = now - timedelta(minutes=get_config().media_check_timeout_minutes)
    rows = db.execute(update(ContentCheck).where(ContentCheck.status == CHECK_PENDING, ContentCheck.created_at < cutoff)
                      .values(status=CHECK_TIMEOUT, decided_at=now)).rowcount
    db.commit()
    return f"timeout={rows}"


def purge_auth_records(db: Session, now: datetime | None = None) -> str:
    now = now or utcnow()
    c = get_config()
    sms = db.execute(delete(SmsCode).where(SmsCode.created_at < now - timedelta(days=c.sms_retention_days))).rowcount
    tokens = db.execute(delete(RefreshToken).where(RefreshToken.expires_at < now - timedelta(days=1),
                                                   RefreshToken.status != TOKEN_ACTIVE)).rowcount
    tokens += db.execute(delete(RefreshToken).where(RefreshToken.expires_at < now - timedelta(days=c.refresh_absolute_days))).rowcount
    old = select(UserSession.id).where(UserSession.status == SESSION_REVOKED,
                                       UserSession.revoked_at < now - timedelta(days=c.session_retention_days))
    db.execute(delete(RefreshToken).where(RefreshToken.session_id.in_(old)))
    sessions = db.execute(delete(UserSession).where(UserSession.status == SESSION_REVOKED,
                                                    UserSession.revoked_at < now - timedelta(days=c.session_retention_days))).rowcount
    db.commit()
    return f"sms={sms} tokens={tokens} sessions={sessions}"


JOBS = (
    Job("identity.anonymize_due", interval_seconds=3600, fn=anonymize_due, lock_ttl=600),
    Job("identity.expire_content_checks", interval_seconds=600, fn=expire_content_checks),
    Job("identity.purge_auth_records", interval_seconds=86400, fn=purge_auth_records, lock_ttl=600),
)


def register(registry: JobRegistry) -> None:
    for job in JOBS:
        registry.add(Job(job.name, job.interval_seconds, job.fn, job.lock_ttl))
