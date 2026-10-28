# 后端规范（SPEC）v2

本文件是后端的总则：技术基线、模块清单、分层与接口约定、错误码、鉴权、硬规则（事务 / 并发 / 幂等）、领域模型、订单状态机、弹性与降级、任务、缓存、观测、部署、测试、生成顺序与项目文档模板。每个功能模块的细节在 `backend/<模块>/MODULE.md`，接口清单在 `API.md`，安全基线在 `SECURITY.md`，网关在 `ROUTING.md`，服务拆分在 `MICROSERVICES.md`。

冲突时以本文 §6 硬规则为准；数值冲突时以本文 §9 弹性参数表为准（`MICROSERVICES.md` 与 `ROUTING.md` 的旧数值以此表修订）。

阅读顺序：§1 → §2 模块清单 → §3 约定 → §6 硬规则 → §8 状态机 → §9 弹性 → §17 生成顺序。生成项目时先填 `requirements/<项目名>.md`（§18），再按 §17 实施。

---

## 1 技术基线

| 项 | 规定 |
|---|---|
| 语言与框架 | Python 3.11+，FastAPI，Pydantic v2，SQLAlchemy 2.x（`expire_on_commit=False`） |
| 数据库 | 开发 SQLite；生产 PostgreSQL 14+（首选，支持 `SKIP LOCKED`、分区、pgvector）；MySQL 8 可选 |
| 缓存与锁 | Redis 7（限流、缓存、分布式锁、热点库存预扣、事件回放） |
| 迁移 | Alembic；禁止生产使用 `create_all` |
| 异步与任务 | 发件箱表 + worker 进程；定时任务由 scheduler 进程（APScheduler 或 K8s CronJob）触发，带分布式锁 |
| 认证 | JWT HS256（开发）/ RS256（生产）；后台 + TOTP MFA；服务间 HMAC 签名 |
| 对象存储 | S3 兼容（MinIO / OSS / COS），预签名上传 |
| 观测 | 结构化 JSON 日志、OpenTelemetry（trace）、Prometheus（`/metrics`） |
| 测试 | pytest + httpx TestClient；并发用线程池 + 文件 SQLite 或 Postgres 容器 |
| 包管理 | `requirements.txt`（锁版本）；`pip install -r requirements.txt` |

仓库形态：模块化单体（一个 FastAPI 应用，按模块挂载路由），按 `MICROSERVICES.md` 的阶段拆分。

---

## 2 模块清单（全集）

| 目录 | 职责 | 状态 | 依赖 |
|---|---|---|---|
| `00-framework/` | 配置、数据库、错误码、信封、JWT、幂等、发件箱、限流、安全头、trace、测试工具 | 已实现 | — |
| `01-identity/` | 微信登录、刷新、登出、手机号绑定（短信 / 一键）、用户资料、注销 | 登录 / 刷新已实现；其余设计 | framework |
| `store/` | 门店信息、营业时间、履约参数、打印机、连锁复制 | 待建 | identity |
| `catalog/` | 分类、商品、规格组 / 选项、套餐、标签、可售渠道与时段、估清、搜索与热词 | 待建 | store、inventory |
| `cart/` | 服务端购物车（登录合并本地）、失效项清理 | 待建 | catalog |
| `address/` | 收货地址 CRUD、默认地址 | 已实现（PUT 设计） | identity |
| `06-geo/` | 距离、配送范围、逆地理、地理编码缓存 | 函数已实现；接口设计 | store |
| `07-inventory/` | SKU 库存条件扣减 / 回补、预留、热点预扣、变动记录 | 函数已实现；接口设计 | catalog |
| `order/` | 下单（报价预览、幂等）、状态机、列表 / 详情、取消、催单、改地址、核销、堂食加菜与结账 | 创建 / 详情 / 取消已实现；其余设计 | catalog、inventory、address、marketing、membership |
| `09-wechat-pay/` | 支付单（业务类型：订单 / 充值 / 会员）、渠道、回调、查询、超时关闭、退款与退款回调 | 主路径已实现；退款设计 | order |
| `after-sales/` | 售后单（退款 / 部分退款 / 补发）、审核、退款执行与失败处理 | 待建 | order、payment |
| `delivery-dispatch/` | 配送任务、骑手、指派 / 改派、轨迹、异常、第三方配送适配 | 设计 | order |
| `queue/` | 排队取号、叫号、过号、入座、桌台管理、桌台码 | 设计 | store |
| `membership/` | 会员等级、权益、积分获取 / 抵扣 / 过期、开通与续费 | 积分已实现；等级 / 开通设计 | identity、payment |
| `recharge/` | 充值档位、储值账户与流水、充值退款、对账 | 设计 | payment |
| `16-coupon/` | 优惠券模板、领取 / 核销 / 退回、定向发放、活动（满减 / 限时 / 第二件半价 / 新人礼 / 专题）、冲突与优先级 | 领券已实现；其余设计 | catalog、order |
| `review/` | 评价、图片、审核、商家回复、汇总 | 创建已实现；其余设计 | order、media |
| `notification/` | 事件消费、订阅消息 / 短信 / 企微、模板、失败重发 | 设计 | outbox |
| `media/` | 预签名上传、图片校验与裁切、CDN 地址 | 待建 | — |
| `22-admin-rbac/` | 后台账号、角色、权限码、门店范围、MFA、审计 | 设计 | identity |
| `settings/` | 分组参数（营业 / 费用 / 积分 / 通知 / 打印 / 隐私 / 品牌），版本与生效时间 | 待建 | admin-rbac |
| `report/` | 驾驶舱汇总、日报 / 周报、导出任务 | 待建 | order、payment、membership |
| `scheduler/` | 定时任务注册、分布式锁、运行记录 | 待建 | framework |
| `agent-support/` | 为智能体提供 `price_preview`、知识库文档表与索引接口、服务签名校验 | 待建 | order、marketing |

规则：只挂载需要的模块；未开启的模块不导入、不建表、不暴露接口；项目的模块开关记录在 `requirements/<项目名>.md`。

---

## 3 分层与接口约定

### 3.1 分层

`api.py`（路由 + 请求 / 响应模型）→ `service.py`（用例、事务边界）→ `repo.py`（查询与条件 UPDATE）→ `models.py`（ORM）。路由不写业务；service 不依赖 FastAPI；跨模块只调用对方 service 的公开函数或发事件，不直接读对方表。

### 3.2 路径

- 顾客端 `/api/v1/app/<资源>`；后台 `/api/v1/admin/<资源>`；员工端（叫号屏、骑手）`/api/v1/staff/<资源>`、`/api/v1/rider/<资源>`；回调 `/api/v1/webhooks/<渠道>/<事件>`；智能体 `/api/v1/agent/*`（由智能体服务提供）。
- 资源用复数名词；动作用 `POST /<资源>/{id}/<动词>`；版本号在路径；破坏性变更升 v2 并并行 6 个月。

### 3.3 信封与头

响应统一 `{code, message, data}`；`code = 0` 成功。请求头：`Authorization: Bearer`、`Idempotency-Key`（写接口必带）、`X-Trace-Id`（可选，网关生成）、`X-Client: miniprogram|h5|admin|staff|agent`、`X-Client-Version`。响应头回显 `X-Trace-Id`、`X-Request-Id`。

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
| 导出 | 与列表同一筛选；≤ 5000 行同步，超过转异步任务；单次上限 20 万行（4225）；UTF-8 BOM；防公式注入；敏感字段脱敏；写审计 |

### 3.5 字段约定

- 主键：`id` 自增 bigint（对外展示用 `order_no` 等业务号）；外键显式索引。
- 时间：UTC 存储，接口返回 ISO 8601 带时区；门店时区在 `stores.timezone`。
- 金额：整数分，字段后缀 `_cents`；折扣用万分比 `_bp`。
- 状态：小写蛇形字符串枚举；状态词映射到前端词表在 `order/MODULE.md` §4。
- 软删除：`deleted_at`（用户地址、商品、券模板、门店、员工）；默认查询过滤；唯一约束改为部分唯一索引 `WHERE deleted_at IS NULL`。订单、支付、流水、审计不可删除。
- 审计字段：`created_at, updated_at, created_by, updated_by`（后台对象）。
- 多门店：业务表带 `store_id`；多租户（SaaS）时再加 `tenant_id`，在 `requirements` 中开启。
- 手机号、身份证：加密存储（AES-GCM），响应脱敏；完整值只在 `*:sensitive` 权限下返回并审计。

### 3.6 请求校验

Pydantic 模型；字符串去首尾空格；长度与范围约束在模型上；4001 的 `data.errors` 为 `[{field, message}]`。

---

## 4 错误码

| code | HTTP | 含义 | 前端处理（小程序 SPEC §8） |
|---|---|---|---|
| 0 | 200 | 成功 | — |
| 4001 | 400 | 参数错误（含缺幂等键） | 就近提示 |
| 4010 | 401 | 未登录 / 令牌无效或过期 | 刷新一次 |
| 4011 | 401 | 需要 MFA 或二次验证 | 后台跳 MFA |
| 4030 | 403 | 无权限 | 提示 / 403 页 |
| 4031 | 403 | 不在门店范围 | 404 页（后台） |
| 4040 | 404 | 不存在（含他人资源） | 不存在页 |
| 4090 | 409 | 状态冲突 / 版本冲突 | 刷新并提示 |
| 4091 | 409 | 库存不足 | 行内提示 |
| 4092 | 409 | 同幂等键处理中 | 1 秒后查询 |
| 4093 | 409 | 幂等键被不同请求体复用 | 新键重试 |
| 4220 | 422 | 业务规则不满足 | 就近说明 |
| 4221 | 422 | 门店休息 / 不在可售时段 | 预约或提示 |
| 4222 | 422 | 超出配送范围 | 地址行提示 |
| 4223 | 422 | 价格已变化（报价过期） | 重新报价 |
| 4290 | 429 | 限流（`Retry-After`） | 禁用按钮 |
| 5000 | 500 | 内部错误（带 trace_id） | 通用提示 |
| 5020 | 502 | 上游失败（支付 / 地图 / 短信） | 通用提示 + 降级 |
| 5030 | 503 | 熔断打开 / 维护（`Retry-After`） | 降级 |

未知异常必须被全局处理器转为 5000 并记录 trace_id（当前框架缺少该处理器，实现时补上）。支付回调等渠道要求的特殊响应体在对应 MODULE 标注为例外。

---

## 5 认证与授权

| 主体 | 方式 | 说明 |
|---|---|---|
| 顾客 | 微信 code2session → 访问令牌 2h + 刷新令牌 30d（旋转，`refresh_version`） | 手机号绑定后 `phone_verified` 声明；敏感操作（注销）需 `fresh` 声明（15 分钟内登录） |
| 后台员工 | 账号密码（bcrypt / argon2）+ TOTP MFA → 访问 2h 滑动 + 刷新 7d | 5 次失败锁 15 分钟；权限码装饰器；门店范围过滤 |
| 员工端 / 骑手 | 员工令牌（`kind=staff`），权限限 queue / order 核销 / delivery | — |
| 智能体服务 | 转发用户令牌 + `X-Service: agent` + HMAC 签名（时间戳 ± 5 分钟，nonce 去重） | 后端按用户令牌做数据范围；写接口只对签名有效的请求开放 `agent: yes` |
| 渠道回调 | 微信支付 RSA / HMAC 验签；第三方配送签名 | 失败 4010，不重试 |

数据范围：顾客只能访问 `user_id = 自己` 的资源，他人资源返回 4040；后台按 `store_scope`；全局管理员除外。

---

## 6 硬规则（事务、并发、幂等）

1. **金额用整数分**；所有计算在服务端完成，前端只展示。
2. **库存、余额、名额、券库存的扣减必须是带条件的原子 UPDATE**（`WHERE stock >= :qty`），`rowcount = 0` 即失败（4091 / 4220），由数据库裁决，不先查后改。
3. **状态流转必须是条件 UPDATE**（`WHERE status = :from`），非法流转 4090。
4. **一个用例一个事务**：下单 = 校验 + 报价 + 扣库存 + 扣券 + 扣积分 + 写订单 + 写发件箱事件，同一事务；失败全部回滚。
5. **幂等**：
   - 所有写接口（POST / PUT / DELETE 产生副作用的）必带 `Idempotency-Key`（UUID 36 位）；缺失 4001。
   - 作用域 = `(user_or_staff_id, method+path)`；同键同作用域返回首次响应；同键不同请求体（指纹 = sha256(body)）返回 4093。
   - `processing` 记录超过 60 秒视为僵尸，可被接管；记录保留 24 小时后清理（定时任务）。
   - 幂等记录与业务写同事务提交。
6. **发件箱**：业务事件在同一事务写入 `outbox_events`（`event_id uuid, type, aggregate_type, aggregate_id, version, occurred_at, payload, status, attempts, next_attempt_at`），worker 用 `SELECT … FOR UPDATE SKIP LOCKED` 拉取，失败按 1m / 5m / 30m / 2h / 12h 退避，5 次后 `failed` 并告警；消费者按 `event_id` 去重。
7. **回调优先**：支付回调到达时若订单已被取消，仍记录支付成功并进入"待人工处理"（自动退款任务），不回滚支付记录。
8. **时间以服务端为准**：倒计时、截单、过期由服务端字段驱动。
9. **读写分离的边界**：报表与导出只读从库或物化视图，不锁业务表。
10. **禁止**：`SELECT *` 后在应用层过滤敏感字段；在事务中调用外部 HTTP；用浮点算钱；在循环中逐条查询（N+1）。

---

## 7 领域模型（ERD 概览）

```
tenants?(SaaS) ─┬─ stores ─┬─ store_hours, store_fulfillment, printers, tables
                │          ├─ categories ─ products ─ product_specs(groups/options) ─ skus ─ stock_logs
                │          ├─ set_meals ─ set_meal_groups ─ set_meal_items
                │          └─ queue_counters ─ queue_tickets
users ─┬─ user_phones, user_profiles, addresses, carts ─ cart_items
       ├─ members(level) ─ points_ledger, stored_accounts ─ stored_ledger, recharge_orders
       ├─ user_coupons ─ coupon_templates ─ campaigns
       ├─ orders ─ order_items(snapshot) ─ order_status_logs, payment_records(business_type), after_sales ─ after_sales_items
       │        └─ delivery_tasks ─ delivery_events, rider_locations
       ├─ reviews ─ review_images, review_replies
       └─ notifications ─ notification_templates, subscribe_consents
admin_users ─ admin_roles ─ role_permissions; admin_store_scopes; audit_logs
settings(group, key, value, version, effective_at); media_assets; scheduler_runs; idempotency_records; outbox_events
kb_docs ─ kb_chunks(embedding)
```

关键字段：`orders(store_id, user_id, order_no, channel: dine_in|delivery|pickup, table_id?, status, sub_status?, items_cents, packaging_cents, delivery_cents, discount_cents, coupon_cents, points_cents, member_discount_cents, total_cents, paid_cents, refunded_cents, address_snapshot(json), expected_at, remark, pickup_code, cancel_reason, paid_at, accepted_at, ready_at, delivered_at, completed_at, cancelled_at, version)`；`payment_records(business_type: order|recharge|membership, business_id, out_trade_no, amount_cents, status, channel, channel_txn_id, paid_at)`；`skus(product_id, spec_values(json), price_cents, stock, sold_out_today, version)`。

索引清单与完整字段在各 MODULE.md §2；新增表先登记到本节。

---

## 8 订单状态机 v2

```
pending_payment ──pay──► paid ──accept──► accepted ──start──► preparing ──ready──► ready
      │                   │                                                    ├─(delivery)─► delivering ──delivered──► delivered ─┐
      │ cancel/timeout    │ reject(→refund)                                     ├─(pickup)───► awaiting_pickup ──verify──► picked_up ┤
      ▼                   ▼                                                     └─(dine_in)──► served ──checkout──► ────────────────┼──► completed
   cancelled           refunding ──refund_ok──► refunded                                                                           │
                            └──refund_failed──► refund_failed（人工）                                      after_sales(任意已付款后) ─┘
```

| 从 | 到 | 触发 | 副作用 |
|---|---|---|---|
| pending_payment | paid | 支付回调 | `paid_at`，事件 `order.paid`，通知商家（新单） |
| pending_payment | cancelled | 用户取消 / 15 分钟超时 | 回补库存、退券、退积分，事件 `order.cancelled` |
| paid | accepted | 商家接单（或自动接单开关） | `accepted_at`，事件 `order.accepted`，通知用户 |
| paid | refunding | 商家拒单（原因） | 自动创建售后单（全额），回补库存 |
| accepted | preparing | 开始备餐 | 事件 `order.preparing` |
| preparing | ready | 出餐完成 | `ready_at`；外卖 → 创建配送任务；自提 → 生成取餐码通知；堂食 → `served` |
| ready | delivering | 骑手取餐 | 事件 `order.delivering` |
| delivering | delivered | 骑手送达（300m 内） | `delivered_at`，事件 `order.delivered` |
| ready | awaiting_pickup | 自提出餐 | 通知取餐码 |
| awaiting_pickup | picked_up | 核销取餐码 | — |
| ready | served | 堂食上菜 | — |
| served | completed | 结账（堂食）/ 自动 | 积分入账 |
| delivered / picked_up | completed | 用户确认或 24 小时自动 | 积分入账，事件 `order.completed`，开放评价 |
| 任意已付款状态 | refunding | 售后通过 | 冻结后续动作 |
| refunding | refunded / refund_failed | 渠道回调 | 成功：回补库存（未出餐）、积分追回；失败：人工 |

- 花店映射：`preparing = 已备花`，`delivering = 配送中 / 已发货`（按配送方式），`served` 不用。
- 自动接单：`settings.order.auto_accept = true` 时 `paid → accepted` 同事务完成。
- 超时：待接单 > 3 分钟、备餐 > 预计出餐 × 1.5、配送 > 预计 × 1.5 → `sub_status = overdue`，后台标红、通知。
- 堂食加菜：`served` 状态允许追加子订单（`parent_order_id`），结账合并。
- 状态词映射到前端：`backend/order/MODULE.md` §4。

**售后单**：`pending_review → approved → refunding → refunded | refund_failed`；`pending_review → rejected`；`approved → reshipped`（补发生成新订单）；任意 → `closed`（用户撤销）。退款金额 ≤ 实付 − 已退；按商品行部分退时回补对应库存（未出餐）。

**支付单**：`pending → success | failed | closed`；退款单 `refund_pending → refund_success | refund_failed`；一个订单同一时刻只允许一个 `pending` 支付单（复用未过期的）。

**事件全集**：`order.created / paid / accepted / preparing / ready / delivering / delivered / picked_up / served / completed / cancelled / refunding / refunded`、`payment.succeeded / failed / refunded / refund_failed`、`after_sales.created / approved / rejected`、`delivery.assigned / picked_up / delivered / exception`、`queue.created / called / missed / seated / cancelled`、`member.joined / expiring / expired`、`points.earned / redeemed / expired`、`recharge.succeeded`、`coupon.claimed / used / expired`、`review.created`、`stock.low`。

---

## 9 外部依赖弹性与降级（统一参数表）

| 依赖 | 超时 | 重试 | 熔断（60 秒窗口） | 舱壁 | 降级 |
|---|---|---|---|---|---|
| 数据库 | 读 2s / 写 5s | 读 1 次（连接错误） | — | 连接池 20 / worker 10 | 报表走从库；不可用 → 5000 |
| Redis | 200ms | 1 次 | 10 次失败 → 打开 30s | — | 限流放行并记日志；缓存穿透到 DB；库存预扣退化为 DB 条件 UPDATE |
| 微信登录 code2session | 3s | 2 次（退避 0.5/1s） | 10 次 / 50% → 30s | — | 5020，前端提示重试 |
| 微信支付（下单 / 查询 / 退款） | 8s / 5s / 10s | 下单 0 次；查询 2 次；退款 0 次（任务重试） | 10 次 / 50% → 30s | 30 并发 | 下单失败订单保持待支付；查询失败走对账任务 |
| 地图（逆地理 / 路线） | 3s | 1 次 | 10 次 / 50% → 60s | — | 直线距离 + 系数 1.3；无结果 → 手动选址 |
| 短信 | 3s | 任务重试 1m/5m/30m | 10 次 / 50% → 60s | — | 改订阅消息；仍失败入失败列表 |
| 订阅消息 / 企微 | 3s | 任务重试 | 同上 | — | 入失败列表可重发 |
| 第三方配送 | 5s | 1 次 | 10 次 / 50% → 60s | — | 转自配送或人工指派 |
| 对象存储 | 10s（签名）/ 30s（上传） | 1 次 | — | — | 返回 5020；评价可无图提交 |
| 大模型（智能体服务内） | 20s | 2 次 | 见 `agent/backend/SPEC.md` §8 | — | 见该文 |

通用规则：只重试可重试错误（超时、连接、429、5xx）且调用幂等；退避带抖动；熔断半开放 3 个探测请求；每次降级写日志 `degraded=<依赖>` 并上报指标；网关熔断返回 5030 + `Retry-After: 5`。

服务端降级矩阵：

| 场景 | 行为 |
|---|---|
| 支付渠道不可用 | 下单成功但返回 `payment_unavailable=true`，订单保留 15 分钟，前端提示稍后支付 |
| 库存 Redis 不可用 | 退化为 DB 条件 UPDATE（吞吐下降但正确） |
| 推荐 / 评价汇总慢 | 接口返回空列表 + `degraded`，不阻塞首页 |
| 地图不可用 | 配送范围用直线距离；轨迹接口返回状态与 ETA |
| 通知渠道不可用 | 事件留在失败列表，不影响订单流转 |
| 从库延迟 > 30s | 报表标注"数据延迟"，驾驶舱 KPI 退回主库轻查询 |

---

## 10 后台任务与定时

`scheduler/` 注册任务（名称、cron、超时、锁 key、幂等键规则），运行记录 `scheduler_runs(job, started_at, finished_at, status, error, host)`；同一任务用 Redis 锁（`SET NX PX`）保证单实例执行；任务体必须幂等（按时间窗口 + 业务键）。

| 任务 | 周期 | 说明 |
|---|---|---|
| 待支付超时关闭 | 每分钟 | `pending_payment` 超过 `order.pay_timeout_minutes` → cancelled |
| 支付对账 | 每 5 分钟 / 日终 | 查询 `pending` 超 2 分钟的支付单；日终与渠道账单核对 |
| 自动接单 / 超时提醒 | 每分钟 | 标 `overdue`，发通知 |
| 自动完成 | 每小时 | delivered / picked_up 超 24 小时 → completed |
| 退款执行与重试 | 每分钟 | 处理 `refunding`，失败退避，5 次后 `refund_failed` |
| 发件箱 worker | 常驻 | §6.6 |
| 积分 / 券 / 会员过期 | 每日 02:00 | FIFO 过期、券过期、会员到期提醒（提前 7 天）与过期 |
| 估清恢复 | 每日 00:05 | `sold_out_today` 清零 |
| 排队过号 | 每 30 秒 | `called` 超 5 分钟 → missed |
| 库存预警 | 每 10 分钟 | 低于阈值 → `stock.low` 事件 |
| 幂等记录清理 | 每小时 | 删除 24 小时前记录 |
| 导出任务 | 常驻 worker | 生成文件 → 对象存储 → 通知 |
| 知识库重建索引 | 事件触发 | 文档保存后 |
| 报表物化 | 每 5 分钟 / 日终 | 驾驶舱汇总表、日报 |

---

## 11 缓存

| 数据 | Key | TTL | 失效 |
|---|---|---|---|
| 门店信息与营业状态 | `store:{id}` | 5 分钟 | 后台保存时删除 |
| 分类与商品列表 | `catalog:{store_id}:{category}:{v}` | 5 分钟 | `catalog_version` 自增（商品 / 套餐 / 估清变更） |
| 商品详情 | `product:{id}:{version}` | 10 分钟 | 版本号 |
| 库存展示 | `stock:{sku_id}` | 30 秒 | 扣减时更新 |
| 逆地理 | `geo:{sha1}` | 7 天 | — |
| 用户会话 / 刷新版本 | `session:{user_id}` | 跟随令牌 | 登出 / 刷新 |
| 限流桶 | `rl:{scope}:{key}` | 1 分钟 | — |
| 驾驶舱汇总 | `dash:{store_id}:{date}` | 30 秒 | 定时物化 |

不缓存：订单、支付、余额、积分、排号、售后。缓存穿透用空值 60 秒；雪崩用 TTL 抖动 ±10%；热点用单飞（single-flight）。

---

## 12 安全

基线见 `SECURITY.md`。补充：
- 限流统一（网关或框架）：按 IP 20 次/秒（突发 40）；按用户：写 3 次/秒、读 20 次/秒；登录按设备 5 次/分钟、短信按手机号 1 次/分钟且 10 次/天；智能体按用户 60 次/分钟。
- 服务间签名：`X-Service`、`X-Timestamp`、`X-Nonce`、`X-Signature = HMAC_SHA256(secret, method+path+timestamp+nonce+sha256(body))`；时间偏差 ≤ 5 分钟；nonce 10 分钟去重。
- PII：手机号 / 身份证加密列 + 哈希列（用于查询）；日志脱敏；导出水印与审计。
- 文件上传：预签名直传；类型白名单 jpg / png / webp；≤ 5MB；服务端二次校验魔数；图片剥离 EXIF。
- 回调：验签 + 金额核对 + 幂等（`channel_txn_id` 唯一）；回调处理 ≤ 5 秒，重活放任务。
- 管理后台：密码 argon2id；MFA；会话绑定 UA 指纹；敏感操作重新验证。

---

## 13 可观测性

- 日志：JSON，字段 `ts, level, trace_id, request_id, user_id?, store_id?, module, event, duration_ms, code, degraded?, msg`；不记录正文中的手机号、地址、令牌、支付参数。
- Trace：OpenTelemetry，HTTP / DB / Redis / 外部调用自动埋点；`X-Trace-Id` 贯穿网关、后端、任务、智能体。
- 指标（Prometheus）：请求 QPS / 延迟 p50 p95 p99 / 错误率（按路由）；外部依赖延迟与失败率；熔断状态；发件箱积压与失败数；任务运行时长与失败；库存扣减失败率；支付成功率；退款失败数；队列等待时长。
- 告警：5xx 率 > 1% / 5 分钟；p95 > 1s；熔断打开；发件箱积压 > 1000 或失败 > 10；支付回调失败；退款失败；任务超时；从库延迟 > 60s。
- 健康：`/healthz`（进程存活）、`/readyz`（DB / Redis / 迁移版本）、`/metrics`。

---

## 14 配置与部署

### 14.1 环境矩阵

| 配置 | dev | staging | prod |
|---|---|---|---|
| `LUZ_ENV` | dev | staging | prod |
| `LUZ_DATABASE_URL` | sqlite | postgres | postgres（主从） |
| `LUZ_REDIS_URL` | 可空（内存） | redis | redis 集群 |
| `LUZ_JWT_SECRET` / `LUZ_JWT_PRIVATE_KEY` | 默认值 | 密钥管理 | 密钥管理（RS256） |
| `WECHAT_APPID / SECRET / MCH_ID / PAY_*` | 沙箱 | 沙箱 | 正式 |
| `MAP_PROVIDER / MAP_KEY` | mock | 真实 | 真实 |
| `SMS_*` / `STORAGE_*` | mock | 真实 | 真实 |
| `AGENT_SERVICE_SECRET` | dev | 密钥 | 密钥 |
| `LOG_LEVEL` | debug | info | info |
| `FEATURE_*`（模块开关） | 全开 | 按项目 | 按项目 |

模块级配置（如 `ORDER_PAY_TIMEOUT_MINUTES`、`ADDRESS_MAX_PER_USER`）必须由 `config.py` 读取并注入，不在代码里写常量；`settings` 模块的业务参数（后台可改）优先于环境变量。

### 14.2 部署

- `docker-compose.yml`（开发 / 预发）：`api`、`worker`（发件箱 + 导出）、`scheduler`、`agent`、`postgres`（含 pgvector）、`redis`、`minio`。
- 生产：K8s Deployment × 3 角色（api / worker / scheduler），HPA 按 CPU 与队列积压；滚动发布；迁移作为 Job 先于发布运行（"先加列、后写数据、再删列"三步）。
- 启动顺序：迁移 → api（`/readyz` 通过）→ worker / scheduler。
- 种子数据：`scripts/seed_dev.py`（门店、分类、商品、规格、套餐、券、员工账号、桌台），幂等可重复执行。

---

## 15 测试策略

| 层 | 要求 |
|---|---|
| 单元 | service 函数；每个状态流转的合法与非法用例；金额计算（含券、积分、会员价、配送费） |
| 并发 | 库存 50 线程、券库存、积分抵扣、排队取号、幂等重放；断言无超卖、无重复 |
| API | 每个接口：成功、4001、4010、4040（他人资源）、4090、幂等重放；分页与筛选 |
| 回调 | 验签失败、金额不符、重复回调、已取消订单的回调 |
| 任务 | 超时关闭、对账、退款重试、过期任务的幂等 |
| 合同 | OpenAPI 导出与 `API.md` 对比脚本（`scripts/check_api_catalog.py`，待补） |
| 端到端 | 登录 → 加购 → 报价 → 下单 → 支付回调 → 接单 → 出餐 → 完成 → 评价；拒单退款；售后部分退款；排队全流程 |
| 混沌（预发） | 杀 Redis、延迟支付回调、地图超时，验证降级矩阵 |
| 负载 | 下单接口 200 rps p95 < 500ms（单实例参考值） |

运行：`cd backend && python -m pytest -q`；新模块合入前必须带测试。

---

## 16 智能体接入

- 只有 `API.md` 标注 `agent: yes` 且状态为"已实现"的接口可被工具调用；标注时同时写明风险等级（read / low_write / high）。
- 必须提供 `POST /api/v1/app/orders/preview`（报价预览，只读）供确认摘要使用；写接口接受智能体服务签名（§5）。
- 高风险操作由智能体服务在用户确认后执行，幂等键由智能体生成（UUID v5）；后端不区分调用方，只认令牌与幂等键。本条修订旧 §9"由前端执行"的说法。
- 后台工具按员工令牌权限码校验；admin 接口的 `agent: yes` 只对 `X-Client: admin` 的会话有效。
- 知识库文档表与索引由 `agent-support` 模块提供后台 CRUD，向量与检索在智能体服务内。

---

## 17 生成顺序（与根目录 WORKFLOW.md 对齐）

每一阶段结束的判定：对应测试通过、`API.md` 状态更新、`requirements/<项目名>.md` 打勾。

| 阶段 | 内容 | 产物 |
|---|---|---|
| P0 骨架 | 复制 `00-framework/`，配置环境矩阵，Alembic 初始化，`docker-compose`，`/healthz /readyz`，全局异常处理器，幂等作用域修正，发件箱 worker 入口 | 可启动的空应用 + 迁移 |
| P1 身份与门店 | `auth-login`（登录 / 刷新 / 登出 / 手机绑定 / 资料 / 注销）、`store`、`settings` 最小集 | 登录可用、门店可配 |
| P2 商品与购物车 | `catalog`（分类 / 商品 / 规格 / 套餐 / 估清 / 搜索）、`inventory`、`cart`、`media` | 可浏览与加购 |
| P3 地址与定位 | `address`（含 PUT）、`map-location` 接口 | 可选址、判范围 |
| P4 交易主链 | `order`（预览 / 下单 / 列表 / 详情 / 取消）、`payment`（下单 / 回调 / 查询 / 超时）、定时任务（超时、对账） | 端到端可付款 |
| P5 履约与售后 | 订单状态机全量（接单 → 完成）、核销、堂食结账、`after-sales`、退款、`notification` 最小集 | 商家可处理订单 |
| P6 会员与营销 | `membership`（等级 / 积分 / 开通）、`recharge`、`marketing`（券 / 活动 / 冲突） | 营销闭环 |
| P7 行业能力 | `queue`（+ 桌台）、`delivery-dispatch`、`review` 完整 | 排队 / 配送 / 评价 |
| P8 后台 | `admin-rbac`（登录 / MFA / 权限 / 审计）、所有 admin 接口、`report` 与导出 | 后台可用 |
| P9 智能体支持 | `agent-support`（预览、知识库表、服务签名）、`agent: yes` 标注 | 智能体可接 |
| P10 加固 | 弹性参数落地、观测、限流、混沌与负载测试、SECURITY §10 待办 | 上线 |

---

## 18 项目 BACKEND.md 的结构

`requirements/<项目名>.md` 固定章节（示例 `requirements/xiangcai-restaurant.md`）：

1. 项目概述与规模假设（日订单量、门店数、是否 SaaS）
2. 模块开关矩阵（§2 全集：开 / 关 / 阶段）
3. 接口清单（按阶段，引用 `API.md`，标状态）
4. 业务参数（营业、费用、积分、会员、券、排队、配送）
5. 状态机裁剪（§8 中启用的状态与动作）
6. 外部依赖与密钥清单（支付 / 地图 / 短信 / 存储 / 模型）
7. 环境矩阵与部署形态
8. 数据迁移与种子
9. 待确认项
10. 验收清单（复制 §19）

---

## 19 验收清单

- [ ] 全局异常处理器存在；未知异常返回 5000 + trace_id
- [ ] 所有写接口要求幂等键；作用域含用户；不同请求体复用键返回 4093；僵尸记录可接管（有测试）
- [ ] 库存 / 券 / 积分 / 余额扣减为条件 UPDATE（并发测试通过）
- [ ] 状态流转为条件 UPDATE；非法流转 4090（每条边有测试）
- [ ] 下单事务包含报价、扣减、写单、发件箱；失败全回滚
- [ ] 支付回调验签、金额核对、重复回调幂等、已取消订单的回调不丢
- [ ] 发件箱 worker 用 SKIP LOCKED，退避与失败告警可用
- [ ] 分页、筛选、排序符合 §3.4；他人资源 4040
- [ ] 软删除对象使用部分唯一索引；订单 / 流水 / 审计不可删
- [ ] 弹性参数按 §9 落地；熔断可触发并恢复；降级写日志与指标
- [ ] 定时任务有锁、幂等、运行记录；超时关闭与对账可用
- [ ] 日志脱敏；trace_id 贯穿；`/readyz` 与 `/metrics` 可用
- [ ] Alembic 迁移可从零建库；种子脚本幂等
- [ ] `API.md` 状态与代码一致（对比脚本）；`agent: yes` 只标已实现接口
- [ ] 端到端测试覆盖 §15 列出的主流程
