# 模块：recharge（储值充值）

| 项 | 内容 |
|---|---|
| 状态 | 钱包核心已实现：本金 / 赠送分账、不可变流水、充值 / 消费 / 冲回 / 退卡幂等、条件扣减；测试 5 项（`15-recharge/code/wallet/`）。充值档位、接口与对接支付待实现；上线前需财务与合规评审（MBR-002、MBR-003） |
| 依赖 | framework、payment（充值支付）、order（储值抵扣，设计）、notification |

## 1 职责与边界

| 负责 | 不负责 |
|---|---|
| 充值档位（充值额、赠送额） | 支付渠道（payment） |
| 储值账户余额与流水 | 发票 |
| 充值与赠送到账（幂等） | 提现（默认不开放，见 §4） |
| 储值抵扣下单（经订单接口） | — |

## 2 数据模型（设计）

| 表 | 字段 | 约束 | 说明 |
|---|---|---|---|
| `recharge_plans` | id, pay_cents, gift_cents, status, sort | — | 档位 |
| `stored_accounts` | user_id(PK), balance_cents, frozen_cents | `balance_cents >= 0` | 余额 |
| `stored_ledger` | id, user_id, delta_cents, reason, ref_key(唯一), created_at | — | 台账，充值 / 赠送 / 消费 / 退款 |
| `recharge_orders` | id, user_id, plan_id, pay_cents, gift_cents, status, out_trade_no(唯一) | — | 充值单，状态同支付单 |

## 3 流程

1. 用户选择档位，创建充值单（幂等），调用 payment 下单。
2. 支付成功回调 → 充值单 `pending → success`（条件 UPDATE）。
3. 同一事务写入台账：充值额与赠送额两条流水（ref_key 分别为 `recharge:{id}:pay`、`recharge:{id}:gift`）并更新余额。
4. 发送到账通知。重复回调返回已处理，不重复入账。

## 4 规则

| 规则 | 说明 |
|---|---|
| 余额不可提现 | 默认不提供提现接口 |
| 有效期 | 充值额 1 年；赠送额按档位声明（默认与充值额相同） |
| 抵扣 | 下单时可选储值抵扣，不足部分走支付；抵扣与订单同一事务 |
| 退款 | 未使用的充值额可按商家政策原路退回（需人工审核，设计） |
| 对账 | 每日：余额必须等于台账 SUM(delta_cents)；充值单与支付单逐笔一致 |

## 5 接口

| 方法 | 路径 | 鉴权 | 幂等 | agent | 说明 |
|---|---|---|---|---|---|
| GET | `/api/v1/app/recharge/plans` | access | — | yes | 档位列表 |
| POST | `/api/v1/app/recharge/orders` | access | 必须 | yes（需确认） | 创建充值单，返回支付参数 |
| GET | `/api/v1/app/recharge/account` | access | — | yes | 余额与近期流水 |

## 6 并发与一致性

余额变动只通过台账 + 条件 UPDATE；充值回调与重复回调通过充值单状态条件更新保证只入账一次。

## 7 错误码

| 场景 | code |
|---|---|
| 档位不存在或已下架 | 4040 |
| 金额超出上限（默认 5000 元） | 4220 |

## 8 测试计划（实现时补）

重复回调只入账一次、余额与台账一致、抵扣并发不超额、退款金额不超过充值额。

## 9 前端对接

| 页面 | 接口 | 行为 |
|---|---|---|
| 储值充值页 | plans、account | 档位网格、自定义金额、到账金额预览（服务端计算为准） |
| 充值成功 | — | 复用支付成功页结构 |
