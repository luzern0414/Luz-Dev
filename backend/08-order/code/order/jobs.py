"""order 定时任务（25-jobs 按 JOBS 装配；每个任务幂等、可重跑，逐单提交）。

| 任务 | 间隔 | 做什么 |
|---|---|---|
| order.close_expired | 60s | 超时未支付：先查单（已付 → 支付钩子成交）→ 关单 → 关闭订单、释放库存 / 券 / 限购（R1 关单规则） |
| order.auto_complete | 3600s | 已履约（或已发货）超过 ORDER_AUTO_COMPLETE_DAYS 天且无处理中售后 → 完成 |
| order.shipping_sync | 300s | 微信支付订单发货信息补录（09 request_shipping_upload） |
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from luzback.scheduler import Job

from . import service


def close_expired_job(db: Session) -> dict:
    report = service.close_expired(db)
    return {k: len(v) for k, v in report.items()}


def auto_complete_job(db: Session) -> dict:
    report = service.auto_complete(db)
    return {k: len(v) for k, v in report.items()}


def shipping_sync_job(db: Session) -> int:
    return service.shipping_sync(db)


JOBS = (
    Job("order.close_expired", interval_seconds=60, fn=close_expired_job, lock_ttl=55),
    Job("order.auto_complete", interval_seconds=3600, fn=auto_complete_job, lock_ttl=600),
    Job("order.shipping_sync", interval_seconds=300, fn=shipping_sync_job, lock_ttl=280),
)
