"""after_sales 定时任务（25-jobs 按 JOBS 装配；幂等、逐单提交）。

| 任务 | 间隔 | 做什么 |
|---|---|---|
| after_sales.submit_refunds | 60s | 提交到期的退款（首次提交失败、1 分钟间隔限制、渠道不可用、崩溃补偿；已有有效退款单则采纳） |
| after_sales.close_unshipped | 3600s | 买家超过 AFTER_SALE_RETURN_SHIP_DAYS 天未寄回 → 关闭并释放额度 |
| after_sales.auto_approve | 3600s | AFTER_SALE_AUTO_APPROVE_DAYS > 0 时，商家超时未处理自动同意 |
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from luzback.scheduler import Job

from . import service


def submit_refunds_job(db: Session) -> dict:
    return service.submit_due_refunds(db)


def close_unshipped_job(db: Session) -> int:
    return len(service.close_unshipped(db))


def auto_approve_job(db: Session) -> int:
    return len(service.auto_approve(db))


JOBS = (
    Job("after_sales.submit_refunds", interval_seconds=60, fn=submit_refunds_job, lock_ttl=55),
    Job("after_sales.close_unshipped", interval_seconds=3600, fn=close_unshipped_job, lock_ttl=600),
    Job("after_sales.auto_approve", interval_seconds=3600, fn=auto_approve_job, lock_ttl=600),
)
