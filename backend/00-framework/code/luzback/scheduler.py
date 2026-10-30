"""定时任务：注册、分布式锁、运行记录（SPEC §10）。

    registry = JobRegistry()
    registry.add(Job("order.timeout_close", interval_seconds=60, fn=close_timeout_orders, lock_ttl=50))
    scheduler = Scheduler(registry, session_factory, lock=InMemoryLock())
    scheduler.run_due()   # 由进程循环或 CronJob 调用

- 同名任务同一时刻只有一个实例执行（锁 SET NX PX）。
- 任务体必须幂等：按时间窗口 + 业务键判断，重复执行不产生副作用。
- 每次运行写 scheduler_runs：开始、结束、状态、错误、主机。
- 每次运行新建 trace，日志带 job 与 run_id（scheduler_runs.id）；开始时取一次运行配置快照，运行中热加载不影响本次；
  结束写一条 job.finished（outcome、duration_ms），失败带堆栈记一次 ERROR。
"""
from __future__ import annotations

import logging
import socket
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, Session, mapped_column

from . import log_events as ev
from .db import Base, BigIdMixin, utcnow
from .logging import bind_log_fields, log_event
from .runtime_loader import use_snapshot
from .tracing import new_trace

log = logging.getLogger("luzback.scheduler")


class LockProvider(Protocol):
    def acquire(self, name: str, ttl_seconds: int) -> str | None: ...
    def release(self, name: str, token: str) -> None: ...


class InMemoryLock:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._locks: dict[str, tuple[str, float]] = {}
        self._mutex = threading.Lock()
        self.clock = clock

    def acquire(self, name: str, ttl_seconds: int) -> str | None:
        now = self.clock()
        with self._mutex:
            holder = self._locks.get(name)
            if holder and holder[1] > now:
                return None
            token = uuid.uuid4().hex
            self._locks[name] = (token, now + ttl_seconds)
            return token

    def release(self, name: str, token: str) -> None:
        with self._mutex:
            if self._locks.get(name, ("", 0))[0] == token:
                del self._locks[name]


class RedisLock:  # pragma: no cover - 需要 Redis
    RELEASE = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"

    def __init__(self, client, prefix: str = "lock"):
        self.client = client
        self.prefix = prefix

    def acquire(self, name: str, ttl_seconds: int) -> str | None:
        token = uuid.uuid4().hex
        ok = self.client.set(f"{self.prefix}:{name}", token, nx=True, px=int(ttl_seconds * 1000))
        return token if ok else None

    def release(self, name: str, token: str) -> None:
        self.client.eval(self.RELEASE, 1, f"{self.prefix}:{name}", token)


class SchedulerRun(Base, BigIdMixin):
    __tablename__ = "scheduler_runs"

    job: Mapped[str] = mapped_column(String(64), index=True)
    host: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running | ok | error | skipped
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)


@dataclass
class Job:
    name: str
    interval_seconds: int
    fn: Callable[[Session], Any]
    lock_ttl: int = 55
    enabled: bool = True
    last_run_at: float = field(default=0.0)

    def due(self, now: float) -> bool:
        return self.enabled and now - self.last_run_at >= self.interval_seconds


class JobRegistry:
    def __init__(self):
        self._jobs: dict[str, Job] = {}

    def add(self, job: Job) -> Job:
        if job.name in self._jobs:
            raise ValueError(f"duplicate job {job.name}")
        self._jobs[job.name] = job
        return job

    def get(self, name: str) -> Job:
        return self._jobs[name]

    def all(self) -> list[Job]:
        return list(self._jobs.values())


def run_job(job: Job, session_factory: Callable[[], Session], lock: LockProvider) -> str:
    """执行一次任务：拿锁 → 写 running → 执行 → 写结果；拿不到锁返回 skipped。"""
    token = lock.acquire(job.name, job.lock_ttl)
    if token is None:
        return "skipped"
    with new_trace(job=job.name), use_snapshot():
        db = session_factory()
        run = SchedulerRun(job=job.name, host=socket.gethostname()[:64])
        started = time.perf_counter()
        try:
            db.add(run)
            db.commit()
            with bind_log_fields(run_id=str(run.id)):
                failure: Exception | None = None
                try:
                    result = job.fn(db)
                    db.commit()
                    run.status = "ok"
                    run.result = str(result)[:2000] if result is not None else None
                except Exception as exc:  # noqa: BLE001
                    db.rollback()
                    run.status = "error"
                    run.error = str(exc)[:500]
                    failure = exc
                run.finished_at = utcnow()
                db.add(run)
                db.commit()
                log_event(log, ev.JOB_FINISHED, logging.ERROR if failure else logging.INFO, exc_info=failure,
                          outcome="failure" if failure else "success",
                          duration_ms=round((time.perf_counter() - started) * 1000, 1))
            return run.status
        finally:
            db.close()
            lock.release(job.name, token)


class Scheduler:
    def __init__(self, registry: JobRegistry, session_factory: Callable[[], Session], lock: LockProvider,
                 clock: Callable[[], float] = time.monotonic):
        self.registry = registry
        self.session_factory = session_factory
        self.lock = lock
        self.clock = clock
        self._stop = threading.Event()

    def run_due(self) -> dict[str, str]:
        now = self.clock()
        results: dict[str, str] = {}
        for job in self.registry.all():
            if job.due(now):
                job.last_run_at = now
                results[job.name] = run_job(job, self.session_factory, self.lock)
        return results

    def run_forever(self, tick_seconds: float = 1.0) -> None:  # pragma: no cover - 进程入口
        while not self._stop.is_set():
            self.run_due()
            self._stop.wait(tick_seconds)

    def stop(self) -> None:
        self._stop.set()
