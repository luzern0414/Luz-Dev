"""任务发现、事件路由、心跳。"""
from __future__ import annotations

import sys
import types
from datetime import datetime, timezone

import pytest

from jobs.runner import EventRouter, discover_jobs, discover_subscriptions, make_scheduler, write_heartbeat
from luzback.db import get_session_factory
from luzback.outbox import OutboxEvent, dispatch_pending, enqueue_event, mark_processed
from luzback.scheduler import InMemoryLock, Job
from luzback.testing import memory_session


def _fake_package(name: str, jobs=None, subscriptions=None):
    pkg = types.ModuleType(name)
    pkg.__path__ = []  # 标记为包
    sys.modules[name] = pkg
    for suffix, attr, value in (("jobs", "JOBS", jobs), ("events", "SUBSCRIPTIONS", subscriptions)):
        if value is not None:
            mod = types.ModuleType(f"{name}.{suffix}")
            setattr(mod, attr, value)
            sys.modules[f"{name}.{suffix}"] = mod
    return pkg


@pytest.fixture()
def db():
    return memory_session()


def test_discover_accepts_job_objects_and_tuples():
    _fake_package("pkg_a", jobs=(Job("a.one", 60, lambda db: "x"),))
    _fake_package("pkg_b", jobs=(("b.two", 30, lambda db: "y"),))
    _fake_package("pkg_c")  # 没有 jobs.py
    registry = discover_jobs(["pkg_a", "pkg_b", "pkg_c"])
    assert sorted(j.name for j in registry.all()) == ["a.one", "b.two"]
    assert registry.get("b.two").lock_ttl == 25


def test_duplicate_job_names_rejected():
    _fake_package("pkg_d", jobs=(("dup.job", 60, lambda db: None),))
    _fake_package("pkg_e", jobs=(("dup.job", 60, lambda db: None),))
    with pytest.raises(ValueError):
        discover_jobs(["pkg_d", "pkg_e"])


def test_scheduler_runs_due_jobs_once_per_interval(db):
    calls = []
    _fake_package("pkg_f", jobs=(Job("f.tick", 60, lambda s: calls.append(1)),))
    now = [1000.0]
    scheduler = make_scheduler(get_session_factory(), packages=["pkg_f"], lock=InMemoryLock())
    scheduler.clock = lambda: now[0]
    assert scheduler.run_due() == {"f.tick": "ok"}
    assert scheduler.run_due() == {}
    now[0] += 61
    assert scheduler.run_due() == {"f.tick": "ok"} and len(calls) == 2


def test_router_retries_failed_subscriber_without_repeating_successful_one(db):
    seen = []
    state = {"fail": True}

    def first(session, payload, event_id):
        if mark_processed(session, "first", event_id):
            seen.append(("first", payload["n"]))

    def second(session, payload, event_id):
        if state["fail"]:
            raise RuntimeError("downstream busy")
        if mark_processed(session, "second", event_id):
            seen.append(("second", payload["n"]))

    _fake_package("pkg_g", subscriptions={"thing.happened": first})
    _fake_package("pkg_h", subscriptions={"thing.happened": second})
    routes = discover_subscriptions(["pkg_g", "pkg_h"])
    assert [name for name, _ in routes["thing.happened"]] == ["pkg_g:first", "pkg_h:second"]
    router = EventRouter(routes, get_session_factory())

    enqueue_event(db, "thing.happened", {"n": 7}, "thing", "1")
    db.commit()
    first_round = dispatch_pending(db, router)
    db.commit()
    assert first_round["retried"] == 1 and seen == [("first", 7)]

    state["fail"] = False
    event = db.query(OutboxEvent).one()
    event.next_attempt_at = datetime(2000, 1, 1, tzinfo=timezone.utc)  # 让退避立即到期
    db.commit()
    second_round = dispatch_pending(db, router)
    db.commit()
    assert second_round["sent"] == 1
    assert seen == [("first", 7), ("second", 7)]  # first 没有重复生效


def test_unsubscribed_topic_is_acknowledged(db):
    router = EventRouter({}, get_session_factory())
    enqueue_event(db, "nobody.cares", {}, "x", "1")
    db.commit()
    assert dispatch_pending(db, router)["sent"] == 1


def test_heartbeat(tmp_path):
    target = tmp_path / "run" / "heartbeat"
    write_heartbeat(str(target))
    assert target.read_text(encoding="utf-8").isdigit()
    write_heartbeat(None)  # 未配置时不写
