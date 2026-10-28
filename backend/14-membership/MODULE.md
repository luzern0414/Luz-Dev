# 模块：membership（会员与积分）

| 项 | 内容 |
|---|---|
| 状态 | 积分核心已实现（入账、扣减、流水、幂等），测试 4 项通过；会员等级与权益为设计 |
| 代码 | `membership/code/membership/`、测试 `membership/code/tests/` |
| 依赖 | framework、order（订单完成后入账，抵扣时扣减） |

## 1 职责与边界

| 负责 | 不负责 |
|---|---|
| 积分余额与流水台账 | 积分商城商品（设计，走订单） |
| 积分入账、抵扣（幂等） | 储值余额（recharge） |
| 会员等级判定（设计） | 优惠券发放（marketing-coupon） |
| 会员权益配置（设计） | 支付 |

## 2 数据模型

| 表 | 字段 | 约束 | 说明 |
|---|---|---|---|
| `user_points` | user_id(PK), balance | `balance >= 0`（条件 UPDATE 保证） | 余额快照 |
| `points_ledger` | id, user_id, delta, reason, ref_key, created_at | `ref_key` 唯一 | 台账；delta 正负均有 |
| `member_levels`（设计） | id, name, min_points, discount_rate, benefits_json | — | 等级门槛与权益 |
| `members`（设计） | user_id(PK), level_id, valid_until, joined_at | — | 会员状态；开通方式见 §5 |

## 3 接口

| 方法 | 路径 | 鉴权 | 幂等 | agent | 说明 |
|---|---|---|---|---|---|
| GET | `/api/v1/app/points` | access | — | yes | 余额与最近 20 条流水 |
| GET | `/api/v1/app/points/ledger` | access | — | no | 流水分页 |
| GET | `/api/v1/app/member` | access | — | yes | 会员状态、等级、升级进度（设计） |
| POST | `/api/v1/app/member/subscribe` | access | 必须 | yes（需确认） | 开通会员（设计，走支付） |

积分抵扣不单独开接口，由订单接口的 `points` 字段触发（下单事务内调用 `spend`，ref_key = `order:{order_id}:redeem`）。

## 4 关键规则

| 规则 | 说明 |
|---|---|
| 入账时机 | 订单 completed 后入账（不是支付后），避免退款后积分无法追回 |
| 入账 ref_key | `order:{order_id}:earn`，同一订单只入账一次 |
| 抵扣比例 | 100 积分抵 1 元，单笔订单抵扣不超过应付金额的 30%（设计参数） |
| 退款追回 | 订单退款时，按该订单入账积分全额扣回（ref_key = `order:{order_id}:clawback`）。余额不足时余额最低扣到 0，差额记入 `points_debt` 表（设计），下次入账时先抵销欠款。余额永远不为负数 |
| 积分有效期 | 12 个月，过期批次按 FIFO 清零（设计，需要批次表） |

## 5 会员开通（设计）

- 付费会员：走支付模块，订单类型 `member_subscription`，支付成功后写 `members.valid_until`（续期时顺延）。
- 免费会员：消费满额自动开通，由积分或消费事件触发，ref_key = `member:{user_id}:auto`。
- 会员到期：定时任务将过期会员标记为普通会员，权益即时失效，不回收已使用的权益。

## 6 并发与一致性

| 场景 | 机制 |
|---|---|
| 同一订单重复入账 | ref_key 唯一 + 先查后写；并发时唯一索引兜底 |
| 并发抵扣导致超额 | 条件 UPDATE `balance >= amount` |
| 余额与台账不一致 | 每日对账：`balance` 必须等于台账 SUM(delta)，差异告警 |

## 7 配置

| 变量 | 默认 | 说明 |
|---|---|---|
| `POINTS_PER_YUAN` | 1 | 每元获得积分（消费金额取分的整数部分） |
| `POINTS_REDEEM_RATE` | 100 | 每元需要的积分 |
| `POINTS_REDEEM_MAX_RATIO` | 0.3 | 单笔最高抵扣比例 |

## 8 错误码

| 场景 | code | HTTP |
|---|---|---|
| 积分不足 | 4220 | 422 |
| 数量非正 | 4001 | 400 |

## 9 测试用例（已实现）

| 用例 | 断言 |
|---|---|
| 入账后扣减 | 余额正确 |
| 扣减超过余额 | 4220，余额不变 |
| 同 ref_key 重复入账 | 只入账一次 |
| 台账与余额一致 | SUM(delta) = balance |

## 10 可复用代码

`membership/code/membership/__init__.py`：`UserPoints`、`PointsLedger`、`earn`、`spend`、`balance`。

## 11 前端对接

| 页面 | 接口 | 行为 |
|---|---|---|
| 我的 → 积分 | `/points` | 余额 + 流水 |
| 确认订单 → 积分抵扣开关 | 下单参数 `points` | 开关打开时显示“可抵扣 ¥X（Y 积分）”，以服务端计算为准 |
| 会员中心（设计） | `/member` | 等级卡、升级进度、权益列表 |
