"""幂等、发件箱、审计、任务调度（内存 SQLite）。"""
from __future__ import annotations

from datetime import timedelta, timezone

import pytest
from sqlalchemy import select

from luzback.audit import AuditLog, diff, write_audit
from luzback.db import utcnow
from luzback.errors import AppError, ErrorCode
from luzback.idempotency import IdempotencyRecord, purge_idempotency, run_idempotent
from luzback.mq import InMemoryBroker, outbox_to_broker
from luzback.outbox import OutboxEvent, dispatch_pending, enqueue_event, mark_processed, requeue_failed
from luzback.scheduler import InMemoryLock, Job, JobRegistry, Scheduler, SchedulerRun, run_job
from luzback.testing import FakeClock, memory_session


@pytest.fixture
def db():
    session = memory_session("luzback.idempotency", "luzback.outbox", "luzback.audit", "luzback.scheduler")
    yield session
    session.close()


def test_idempotent_replay_same_actor_only(db):
    calls = []

    def work():
        calls.append(1)
        return {"n": len(calls)}

    first = run_idempotent(db, "order.create", "11111111-aaaa", work, actor="user:1", fingerprint="f1", require_key=True)
    again = run_idempotent(db, "order.create", "11111111-aaaa", work, actor="user:1", fingerprint="f1", require_key=True)
    other = run_idempotent(db, "order.create", "11111111-aaaa", work, actor="user:2", fingerprint="f1", require_key=True)
    assert first == again == {"n": 1} and other == {"n": 2}


def test_idempotent_fingerprint_mismatch_and_missing_key(db):
    run_idempotent(db, "s", "22222222-bbbb", lambda: {"ok": 1}, actor="u", fingerprint="a", require_key=True)
    with pytest.raises(AppError) as exc:
        run_idempotent(db, "s", "22222222-bbbb", lambda: {"ok": 2}, actor="u", fingerprint="b", require_key=True)
    assert exc.value.code == ErrorCode.IDEMPOTENCY_MISMATCH
    with pytest.raises(AppError) as exc:
        run_idempotent(db, "s", None, lambda: 1, require_key=True)
    assert exc.value.code == ErrorCode.BAD_REQUEST
    with pytest.raises(AppError):
        run_idempotent(db, "s", "bad key!", lambda: 1)
    assert run_idempotent(db, "s", None, lambda: {"free": True}) == {"free": True}  # 兼容旧调用


def test_idempotent_failure_releases_key_and_stale_takeover(db):
    with pytest.raises(RuntimeError):
        run_idempotent(db, "s", "33333333-cccc", lambda: (_ for _ in ()).throw(RuntimeError("boom")), actor="u")
    assert db.execute(select(IdempotencyRecord)).scalars().all() == []
    # 模拟僵尸处理记录
    stale = IdempotencyRecord(actor="u", scope="s", key="44444444-dddd", status="processing",
                              created_at=utcnow() - timedelta(minutes=5), updated_at=utcnow() - timedelta(minutes=5))
    db.add(stale)
    db.commit()
    assert run_idempotent(db, "s", "44444444-dddd", lambda: {"took": "over"}, actor="u") == {"took": "over"}
    fresh = IdempotencyRecord(actor="u", scope="s", key="55555555-eeee", status="processing")
    db.add(fresh)
    db.commit()
    with pytest.raises(AppError) as exc:
        run_idempotent(db, "s", "55555555-eeee", lambda: 1, actor="u")
    assert exc.value.code == ErrorCode.DUPLICATE_REQUEST and exc.value.retry_after == 1
    assert purge_idempotency(db, older_than_seconds=0) >= 2


def test_outbox_backoff_failed_requeue_and_dedup(db):
    enqueue_event(db, "order.paid", {"order_id": 1}, aggregate_type="order", aggregate_id=1)
    db.commit()
    attempts = []

    def failing(topic, payload, event):
        attempts.append(event.event_id)
        raise RuntimeError("down")

    now = utcnow()
    assert dispatch_pending(db, failing, now=now) == {"sent": 0, "retried": 1, "failed": 0}
    event = db.execute(select(OutboxEvent)).scalar_one()
    aware = lambda dt: dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)  # noqa: E731  SQLite 读出 naive
    assert event.attempts == 1 and aware(event.next_attempt_at) > now and "down" in event.last_error
    assert dispatch_pending(db, failing, now=now) == {"sent": 0, "retried": 0, "failed": 0}  # 未到期
    for _ in range(4):
        dispatch_pending(db, failing, now=aware(event.next_attempt_at) + timedelta(seconds=1))
        db.refresh(event)
    assert event.status == "failed" and event.attempts == 5
    assert requeue_failed(db, [event.event_id]) == 1
    broker = InMemoryBroker()
    assert dispatch_pending(db, outbox_to_broker(broker))["sent"] == 1
    assert broker.size("order.paid") == 1
    assert mark_processed(db, "notification", event.event_id) is True
    db.commit()
    assert mark_processed(db, "notification", event.event_id) is False


def test_outbox_handler_two_arg_compat(db):
    enqueue_event(db, "t", {"x": 1})
    db.commit()
    seen = []
    dispatch_pending(db, lambda topic, payload: seen.append((topic, payload)))
    assert seen == [("t", {"x": 1})]


def test_broker_consumer_group_retries_failed_message():
    broker = InMemoryBroker()
    broker.publish("q", {"n": 1})
    broker.publish("q", {"n": 2})
    seen = []

    def handler(message):
        if message.payload["n"] == 1 and message.attempts == 1:
            raise RuntimeError("first time fails")
        seen.append(message.payload["n"])

    assert broker.consume("q", "g", handler) == 1
    assert broker.consume("q", "g", handler) == 1
    assert sorted(seen) == [1, 2]


def test_audit_and_diff(db):
    write_audit(db, "staff", 7, "order.cancel", "order", 99, before={"status": "paid"}, after={"status": "cancelled"}, reason="customer")
    db.commit()
    row = db.execute(select(AuditLog)).scalar_one()
    assert row.actor_id == "7" and '"cancelled"' in row.after_json
    assert diff({"a": 1, "b": 2}, {"a": 1, "b": 3, "c": 4}) == {"b": {"from": 2, "to": 3}, "c": {"from": None, "to": 4}}


def test_scheduler_lock_and_run_records(db):
    from luzback.db import get_session_factory

    registry = JobRegistry()
    runs = []
    registry.add(Job("tick", interval_seconds=10, fn=lambda session: runs.append(1) or "done"))
    registry.add(Job("broken", interval_seconds=10, fn=lambda session: (_ for _ in ()).throw(ValueError("bad"))))
    clock = FakeClock(100)
    scheduler = Scheduler(registry, get_session_factory(), InMemoryLock(), clock=clock)
    assert scheduler.run_due() == {"tick": "ok", "broken": "error"}
    assert scheduler.run_due() == {}
    clock.advance(10)
    assert scheduler.run_due() == {"tick": "ok", "broken": "error"} and runs == [1, 1]
    lock = InMemoryLock()
    token = lock.acquire("tick", 30)
    assert token and run_job(registry.get("tick"), get_session_factory(), lock) == "skipped"
    statuses = [r.status for r in db.execute(select(SchedulerRun)).scalars().all()]
    assert statuses.count("error") == 2 and statuses.count("ok") == 2
