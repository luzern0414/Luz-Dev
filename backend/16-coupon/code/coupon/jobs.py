"""coupon 定时任务（顶层用例，自行分批提交；全部幂等、可重跑）。

| 任务 | 间隔 | 做什么 |
|---|---|---|
| coupon.expire | 300 秒 | unused 且 valid_to ≤ now → expired（批量条件 UPDATE … RETURNING），每张写 coupon.expired |
| coupon.remind_expiring | 3600 秒 | unused、未提醒、将在 COUPON_REMIND_BEFORE_HOURS 内到期 → 写 coupon.expiring_soon（reminded_at 去重） |
| coupon.issue_batches | 30 秒 | 逐个会员处理批量发放，(batch, member) 由 ref_key 与明细唯一约束保证只发一次 |

锁定中的券不由过期任务处理：订单关单时 `unlock` 会把已过期的锁定券直接置为 expired。
注册方式：`register_jobs(registry)`（25-jobs 的 JobRegistry / luzback.scheduler.JobRegistry）。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from luzback import AppError, enqueue_event, utcnow
from luzback.scheduler import Job, JobRegistry

from .config import get_config
from .models import (
    BATCH_DONE, BATCH_PENDING, ITEM_FAILED, ITEM_ISSUED, ITEM_PENDING, STATUS_EXPIRED, STATUS_UNUSED,
    CouponIssueBatch, CouponIssueBatchItem, UserCoupon,
)
from .rules import aware
from .service import issue_batch_item


def expire_coupons(db: Session, now: datetime | None = None, max_batches: int = 20) -> str:
    now = aware(now) if now else utcnow()
    size = get_config().expire_batch_size
    total = 0
    for _ in range(max_batches):
        ids = select(UserCoupon.id).where(UserCoupon.status == STATUS_UNUSED, UserCoupon.valid_to <= now) \
            .order_by(UserCoupon.id).limit(size).scalar_subquery()
        rows = db.execute(
            update(UserCoupon).where(UserCoupon.id.in_(ids), UserCoupon.status == STATUS_UNUSED,
                                     UserCoupon.valid_to <= now)
            .values(status=STATUS_EXPIRED, expired_at=now, updated_at=now)
            .returning(UserCoupon.id, UserCoupon.user_id, UserCoupon.template_id)
            .execution_options(synchronize_session=False)
        ).all()
        for coupon_id, user_id, template_id in rows:
            enqueue_event(db, "coupon.expired", {"user_coupon_id": coupon_id, "user_id": user_id,
                                                 "template_id": template_id, "reason": "validity_ended"},
                          "user_coupon", coupon_id)
        db.commit()
        total += len(rows)
        if len(rows) < size:
            break
    return f"expired={total}"


def remind_expiring(db: Session, now: datetime | None = None, max_batches: int = 20) -> str:
    now = aware(now) if now else utcnow()
    cfg = get_config()
    horizon = now + timedelta(hours=cfg.remind_before_hours)
    total = 0
    for _ in range(max_batches):
        ids = select(UserCoupon.id).where(
            UserCoupon.status == STATUS_UNUSED, UserCoupon.reminded_at.is_(None),
            UserCoupon.valid_to > now, UserCoupon.valid_to <= horizon,
        ).order_by(UserCoupon.id).limit(cfg.expire_batch_size).scalar_subquery()
        rows = db.execute(
            update(UserCoupon).where(UserCoupon.id.in_(ids), UserCoupon.reminded_at.is_(None))
            .values(reminded_at=now)
            .returning(UserCoupon.id, UserCoupon.user_id, UserCoupon.template_id, UserCoupon.name, UserCoupon.valid_to)
            .execution_options(synchronize_session=False)
        ).all()
        for coupon_id, user_id, template_id, name, valid_to in rows:
            enqueue_event(db, "coupon.expiring_soon", {"user_coupon_id": coupon_id, "user_id": user_id,
                                                       "template_id": template_id, "name": name,
                                                       "valid_to": aware(valid_to).isoformat()},
                          "user_coupon", coupon_id)
        db.commit()
        total += len(rows)
        if len(rows) < cfg.expire_batch_size:
            break
    return f"reminded={total}"


def process_issue_batches(db: Session, now: datetime | None = None, limit: int | None = None) -> str:
    """每个会员一个事务：成功 → 明细 issued、批次 issued + 1；失败 → 回滚发券，明细 failed（附原因）、批次 failed + 1。"""
    now = aware(now) if now else utcnow()
    limit = limit or get_config().issue_job_batch
    items = db.execute(select(CouponIssueBatchItem.id, CouponIssueBatchItem.batch_id, CouponIssueBatchItem.user_id)
                       .where(CouponIssueBatchItem.status == ITEM_PENDING)
                       .order_by(CouponIssueBatchItem.id).limit(limit)).all()
    batches = {b.id: b for b in db.execute(select(CouponIssueBatch).where(
        CouponIssueBatch.id.in_({i.batch_id for i in items}))).scalars().all()} if items else {}
    issued = failed = 0
    for item_id, batch_id, user_id in items:
        batch = batches[batch_id]
        try:
            coupon = issue_batch_item(db, batch, user_id, now)
            done = db.execute(update(CouponIssueBatchItem).where(
                CouponIssueBatchItem.id == item_id, CouponIssueBatchItem.status == ITEM_PENDING,
            ).values(status=ITEM_ISSUED, user_coupon_id=coupon.id, processed_at=now)).rowcount
            if done == 1:
                db.execute(update(CouponIssueBatch).where(CouponIssueBatch.id == batch_id)
                           .values(issued=CouponIssueBatch.issued + 1, updated_at=now))
            db.commit()
            issued += done
        except AppError as exc:
            db.rollback()
            done = db.execute(update(CouponIssueBatchItem).where(
                CouponIssueBatchItem.id == item_id, CouponIssueBatchItem.status == ITEM_PENDING,
            ).values(status=ITEM_FAILED, error=str((exc.data or {}).get("reason") or exc.message)[:200],
                     processed_at=now)).rowcount
            if done == 1:
                db.execute(update(CouponIssueBatch).where(CouponIssueBatch.id == batch_id)
                           .values(failed=CouponIssueBatch.failed + 1, updated_at=now))
            db.commit()
            failed += done
    pending_batches = select(CouponIssueBatchItem.batch_id).where(CouponIssueBatchItem.status == ITEM_PENDING)
    finished = db.execute(update(CouponIssueBatch).where(
        CouponIssueBatch.status == BATCH_PENDING, CouponIssueBatch.id.not_in(pending_batches),
    ).values(status=BATCH_DONE, finished_at=now, updated_at=now)).rowcount
    db.commit()
    return f"issued={issued} failed={failed} batches_done={finished}"


JOBS = (
    Job("coupon.expire", 300, expire_coupons, lock_ttl=290),
    Job("coupon.remind_expiring", 3600, remind_expiring, lock_ttl=600),
    Job("coupon.issue_batches", 30, process_issue_batches, lock_ttl=25),
)


def register_jobs(registry: JobRegistry) -> list[Job]:
    """把 JOBS 登记到调度器（每次登记新的 Job 实例，避免多个注册表共享 last_run_at）。"""
    return [registry.add(Job(j.name, j.interval_seconds, j.fn, lock_ttl=j.lock_ttl)) for j in JOBS]
