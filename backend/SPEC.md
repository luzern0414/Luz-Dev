# 后端规范（SPEC）

本文件是后端的总则：技术基线、分层与接口约定、错误码、硬规则（事务 / 并发 / 幂等）、弹性与降级、缓存、观测、配置与部署、测试与验收。各模块的数据表、状态机、接口、事件、配置与错误码在 `NN-name/MODULE.md`；模块目录、状态与事件总表在 `MODULES.md`；模块间可调用的导出在 `CONTRACTS.md`；接口约定与索引在 `API.md`；安全基线在 `SECURITY.md`；网关在 `ROUTING.md`；服务拆分在 `MICROSERVICES.md`。

冲突时以本文 §6 硬规则为准；外部依赖的超时、重试、熔断默认值以 §9 为准。项目从方案到上线的步骤见上一级 `WORKFLOW.md`；一个项目启用哪些模块、参数与偏离记在技术方案的标准模块表（`raw/plan.json` → `standardModules`，`tech_plan.py standard`）。

阅读顺序：§1 → §3 约定 → §6 硬规则 → §9 弹性 → §13 观测 → §14 配置 → 所选模块的 MODULE.md。

---

## 1 技术基线

| 项 | 规定 |
|---|---|
| 语言与框架 | Python 3.12+，FastAPI，Pydantic v2，SQLAlchemy 2.x（`expire_on_commit=False`） |
| 数据库 | 开发与测试 SQLite；生产 PostgreSQL（首选，支持 `SKIP LOCKED`、分区、pgvector）；MySQL 8 可选 |
| 缓存与锁 | Redis 7（限流、缓存、分布式锁、热点库存预扣） |
| 迁移 | Alembic；禁止生产使用 `create_all` |
| 异步与任务 | 发件箱表 + worker 进程；定时任务由 scheduler 进程按固定间隔触发，带分布式锁（25-jobs） |
| 认证 | JWT（HS256，`LUZ_JWT_SECRET`）+ 服务端会话（会员与后台都每请求校验会话）；后台口令 + TOTP MFA；服务间 HMAC 签名（`SECURITY.md`） |
| 对象存储 | S3 兼容（MinIO / OSS / COS），预签名直传（21-media，规划） |
| 观测 | 一行一条 JSON 日志、W3C `traceparent` 链路、Prometheus（`/metrics`），见 §13 |
| 运行配置 | `config/runtime/*.toml` + 字段说明 `luzback/runtime_config.py`，服务端热加载，见 §14 |
| 测试 | pytest + httpx TestClient；并发用线程池 + 文件 SQLite 或 Postgres 容器 |
| 包管理 | `requirements.txt`（运行时）、`requirements-dev.txt`（开发与测试） |

仓库形态：模块化单体（一个 FastAPI 应用，按模块挂载路由），按 `MICROSERVICES.md` 的阶段拆分。

---

## 2 模块

模块目录、分类与状态（标准 / 基础 / 规划）只在 `MODULES.md` 维护；每个模块对外可调用的导出见 `CONTRACTS.md`。

- 装配：`LUZ_FEATURES` 未启用的模块不导入、不暴露接口；表仍由迁移统一建立；23-app-settings 是基础设施，总是装配。
- 数据库：所有表由 `alembic upgrade head` 建立（`alembic/versions/0001_baseline.py` 起）；每次发布后执行 `python -m admin_rbac.bootstrap --seed`（权限码与内置角色）与 `python -m app_settings sync`（文案种子）。生产禁止 `create_all`。

---

## 3 分层与接口约定

### 3.1 分层

模块内的目录与分层见 `MODULE-TEMPLATE.md` §1–§2：`api.py`（路由与请求 / 响应模型）→ `service.py`（用例、事务边界、条件 UPDATE）→ `models.py`（ORM）；按需加 `rules.py`（纯函数）、`ports.py`（外部适配）、`events.py`（订阅）、`jobs.py`（定时任务）、`config.py`（模块配置）。路由不写业务；service 不依赖 FastAPI；跨模块只调用对方 `__init__` 导出的函数（`CONTRACTS.md`）或发事件，不直接读对方的表。

### 3.2 路径

- 顾客端 `/api/v1/app/<资源>`；后台 `/api/v1/admin/<资源>`；员工端与骑手端（规划）`/api/v1/staff/<资源>`、`/api/v1/rider/<资源>`；回调 `/api/v1/webhooks/<渠道>/<事件>`；智能体 `/api/v1/agent/*`（由智能体服务提供）；运维 `/internal/*`（只对内网，网关不转发）。
- 资源用复数名词；动作用 `POST /<资源>/{id}/<动词>`；版本号在路径；破坏性变更升 v2 并并行 6 个月。

### 3.3 信封与头

响应统一 `{code, message, data}`；`code = 0` 成功。请求头与响应头的完整列表见 `API.md` §1.2–§1.3：写接口必带 `Idempotency-Key`；链路用 W3C `traceparent`（没有时沿用旧的 `X-Trace-Id`）；响应头回显 `X-Request-Id`、`X-Trace-Id`、`traceparent`。

### 3.4 分页、筛选、排序、搜索与导出

所有列表接口用 `luzback.listing.ListSpec` 声明可筛选、可排序、可搜索的字段，导出用 `luzback.export.export_csv` 复用同一份声明。不在接口里手写分页和 WHERE 拼接。

| 端 | 分页 | 响应 |
|---|---|---|
| 顾客端列表 | 游标：`?cursor=<opaque>&limit=10`（上限 20）；游标签名防篡改，绑定排序与筛选条件，3 天后失效 | `{items, next_cursor, has_more}` |
| 后台列表 | 页码：`?page=1&page_size=20`（上限 100；`page × page_size ≤ 10000`，更深的翻页要求缩小筛选） | `{items, page, page_size, total}` |

| 规则 | 约定 |
|---|---|
| 等于 / 多值 | `status=paid`、`status=paid,refunded`（逗号分隔，最多 50 个）；枚举字段只接受声明的值 |
| 数值区间 | `amount_cents_min` / `amount_cents_max`，两端包含；`min > max` 返回 4001 |
| 时间区间 | `created_at_from` / `created_at_to`：只写日期时按门店时区整天计算，`to` 当天包含；带时刻时 `from` 包含、`to` 不包含；可声明最长跨度（报表与导出 31 天） |
| 空值 | `field_null=true|false`；空字符串视为未传 |
| 关键词 | `keyword` 在声明的列里模糊匹配，转义 `%` `_`，1–50 个字符，前后空格忽略 |
| 排序 | `sort=-created_at,amount_cents`，字段白名单，最多 3 个；自动追加 `id` 同向排序保证稳定；NULL 一律排在最后；默认 `-created_at` |
| 未知参数 | 返回 4001，拼错的筛选条件不会被静默忽略 |
| 导出 | 与列表同一筛选；同步导出不超过 5000 行（`sync_limit`），更多转异步导出任务（24-report，规划）；单次硬上限默认 20 万行，模块可调低（如后台审计导出 5 万行），超出 4225；UTF-8 BOM；防公式注入；敏感字段脱敏；写审计 |

### 3.5 字段约定

- 主键：`id` 自增 bigint（对外展示用 `order_no` 等业务号）；外键显式索引。
- 时间：UTC 存储，接口返回 ISO 8601 带时区；按日统计与筛选用门店时区。
- 金额：整数分，字段后缀 `_cents`；折扣用万分比 `_bp`。
- 状态：小写蛇形字符串枚举；各模块的状态值与流转在其 MODULE.md §5，前端显示文字在前端或 23-app-settings 文案库。
- 软删除：`deleted_at`（用户地址、商品、券模板、门店、员工）；默认查询过滤；唯一约束改为部分唯一索引 `WHERE deleted_at IS NULL`。订单、支付、流水、审计不可删除。
- 审计字段：`created_at, updated_at, created_by, updated_by`（后台对象）。
- 多门店：业务表带 `store_id`；多租户（SaaS）时再加 `tenant_id`，在技术方案中写明。
- 手机号、身份证：加密存储（AES-GCM）+ 哈希列查询，响应脱敏；完整值只在专门的查看权限（如 `member:reveal`、`staff:sensitive`）下返回并写审计。
- 数据表、索引与约束写在各模块 MODULE.md §3；新增表随模块迁移提交。

### 3.6 请求校验

Pydantic 模型；字符串去首尾空格；长度与范围约束在模型上；4001 的 `data.errors` 为 `[{field, message}]`。

---

## 4 错误码

唯一来源是框架 `luzback.errors.ErrorCode` 与它的 HTTP 映射；下表与代码一致，改动时两边一起改。

| code | HTTP | 含义 | 前端处理 |
|---|---|---|---|
| 0 | 200 | 成功 | — |
| 4001 | 400 | 参数错误（含缺幂等键、请求体过大时为 413） | 就近提示 |
| 4010 | 401 | 未登录 / 令牌无效或过期 | 刷新一次 |
| 4011 | 401 | 需要 MFA 或重新认证（`reauth_required` 等） | 后台跳 MFA 或重新认证 |
| 4030 | 403 | 无权限 | 提示 / 403 页 |
| 4031 | 403 | 不在门店范围 | 404 页（后台） |
| 4040 | 404 | 不存在（含他人资源） | 不存在页 |
| 4090 | 409 | 状态冲突 / 版本冲突 | 刷新并提示 |
| 4091 | 409 | 库存不足 | 行内提示 |
| 4092 | 409 | 同一幂等键的首个请求仍在处理 | 1 秒后用同一个键重试 |
| 4093 | 422 | 同一幂等键被不同请求体复用 | 换新键重新提交 |
| 4220 | 422 | 业务规则不满足（`data.reason` 给稳定原因） | 就近说明 |
| 4221 | 422 | 门店休息 / 不在可售时段 | 预约或提示 |
| 4222 | 422 | 超出配送范围 | 地址行提示 |
| 4223 | 422 | 报价已过期或价格变化 | 重新报价 |
| 4224 | 422 | 余额 / 积分不足 | 行内提示 |
| 4225 | 422 | 次数 / 数量上限（领券、抽奖、地址数、导出行数） | 提示上限 |
| 4290 | 429 | 限流（`Retry-After`） | 禁用按钮到时间 |
| 5000 | 500 | 内部错误（带 `request_id` / `trace_id`） | 通用提示，可复制编号 |
| 5020 | 502 | 上游失败（支付 / 地图 / 短信） | 通用提示 + 降级 |
| 5030 | 503 | 熔断打开 / 维护（`Retry-After`） | 降级或维护页 |

未知异常由全局处理器转为 5000，在请求上下文里记一次带堆栈的日志，不回显内部信息。支付回调等渠道要求的特殊响应体在对应 MODULE.md 标注为例外。

---

## 5 认证与授权

会员令牌与会话、后台口令与 MFA、新鲜认证、权限码与门店范围、服务间签名、回调验签的规定与数值见 `SECURITY.md` §1–§2、§7（实现分别在 01-identity、22-admin-rbac、09-wechat-pay）。数据范围：顾客只能访问 `user_id = 自己` 的资源，他人资源返回 4040；后台按 `store_scope(staff)` 过滤，越界 4031。

---

## 6 硬规则（事务、并发、幂等）

1. **金额用整数分**；所有计算在服务端完成，前端只展示。
2. **库存、余额、名额、券库存的扣减必须是带条件的原子 UPDATE**（`WHERE stock >= :qty`），`rowcount = 0` 即失败（4091 / 4220 / 4224 / 4225），由数据库裁决，不先查后改。
3. **状态流转必须是条件 UPDATE**（`WHERE status = :from`），非法流转 4090。
4. **一个用例一个事务**：下单 = 校验 + 报价 + 预占库存 + 锁券 + 写订单 + 写发件箱事件，同一事务；失败全部回滚。
5. **幂等**（`luzback.idempotency.run_idempotent`）：
   - 所有产生副作用的写接口（POST / PUT / PATCH / DELETE）必带 `Idempotency-Key`（UUID 36 位）；缺失 4001。
   - 作用域 = `(操作者, 用例名, 键)`，操作者如 `user:12`、`staff:3`，用例名如 `order.create`；同键同请求体返回首次响应；同键不同请求体（指纹 = 请求体的 sha256）返回 4093（HTTP 422）；同键的首个请求仍在处理返回 4092（HTTP 409）。
   - `processing` 记录超过 60 秒视为僵尸，可被接管；记录保留 24 小时后由定时任务清理。
   - 幂等记录与业务写同事务提交。
6. **发件箱**（`luzback.outbox`）：业务事件在同一事务写入 `outbox_events`（`event_id, topic, aggregate_type, aggregate_id, version, payload_json, status, attempts, next_attempt_at, occurred_at, traceparent`），worker 用 `SELECT … FOR UPDATE SKIP LOCKED` 拉取，失败按 1m / 5m / 30m / 2h / 12h 退避，5 次后 `failed` 并告警；消费者用 `mark_processed` 按 `event_id` 去重。主题与字段见 `MODULES.md` 事件总表。
7. **回调优先**：支付回调到达时若订单已被取消，仍记录支付成功并进入“待人工处理”（自动退款），不回滚支付记录。
8. **时间以服务端为准**：倒计时、截单、过期由服务端字段驱动。
9. **读写分离的边界**：报表与导出只读从库或物化视图，不锁业务表。
10. **禁止**：`SELECT *` 后在应用层过滤敏感字段；在事务中调用外部 HTTP；用浮点算钱；在循环中逐条查询（N+1）。

---

## 7 数据模型

各模块的数据表、字段、索引与约束只写在各自 MODULE.md §3（规划模块写在 MODULES.md 条目里，实现时补 MODULE.md）；全部表由 `alembic/versions/` 建立。本文不另列总图。

---

## 8 状态机与事件

每个模块的状态与流转（含失败分支）写在其 MODULE.md §5：订单 08-order、支付与退款单 09-wechat-pay、售后单 10-after-sales、储值 15-recharge、会员卡 29-member-card、抽奖 18-lottery。跨模块事件的主题、生产者、消费者与字段见 `MODULES.md` 事件总表，`tests/test_event_contracts.py` 自动核对。外卖接单、备餐、配送、堂食等流转属于规划模块（02-store、11-delivery、12-queue），实现时先写进它们的 MODULE.md。

---

## 9 外部依赖弹性与降级（统一参数表）

下表是默认值；可调的超时、重试与熔断参数写在运行配置（`[http]`、`[breaker]` 与各模块分组，如 `[wechat_pay]`），改文件热加载（熔断器参数重启生效）。

| 依赖 | 超时 | 重试 | 熔断（60 秒窗口） | 舱壁 | 降级 |
|---|---|---|---|---|---|
| 数据库 | 语句 5s（`[db] statement_timeout_ms`） | 读 1 次（连接错误） | — | 连接池按部署参数 | 报表走从库；不可用 → 5000 |
| Redis | 200ms | 1 次 | 10 次失败 → 打开 30s | — | 限流放行并记日志；缓存穿透到 DB；库存预扣退化为 DB 条件 UPDATE |
| 微信登录 code2session | 3s | 2 次（退避 0.5/1s） | 10 次 / 50% → 30s | — | 5020，前端提示重试 |
| 微信支付（下单 / 查询 / 退款） | 8s / 5s / 10s | 下单 0 次；查询 2 次；退款 0 次（任务重试） | 10 次 / 50% → 30s | 30 并发 | 下单失败订单保持待支付；查询失败走补偿任务 |
| 地图（逆地理 / 路线） | 3s | 1 次 | 10 次 / 50% → 60s | — | 直线距离 + 系数 1.3；无结果 → 手动选址 |
| 短信 | 3s | 任务重试 1m/5m/30m | 10 次 / 50% → 60s | — | 改订阅消息；仍失败入失败列表 |
| 订阅消息 / 企微 | 3s | 任务重试 | 同上 | — | 入失败列表可重发 |
| 第三方配送 | 5s | 1 次 | 10 次 / 50% → 60s | — | 转自配送或人工指派 |
| 对象存储 | 10s（签名）/ 30s（上传） | 1 次 | — | — | 返回 5020；评价可无图提交 |
| 大模型（智能体服务内） | 20s，首 token 8s | 2 次后回退备用模型 | 见 `references/agent-foundation.md` §16 | — | 降级梯度见 `agent/backend/SPEC.md` §8.2 |

通用规则：只重试可重试错误（超时、连接、429、5xx）且调用幂等；退避带抖动；熔断半开放 3 个探测请求；每次降级写日志 `degraded=<依赖>` 并上报指标；网关熔断返回 5030 + `Retry-After: 5`。

服务端降级矩阵：

| 场景 | 行为 |
|---|---|
| 支付渠道不可用 | 订单保持待支付到超时；发起支付返回 5020 并带 `payment_unavailable`，前端提示稍后支付 |
| 库存 Redis 不可用 | 退化为 DB 条件 UPDATE（吞吐下降但正确） |
| 推荐 / 评价汇总慢 | 接口返回空列表 + `degraded`，不阻塞首页 |
| 地图不可用 | 配送范围用直线距离；轨迹接口返回状态与 ETA |
| 通知渠道不可用 | 事件留在失败列表，不影响订单流转 |
| 从库延迟 > 30s | 报表标注“数据延迟”，驾驶舱 KPI 退回主库轻查询 |

---

## 10 后台任务与定时

任务写在各模块 `<pkg>/jobs.py` 的 `JOBS`（`luzback.scheduler.Job`，固定间隔），事件订阅写在 `<pkg>/events.py` 的 `SUBSCRIPTIONS`；由 25-jobs 的 scheduler 与 worker 进程发现并运行（分布式锁、心跳、每次运行新建 trace），`python -m jobs list` 列出全部任务与订阅。任务体必须幂等（按时间窗口 + 业务键），按日任务在任务体里判断窗口。每个模块有哪些任务、多久跑一次写在其 MODULE.md §9；运行进程与规则见 `25-jobs/MODULE.md`。

---

## 11 缓存

| 数据 | Key | TTL | 失效 |
|---|---|---|---|
| 门店信息与营业状态 | `store:{id}` | 5 分钟 | 后台保存或开业 / 停业时删除 |
| 分类与商品列表 | `catalog:{store_id}:{category}:{v}` | 5 分钟 | `catalog_version` 自增（商品 / 套餐 / 估清变更） |
| 商品详情 | `product:{id}:{version}` | 10 分钟 | 版本号 |
| 库存展示 | `stock:{sku_id}` | 30 秒 | 扣减时更新 |
| 逆地理 | `geo:{sha1}` | 7 天 | — |
| 限流桶 | `rl:{scope}:{key}` | 1 分钟 | — |
| 驾驶舱汇总 | `dash:{store_id}:{date}` | 30 秒 | 定时物化 |

不缓存：订单、支付、余额、积分、排号、售后、会话（会话每请求查库，登出即时生效）。缓存穿透用空值 60 秒；雪崩用 TTL 抖动 ±10%；热点用单飞（single-flight）。

---

## 12 安全

安全基线与数值（限流、上传、PII、回调、后台会话）只在 `SECURITY.md` 维护，限流阈值的配置键在运行配置 `[rate_limit]` 与各模块分组。本文不重复。

---

## 13 可观测性

- 日志（`references/config-logging.md` §3，框架 `luzback.logging`）：一行一条 JSON 写 stdout，经有上限的队列异步写出（满时丢 DEBUG / INFO 并计 `luz_log_dropped_total`，WARNING 以上与安全事件不丢）。字段 `ts`（UTC 毫秒 Z）、`level`、`logger`、`service`、`env`、`version`、`instance`、`config_version`、`event`、`message`、`trace_id`、`span_id`、`request_id`、`user_ref?`、`store_id?`、`outcome`、`reason?`、`error.type / error.message / error.stack`；每个请求结束写一条 `http.request`（`http.method`、`http.route` 路由模板、`http.status`、`duration_ms`）。事件名在 `luzback/log_events.py` 登记，安全事件 `security.*`（OWASP 词汇）带 `category=security`。字段名黑名单与正则脱敏，控制字符转义，字符串截断 512 字、堆栈 2000 字；不记录正文中的手机号、地址、令牌、支付参数。
- Trace：W3C `traceparent`（`luzback.tracing`）：入口沿用合法的 traceparent（新 span）或旧 `X-Trace-Id`，否则新建；响应头返回 `X-Request-Id`、`X-Trace-Id`、`traceparent`；发件箱事件与消息带链路，消费端接上；定时任务每次运行新建 trace 并带 `job`、`run_id`。接入 OpenTelemetry 时沿用同一个 trace_id。
- 指标（Prometheus）：请求 QPS / 延迟 p50 p95 p99 / 错误率（按路由）；外部依赖延迟与失败率；熔断状态；发件箱积压与失败数；任务运行时长与失败；库存扣减失败率；支付成功率；退款失败数；日志丢弃数；配置版本。
- 告警：规则与阈值（SLO 燃烧率、5xx、依赖、发件箱、支付回调）见 `references/operations.md` §4；只在日志里看得到的告警（CRITICAL、`security.*` 突增、配置热加载被拒、服务无日志）见 `references/config-logging.md` §3.11。
- 健康：`/healthz`（进程存活）、`/readyz`（DB / Redis / 迁移版本 / 配置版本）、`/metrics`（只对内网或 `LUZ_METRICS_TOKEN`）。

---

## 14 配置与部署

### 14.1 五类设置分开放

依据 `references/config-logging.md` §1：

| 类别 | 放在哪 | 谁改 | 生效 |
|---|---|---|---|
| 秘密（JWT 与字段密钥、商户私钥、APIv3 密钥、AppSecret、服务密钥） | 密钥服务或服务器受限文件，部署时注入（模板 `assets/deploy/api.env.example`） | 运维 | 重启；按轮换流程 |
| 部署参数（数据库与 Redis 地址、连接池、可信网段、模块开关 `LUZ_FEATURES`、配置目录） | 环境变量（`/etc/luz/api.env`） | 运维 | 重启 |
| 运行参数（超时、上限、限流阈值、开关、日志级别、最低可用版本） | 代码仓库 `config/runtime/base.toml` 与 `<环境>.toml`；字段说明 `luzback/runtime_config.py` | 开发，经代码评审 | 服务端热加载，不在后台展示 |
| 经营与运营内容（营业时间、起送价、公告、券、奖品） | 数据库，各业务模块的后台 | 运营 | 即时，写审计 |
| 前台文案与展示（提示、空状态、须知、首页模块顺序与显隐、展示开关） | 数据库（23-app-settings：`ui_texts`、`display_settings`）；后台可见的由运营在“文案与展示”页维护，`admin_visible` 为否的由开发经文案种子（`seed/texts.toml`）维护 | 运营 / 开发 | 保存即生效，小程序下次显示该页时更新 |

环境变量不覆盖运行参数。各模块的运行参数在 `base.toml` 里各自的分组（`[identity]`、`[order]`、`[wechat_pay]` 等），模块的 `config.py` 用 `luzback.module_config.ModuleConfig` 把它们与环境变量里的部署参数合并；读取用 `current_config()` / `config_for()`；迁移前的环境变量名启动时打警告并指出新键。按环境区分的值只写在 `dev.toml` / `staging.toml` / `prod.toml`（日志级别、限流阈值等）。

### 14.2 部署

- 默认方案是单机或少量主机的 Docker Compose：`nginx`、`api`（2 副本）、`worker`、`scheduler`、`postgres`、`redis`，见 `references/operations.md` §2 与 `assets/deploy/`；开发用 `backend/deploy/docker-compose.yml`。拆服务后的形态见 `MICROSERVICES.md`。
- 启动顺序：迁移（含权限与文案种子）→ api（`/readyz` 通过）→ worker / scheduler；每个进程先加载运行配置，校验失败即退出。
- 迁移按 expand / contract：先加列、后写数据、再删列（`assets/deploy/RUNBOOK.md` §1）。
- 运行配置目录整目录只读挂载进 api、worker、scheduler，各进程监听并热加载；`/internal/config` 与 `config_loads` 核对各实例版本（`assets/deploy/RUNBOOK.md` §5）。
- 开发与演示数据由项目按需编写幂等的种子脚本，不放生产库。

---

## 15 测试策略

| 层 | 要求 |
|---|---|
| 单元 | service 函数；每个状态流转的合法与非法用例；金额计算（含活动、券、积分、余额） |
| 并发 | 库存 50 线程、券库存、积分、余额、名额、幂等重放；断言无超卖、无重复 |
| API | 每个接口：成功、4001、4010、4040（他人资源）、4090、幂等重放（4092 / 4093）；分页与筛选 |
| 回调 | 验签失败、金额不符、重复回调、已取消订单的回调 |
| 任务 | 超时关闭、对账、退款重试、过期任务的幂等 |
| 契约 | 路由装配与路径冲突（`tests/test_app_wiring.py`）；事件字段（`tests/test_event_contracts.py`）；对 OpenAPI 的接口测试在 CI（`references/operations.md` §9） |
| 配置 | 三个环境文件按字段说明加载；坏文件被拒、旧值保留（`python -m luzback.config_check --env all`） |
| 端到端 | 登录 → 报价 → 下单 → 支付回调 → 履约 → 完成 → 评价；售后部分退款；超时关单（`tests/test_integration.py`） |
| 混沌（预发） | 杀 Redis、延迟支付回调、地图超时，验证降级矩阵 |
| 负载 | 读 p95 < 300ms、写 p95 < 800ms、错误率 < 0.5%（k6，`references/operations.md` §7） |

运行：`cd backend && python -X utf8 -m pytest -q`；新模块合入前必须带测试。

---

## 16 智能体接入

- 智能体工具只调用 MODULE.md §6 中 agent 列为 `read`、`write` 或 `write_high` 且已实现的接口；`no` 的接口不注册为工具。取值与含义按 `references/agent-foundation.md` §6：`read` 直接执行；`write` 执行后回显并可撤销；`write_high` 必须先用只读预览接口生成摘要、经用户审批后执行。
- 报价预览 `POST /api/v1/app/orders/preview`（08-order，只读）供下单审批摘要使用；写接口接受智能体服务签名（`SECURITY.md` §2）。
- 审批后的写操作由智能体服务执行，幂等键由智能体生成（UUID v5）；后端不区分调用方，只认令牌与幂等键。
- 后台工具按员工令牌权限码校验；后台接口只对 `X-Client: admin` 的会话开放给智能体。
- 知识库文档表与索引由 26-agent-support（规划）提供后台维护，向量与检索在智能体服务内。

---

## 17 验收清单

- [ ] 全局异常处理器存在；未知异常返回 5000 + request_id
- [ ] 所有写接口要求幂等键；作用域含操作者；不同请求体复用键返回 4093（422）；处理中返回 4092；僵尸记录可接管（有测试）
- [ ] 库存 / 券 / 积分 / 余额扣减为条件 UPDATE（并发测试通过）
- [ ] 状态流转为条件 UPDATE；非法流转 4090（每条边有测试）
- [ ] 下单事务包含报价、扣减、写单、发件箱；失败全回滚
- [ ] 支付回调验签、金额核对、重复回调幂等、已取消订单的回调不丢
- [ ] 发件箱 worker 用 SKIP LOCKED，退避与失败告警可用
- [ ] 分页、筛选、排序符合 §3.4；他人资源 4040
- [ ] 软删除对象使用部分唯一索引；订单 / 流水 / 审计不可删
- [ ] 弹性参数按 §9 落地；熔断可触发并恢复；降级写日志与指标
- [ ] 定时任务有锁、幂等、运行记录；超时关闭与对账可用
- [ ] 日志字段与脱敏符合 §13；trace 贯穿请求、事件与任务；`/readyz` 与 `/metrics` 可用
- [ ] 运行配置三个环境文件按字段说明加载通过（`python -m luzback.config_check --env all`）；坏文件被拒、旧值保留；各实例版本一致
- [ ] Alembic 迁移可从零建库；权限与文案种子幂等
- [ ] 各模块 MODULE.md §6 与实际路由一致；agent 列只把已实现接口标为 `read` / `write` / `write_high`
- [ ] 端到端测试覆盖 §15 列出的主流程
