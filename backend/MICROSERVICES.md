# 微服务拆分与运行规范 v2

代码按编号模块组织（每个模块独立的表、服务函数与路由），当前形态是**模块化单体**。本文规定何时拆、拆成哪些服务、服务间怎样通信、怎样部署。弹性参数、降级矩阵、观测与测试策略只在 `SPEC.md` §9、§13、§15 定义，本文不重复数值。

## 1 阶段

| 阶段 | 形态 | 适用 | 判断依据 |
|---|---|---|---|
| 一期 | 模块化单体（api / worker / scheduler 三种进程共用一份代码） | 单店或小连锁，日订单 < 5000 | 团队小、事务简单、部署成本低 |
| 二期 | 拆出 1–3 个独立进程：支付回调与退款、通知与打印、地图代理、智能体 | 多店，日订单 5000–5 万 | 独立伸缩或发布需求 |
| 三期 | 按 §2 领域拆分 | 连锁，日订单 > 5 万 | 多团队并行、发布频繁 |

拆分前提：模块只通过 `CONTRACTS.md` 列出的函数或事件通信，不读对方的表。

## 2 服务划分

| 服务 | 模块 | 拥有的表（只有它能写） | 对外前缀 |
|---|---|---|---|
| identity | 01-identity、05-address | users、sms_codes、addresses | `/app/auth/*`、`/app/me*`、`/app/addresses*` |
| store | 02-store、06-geo、23-app-settings | stores、store_hours、store_holidays、geo_cache、app_settings | `/app/stores*`、`/app/geo/*`、`/app/config` |
| catalog | 03-catalog、07-inventory、04-cart、21-media | categories、products、spec_*、set_meal_*、skus、stock_logs、carts、cart_items、media_assets | `/app/products*`、`/app/cart*`、`/app/media/*` |
| order | 08-order、10-after-sales | orders、order_items、order_status_logs、order_quotes、order_counters、after_sales* | `/app/orders*`、`/app/after-sales*` |
| payment | 09-wechat-pay | payment_records、refund_records、pay_notify_logs | `/app/payments*`、`/webhooks/wechat/*` |
| fulfillment | 11-delivery、12-queue、13-reservation | delivery_*、riders、queue_*、tables、reservation_* | `/app/queue*`、`/app/reservations*`、`/staff/*`、`/rider/*` |
| growth | 14-membership、15-recharge、16-coupon、17-campaign、18-lottery | member_*、points_*、stored_*、recharge_*、coupon_*、user_coupons、campaign_*、lottery_* | `/app/member*`、`/app/points*`、`/app/recharge*`、`/app/coupon*`、`/app/campaigns*`、`/app/lottery*` |
| review | 19-review | reviews、review_summaries | `/app/*/reviews*` |
| notification | 20-notification | notification_*、subscribe_consents、sms_counters | 事件消费 |
| admin | 22-admin-rbac、24-report、各模块的 `/admin` 路由 | admin_*、audit_logs、store_daily_stats、export_jobs | `/admin/*` |
| jobs | 25-jobs | scheduler_runs、outbox_events（投递） | 无 |
| agent | `agent/`（独立仓库目录） | 会话、检查点、长期记忆、向量（不写业务表） | `/agent/*` |
| agent-support | 26-agent-support | kb_docs、kb_chunks、kb_eval_cases | `/admin/kb/*`、`/admin/agent/*` |

规则：每张表只有一个服务能写；跨服务读写通过接口或事件。

## 3 通信

| 类型 | 用途 | 技术 | 规则 |
|---|---|---|---|
| 同步调用 | 查询、需要即时结果的命令 | 单体内为函数调用；拆分后 HTTP/JSON（内网）带 `X-Service` 签名 | 超时与重试按 `SPEC.md` §9；写操作带幂等键 |
| 事件 | 状态变化通知 | 发件箱（`luzback.outbox`）→ `luzback.mq`（内存 / Redis Streams）→ 消费者 | 生产与业务同事务；消费端 `mark_processed` 去重；主题见 `CONTRACTS.md` |
| 定时任务 | 超时、对账、过期、物化 | `luzback.scheduler` + 分布式锁；K8s CronJob 可替代 | 任务幂等；清单见 `SPEC.md` §10 |

事件信封：`event_id, topic, aggregate_type, aggregate_id, version, occurred_at, payload`。

## 4 Saga：下单到支付

单体阶段步骤 2–7 在一个数据库事务内完成（本地事务是 Saga 的退化形式）；拆分后按下表编排，每步补偿幂等。

| 步骤 | 动作 | 失败补偿 |
|---|---|---|
| 1 | 报价校验（catalog、store、geo、campaign、coupon、membership） | 返回错误 |
| 2 | 标记 quote 已用（条件 UPDATE） | — |
| 3 | 扣库存（inventory，按 sku 升序） | 恢复 quote |
| 4 | 锁券（coupon） | 回补库存 |
| 5 | 扣积分（membership） | 退回券、回补库存 |
| 6 | 写订单与明细、参与记录（order、campaign） | 退积分、退券、回补库存 |
| 7 | 发件箱 `order.created` | 同上 |
| 8 | 支付（payment）→ 回调 → `order.paid` | 超时：先查支付再关单，回补 / 退回全部 |

## 5 服务内部结构

```
service/
  api/        路由与请求模型
  service/    业务规则与事务边界
  models/     ORM
  rules/      纯函数（计价、概率、状态表）
  jobs/       定时任务
  clients/    调用其他服务（resilient 包装）
  tests/
```

与本仓库 `NN-name/code/<pkg>/` 的文件一一对应（`MODULE-TEMPLATE.md`）。

## 6 部署

| 项 | 规定 |
|---|---|
| 进程 | `api`（FastAPI）、`worker`（发件箱投递 + 消费者）、`scheduler`（定时任务）、`agent`（智能体）；同一镜像不同入口 |
| 开发 / 预发 | `docker-compose.yml`：api、worker、scheduler、agent、postgres（pgvector）、redis、minio |
| 生产 | K8s Deployment × 4，HPA 按 CPU 与队列积压；库存与支付所在服务最小副本 2；非 root、只读根文件系统 |
| 发布 | 迁移 Job 先于发布（先加列、后写数据、再删列）；滚动发布；灰度 5% → 25% → 100%；失败自动回滚 |
| 配置 | 环境变量 + 密钥服务；`Settings.assert_production_safe()` 启动即检查（`SPEC.md` §14.1） |

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
| `backend/API.md`、`ROUTING.md`、`CONTRACTS.md` | 网关路由、接口契约、模块契约 |
| `agent/` | 智能体服务，只通过 `agent: yes` 接口与服务间签名访问业务服务 |
