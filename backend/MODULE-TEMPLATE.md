# 模块模板（每个后端模块的统一结构与铁律）

所有 `backend/NN-name/` 模块按本文组织。样板：`01-identity/`。实现前先读 `SPEC.md` §3、§6、§8、§9 与 `MODULES.md` 中本模块的条目。

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
      jobs.py            （可选）定时任务函数 fn(db) -> str，注册到 25-jobs
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
8. 查询：顾客端游标分页 `encode_cursor / decode_cursor`，后台页码 `parse_page`；排序 `parse_sort` 白名单；默认过滤 `deleted_at IS NULL`。
9. 安全：`user_id` 只来自令牌；后台接口 `Depends(require_permission("order:accept"))`；手机号等 PII 加密存储 + 哈希查询，响应脱敏；日志不写正文。
10. 并发：唯一约束做最后一道防线（`IntegrityError` → 4090 / 4225）；热点计数（库存、券、奖品、排号）用条件 UPDATE，必要时按 `id` 升序处理避免死锁。
11. 时间窗口任务幂等：按 `(窗口, 业务键)` 判断是否已处理；任务体内不发外部请求，改为写事件。
12. 不信任客户端：价格、折扣、库存、权限、身份一律服务端计算。

## 4 测试要求

| 类型 | 必须覆盖 |
|---|---|
| 用例 | 每条规则一个用例；每条状态边合法 + 非法；他人资源 4040；上限 4225 |
| 幂等 | 同键重放返回同结果且只执行一次 |
| 并发 | 扣减 / 计数类：50 线程文件 SQLite（见 `07-inventory/code/tests`），断言无超卖 / 无重复 |
| HTTP | 路由存在、鉴权 4010、权限 4030、参数 4001、信封格式 |
| 事件 | 关键用例写入了正确主题的发件箱事件 |

运行：`cd backend && python -m pytest -q NN-name`。

## 5 MODULE.md 章节

1. 状态（已实现 / 设计）与依赖
2. 职责与边界
3. 数据模型（表、字段、索引、约束）
4. 业务规则（编号列出，每条可测）
5. 状态机 / 关键流程（含失败分支）
6. 接口（方法、路径、鉴权、幂等、权限码、agent 标记、说明）
7. 事件（发出 / 消费）
8. 并发与一致性
9. 配置
10. 错误码
11. 测试清单
12. 前端对接（页面 → 接口 → 行为）
