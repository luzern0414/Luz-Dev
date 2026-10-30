# 模块模板（每个后端模块的统一结构与铁律）

所有 `backend/NN-name/` 模块按本文组织。样板：`01-identity/`。实现前先读 `SPEC.md` §3、§6、§9、§14 与 `MODULES.md` 中本模块的条目。

## 1 目录

```
NN-name/
  MODULE.md              规范：职责、模型、规则、状态机、接口、事件、配置、错误码、测试、前端对接
  code/
    <pkg>/
      __init__.py        对外导出（模型、用例函数、常量），其他模块只从这里导入
      models.py          ORM 模型：Base + BigIdMixin + TimestampMixin（后台对象加 SoftDeleteMixin）
      service.py         用例函数：参数校验 → 条件 UPDATE / 插入 → 发件箱事件 → 审计 → commit
      api.py             路由、Pydantic 请求模型、依赖；不写业务
      rules.py           （可选）纯函数规则：计价、概率、费用、状态表，便于单测
      ports.py           （可选）外部平台与其他模块的适配接口，延迟导入、可替换；测试替身放 testing.py
      events.py          （有订阅时）SUBSCRIPTIONS = {主题: handler(db, payload, event_id)}，幂等、不提交，25-jobs 的 worker 运行
      jobs.py            （有定时任务时）JOBS：luzback.scheduler.Job 或 (名称, 间隔秒, 函数) 三元组，25-jobs 的 scheduler 运行
      config.py          （有配置时）部署参数与秘密读环境变量；运行参数用 ModuleConfig 从运行配置合并（§3 第 13 条）
    tests/
      test_<pkg>.py      用例测试（必有）；并发用例（涉及扣减 / 计数时必有）；HTTP 用例（有路由时必有）
```

包名用蛇形英文；不与标准库同名（排队用 `queueing`，设置用 `app_settings`）。目录名 `NN-name`，序号固定，新模块追加序号。

## 2 三层职责

| 层 | 做 | 不做 |
|---|---|---|
| api | 解析参数（Pydantic 约束）、取当前用户 / 员工与权限码、幂等键依赖、调用 service、返回 `ok()` | 查库、算钱、判断状态 |
| service | 全部业务规则；一个用例一个事务；显式 `db.commit()`；抛 `AppError` | 依赖 FastAPI 对象；调用外部 HTTP 不经 `resilient` |
| models | 表结构、索引、约束 | 业务方法 |

跨模块：只调用对方 `__init__` 导出的函数；不直接 `select` 对方的表；需要异步解耦的用发件箱事件。

## 3 编码铁律

1. 金额 `*_cents` 整数；折扣 `*_bp` 万分比；时间 `DateTime(timezone=True)`，写入 `utcnow()`；从 SQLite 读回可能是 naive，比较前统一 `aware()`。
2. 扣减 / 计数 / 状态：`UPDATE … WHERE 条件` + `rowcount` 判断；禁止先 `select` 再改值；禁止在循环里逐条查询。
3. 写接口：`Depends(idempotency_key)` 取键 → `run_idempotent(db, scope, key, fn, actor=f"user:{id}", fingerprint=sha256(body), require_key=True)`；`fn` 内不 commit。
4. 事件：业务写与 `enqueue_event(db, "order.paid", payload, aggregate_type, aggregate_id)` 同事务；payload 只放 id 与必要字段，不放 PII。
5. 审计：后台写操作与敏感查看 `write_audit(db, "staff", staff_id, action, resource, id, before, after, reason)`，同事务。
6. 外部调用：`resilient(name, fn, timeout_s, attempts, breaker=registry.get(name))`；只在幂等调用上重试；失败抛 `UpstreamError`；有降级则传 `fallback`。
7. 错误：只用 `ErrorCode` 枚举；他人资源一律 4040；状态不允许 4090；规则不满足 4220 系列；上限 4225。
8. 查询：所有列表用 `luzback.listing.ListSpec` 声明可筛选、可排序、可搜索字段（SPEC §3.4），顾客端 `spec.cursor(...)`、后台 `spec.page(...)`；导出用 `luzback.export.export_csv` 复用同一声明；默认过滤 `deleted_at IS NULL`。
9. 安全：`user_id` 只来自令牌；后台接口 `Depends(require_permission("order:close", fresh=...))`；手机号等 PII 加密存储 + 哈希查询，响应脱敏；日志不写正文。
10. 并发：唯一约束做最后一道防线（`IntegrityError` → 4090 / 4225）；热点计数（库存、券、奖品、排号）用条件 UPDATE，必要时按 `id` 升序处理避免死锁。
11. 任务幂等：按 `(窗口, 业务键)` 判断是否已处理；每轮处理量有上限（批量键写在运行配置）；任务里的外部调用（如查单补偿）同样经 `resilient`、只对幂等调用重试，不在持有数据库事务时等待外部请求。
12. 不信任客户端：价格、折扣、库存、权限、身份一律服务端计算。
13. 配置分开放：秘密与部署参数（商户号、密钥与证书路径、appid、回调与接口地址、单号前缀、时区）读环境变量；运行参数（超时、上限、频控、开关、时长、批量）登记在 `luzback/runtime_config.py` 的模块分组并写进 `config/runtime/base.toml`（键名与配置属性同名，单位写进键名，每个键上方一行注释），模块 `config.py` 用 `ModuleConfig("<分组>", <数据类>)` 读取，不读环境变量、不写常量；测试用 `set_config(...)` 或 `luzback.testing.override_runtime(...)` 注入（写进测试用运行配置，测试结束撤销）。
14. 日志：只用 `log_event(log, <log_events 常量>, ...)`；登录失败、越权、令牌重放等写 `security.*` 事件；鉴权依赖调用 `luzback.app.bind_identity(request, user_ref=, store_id=)`。

## 4 测试要求

| 类型 | 必须覆盖 |
|---|---|
| 用例 | 每条规则一个用例；每条状态边合法 + 非法；他人资源 4040；上限 4225 |
| 幂等 | 同键重放返回同结果且只执行一次 |
| 并发 | 扣减 / 计数类：50 线程文件 SQLite（见 `07-inventory/code/tests`），断言无超卖 / 无重复 |
| HTTP | 路由存在、鉴权 4010、权限 4030、参数 4001、信封格式 |
| 事件 | 关键用例写入了正确主题的发件箱事件 |

运行：`cd backend && python -m pytest -q NN-name`。

## 5 标准模块的完成标准

一个模块标为“标准”（可直接复用）前，必须全部满足：

| 项 | 要求 |
|---|---|
| 官方依据 | 涉及平台接口、法规、行业规则的每条规则都有官方来源（链接 + 核对日期），写在 MODULE.md §0 |
| 功能完整 | MODULES.md 本模块条目的每条规则都已实现；没有“设计”状态的功能，暂不做的写进“不包含” |
| 接口 | 顾客端与后台接口齐全；后台列表都有筛选、排序、分页、关键词与导出声明；写接口幂等；权限码声明 |
| 状态机 | 每个状态与事件列出；非法流转返回 4090；终态不可变 |
| 一致性 | 扣减与计数用条件 UPDATE；外部调用经 `resilient`；跨模块副作用走发件箱；对账或补偿任务存在 |
| 外部依赖 | 每个外部平台有接口抽象与测试替身（假服务），测试不依赖真实商户号或网络 |
| 定时任务 | 超时、过期、补偿、对账任务注册到调度器，幂等可重跑 |
| 测试 | §4 全部类型；外部接口的成功、失败、超时、重复通知 |
| 定制点 | MODULE.md §9 写明环境变量（只有秘密与部署参数）与运行配置键（键、默认、范围、热加载、下发客户端），§13 写扩展点（钩子函数、策略类），项目定制只改这些地方 |

## 6 MODULE.md 章节

0. 官方来源（主题 | 官方页面 | 本模块用到的结论 | 核对日期）
1. 状态（标准 / 基础 / 规划，定义见 `MODULES.md`）与依赖；不写测试用例数
2. 职责与边界
3. 数据模型（表、字段、索引、约束）
4. 业务规则（编号列出，每条可测）
5. 状态机 / 关键流程（含失败分支）
6. 接口（方法、路径、鉴权、幂等、权限码、agent、说明）；agent 列取 `read` / `write` / `write_high` / `no`（含义见 `references/agent-foundation.md` §6，支付、退款执行、改价、改库存、改权限一律 `no`）；整张表都是 `no` 时可在表下写一句代替该列
7. 事件（发出 / 消费）
8. 并发与一致性
9. 配置：两张表——9.1 环境变量（只列秘密与部署参数：变量、默认、说明）；9.2 运行配置键（`[分组] 键`、默认、范围、热加载、下发客户端、说明含单位；从字段说明生成，迁移前的变量名写在说明里）
10. 错误码
11. 测试清单（按测试文件列覆盖的场景，不写用例数）
12. 前端对接（页面 → 接口 → 行为）
13. 定制点（扩展钩子、测试替身、不包含的功能；配置只写在 §9，这里不重复）
