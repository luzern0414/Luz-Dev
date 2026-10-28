# 模块：order（订单）

| 项 | 内容 |
|---|---|
| 状态 | 核心逻辑已实现（幂等创建、原子扣库存、状态机、取消回补、发件箱），测试 6 项通过；履约与售后为设计 |
| 代码 | `order/code/order/`、测试 `order/code/tests/` |
| 依赖 | framework、inventory-oversell（扣减与回补）、payment（回调流转）、marketing-coupon / membership（金额优惠，设计） |

## 1 职责与边界

| 负责 | 不负责 |
|---|---|
| 下单：校验商品、计价、扣库存、写订单与明细 | 支付渠道细节（payment） |
| 订单状态机与流转 | 配送路线与骑手（delivery-dispatch） |
| 取消未支付订单并回补库存 | 优惠券核销细节（marketing-coupon，经接口调用） |
| 订单事件（created / paid / cancelled）写入发件箱 | 消息发送（notification） |
| 订单查询（归属校验） | 评价（review，只读订单状态） |

## 2 数据模型

| 表 | 字段 | 约束 | 说明 |
|---|---|---|---|
| `orders` | id, order_no, user_id, status, total_cents, created_at, paid_at | `order_no` 唯一；`user_id`、`status` 索引 | 金额分为单位 |
| `order_items` | id, order_id, sku_id, qty, unit_price_cents | `order_id` 索引 | 下单时价格快照，后续改价不影响订单 |

设计扩展字段（按需加）：`address_snapshot`（下单时地址快照 JSON）、`coupon_discount_cents`、`points_discount_cents`、`pay_deadline`、`delivery_fee_cents`、`remark`。

## 3 接口

| 方法 | 路径 | 鉴权 | 幂等 | agent | 说明 |
|---|---|---|---|---|---|
| POST | `/api/v1/app/orders` | access | 必须 | yes（草稿，需确认） | body: `{items:[{sku_id,qty}], address_id?, coupon_id?, remark?}`；返回订单号与应付金额 |
| GET | `/api/v1/app/orders` | access | — | yes | 分页列表，`status` 可筛选；只返回本人订单 |
| GET | `/api/v1/app/orders/{id}` | access（归属） | — | yes | 详情（含明细与状态时间线） |
| POST | `/api/v1/app/orders/{id}/cancel` | access（归属） | 必须 | yes（需确认） | 仅 pending_payment 可取消 |
| POST | `/api/v1/app/orders/{id}/confirm-receipt` | access（归属） | 必须 | yes（低风险） | delivered / picked_up → completed（设计） |
| POST | `/api/v1/app/orders/preview` | access | — | yes（只读） | 报价预览：商品、券、积分、会员价、配送费、时段可用性；返回 `quote_token`（10 分钟），下单必带（待建） |
| POST | `/api/v1/app/orders/{id}/urge` · `/address` · `/add-items` · `/checkout` | access（归属） | 必须 | 低 / 高 / 低 / 高 | 催单、改地址（未出餐）、堂食加菜、堂食结账（待建） |
| GET | `/api/v1/app/orders/{id}/pickup-code` | access（归属） | — | yes（只读） | 取餐码（待建） |
| GET | `/api/v1/admin/orders` · `/{id}` · `/board` | admin `order:read` | — | yes（admin 会话） | 列表（门店范围）、详情、工作台看板（设计 / 待建） |
| POST | `/api/v1/admin/orders/{id}/accept` · `/reject` · `/prepare` · `/ready` · `/ship` · `/complete` · `/cancel` · `/note` · `/verify-pickup` | admin 对应 `order:*` | 必须 | no | 各状态动作（待建）；`ship` 等价 v1 的发货 |
| POST | `/api/v1/admin/orders/batch/print` | admin `order:print` | — | no | 批量打印（待建） |

## 4 状态机（v2，完整定义见 `backend/SPEC.md` §8）

当前代码只实现 v1 的 `pending_payment → paid | cancelled`、`paid → shipped → completed`、`paid → refunded`；v2 新增接单、备餐、出餐、三种履约分支与售后流转，实现时按下表扩展（旧状态 `shipped` 等价于 v2 的 `delivering`）。

| 从 | 到 | 触发 | 副作用 | 状态 |
|---|---|---|---|---|
| pending_payment | paid | 支付回调 | `paid_at`；事件 `order.paid`；新单通知 | 已实现 |
| pending_payment | cancelled | 用户取消 / 超时 | 回补库存、退券、退积分；事件 `order.cancelled` | 已实现（退券 / 退积分设计） |
| paid | accepted | 商家接单 / 自动接单 | `accepted_at`；事件 `order.accepted` | 设计 |
| paid | refunding | 商家拒单（原因） | 自动创建全额售后单；回补库存 | 设计 |
| accepted | preparing | 开始备餐 | 事件 `order.preparing` | 设计 |
| preparing | ready | 出餐完成 | `ready_at`；外卖建配送任务 / 自提发取餐码 / 堂食转 `served` | 设计 |
| ready | delivering | 骑手取餐（外卖） | 事件 `order.delivering` | 设计（= 旧 `shipped`） |
| delivering | delivered | 骑手送达 | `delivered_at`；事件 `order.delivered` | 设计 |
| ready | awaiting_pickup → picked_up | 自提出餐 → 核销取餐码 | 通知；核销写 `completed_at` 前置 | 设计 |
| ready | served | 堂食上菜 | 允许加菜子订单 | 设计 |
| served | completed | 堂食结账 | 积分入账；事件 `order.completed` | 设计 |
| delivered / picked_up | completed | 用户确认 / 24 小时自动 | 积分入账；开放评价 | 设计 |
| 任意已付款状态 | refunding | 售后通过 | 冻结后续动作 | 设计 |
| refunding | refunded / refund_failed | 退款回调 | 成功：回补库存（未出餐）、积分追回；失败：人工 | 设计 |

前端状态词映射：`pending_payment 待付款`、`paid 已下单`、`accepted 已接单`、`preparing 备餐中（花店：已备花）`、`ready 待取餐 / 待配送`、`delivering 配送中（花店可显示"已发货"）`、`delivered 已送达`、`awaiting_pickup 待取餐`、`picked_up 已取餐`、`served 用餐中`、`completed 已完成`、`cancelled 已取消`、`refunding 退款中`、`refunded 已退款`、`refund_failed 退款处理中（用户侧）`。

非法流转统一返回 4090。所有流转为 `UPDATE ... WHERE status = 旧状态`，并检查 rowcount；每次流转写 `order_status_logs(order_id, from, to, actor, reason, created_at)`。超时标记（待接单 > 3 分钟等）写 `sub_status = overdue`，不改主状态。

## 5 关键流程

**下单**
1. 幂等：`Idempotency-Key` 作为 `order.create` 的 scope 与 key。
2. 合并同一 SKU 的数量，按 `sku_id` 升序处理（避免死锁）。
3. 逐个校验商品存在且在售，然后调用 `inventory.deduct`（条件 UPDATE）。
4. 计价：使用数据库中的当前价格写入明细快照。
5. 写 `orders` 与 `order_items`，写 `order.created` 事件。
6. 同一事务提交。任一步失败整体回滚，库存不被扣减。

**取消未支付订单**
1. 状态流转 pending_payment → cancelled（条件 UPDATE）。
2. 回补每个明细的库存。
3. 写 `order.cancelled` 事件。
4. 同一事务提交。若支付回调与取消同时到达，二者中只有一个条件 UPDATE 成功，另一方得到 4090；若支付先成功，取消返回 4090，前端应改为展示支付结果。

**超时关闭（定时任务，设计）**
- 每分钟扫描 `status = pending_payment AND created_at < now - 15min`，逐单调用取消流程。

## 6 并发与一致性

| 场景 | 机制 |
|---|---|
| 重复下单 | 幂等键 |
| 多 SKU 库存部分不足 | 同一事务，整单回滚 |
| 支付与取消竞争 | 状态条件更新，唯一赢家 |
| 重复回补库存 | 只在 pending → cancelled 成功的那一次回补 |
| 下单后商品改价 | 明细保存下单价格快照 |

## 7 配置

| 变量 | 默认 | 说明 |
|---|---|---|
| `ORDER_PAY_TIMEOUT_MINUTES` | 15 | 待支付超时 |
| `ORDER_AUTO_CONFIRM_DAYS` | 7 | 发货后自动确认收货 |

## 8 错误码

| 场景 | code | HTTP |
|---|---|---|
| 空订单、数量非正 | 4001 | 400 |
| 商品不存在或已下架 | 4040 | 404 |
| 库存不足 | 4091 | 409 |
| 非法状态流转 | 4090 | 409 |
| 订单不属于当前用户 | 4040 | 404（不泄露存在性） |
| 起送价、配送范围不满足（设计） | 4220 | 422 |

## 9 测试用例（已实现）

| 用例 | 断言 |
|---|---|
| 创建订单 | 扣减库存、金额正确 |
| 幂等重复创建 | 结果一致，只扣一次，只有一条订单 |
| 第二个 SKU 不足 | 整单回滚，第一个 SKU 库存不变 |
| 写入事件 | outbox 含 order.created |
| 支付后取消 | 4090 |
| 取消未支付订单 | 库存回补，状态 cancelled |
| 重复支付流转 | 4090 |

## 10 可复用代码

`order/code/order/__init__.py`：`Order`、`OrderItem`、`create_order`、`transition`、`mark_paid`、`cancel_order`、状态常量。

## 11 前端对接

| 页面 | 接口 | 行为 |
|---|---|---|
| 确认订单 → 提交 | `POST /app/orders` | 生成 Idempotency-Key（每次点击生成一次，失败重试沿用同一个）；成功进入收银台 |
| 订单列表 | `GET /app/orders` | 按状态分组页签 |
| 订单详情 | `GET /app/orders/{id}` | 进度条、金额明细、订单号 |
| 订单详情 → 取消 | `POST /app/orders/{id}/cancel` | 取消原因弹层 → 确认 |
| 收银台 | 见 payment | 支付成功后以服务端状态为准 |

## 超时关单任务（已实现）

`order/jobs.py` 的 `close_expired_orders`：先查支付 → 已付按支付成功处理、查询失败或未知跳过、未付先关渠道单再取消并回补库存；每单独立提交、可重复执行。测试 `test_order_jobs.py` 5 项。见 `references/patterns/order.md` §3、规则 ORDER-004、JOB-001。
