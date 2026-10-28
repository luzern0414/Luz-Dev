"""事务外发件箱 v2：业务写与事件同事务提交，worker 投递到消息队列或直接处理。

规则（SPEC §6.6）：
- 事件字段：event_id（uuid）、topic、aggregate_type、aggregate_id、version、occurred_at、payload。
- worker 用 FOR UPDATE SKIP LOCKED 拉取（Postgres / MySQL），多实例不重复。
- 失败按 1m / 5m / 30m / 2h / 12h 退避，5 次后 failed 并告警。
- 消费者按 (consumer, event_id) 去重（ProcessedEvent）。
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import String, Text, UniqueConstraint, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from .db import Base, BigIdMixin, supports_skip_locked, utcnow

BACKOFF_SECONDS = (60, 300, 1800, 7200, 43200)


class OutboxEvent(Base, BigIdMixin):
    __tablename__ = "outbox_events"

    event_id: Mapped[str] = mapped_column(String(36), unique=True, default=lambda: str(uuid.uuid4()))
    topic: Mapped[str] = mapped_column(String(64), index=True)
    aggregate_type: Mapped[str] = mapped_column(String(32), default="")
    aggregate_id: Mapped[str] = mapped_column(String(64), default="")
    version: Mapped[int] = mapped_column(default=1)
    payload_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending | sent | failed
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(nullable=True)


class ProcessedEvent(Base, BigIdMixin):
    __tablename__ = "processed_events"
    __table_args__ = (UniqueConstraint("consumer", "event_id", name="uq_processed_consumer_event"),)

    consumer: Mapped[str] = mapped_column(String(64))
    event_id: Mapped[str] = mapped_column(String(36))
    processed_at: Mapped[datetime] = mapped_column(default=utcnow)


def enqueue_event(
    db: Session,
    topic: str,
    payload: dict[str, Any],
    aggregate_type: str = "",
    aggregate_id: str | int = "",
    version: int = 1,
) -> OutboxEvent:
    """只加入会话，不提交；与业务写共用同一次 commit。"""
    event = OutboxEvent(
        topic=topic,
        aggregate_type=aggregate_type,
        aggregate_id=str(aggregate_id),
        version=version,
        payload_json=json.dumps(payload, default=str, ensure_ascii=False),
    )
    db.add(event)
    return event


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def dispatch_pending(
    db: Session,
    handler: Callable[..., None],
    max_attempts: int = 5,
    limit: int = 100,
    now: datetime | None = None,
) -> dict[str, int]:
    """投递到期事件。handler(topic, payload) 或 handler(topic, payload, event)。返回 {sent, retried, failed}。"""
    now = now or utcnow()
    stmt = (
        select(OutboxEvent)
        .where(OutboxEvent.status == "pending", OutboxEvent.next_attempt_at <= now)
        .order_by(OutboxEvent.id)
        .limit(limit)
    )
    if supports_skip_locked(db):
        stmt = stmt.with_for_update(skip_locked=True)
    events = db.execute(stmt).scalars().all()
    counts = {"sent": 0, "retried": 0, "failed": 0}
    for event in events:
        payload = json.loads(event.payload_json)
        try:
            try:
                handler(event.topic, payload, event)
            except TypeError as exc:
                if "positional argument" not in str(exc):
                    raise
                handler(event.topic, payload)
            event.status = "sent"
            event.sent_at = now
            counts["sent"] += 1
        except Exception as exc:  # noqa: BLE001
            event.attempts += 1
            event.last_error = str(exc)[:500]
            if event.attempts >= max_attempts:
                event.status = "failed"
                counts["failed"] += 1
            else:
                delay = BACKOFF_SECONDS[min(event.attempts - 1, len(BACKOFF_SECONDS) - 1)]
                event.next_attempt_at = now + timedelta(seconds=delay)
                counts["retried"] += 1
        db.commit()
    return counts


def mark_processed(db: Session, consumer: str, event_id: str) -> bool:
    """消费去重：首次返回 True，重复返回 False。与消费者的业务写同事务使用（调用方 commit）。

    用保存点插入去重记录：重复时只撤销这一条插入，不回滚调用方事务里已经做的其他写入。
    """
    try:
        with db.begin_nested():
            db.add(ProcessedEvent(consumer=consumer, event_id=event_id))
        return True
    except IntegrityError:
        return False


def requeue_failed(db: Session, event_ids: list[str], now: datetime | None = None) -> int:
    """人工重放失败事件。"""
    now = now or utcnow()
    events = db.execute(select(OutboxEvent).where(OutboxEvent.event_id.in_(event_ids), OutboxEvent.status == "failed")).scalars().all()
    for event in events:
        event.status = "pending"
        event.attempts = 0
        event.next_attempt_at = now
        event.last_error = None
    db.commit()
    return len(events)


class OutboxWorker:
    """常驻投递循环：每 interval 秒拉一批；stop() 退出。"""

    def __init__(self, session_factory: Callable[[], Session], handler: Callable[..., None], interval: float = 1.0, batch: int = 100):
        self.session_factory = session_factory
        self.handler = handler
        self.interval = interval
        self.batch = batch
        self._stop = threading.Event()
        self.stats = {"sent": 0, "retried": 0, "failed": 0, "loops": 0}

    def run_once(self) -> dict[str, int]:
        db = self.session_factory()
        try:
            counts = dispatch_pending(db, self.handler, limit=self.batch)
        finally:
            db.close()
        for key, value in counts.items():
            self.stats[key] += value
        self.stats["loops"] += 1
        return counts

    def run_forever(self) -> None:  # pragma: no cover - 进程入口
        while not self._stop.is_set():
            counts = self.run_once()
            if counts["sent"] == 0:
                self._stop.wait(self.interval)

    def stop(self) -> None:
        self._stop.set()
