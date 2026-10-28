"""25-jobs：定时任务与事件消费的运行进程。

两个独立进程（docker-compose.prod.yml 的 scheduler 与 worker 服务）：

    python -m jobs scheduler   # 按间隔执行各模块 jobs.py 里的 JOBS（同名任务全局只跑一个实例）
    python -m jobs worker      # 投递发件箱事件，按主题调用各模块 events.py 里的 SUBSCRIPTIONS
    python -m jobs list        # 列出发现的任务与订阅，用于上线前核对

约定（MODULE-TEMPLATE §3、module brief）：
- `<pkg>/jobs.py`：`JOBS = (Job(...), ...)`；也接受 `(名称, 间隔秒, 函数)` 三元组。
- `<pkg>/events.py`：`SUBSCRIPTIONS = {"payment.succeeded": handler}`，`handler(db, payload, event_id)`；
  处理函数幂等、不提交事务。worker 为每个订阅者开独立会话：成功提交，异常回滚并让发件箱按退避重试。
- 只加载启用的模块（LUZ_FEATURES）。
- 进程每轮写心跳文件（LUZ_WORKER_HEARTBEAT_FILE），容器健康检查据此判断进程是否卡住。
"""
from __future__ import annotations

from .runner import (
    EventRouter,
    discover_jobs,
    discover_subscriptions,
    make_scheduler,
    make_worker,
    write_heartbeat,
)

__all__ = ["EventRouter", "discover_jobs", "discover_subscriptions", "make_scheduler", "make_worker", "write_heartbeat"]
