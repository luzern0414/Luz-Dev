"""运行配置加载记录：每个实例每次加载（启动、文件变化、重载接口、SIGHUP，成功与失败）写一行 config_loads。

进程入口（app.py、python -m jobs）在数据库就绪后调用 `install(store)`：登记监听者并补写启动时的那次加载。
记录失败只写日志，不影响配置加载本身。
"""
from __future__ import annotations

import json
import logging
import threading

from luzback import log_events as ev
from luzback.db import get_session_factory
from luzback.logging import log_event
from luzback.runtime_loader import ConfigSnapshot, ConfigStore, LoadResult, config_store

from .models import ConfigLoad

log = logging.getLogger("luz.app_settings")
_lock = threading.Lock()
_pending: list[LoadResult] = []
MAX_PENDING = 50


def _row(result: LoadResult) -> ConfigLoad:
    return ConfigLoad(
        instance_id=result.instance[:64], config_version=(result.version or "")[:16],
        source_commit=result.source_commit, trigger=result.trigger, result=result.result,
        changed_keys_json=json.dumps(list(result.changed_keys)) if result.changed_keys else None,
        restart_keys_json=json.dumps(list(result.restart_keys)) if result.restart_keys else None,
        error_summary=result.error_summary(), created_at=result.at,
    )


def record_load(result: LoadResult, _snapshot: ConfigSnapshot | None = None) -> None:
    try:
        factory = get_session_factory()
    except RuntimeError:  # 数据库还没初始化：先记在内存里，下一次写入时补上
        with _lock:
            _pending.append(result)
            del _pending[:-MAX_PENDING]
        return
    with _lock:
        batch = [*_pending, result]
        _pending.clear()
    db = factory()
    try:
        for item in batch:
            db.add(_row(item))
        db.commit()
    except Exception as exc:  # noqa: BLE001 - 记录失败不影响加载
        db.rollback()
        log_event(log, ev.CONFIG_LOAD_RECORD_FAILED, logging.WARNING, outcome="failure", trigger=result.trigger,
                  **{"error.type": type(exc).__name__})
    finally:
        db.close()


def install(store: ConfigStore | None = None) -> None:
    """登记加载记录（幂等）；已有的加载结果（启动那次）立即补写。"""
    (store or config_store()).add_listener(record_load, replay=True)
