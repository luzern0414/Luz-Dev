"""任务与订阅的发现、事件路由、心跳。"""
from __future__ import annotations

import importlib
import logging
import os
import sys
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from luzback.config import Settings, get_settings
from luzback.outbox import OutboxWorker
from luzback.scheduler import InMemoryLock, Job, JobRegistry, LockProvider, Scheduler

log = logging.getLogger("luz.jobs")

BACKEND = Path(__file__).resolve().parents[3]
SKIP_PACKAGES = {"luzback", "jobs", "tests"}

Handler = Callable[[Session, dict, str], Any]


def ensure_paths(backend: Path = BACKEND) -> None:
    """与 app.py、conftest.py 相同：把框架与每个模块的 code/ 加入 sys.path。"""
    for code_dir in [backend / "00-framework" / "code", *sorted(backend.glob("*/code"))]:
        if str(code_dir) not in sys.path:
            sys.path.insert(0, str(code_dir))


def module_packages(settings: Settings, backend: Path = BACKEND) -> list[str]:
    names = []
    for code_dir in sorted(backend.glob("*/code")):
        for pkg in sorted(p for p in code_dir.iterdir() if p.is_dir() and (p / "__init__.py").exists()):
            if pkg.name in SKIP_PACKAGES or not settings.feature_enabled(pkg.name):
                continue
            names.append(pkg.name)
    return names


def _optional(name: str) -> Any | None:
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name == name:
            return None
        raise


def _as_job(item: Any, package: str) -> Job:
    if isinstance(item, Job):
        return Job(item.name, item.interval_seconds, item.fn, item.lock_ttl, item.enabled)
    if isinstance(item, tuple) and len(item) == 3:
        name, interval, fn = item
        return Job(name, int(interval), fn, lock_ttl=min(max(25, int(interval) - 5), 3600))
    raise TypeError(f"{package}.jobs.JOBS: unsupported entry {item!r}")


def discover_jobs(packages: Iterable[str]) -> JobRegistry:
    registry = JobRegistry()
    for package in packages:
        module = _optional(f"{package}.jobs")
        for item in getattr(module, "JOBS", ()) if module else ():
            registry.add(_as_job(item, package))
    return registry


def discover_subscriptions(packages: Iterable[str]) -> dict[str, list[tuple[str, Handler]]]:
    """主题 → [(订阅者名, 处理函数)]。"""
    routes: dict[str, list[tuple[str, Handler]]] = {}
    for package in packages:
        module = _optional(f"{package}.events")
        for topic, handler in (getattr(module, "SUBSCRIPTIONS", {}) if module else {}).items():
            routes.setdefault(topic, []).append((f"{package}:{getattr(handler, '__name__', 'handler')}", handler))
    return routes


class EventRouter:
    """发件箱 worker 的处理函数：一个事件交给该主题的全部订阅者。

    每个订阅者独立会话、独立提交；某个订阅者失败时抛出异常，整个事件按退避重试，
    已成功的订阅者靠自身幂等（mark_processed / 业务键）不会重复生效。
    """

    def __init__(self, routes: dict[str, list[tuple[str, Handler]]], session_factory: Callable[[], Session]):
        self.routes = routes
        self.session_factory = session_factory

    def __call__(self, topic: str, payload: dict, event: Any = None) -> None:
        event_id = getattr(event, "event_id", None) or str(payload.get("event_id", ""))
        failures = []
        for name, handler in self.routes.get(topic, []):
            db = self.session_factory()
            try:
                handler(db, payload, event_id)
                db.commit()
            except Exception as exc:  # noqa: BLE001 - 记录后整体重试
                db.rollback()
                log.exception("subscriber_failed", extra={"fields": {"topic": topic, "subscriber": name, "event_id": event_id}})
                failures.append(f"{name}: {type(exc).__name__}: {exc}")
            finally:
                db.close()
        if failures:
            raise RuntimeError("; ".join(failures)[:480])


def write_heartbeat(path: str | None) -> None:
    if not path:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(str(int(time.time())), encoding="utf-8")


def _lock(settings: Settings) -> LockProvider:
    if settings.redis_url:  # pragma: no cover - 需要 Redis
        import redis  # noqa: PLC0415

        from luzback.scheduler import RedisLock  # noqa: PLC0415

        return RedisLock(redis.Redis.from_url(settings.redis_url))
    if settings.is_prod:
        raise RuntimeError("生产环境的调度器必须配置 LUZ_REDIS_URL 作为分布式锁，避免多实例重复执行")
    return InMemoryLock()


def make_scheduler(session_factory: Callable[[], Session], settings: Settings | None = None,
                   packages: Iterable[str] | None = None, lock: LockProvider | None = None) -> Scheduler:
    settings = settings or get_settings()
    registry = discover_jobs(packages if packages is not None else module_packages(settings))
    return Scheduler(registry, session_factory, lock or _lock(settings))


def make_worker(session_factory: Callable[[], Session], settings: Settings | None = None,
                packages: Iterable[str] | None = None, interval: float = 1.0) -> OutboxWorker:
    settings = settings or get_settings()
    routes = discover_subscriptions(packages if packages is not None else module_packages(settings))
    return OutboxWorker(session_factory, EventRouter(routes, session_factory), interval=interval)


def heartbeat_file() -> str | None:
    return os.getenv("LUZ_WORKER_HEARTBEAT_FILE")
