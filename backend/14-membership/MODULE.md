# 模块：14-membership（会员与积分）`membership`

## 0 官方来源

| 主题 | 来源 | 用到的规则 | 核对日期 |
|---|---|---|---|
| 积分规则明示 | 《规范促销行为暂行规定》（市场监管总局令第 32 号）https://www.gov.cn/zhengce/zhengceku/2020-11/06/content_5557730.htm（与 18-lottery §0 同一来源） | 积分兑换规则、范围、有效期须明示（抽奖积分奖品由 18 校验说明文字） | 2026-10-28 |
| 消费者权益 | 《消费者权益保护法实施条例》（国务院令第 778 号）https://www.gov.cn/zhengce/content/202403/content_6940158.htm（与 15-recharge §0 同一来源） | 权益规则提前告知，不得单方降低 | 2026-10-28 |

## 1 状态与依赖

| 项 | 内容 |
|---|---|
| 状态 | 标准：积分账户、流水台账、FIFO 过期、追回与欠分、赠送、对账、会员画像、订单 / 抽奖积分事件；会员等级配置与付费会员只有数据模型（§13 不包含） |
| 代码 | `14-membership/code/membership/`（models / service / rules / events / jobs / api），测试 `code/tests/test_membership.py` |
| 依赖 | 00-framework；01-identity（顾客令牌、注册时间）；可选 16-coupon（画像类型）、18-lottery（发奖回执） |
| 被依赖 | 16-coupon（`member_profile` 作为人群画像）；08-order（事件：完成返积分、退款追回）；18-lottery（积分奖品） |

## 2 职责与边界

| 负责 | 不负责 |
|---|---|
| 积分余额、流水、入账 / 扣减 / 冲回 / 追回 / 赠送 / 过期（幂等） | 积分商城商品（走订单） |
| 订单完成返积分、退款按比例追回 | 储值余额（15）、优惠券（16） |
| 会员画像（注册时间、等级、标签）给优惠券人群 | 等级自动升级与付费会员开通流程（模型已建，用例未做） |

## 3 数据模型

| 表 | 关键字段 | 约束 / 说明 |
|---|---|---|
| `points_accounts` | user_id, balance, debt, version | user_id 唯一；balance ≥ 0（条件 UPDATE） |
| `points_ledger` | user_id, delta, kind, ref_key, balance_after, remaining, expires_at, note | ref_key 唯一（幂等最后防线）；入账行 remaining = 尚未用掉的积分 |
| `points_mismatches` | user_id, balance, ledger_sum, checked_on | 每人每天一行 |
| `member_levels` / `members` / `membership_orders` | 等级阈值与倍率、会员等级、付费会员单 | `members.level` 用于画像与返积分倍率 |

不变量：balance == Σdelta == Σremaining（每日对账核对）。

## 4 业务规则

1. 入账 `credit/earn/gift_points`：先抵销欠分（`rules.split_debt`），余下进余额并成为一个批次（有效期 `[membership] points_valid_days`，默认 365 天；0 不过期）。
2. 扣减 `debit/spend`：条件 UPDATE `balance >= n`，不足 4220 `insufficient_points`；按 FIFO（先到期先用，不过期最后）扣批次 remaining。
3. 冲回 `revert_spend(spend_ref)`：按原扣减数量重新入账（订单取消返还抵扣积分）。
4. 追回 `clawback`：余额够就扣；不够扣到 0，差额记 `debt`，之后入账先抵销；余额永不为负。
5. 订单返积分（`order.completed`）：⌊(total_cents − 完成前已退) / 100⌋ × `points_per_yuan`（运行配置 `[membership]`）× 等级倍率 / 10000，ref = `order:{id}:earn`。
6. 退款追回（`order.refunded`）：应追回 = ⌈已返 × 累计退款 / 订单总额⌉（全额退 = 全部），减去已追回与已记欠分，ref = `order:{id}:clawback:{售后 ref}`；订单尚未返积分时只记 0 积分标记，完成时扣除。
7. 抽奖积分奖品（`lottery.prize_won`，prize_kind=points）：赠 quantity 积分，ref = delivery_key（重投 / 重发奖只入账一次），回执 `lottery.ack_delivery`（可导入时同事务）或 `lottery.delivery_result` 事件。
8. 过期任务：remaining > 0 且到期的批次清零（条件 UPDATE），写 expire 流水 ref = `expire:{批次}`。
9. 画像 `member_profile(db, user_id)`：registered_at = identity 用户注册时间（无则入会时间），level = members.level（默认 1），tags = 空集；会员不存在返回 None。

## 5 状态机与流程

无状态机；积分变动只追加流水。订单：completed → 返积分；refunded（可多次）→ 按累计比例追回。

## 6 接口

agent（`SPEC.md` §16）：`GET /api/v1/app/points` 为 `read`；本模块没有其他接口。

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET | `/api/v1/app/points` | 顾客令牌（identity） | 余额、欠分、最近 20 条流水 |

积分抵扣由订单调用 `spend(ref_key="order:{id}:redeem")`，不单独开接口。

## 7 事件

发出：`points.changed`（user_id, delta, kind, ref, debt_offset? / debt_added?）、`points.expired`（user_id, points, batch_id）、`points.reconcile_mismatch`（user_id, balance, ledger_sum, remaining_sum）、`lottery.delivery_result`（draw_id, delivery_key, ok, ref, error；无 ack_delivery 时）。

消费（`membership/events.py` `SUBSCRIPTIONS`，幂等 `mark_processed` + ref_key，不提交）：

| 主题 | 用到的字段 |
|---|---|
| `order.completed` | order_id, user_id, total_cents |
| `order.refunded` | order_id, user_id, total_cents, refunded_cents, ref |
| `lottery.prize_won` | draw_id, member_id, prize_kind, quantity, delivery_key, prize_name |

## 8 并发与一致性

余额、欠分、批次 remaining 全部条件 UPDATE + rowcount；ref_key 唯一兜底并发重复；建户用保存点 + 唯一索引；20 线程抢 500 积分各扣 100 恰好 5 成功。

## 9 配置

运行参数写在 `backend/config/runtime/*.toml`，字段说明（类型、范围、能否热加载、是否下发客户端）在 `00-framework/code/luzback/runtime_config.py`；模块经 `luzback.module_config.ModuleConfig` 读取，热加载后下一次读取即生效。环境变量只放秘密与部署参数；迁移前的环境变量名仍设置着时，启动会打 `config.unknown_env` 警告并指出新键。测试用 `set_config(...)` 或 `luzback.testing.override_runtime(...)` 注入运行参数（写进一份测试用运行配置，测试结束撤销）。

### 9.1 环境变量（秘密与部署参数）

无：本模块没有秘密与部署参数。

### 9.2 运行配置键

| 键 | 默认 | 范围 | 热加载 | 下发客户端 | 说明（含单位） |
|---|---|---|---|---|---|
| `[membership] points_per_yuan` | 1 | 0–100 | 是 | 否 | 订单完成后每 1 元返多少积分（再乘等级倍率）；商户要在后台自己改时改由会员模块的后台设置提供（原 `POINTS_PER_YUAN`） |
| `[membership] points_valid_days` | 365 | 0–3650 | 是 | 否 | 积分入账批次有效期（天，0 = 不过期）（原 `POINTS_VALID_DAYS`） |

积分抵扣的兑换比例与单笔上限不在本模块（原 Settings 里的 `POINTS_REDEEM_RATE`、`POINTS_REDEEM_CAP_BP` 没有代码读取，已删除）；订单抵扣接入时在计价模块登记。

## 10 错误码

4001 数量非正 / 参数错；4040 冲回的扣减不存在；4090 并发冲突（重试）；4093 同 ref 正在处理；4220 积分不足。

## 11 测试清单

入账 / 扣减 / 幂等 / 台账一致、FIFO 过期与重跑、追回欠分与抵销、赠送与冲回、对账、画像（coupon.MemberProfile）、订单返积分与按比例追回（含完成前退款）、抽奖积分奖品（重发去重、两种回执、无效数量）、20 线程并发扣减。

## 12 前端对接

我的 → 积分：`GET /points`（余额 + 流水）；确认订单积分抵扣以服务端计算为准。

## 13 定制点

- 配置：§9；等级倍率 `member_levels.points_multiplier_bp`（全局等级，store_id 为空）。
- 扩展点：`gift_points` 供营销 / 生日礼；`member_profile` 被 16-coupon 用作默认画像；`credit / debit` 自定义 kind（签到等）。
- 不包含：等级自动升降与付费会员开通 / 到期（模型已建）、积分商城、积分到期前提醒、会员标签模型（画像 tags 为空集）、后台积分调整接口。
