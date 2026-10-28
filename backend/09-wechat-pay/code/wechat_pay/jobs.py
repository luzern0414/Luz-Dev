"""wechat_pay 定时任务：fn(db, now=None) -> str，全部幂等可重跑；`register_jobs(registry)` 注册到 25-jobs。

| 任务 | 周期 | 做什么 |
|---|---|---|
| wechat_pay.compensate | 30s | 未完成支付单按官方节奏查单（5s/30s/1m/3m/5m/10m/30m）；成功落库；过 time_expire 或待关单 → 关单 |
| wechat_pay.refund_retry | 60s | pending 退款先查询（可能已受理），不存在则用同一 out_refund_no 重新提交 |
| wechat_pay.refund_query | 60s | processing 退款每 1 分钟查询，5 分钟后按 5/10/20/30 分钟衰减 |
| wechat_pay.shipping_retry | 300s | 发货信息上传失败（系统繁忙 / 网络）退避重试 |
| wechat_pay.reconcile | 1800s | 次日 10:00 后下载前一日交易账单对账；生成中每 30 分钟重试 |
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from luzback import AppError, CircuitOpenError
from luzback.scheduler import Job, JobRegistry

from . import apiv3, rules, service
from .client import WechatPayApiError
from .config import get_config
from .models import PaymentRecord, ReconBatch, RefundRecord, ShippingUpload
from .rules import PAY_OPEN, REFUND_PENDING, REFUND_PROCESSING, aware

log = logging.getLogger("luz.wechat_pay.jobs")


def _schedule_query(db: Session, p: PaymentRecord, now: datetime) -> None:
    cfg = get_config()
    count = p.query_count + 1
    service._upd(db, PaymentRecord, PaymentRecord.id == p.id, PaymentRecord.status.in_(PAY_OPEN), query_count=count,
                 last_queried_at=now, next_query_at=now + timedelta(seconds=rules.next_query_delay(count, cfg.query_schedule_s)))
    db.commit()


def compensate_payments(db: Session, now: datetime | None = None, limit: int = 100) -> str:
    """补偿：查单优先，关单在后（4012791911）。"""
    now = service._now(now)
    ids = db.scalars(select(PaymentRecord.id).where(
        PaymentRecord.status.in_(PAY_OPEN), PaymentRecord.next_query_at.is_not(None), PaymentRecord.next_query_at <= now)
        .order_by(PaymentRecord.next_query_at).limit(limit)).all()
    counts = {"checked": 0, "paid": 0, "closed": 0, "open": 0, "error": 0}
    for payment_id in ids:
        counts["checked"] += 1
        try:
            status = _compensate_one(db, service._reload(db, PaymentRecord, payment_id), now)
        except Exception:  # noqa: BLE001 - 单条失败不影响其他支付单
            db.rollback()
            log.exception("compensate payment %s failed", payment_id)
            counts["error"] += 1
            continue
        key = status if status in counts else ("open" if status in PAY_OPEN else "closed")
        counts[key] += 1
    return " ".join(f"{k}={v}" for k, v in counts.items())


def _compensate_one(db: Session, p: PaymentRecord, now: datetime) -> str:
    due_close = aware(p.expires_at) <= now or p.close_requested_at is not None
    db.commit()
    try:
        txn = service._query(p.out_trade_no)
    except WechatPayApiError as exc:
        if exc.code == "ORDER_NOT_EXIST" and due_close:
            return service._mark_closed(db, p, "never_created" if not p.prepay_id else "expired", now)
        _schedule_query(db, p, now)
        return p.status
    except CircuitOpenError:
        _schedule_query(db, p, now)
        return p.status
    try:
        outcome = service.apply_transaction(db, p, txn, source="query", now=now)
    except apiv3.WechatPayError as exc:
        db.rollback()
        service._flag_manual(db, PaymentRecord, p.id, f"query_{exc.reason}")
        _schedule_query(db, p, now)
        return "error"
    if outcome == "processed":
        service._log_notify(db, kind="pay", result="processed", plain=txn, verified=True, source="query", now=now)
    current = service._reload(db, PaymentRecord, p.id)
    if current.status not in PAY_OPEN:
        return current.status
    if due_close:
        status = service._close_channel(db, current, "expired" if aware(current.expires_at) <= now else "business_cancel", now)
        if status in PAY_OPEN:
            _schedule_query(db, service._reload(db, PaymentRecord, p.id), now)
        return status
    _schedule_query(db, current, now)
    return current.status


def retry_refunds(db: Session, now: datetime | None = None, limit: int = 50) -> str:
    """pending 退款：先查询再提交，始终沿用原 out_refund_no（4014959631）。"""
    cfg = get_config()
    now = service._now(now)
    ids = db.scalars(select(RefundRecord.id).where(
        RefundRecord.status == REFUND_PENDING, or_(RefundRecord.next_attempt_at.is_(None), RefundRecord.next_attempt_at <= now))
        .order_by(RefundRecord.id).limit(limit)).all()
    counts = {"submitted": 0, "adopted": 0, "failed": 0, "waiting": 0}
    for refund_id in ids:
        refund = service._reload(db, RefundRecord, refund_id)
        try:
            service.sync_refund(db, refund, now)
            counts["adopted"] += 1
            continue
        except WechatPayApiError as exc:
            if exc.code != "RESOURCE_NOT_EXISTS":
                service._refund_retry_later(db, refund, exc.code, now)
                counts["waiting"] += 1
                continue
        except (CircuitOpenError, AppError) as exc:
            service._refund_retry_later(db, refund, type(exc).__name__, now)
            counts["waiting"] += 1
            continue
        except apiv3.WechatPayError as exc:
            db.rollback()
            service._flag_manual(db, RefundRecord, refund_id, exc.reason)
            counts["waiting"] += 1
            continue
        # 渠道确认未受理：次数用尽 → 失败并释放额度；否则用原单原参数重试
        if refund.attempts >= cfg.refund_submit_max_attempts:
            service._finish_refund(db, refund, rules.REFUND_FAILED, now, error=refund.last_error_code or "NOT_ACCEPTED")
            counts["failed"] += 1
            continue
        info = service._submit_refund(db, refund, now)
        counts["failed" if info.status == rules.REFUND_FAILED else "submitted"] += 1
    return " ".join(f"{k}={v}" for k, v in counts.items())


def query_refunds(db: Session, now: datetime | None = None, limit: int = 100) -> str:
    cfg = get_config()
    now = service._now(now)
    ids = db.scalars(select(RefundRecord.id).where(
        RefundRecord.status == REFUND_PROCESSING, RefundRecord.next_query_at.is_not(None), RefundRecord.next_query_at <= now)
        .order_by(RefundRecord.next_query_at).limit(limit)).all()
    counts = {"checked": 0, "finished": 0, "processing": 0, "error": 0}
    for refund_id in ids:
        refund = service._reload(db, RefundRecord, refund_id)
        counts["checked"] += 1
        try:
            status = service.sync_refund(db, refund, now)
        except (WechatPayApiError, CircuitOpenError, AppError, apiv3.WechatPayError):
            db.rollback()
            status = REFUND_PROCESSING
            counts["error"] += 1
        if status == REFUND_PROCESSING:
            current = service._reload(db, RefundRecord, refund_id)
            count = current.query_count + 1
            since = (now - (aware(current.submitted_at) or now)).total_seconds()
            delay = rules.refund_query_delay(since, count, fast_s=cfg.refund_query_fast_s,
                                             fast_window_s=cfg.refund_query_fast_window_s,
                                             backoff_min=cfg.refund_query_backoff_min)
            service._upd(db, RefundRecord, RefundRecord.id == refund_id, RefundRecord.status == REFUND_PROCESSING,
                         query_count=count, next_query_at=now + timedelta(seconds=delay))
            db.commit()
            counts["processing"] += 1
        else:
            counts["finished"] += 1
    return " ".join(f"{k}={v}" for k, v in counts.items())


def retry_shipping(db: Session, now: datetime | None = None, limit: int = 100) -> str:
    now = service._now(now)
    ids = db.scalars(select(ShippingUpload.id).where(
        ShippingUpload.status == "pending", or_(ShippingUpload.next_attempt_at.is_(None), ShippingUpload.next_attempt_at <= now))
        .order_by(ShippingUpload.id).limit(limit)).all()
    results: dict[str, int] = {}
    for upload_id in ids:
        view = service.upload_shipping(db, service._reload(db, ShippingUpload, upload_id), now)
        results[view["status"]] = results.get(view["status"], 0) + 1
    return " ".join(f"{k}={v}" for k, v in sorted(results.items())) or "none"


def reconcile_due(db: Session, now: datetime | None = None, lookback_days: int = 3) -> str:
    """次日 bill_ready_hour 之后对账前一日；近 lookback_days 天未完成的批次按 next_attempt_at 重试。"""
    cfg = get_config()
    now = service._now(now)
    today = now.astimezone(ZoneInfo(cfg.tz)).date()
    done: list[str] = []
    for back in range(1, lookback_days + 1):
        day = today - timedelta(days=back)
        if now < service._bill_ready_at(day):
            continue
        batch = db.scalar(select(ReconBatch).where(ReconBatch.bill_date == day))
        if batch is not None and (batch.status in ("done", "no_statement")
                                  or (batch.next_attempt_at and aware(batch.next_attempt_at) > now)):
            continue
        result = service.reconcile_bill(db, day, now)
        done.append(f"{day.isoformat()}:{result['status']}")
    return " ".join(done) or "none"


JOBS = (
    Job("wechat_pay.compensate", interval_seconds=30, fn=compensate_payments, lock_ttl=25),
    Job("wechat_pay.refund_retry", interval_seconds=60, fn=retry_refunds, lock_ttl=55),
    Job("wechat_pay.refund_query", interval_seconds=60, fn=query_refunds, lock_ttl=55),
    Job("wechat_pay.shipping_retry", interval_seconds=300, fn=retry_shipping, lock_ttl=290),
    Job("wechat_pay.reconcile", interval_seconds=1800, fn=reconcile_due, lock_ttl=1700),
)


def register_jobs(registry: JobRegistry) -> JobRegistry:
    """逐个注册 JOBS 的副本（Job 带运行状态 last_run_at，不同注册表互不影响）。"""
    for job in JOBS:
        registry.add(Job(job.name, interval_seconds=job.interval_seconds, fn=job.fn, lock_ttl=job.lock_ttl))
    return registry
