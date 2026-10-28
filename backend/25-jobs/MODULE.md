# 模块：jobs（定时任务与事件消费进程）

| 项 | 内容 |
|---|---|
| 状态 | 标准：任务发现、事件路由、心跳、调度与投递进程；测试 6 项 |
| 代码 | `25-jobs/code/jobs/`，测试 `25-jobs/code/tests/` |
| 依赖 | framework（`luzback.scheduler`、`luzback.outbox`）；各模块的 `jobs.py`、`events.py` |

## 0 官方来源

| 主题 | 页面 | 用到的结论 | 核对日期 |
|---|---|---|---|
| 一个容器一个进程 | [FastAPI 部署：容器](https://fastapi.tiangolo.com/deployment/docker/) | API、worker、scheduler 分成三个服务，各自横向扩展 | 2026-10-28 |
| 优雅退出 | [Docker compose stop_grace_period](https://docs.docker.com/reference/compose-file/services/#stop_grace_period) | 收到 SIGTERM 后完成当前一轮再退出 | 2026-10-28 |
| 行锁跳过 | [PostgreSQL SELECT … SKIP LOCKED](https://www.postgresql.org/docs/current/sql-select.html#SQL-FOR-UPDATE-SHARE) | 多个 worker 拉取发件箱不重复 | 2026-10-28 |

## 2 职责与边界

| 负责 | 不负责 |
|---|---|
| 发现各模块的 `JOBS` 与 `SUBSCRIPTIONS` | 任务和订阅的业务逻辑（在各模块） |
| `scheduler` 进程：按间隔执行任务，同名任务全局只有一个实例（Redis 锁） | 消息队列（发件箱直接投递；需要 Redis Streams 时见 `luzback.mq`） |
| `worker` 进程：投递发件箱事件，按主题调用全部订阅者 | — |
| 心跳文件供容器健康检查 | — |

## 4 规则

1. `<pkg>/jobs.py` 导出 `JOBS`：`luzback.scheduler.Job` 实例，或 `(名称, 间隔秒, 函数)` 三元组；任务名全局唯一，重名启动即失败。
2. `<pkg>/events.py` 导出 `SUBSCRIPTIONS = {主题: handler(db, payload, event_id)}`；处理函数幂等、不提交。
3. 每个订阅者独立会话：成功即提交；任一订阅者失败则整个事件按发件箱退避重试（1m/5m/30m/2h/12h，5 次后 failed 告警），已成功的订阅者靠自身幂等不重复生效。
4. 没有订阅者的主题直接确认。
5. 只加载启用的模块（`LUZ_FEATURES`）。
6. 生产环境的 scheduler 必须配置 `LUZ_REDIS_URL` 作分布式锁，否则拒绝启动。
7. 每轮写 `LUZ_WORKER_HEARTBEAT_FILE`；SIGTERM 后完成当前一轮退出。

## 6 运行

```powershell
python -m jobs list        # 上线前核对：全部任务与订阅
python -m jobs scheduler
python -m jobs worker
```

## 11 测试

任务发现（实例与三元组、重名）、按间隔执行、订阅者失败重试而成功者不重复、无订阅主题确认、心跳。

## 13 定制点

| 项 | 默认 | 说明 |
|---|---|---|
| `--tick` | 1 秒 | 循环间隔 |
| `LUZ_WORKER_HEARTBEAT_FILE` | 未设置 | 健康检查读取的心跳文件 |
| `LUZ_FEATURES` | 全部模块 | 只运行启用模块的任务与订阅 |

不包含：Cron 表达式（只支持固定间隔；按日任务在任务体里判断窗口）、跨进程消息队列消费者组。
