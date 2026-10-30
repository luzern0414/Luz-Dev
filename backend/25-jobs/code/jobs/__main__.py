"""python -m jobs scheduler|worker|list

scheduler / worker 启动顺序与 api 相同：日志 → 运行配置（校验失败逐条打印后退出）→ 数据库 → 模块；
运行中监听配置目录热加载，收到 SIGHUP 也立即重新加载（docker compose kill -s HUP worker scheduler）。
list 只打印 JSON，不配置日志、不加载运行配置。
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading

from .runner import ensure_paths


def _install_config_records(store) -> None:
    """运行配置加载记录（23-app-settings）：数据库就绪后登记，并补写启动时的那次加载。"""
    try:
        import app_settings  # noqa: PLC0415
    except ModuleNotFoundError:
        return
    app_settings.install(store)


def main(argv: list[str] | None = None) -> int:
    ensure_paths()
    from luzback import log_events as ev  # noqa: PLC0415
    from luzback.config import get_settings  # noqa: PLC0415
    from luzback.db import Base, get_session_factory, init_engine  # noqa: PLC0415
    from luzback.logging import log_event, shutdown_logging  # noqa: PLC0415
    from luzback.runtime_loader import start_process, watcher_for  # noqa: PLC0415

    from .runner import discover_jobs, discover_subscriptions, heartbeat_file, make_scheduler, make_worker, module_packages, write_heartbeat  # noqa: PLC0415

    parser = argparse.ArgumentParser(description="Luz 定时任务与事件消费进程")
    parser.add_argument("role", choices=("scheduler", "worker", "list"))
    parser.add_argument("--tick", type=float, default=1.0, help="循环间隔秒")
    args = parser.parse_args(argv)

    settings = get_settings()
    packages = module_packages(settings)

    if args.role == "list":
        jobs = discover_jobs(packages)
        routes = discover_subscriptions(packages)
        print(json.dumps({
            "jobs": [{"name": j.name, "interval_seconds": j.interval_seconds} for j in jobs.all()],
            "subscriptions": {topic: [name for name, _ in subs] for topic, subs in sorted(routes.items())},
        }, ensure_ascii=False, indent=2))
        return 0

    store = start_process(settings)  # 日志 + 运行配置
    settings.assert_production_safe() if hasattr(settings, "assert_production_safe") else None
    init_engine(settings.database_url, pool_size=settings.db_pool_size,
                statement_timeout_ms=store.current().config.db.statement_timeout_ms)
    factory = get_session_factory()
    stop = threading.Event()

    def _stop(*_args) -> None:  # SIGTERM：完成当前一轮后退出（stop_grace_period 30–60s）
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    if hasattr(signal, "SIGHUP"):  # 运维手动触发热加载（没有 HTTP 接口的进程）
        signal.signal(signal.SIGHUP, lambda *_a: threading.Thread(target=store.reload, args=("signal",), daemon=True).start())
    watcher = watcher_for(store, settings)
    watcher.start()
    beat = heartbeat_file()
    log = logging.getLogger("luz.jobs")
    try:
        if args.role == "scheduler":
            scheduler = make_scheduler(factory, settings, packages)  # 导入模块：登记任务
            _install_config_records(store)
            log_event(log, ev.SCHEDULER_STARTED, jobs=[j.name for j in scheduler.registry.all()])
            while not stop.is_set():
                scheduler.run_due()
                write_heartbeat(beat)
                stop.wait(args.tick)
        else:
            worker = make_worker(factory, settings, packages, interval=args.tick)
            _install_config_records(store)
            log_event(log, ev.WORKER_STARTED, topics=len(worker.handler.routes))
            while not stop.is_set():
                counts = worker.run_once()
                write_heartbeat(beat)
                if counts.get("sent", 0) == 0:
                    stop.wait(args.tick)
    finally:
        watcher.stop()
        shutdown_logging()
    _ = Base  # 模型由各模块导入时注册
    return 0


if __name__ == "__main__":
    sys.exit(main())
