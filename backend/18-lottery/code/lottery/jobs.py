"""定时任务（fn(db) -> str，注册到 25-jobs）。全部幂等，可重跑；任务内不发外部请求，只写事件。"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from luzback import enqueue_event, utcnow
from luzback.scheduler import Job

from . import rules
from .config import get_config
from .models import (
    A_ENDED, A_PUBLISHED, A_RUNNING, D_AWAITING, D_EXPIRED, D_FAILED, D_PENDING, LotteryActivity, LotteryChanceAccount,
    LotteryChanceGrant, LotteryChancePeriod, LotteryDraw, LotteryMemberPrizeWin,
)
from .service import _end, _enqueue_prize_won


def advance_activities(db: Session, now: datetime | None = None) -> str:
    """published → running（到开始时间）；published / running → ended（到结束时间）。"""
    now = rules.aware(now) if now else utcnow()
    started = db.execute(update(LotteryActivity).where(
        LotteryActivity.status == A_PUBLISHED, LotteryActivity.start_at <= now, LotteryActivity.end_at > now,
        LotteryActivity.deleted_at.is_(None)).values(status=A_RUNNING)).rowcount
    due = db.execute(select(LotteryActivity).where(
        LotteryActivity.status.in_((A_PUBLISHED, A_RUNNING)), LotteryActivity.end_at <= now)).scalars().all()
    ended = sum(1 for act in due if _end(db, act, now, "time_up"))
    db.commit()
    return f"started={started} ended={ended}"


def retry_deliveries(db: Session, now: datetime | None = None, limit: int = 200) -> str:
    """虚拟奖发奖补偿：pending 且超过回执期限 → 按退避重发 lottery.prize_won；超过最大次数 → failed（人工处理）。"""
    cfg = get_config()
    now = rules.aware(now) if now else utcnow()
    rows = db.execute(select(LotteryDraw).where(
        LotteryDraw.delivery_status == D_PENDING, LotteryDraw.delivery_due_at <= now,
    ).order_by(LotteryDraw.id).limit(limit).execution_options(populate_existing=True)).scalars().all()
    resent = failed = 0
    for d in rows:
        D = LotteryDraw
        if d.delivery_attempts >= cfg.delivery_max_attempts:
            n = db.execute(update(D).where(D.id == d.id, D.delivery_status == D_PENDING,
                                           D.delivery_attempts == d.delivery_attempts)
                           .values(delivery_status=D_FAILED, updated_at=now)).rowcount
            if n == 1:
                failed += 1
                enqueue_event(db, "lottery.prize_delivery_failed", {"draw_id": d.id, "member_id": d.member_id,
                                                                    "prize_kind": d.prize_kind}, "lottery_draw", d.id)
            continue
        backoff = cfg.delivery_backoff_seconds[min(d.delivery_attempts - 1, len(cfg.delivery_backoff_seconds) - 1)]
        n = db.execute(update(D).where(D.id == d.id, D.delivery_status == D_PENDING,
                                       D.delivery_attempts == d.delivery_attempts)
                       .values(delivery_attempts=D.delivery_attempts + 1,
                               delivery_due_at=now + timedelta(seconds=backoff), updated_at=now)).rowcount
        if n == 1:
            db.refresh(d)
            _enqueue_prize_won(db, d, attempt=d.delivery_attempts)
            resent += 1
    db.commit()
    return f"resent={resent} failed={failed}"


def expire_unclaimed(db: Session, now: datetime | None = None) -> str:
    """实物奖逾期未填写收货信息 → expired（不回补库存：中奖结果已公示）。"""
    now = rules.aware(now) if now else utcnow()
    n = db.execute(update(LotteryDraw).where(
        LotteryDraw.delivery_status == D_AWAITING, LotteryDraw.claim_deadline < now,
    ).values(delivery_status=D_EXPIRED, updated_at=now)).rowcount
    db.commit()
    return f"expired={n}"


def purge_expired(db: Session, now: datetime | None = None) -> str:
    """保留期（≥ 2 年，第十九条）届满后清理参与明细；活动、奖池快照、规则保留。到期前绝不删除。"""
    cfg = get_config()
    now = rules.aware(now) if now else utcnow()
    cutoff = now - timedelta(days=cfg.retention_days)
    acts = db.execute(select(LotteryActivity.id).where(
        LotteryActivity.status == A_ENDED, LotteryActivity.ended_at < cutoff, LotteryActivity.purged_at.is_(None),
    ).limit(cfg.purge_batch)).scalars().all()
    for aid in acts:
        for model in (LotteryDraw, LotteryChanceGrant, LotteryChancePeriod, LotteryChanceAccount, LotteryMemberPrizeWin):
            db.execute(delete(model).where(model.activity_id == aid))
        db.execute(update(LotteryActivity).where(LotteryActivity.id == aid).values(purged_at=now))
    db.commit()
    return f"purged={len(acts)}"


# 25-jobs 约定：JOBS 是 luzback.scheduler.Job 元组
JOBS = (
    Job("lottery.advance_activities", interval_seconds=60, fn=advance_activities),
    Job("lottery.retry_deliveries", interval_seconds=60, fn=retry_deliveries),
    Job("lottery.expire_unclaimed", interval_seconds=600, fn=expire_unclaimed, lock_ttl=300),
    Job("lottery.purge_expired", interval_seconds=86400, fn=purge_expired, lock_ttl=600),
)


def register_jobs(registry) -> None:
    for job in JOBS:
        registry.add(Job(job.name, interval_seconds=job.interval_seconds, fn=job.fn, lock_ttl=job.lock_ttl))
