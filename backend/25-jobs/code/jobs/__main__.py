"""python -m jobs scheduler|worker|list"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading

from .runner import ensure_paths


def main(argv: list[str] | None = None) -> int:
    ensure_paths()
    from luzback.config import get_settings  # noqa: PLC0415
    from luzback.db import Base, get_session_factory, init_engine  # noqa: PLC0415
    from luzback.logging import configure_logging  # noqa: PLC0415

    from .runner import discover_jobs, discover_subscriptions, heartbeat_file, make_scheduler, make_worker, module_packages, write_heartbeat  # noqa: PLC0415

    parser = argparse.ArgumentParser(description="Luz 定时任务与事件消费进程")
    parser.add_argument("role", choices=("scheduler", "worker", "list"))
    parser.add_argument("--tick", type=float, default=1.0, help="循环间隔秒")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level) if callable(configure_logging) else None
    packages = module_packages(settings)

    if args.role == "list":
        jobs = discover_jobs(packages)
        routes = discover_subscriptions(packages)
        print(json.dumps({
            "jobs": [{"name": j.name, "interval_seconds": j.interval_seconds} for j in jobs.all()],
            "subscriptions": {topic: [name for name, _ in subs] for topic, subs in sorted(routes.items())},
        }, ensure_ascii=False, indent=2))
        return 0

    settings.assert_production_safe() if hasattr(settings, "assert_production_safe") else None
    init_engine(settings.database_url)
    factory = get_session_factory()
    stop = threading.Event()

    def _stop(*_args) -> None:  # SIGTERM：完成当前一轮后退出（stop_grace_period 30–60s）
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    beat = heartbeat_file()
    log = logging.getLogger("luz.jobs")
    if args.role == "scheduler":
        scheduler = make_scheduler(factory, settings, packages)
        log.info("scheduler_started", extra={"fields": {"jobs": [j.name for j in scheduler.registry.all()]}})
        while not stop.is_set():
            scheduler.run_due()
            write_heartbeat(beat)
            stop.wait(args.tick)
    else:
        worker = make_worker(factory, settings, packages, interval=args.tick)
        log.info("worker_started")
        while not stop.is_set():
            counts = worker.run_once()
            write_heartbeat(beat)
            if counts.get("sent", 0) == 0:
                stop.wait(args.tick)
    _ = Base  # 模型由各模块导入时注册
    return 0


if __name__ == "__main__":
    sys.exit(main())
