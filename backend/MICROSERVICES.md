# 微服务拆分与运行规范

代码按编号模块组织（每个模块独立的表、服务函数与路由），当前形态是**模块化单体**。本文规定何时拆、拆成哪些服务、服务间怎样通信、怎样部署。弹性参数、降级矩阵、观测与测试策略只在 `SPEC.md` §9、§13、§15 定义，本文不重复数值。

## 1 阶段

| 阶段 | 形态 | 适用 | 判断依据 |
|---|---|---|---|
| 一期 | 模块化单体（api / worker / scheduler 三种进程共用一份代码） | 单店或小连锁，日订单 < 5000 | 团队小、事务简单、部署成本低 |
| 二期 | 拆出 1–3 个独立进程：支付回调与退款、通知与打印、地图代理、智能体 | 多店，日订单 5000–5 万 | 独立伸缩或发布需求 |
| 三期 | 按 §2 领域拆分 | 连锁，日订单 > 5 万 | 多团队并行、发布频繁 |

拆分前提：模块只通过 `CONTRACTS.md` 列出的函数或事件通信，不读对方的表。

## 2 服务划分

拆分时每个服务拥有下表的模块与表（表名以各 MODULE.md §3 为准，`*` 表示同前缀的全部表；规划模块的表在实现时补）。

| 服务 | 模块 | 拥有的表（只有它能写） | 对外前缀 |
|---|---|---|---|
| identity | 01-identity、05-address、27-legal、28-wechat-mp | users、user_sessions、user_refresh_tokens、sms_codes、user_content_checks、addresses、legal_documents、user_consents、wechat_mp_* | `/app/auth/*`、`/app/me*`、`/app/addresses*`、`/webhooks/wechat-mp/*` |
| store | 02-store、06-geo、23-app-settings | stores、store_hours、store_holidays（规划：门店经营与展示设置）、config_loads、ui_texts、display_settings、content_revisions | `/app/stores*`、`/app/geo/*`、`/app/config`、`/app/content`、`/admin/content/*`、`/internal/*` |
| catalog | 03-catalog、07-inventory、04-cart、21-media | skus、stock_reservations、stock_ledger（规划：categories、products、set_meal_*、carts、media_assets） | `/app/products*`、`/app/cart*`、`/app/media/*` |
| order | 08-order、10-after-sales、17-campaign | orders、order_items、order_status_logs、order_quotes、order_notes、order_purchase_counters、after_sales、after_sale_lines、after_sale_logs | `/app/orders*`、`/app/after-sales*` |
| payment | 09-wechat-pay | payment_records、refund_records、pay_notify_logs、pay_recon_*、pay_shipping_uploads | `/app/payments*`、`/webhooks/wechat/*` |
| fulfillment | 11-delivery、12-queue、13-reservation | booking_slots、bookings（规划：delivery_*、riders、queue_*、tables） | `/app/queue*`、`/app/slots*`、`/staff/*`、`/rider/*` |
| growth | 14-membership、15-recharge、29-member-card、16-coupon、18-lottery | points_*、member_levels、membership_orders、wallet_*、member_card_*、coupon_*、user_coupons、lottery_* | `/app/points`、`/app/wallet*`、`/app/member-cards*`、`/app/coupon*`、`/app/lottery*` |
| review | 19-review | reviews | `/app/orders/{id}/review` |
| notification | 20-notification | notification_* | `/app/notifications/*`、事件消费 |
| admin | 22-admin-rbac、24-report、各模块的 `/admin` 路由 | admin_*、audit_logs（规划：store_daily_stats、export_jobs） | `/admin/*` |
| jobs | 25-jobs | scheduler_runs、outbox_events（投递）、processed_events | 无 |
| agent | `agent/`（独立服务） | 会话、检查点、长期记忆、向量（不写业务表） | `/agent/*` |
| agent-support | 26-agent-support（规划） | kb_docs、kb_chunks、kb_eval_cases | `/admin/kb/*`、`/admin/agent/*` |

规则：每张表只有一个服务能写；跨服务读写通过接口或事件。

## 3 通信

| 类型 | 用途 | 技术 | 规则 |
|---|---|---|---|
| 同步调用 | 查询、需要即时结果的命令 | 单体内为函数调用；拆分后 HTTP/JSON（内网）带 `X-Service` 签名 | 超时与重试按 `SPEC.md` §9；写操作带幂等键 |
| 事件 | 状态变化通知 | 发件箱（`luzback.outbox`）→ `luzback.mq`（内存 / Redis Streams）→ 消费者 | 生产与业务同事务；消费端 `mark_processed` 去重；主题见 `CONTRACTS.md` |
| 定时任务 | 超时、对账、过期、物化 | 各模块 `jobs.py` 的 `JOBS` 由 25-jobs scheduler 运行（`luzback.scheduler` + 分布式锁） | 任务幂等；清单在各模块 MODULE.md §9，`python -m jobs list` 列出 |

事件信封：`event_id, topic, aggregate_type, aggregate_id, version, occurred_at, payload, traceparent`（消费端接上生产者的链路）。

## 4 Saga：下单到支付

单体阶段步骤 2–6 在一个数据库事务内完成（本地事务是 Saga 的退化形式）；拆分后按下表编排，每步补偿幂等。

| 步骤 | 动作 | 失败补偿 |
|---|---|---|
| 1 | 报价校验（catalog、store、geo、campaign、coupon） | 返回错误 |
| 2 | 标记 quote 已用（条件 UPDATE） | — |
| 3 | 预占库存（inventory，按 sku 升序） | 恢复 quote |
| 4 | 锁券（coupon） | 回补库存 |
| 5 | 写订单与明细（order） | 退券、释放库存 |
| 6 | 发件箱 `order.created` | 同上 |
| 7 | 支付（payment）→ 回调 → `order.paid` | 超时：先查支付再关单，释放库存与券 |

## 5 服务内部结构

拆出的服务沿用本仓库模块的结构（`MODULE-TEMPLATE.md` §1）：每个模块包的 `api.py`、`service.py`、`models.py`、`rules.py`、`events.py`、`jobs.py`、`config.py` 原样迁入；原来经 `ports.py` 的进程内调用改为调用其他服务的客户端（`resilient` 包装、带服务签名与 `traceparent`）。

## 6 部署

| 项 | 规定 |
|---|---|
| 进程 | `api`（FastAPI）、`worker`（发件箱投递 + 消费者）、`scheduler`（定时任务）、`agent`（智能体）；同一镜像不同入口 |
| 一期（默认） | Docker Compose：nginx、api（2 副本）、worker、scheduler、postgres、redis（`references/operations.md` §2、`assets/deploy/`）；开发用 `backend/deploy/docker-compose.yml` |
| 二、三期 | 容器编排（如 K8s），HPA 按 CPU 与队列积压；库存与支付所在服务最小副本 2；非 root、只读根文件系统；运行配置目录整目录挂载才会热加载（子路径挂载或注入环境变量不会更新） |
| 发布 | 迁移先于发布（先加列、后写数据、再删列）；滚动发布；观察期内按阈值自动回滚（`assets/deploy/RUNBOOK.md`）；灰度写在运行配置的覆盖规则里 |
| 配置 | 秘密与部署参数在环境变量与密钥服务，运行参数在 `config/runtime/*.toml` 热加载（`SPEC.md` §14.1）；`Settings.assert_production_safe()` 启动即检查 |

## 7 一致性边界

| 场景 | 要求 | 实现 |
|---|---|---|
| 库存、券、积分、余额、名额、排号 | 强一致，不超卖不重复 | 条件 UPDATE + 唯一约束 |
| 订单与支付状态 | 强一致 | 同事务 + 条件更新；回调优先（`SPEC.md` §6.7） |
| 订单与通知、报表、评价汇总 | 最终一致 | 发件箱 + 消费幂等 |
| 商品展示（销量、价格） | 允许 5 分钟延迟 | 缓存 + 版本失效 |
| 报表 | 今日 5 分钟延迟，历史 T+1 定稿 | 物化表 |

## 8 与本仓库的对应

| 本仓库 | 服务中的位置 |
|---|---|
| `backend/00-framework/code/luzback` | 所有服务共用的基础库 |
| `backend/NN-name/code/<pkg>` | 对应服务的 service / models / api / rules / jobs |
| `backend/API.md`、`ROUTING.md`、`CONTRACTS.md`、各 MODULE.md §6 | 接口约定、网关路由、模块契约、接口表 |
| `agent/` | 智能体服务，只通过 MODULE.md §6 中 agent 列不为 `no` 的接口与服务间签名访问业务服务 |
