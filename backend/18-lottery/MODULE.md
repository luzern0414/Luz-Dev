# 18-lottery 抽奖（包名 `lottery`）

## 0 官方来源

| 主题 | 官方页面 | 本模块用到的结论 | 核对日期 |
|---|---|---|---|
| 有奖销售公示与不得变更 | 规范促销行为暂行规定（市场监管总局令第 32 号）第十三条第一款 https://www.gov.cn/zhengce/zhengceku/2020-11/06/content_5557730.htm | 开奖前公布奖项种类、参与条件、开奖方式、奖品数量或中奖概率、开奖时间；不得变更、不得附加条件，有利于消费者的除外 → 发布即冻结（规则 R4–R6） | 2026-10-28 |
| 即时开奖公示 | 同上 第十三条第二款 | 现场即时开奖的，超过 500 元的奖项兑奖情况随时公示 → 公示接口（R27） | 2026-10-28 |
| 积分 / 礼券作奖品 | 同上 第十四条 | 须公布兑换规则、使用范围、有效期 → 券 / 积分奖必填 description（R10） | 2026-10-28 |
| 谎称有奖、内定 | 同上 第十五、十六条 | 不得虚构奖项、不按明示信息兑奖；不得让内部员工或指定人中奖 → 权重必须 > 0、钩子只能拒绝参与不能指定奖品、没有后台“指定中奖”接口（R9、R17） | 2026-10-28 |
| 最高奖 5 万元 | 同上 第十七、十八条；反不正当竞争法（2025 修订）第十一条第（四）项 https://www.cnipa.gov.cn/art/2026/5/20/art_104_206437.html | 抽奖式有奖销售最高奖 ≤ 5 万元；非现金奖品按同期市场价折算；违反处 5–50 万元罚款（第二十七条）→ value_cents ≤ 5_000_000，配置只能调低（R8） | 2026-10-28 |
| 资料保存 2 年 | 规范促销行为暂行规定 第十九条 | 设奖规则、公示信息、兑奖结果、获奖人员资料保存 2 年 → retention_days ≥ 730，到期前不删（R29） | 2026-10-28 |
| 促销规则公示 | 同上 第七条 | 公示规则、期限与不利限制条件 → 详情返回 disclosure（R7） | 2026-10-28 |
| 小程序诱导 | 微信小程序运营规范 §2.5.1、§2.5.21 https://developers.weixin.qq.com/miniprogram/product/ | 不得以抽奖机会利诱分享朋友圈或订阅消息 → 次数来源不含分享 / 订阅（R15） | 2026-10-28 |
| 个人信息 | 个人信息保护法 第五、六、五十一条 https://www.cac.gov.cn/2021-08/20/c_1631050028355286.htm | 最小必要、加密、去标识化 → 收货信息加密存储、后台默认脱敏、明文查看写审计、公示脱敏（R25、R27） | 2026-10-28 |
| 随机数 | Python `secrets` 文档 https://docs.python.org/3/library/secrets.html | `secrets` 使用操作系统 CSPRNG，适用于安全场景（`random` 不适用）→ 默认随机源（R16） | 2026-10-28 |
| 并发扣减 | PostgreSQL 事务隔离 https://www.postgresql.org/docs/current/transaction-iso.html | Read Committed 下 UPDATE 对并发修改的行重新求值 WHERE → 条件 UPDATE 防超发（§8） | 2026-10-28 |

参考设计（非官方，仅借鉴）：big-market 三层额度账户 + 领次数单幂等 + 本地消息表补偿发奖；CRMEB 抽奖“先读后写超发”反例；002Gym“库存为 0 不重抽、按未中奖处理”。

## 1 状态与依赖

状态：**标准**（可直接复用）。依赖：00-framework（db、errors、idempotency、outbox、audit、listing、export、statemachine、ratelimit、crypto、scheduler）；identity（`current_user_id` 解析会员令牌）；admin_rbac（`load_staff`，通过本地适配器 `deps.py`，见 §13）。发奖对 coupon / membership / member_card **不做导入**，只通过事件 `lottery.prize_won` 交互。

## 2 职责与边界

| 做 | 不做 |
|---|---|
| 活动（转盘 / 九宫格 / 刮刮卡同一引擎）、时间窗、门店范围、人群钩子、规则文本、状态机、发布冻结、有利变更与公示 | 前端动画（展示形态只是 `kind` 字段） |
| 奖池：券 / 积分 / 会员卡天数 / 实物 / 谢谢参与；万分比权重；库存；个人中奖上限；预算；5 万封顶 | 实际发券、加积分、延长会员卡（消费方模块） |
| 次数：每日免费、事件获得（满额支付、签到）、后台发放；日 / 月 / 总上限 | 积分兑换抽奖次数（可由积分模块扣分后调用 `grant_chances`） |
| 服务端抽奖、可复现审计记录、幂等、限流、风控钩子 | 微信转账红包类奖品 |
| 发奖事件与回执、补偿重发、实物领奖 / 发货 / 签收 / 逾期 | 物流轨迹查询 |
| 中奖公示（> 500 元，脱敏）、2 年保留、统计、CSV 导出 | — |

## 3 数据模型

| 表 | 关键字段 | 索引 / 约束 |
|---|---|---|
| `lottery_activities` | store_id?（空 = 全部门店）, name, kind(wheel/grid/scratch), status, start_at, end_at, rules_text, audience, free_daily_chances, limit_daily/monthly/total(0 不限), earn_payment_min_cents, earn_payment_chances, earn_checkin_chances, out_of_stock_policy(thanks/exclude_disclosed), budget_cents, reserved_cents, spent_cents, unlimited_spent_cents, claim_days, early_end_allowed, publicity_threshold_cents, odds_json（发布快照）, rules_hash, published_at, ended_at, end_reason, purged_at, version, created_by/updated_by, deleted_at | ix store_id；ix (status, start_at) |
| `lottery_prizes` | activity_id, seq, name, kind, ref_id?（券模板 / 卡）, quantity, value_cents, weight_bp, stock_total?(空 = 不限), stock_left?, per_member_limit, win_count, image_url, description | uq (activity_id, seq)；ix activity_id |
| `lottery_chance_accounts` | activity_id, member_id, granted, used, draws | uq (activity_id, member_id) |
| `lottery_chance_periods` | activity_id, member_id, period('YYYY-MM-DD' / 'YYYY-MM'，门店时区), draws, free_used | uq (activity_id, member_id, period) |
| `lottery_chance_grants` | activity_id, member_id, source(payment/checkin/admin/event), source_ref, chances, status(active/revoked), operator_id, reason | uq (activity_id, source, source_ref) |
| `lottery_member_prize_wins` | activity_id, prize_id, member_id, wins | uq (prize_id, member_id) |
| `lottery_draws` | activity_id, member_id, store_id, request_key, prize_*（结果快照）, won, hit_prize_id, fallback_reason, random_value, random_range, rng, odds_json（当次奖池）, rules_hash, chance_source, ip, delivery_status, delivery_attempts, delivery_due_at, delivery_ref, delivery_error, delivered_at, claim_deadline, claim_*_enc（加密）, claimed_at, carrier, tracking_no, shipped_at | uq (activity_id, member_id, request_key)；ix (activity_id, created_at)；ix (delivery_status, delivery_due_at)；ix member_id；ix store_id |
| `lottery_amendments` | activity_id, field, before, after, staff_id, created_at | ix activity_id |

## 4 业务规则

1. 活动字段：start_at < end_at；次数与金额字段 ≥ 0；claim_days ≥ 1；设置满额送次数时 earn_payment_chances ≥ 1；publicity_threshold_cents ∈ (0, 500 元]。
2. 草稿可任意修改、整体替换奖池、删除（软删）；只有草稿可删。
3. 发布前校验：rules_text 非空；end_at 在未来；奖品 2–12 个；seq 唯一；**权重整数 1–10000，含谢谢参与合计恰好 10000**；至少一个谢谢参与与一个真实奖品；预算 > 0；Σ(价值 × 有限库存) ≤ 预算；有不限量奖品时预算须有余量。
4. 发布冻结：写入 `odds_json`（奖池快照）、`rules_hash`、`reserved_cents`，重置 stock_left = stock_total。抽奖只读快照。
5. 发布后**奖项、概率、价值、条件、门店、开始时间、规则文本、退化策略不可改**（4090）。只允许有利变更：延后 end_at、增加每日免费 / 获得次数、放宽上限（调大或改为不限）、提高预算、延长领奖期、降低满额门槛、给有限奖品追加库存（预算须够）。每次变更写 `lottery_amendments` 并在详情公示，同时重算 rules_hash。
6. 结束：已发布未开始可撤下；进行中只有 `early_end_allowed`（发布前公示）才能提前结束，否则 4220；到点由任务结束。终态 ended 不可改（4090）。
7. 详情返回 `disclosure`：开奖方式、开奖时间、参与条件、次数上限、库存不足处理、公示门槛；以及每个奖项的 weight_bp、百分比、数量、剩余。
8. 单个奖品 value_cents ≤ 5_000_000（5 万元），保存草稿时即校验（4220）；项目可调低，不可调高。每次抽奖只产生一个奖品，同一机会不会累计两次中奖金额。
9. 不得设置不可能中出的奖项（weight_bp > 0）；后台没有指定中奖人的能力。
10. 券 / 积分奖必须填写 description（兑换规则、使用范围、有效期）；券奖必须 ref_id；实物奖必须有限库存。
11. 次数：先用当日免费（free_used < free_daily_chances），再用获得的（used < granted）；同时日计数 < limit_daily、月计数 < limit_monthly、总计数 < limit_total；任何一步 0 行 → 4225（data.reason = daily_limit / no_chance / monthly_limit / total_limit），整笔回滚。
12. 获得次数幂等：(activity, source, source_ref) 唯一；支付按 payment_id、签到按 `member:checkin_id`、后台按 `staff:id:幂等键`。
13. 支付送次数：活动 published / running 且支付时间在窗口内、门店匹配（全局活动或同门店）、实付 ≥ 门槛、business_type ∈ 配置（默认只 `order`）。
14. 支付退款（`payment.refunded`）：撤销该笔发放，只收回未用的次数（granted 不低于 used），已用的不追溯。
15. 次数来源不包含分享、订阅消息等（小程序 §2.5.1 / §2.5.21）；项目接入新来源须同样遵守。
16. 抽奖只在服务端：`secrets.randbelow(range)`，累积权重 + 二分定位；客户端只传活动 id 与门店 id。
17. 人群钩子、风控钩子在随机数之前执行，只能拒绝（4220 / 4290 等），被拒不扣次数，不能影响奖品。
18. 限流：每会员每分钟 ≤ 10 次、每 IP 每分钟 ≤ 60 次（配置），超限 4290 且不扣次数。
19. 中奖占用顺序：个人中奖上限 → 有限奖品条件扣库存（预算已预留）/ 不限量奖品条件占用预算余量（budget − reserved）；失败回退已占用部分并退化。
20. 库存不足默认（thanks）：**抽中库存为 0 的奖品不重抽，按谢谢参与处理**，记录 hit_prize_id 与 fallback_reason=out_of_stock，其余奖项概率不变。可选 `exclude_disclosed`：无库存奖品移出奖池、按剩余权重归一，必须在发布前选择并公示（详情 disclosure.out_of_stock），发布后不可切换。
21. 个人中奖上限到达 → 退化为谢谢参与（member_limit）；不限量奖品预算用尽 → 退化（budget）。
22. 每条记录保存 random_value、random_range、rng、当次奖池 odds_json、rules_hash；`replay(odds, r, range)` 必须得到 hit_prize_id（后台详情 replay_ok）。
23. 幂等：`Idempotency-Key` 必填；同键同体返回首次结果、不重抽不再扣次数；同键不同体 4093；并发同键 4092；唯一约束 (activity, member, request_key) 兜底。
24. 一个事务完成：消耗次数 + 扣库存 / 预算 + 写记录 + 发件箱（`lottery.drawn`，中奖再加 `lottery.prize_won`）。
25. 虚拟奖（券 / 积分 / 会员卡天数）：delivery_status=pending，delivery_due_at = 现在 + 回执期限；消费方回执成功 → delivered；未回执到期 → 任务按退避重发（attempt + 1）；超过 delivery_max_attempts → failed（发 `lottery.prize_delivery_failed` 告警），后台可重试；晚到的成功回执仍可把 failed 改为 delivered。
26. 实物奖：awaiting_claim → 中奖人在 claim_days 内填写姓名 / 手机 / 地址（发货前可修改）→ claimed → 后台发货（承运商 + 运单号）→ shipped → 签收 → delivered；逾期 → expired（不回补库存：结果已公示）。
27. 公示：价值 > publicity_threshold_cents（默认 500 元）的中奖记录公开列出，会员脱敏（昵称首字 + `**`，无昵称用 `用户****尾号`），含兑奖状态；收货信息加密存储，后台列表脱敏，查看明文需 `lottery:fulfil` 并写审计 `lottery_draw.sensitive_read`。
28. 他人记录 4040；后台按门店范围（全局活动只有全门店员工可管理），越界 4031。
29. 保留：活动结束满 retention_days（≥ 730 天）后才清理参与明细（记录、次数、发放）；活动、奖池快照、规则、变更记录保留。
30. 所有后台写操作写审计；导出写审计且不含收货信息。

## 5 状态机 / 关键流程

活动：`draft → published → running → ended`；`published → ended`（撤下 / 到期）。非法流转 4090；ended 为终态。

发奖（`lottery_draws.delivery_status`）：

| 当前 | 事件 | 下一 | 失败分支 |
|---|---|---|---|
| none | — | — | 谢谢参与，终态 |
| pending | 回执成功 | delivered | 回执失败：记录原因，到期重发 |
| pending | 重试耗尽 | failed | — |
| failed | 后台重试 / 晚到成功回执 | pending / delivered | — |
| awaiting_claim | 中奖人填写 | claimed | 逾期 → expired |
| claimed | 修改 / 发货 | claimed / shipped | 未领奖就发货 → 4090 |
| shipped | 签收 | delivered | — |

抽奖流程：校验活动开放（到点惰性 published→running）→ 门店 → 人群钩子 → 限流 → 风控钩子 → 消耗次数 → 随机选奖 → 占用上限 / 库存 / 预算（失败退化）→ 写记录 → 事件 → 提交。任一异常整笔回滚并释放幂等占位。

## 6 接口

顾客端 `/api/v1/app/lottery`（会员令牌；写接口必须 `Idempotency-Key`）：

| 方法 | 路径 | 鉴权 | 幂等 | 说明 |
|---|---|---|---|---|
| GET | `/activities` | 无 | — | 进行中 / 即将开始的活动，游标分页，`store_id` 含全局活动 |
| GET | `/activities/{id}` | 无 | — | 公示规则、奖项、概率、数量、disclosure、变更记录 |
| GET | `/activities/{id}/winners` | 无 | — | 中奖公示（> 500 元，脱敏），游标 |
| GET | `/activities/{id}/chances` | 会员 | — | 今日免费剩余、获得剩余、日 / 月 / 总剩余、available |
| POST | `/activities/{id}/draw` | 会员 | 必须 | 抽一次 `{store_id?}` |
| GET | `/records` | 会员 | — | 我的记录（游标；activity_id、won、delivery_status） |
| GET | `/records/{id}` | 会员 | — | 我的记录详情（他人 4040） |
| POST | `/records/{id}/claim` | 会员 | 必须 | 实物领奖 `{name, phone, address}` |

后台 `/api/v1/admin/lottery`（员工令牌 + 权限码；写接口必须 `Idempotency-Key`）：

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/activities` | lottery:read | ListSpec：status(in)、kind(in)、store_id(eq/in/null)、start_at/end_at/created_at 区间与排序、keyword=名称 |
| POST | `/activities` | lottery:write | 创建草稿（可带 prizes） |
| GET | `/activities/{id}` | lottery:read | 详情（含预算、预留、版本） |
| PATCH | `/activities/{id}` | lottery:write | 草稿任意改；发布后只允许有利变更；`expected_version` 乐观锁 |
| DELETE | `/activities/{id}` | lottery:write | 只删草稿 |
| PUT | `/activities/{id}/prizes` | lottery:write | 整体替换奖池（只草稿） |
| POST | `/activities/{id}/prizes/{pid}/stock` | lottery:write | 追加库存（发布后） |
| POST | `/activities/{id}/publish` | lottery:publish | 发布并冻结 |
| POST | `/activities/{id}/end` | lottery:publish | 结束 `{reason}` |
| POST | `/activities/{id}/simulate` | lottery:read | 离线模拟 `{n, seed?}`，不写库 |
| GET | `/activities/{id}/stats` | lottery:read | 抽奖数、人数、中奖数、中奖率、成本、已到账成本、各奖项、退化原因、发奖状态分布 |
| POST | `/activities/{id}/grants` | lottery:grant | 发放次数 `{member_id, chances ≤ 100, reason}` |
| GET | `/grants` | lottery:read | ListSpec：activity_id、member_id、source、status、created_at、chances；keyword=source_ref |
| GET | `/records` | lottery:read | ListSpec：activity_id、prize_id、prize_kind、won、member_id、store_id、delivery_status、fallback_reason、value_cents、created_at；keyword=会员号 / 奖品名 / 运单号 |
| GET | `/records/export` | lottery:export | 同一 ListSpec 的 CSV（不含收货信息，写审计） |
| GET | `/records/{id}` | lottery:read | 详情（收货信息脱敏、当次奖池、replay_ok） |
| GET | `/records/{id}/contact` | lottery:fulfil | 收货信息明文（写敏感查看审计） |
| POST | `/records/{id}/ship` | lottery:fulfil | 发货 `{carrier, tracking_no}` |
| POST | `/records/{id}/received` | lottery:fulfil | 签收 |
| POST | `/records/{id}/retry-delivery` | lottery:fulfil | failed → pending 并重发事件 |

agent 标记：无（抽奖不开放给智能体写操作）。

## 7 事件

发出（发件箱，与业务同事务）：

| 主题 | payload | 说明 |
|---|---|---|
| `lottery.prize_won` v1 | `draw_id, activity_id, member_id, store_id, prize_id, prize_kind, prize_name, ref_id, quantity, value_cents, delivery_key, attempt, won_at` | 中奖（含实物，用于通知）；重发时 attempt 递增、delivery_key 不变 |
| `lottery.drawn` | `draw_id, activity_id, member_id, won, prize_kind` | 每次抽奖（报表） |
| `lottery.prize_delivered` | `draw_id, member_id, prize_kind` | 虚拟奖到账 |
| `lottery.prize_delivery_failed` | `draw_id, member_id, prize_kind` | 重试耗尽，告警 |
| `lottery.prize_shipped` | `draw_id, member_id, carrier, tracking_no` | 实物发货（通知） |
| `lottery.chances_granted` | `activity_id, member_id, chances, source` | 获得次数（通知） |
| `lottery.activity_published / activity_ended` | `activity_id, store_id / reason` | 活动生命周期 |

**发奖契约（消费方必须遵守）**

| prize_kind | 消费方 | 动作 | 去重键 |
|---|---|---|---|
| coupon | coupon | `issue(db, template_id=ref_id, user_id=member_id, ref_key=delivery_key, source="lottery")`，发 quantity 张 | delivery_key（同键只发一次） |
| points | membership | `gift_points(db, member_id, quantity, ref_key=delivery_key, note=prize_name)` | delivery_key |
| member_card_days | member_card / membership | 给会员卡（ref_id 为空时为当前卡）延长 quantity 天 | delivery_key |
| physical | notification | 只发中奖通知，不回执 | — |

消费方处理完成后在**自己的事务里**发 `lottery.delivery_result`：`{draw_id, delivery_key, ok: bool, ref?: 发放单号, error?: 原因}`（或进程内直接调用 `lottery.ack_delivery(db, draw_id, ok, ref, error)`）。重复 / 重发的 `lottery.prize_won` 必须按 delivery_key 幂等，不能按 event_id（重发会生成新 event_id）。

消费（`lottery.handle_event(db, topic, payload, event_id)`，按 (lottery, event_id) 去重）：

| 主题 | 需要的 payload | 动作 |
|---|---|---|
| `payment.succeeded` | payment_id, user_id / member_id, amount_cents, store_id?, business_type?, paid_at? | 满额送次数 |
| `payment.refunded` | payment_id | 收回该笔未用次数 |
| `member.checked_in`（可配置） | user_id / member_id, checkin_id 或 date, store_id?, checked_in_at? | 签到送次数 |
| `lottery.delivery_result` | draw_id, ok, ref?, error? | 发奖回执 |

## 8 并发与一致性

- 全部计数为条件 UPDATE + rowcount：`free_used < N`、`used < granted`、`draws < limit`、`stock_left > 0`、`wins < limit`、`unlimited_spent + v ≤ budget − reserved`、`reserved + unlimited_spent + extra ≤ budget`；计数行用 `INSERT … ON CONFLICT DO NOTHING`（SQLite / Postgres；MySQL `INSERT IGNORE`）创建。
- 状态推进用 `StateMachine.advance`（`WHERE status = 旧值`）；活动编辑带 `version` 条件。
- 唯一约束兜底：抽奖请求键、领次数来源、奖池序号、计数行。
- 有限奖品的预算在发布时预留，库存扣成功即不会因预算退化（公示数量一定能发出）。
- 发奖：本地发件箱 + 回执 + `retry_deliveries` 补偿任务（最终一致）；消费方按 delivery_key 幂等。
- 测试：50 线程抢 5 件实物恰好 5 件中出；同一会员 50 线程只用掉 3 次；每日上限 2 在并发下恰好 2 次；同键 20 线程只有一条记录；30 个并发支付事件只发一次次数。

## 9 配置

`lottery.config.LotteryConfig`（`set_config(**overrides)`；违反法定上限 ValueError）：

| 键 | 默认 | 说明 |
|---|---|---|
| prize_value_cap_cents（LUZ_LOTTERY_PRIZE_CAP_CENTS） | 5_000_000 | 单奖封顶，≤ 5 万元 |
| publicity_threshold_cents（LUZ_LOTTERY_PUBLICITY_CENTS） | 50_000 | 公示门槛，≤ 500 元 |
| retention_days（LUZ_LOTTERY_RETENTION_DAYS） | 730 | ≥ 730 |
| timezone（LUZ_TIMEZONE） | Asia/Shanghai | 日 / 月计数口径 |
| member_draws_per_minute / ip_draws_per_minute | 10 / 60 | 0 = 关闭 |
| delivery_ack_timeout_seconds | 600 | 首次回执期限 |
| delivery_max_attempts / delivery_backoff_seconds | 5 / (600, 1800, 7200, 21600, 43200) | 重发退避 |
| default_claim_days | 7 | 实物领奖期限默认值（活动可改，1–90） |
| max_prizes / max_admin_grant / max_simulate | 12 / 100 / 1_000_000 | 上限 |
| earn_payment_business_types | ("order",) | 可送次数的支付业务类型 |
| checkin_topic / payment_succeeded_topic / payment_refunded_topic / delivery_result_topic | member.checked_in / payment.succeeded / payment.refunded / lottery.delivery_result | 消费主题 |

## 10 错误码

| 场景 | code |
|---|---|
| 参数错误、缺幂等键、未声明的筛选参数、权重 0、券缺说明 | 4001 |
| 未登录 / 令牌无效 | 4010 |
| 无权限 | 4030 |
| 门店越界 | 4031 |
| 活动不存在 / 草稿 / 他人记录 | 4040 |
| 非法状态流转、发布后修改冻结字段、版本冲突、终态修改 | 4090 |
| 同幂等键处理中 | 4092 |
| 同幂等键不同请求体 | 4093 |
| 奖池校验失败、超 5 万、超预算、未开始 / 已结束、门店不符、人群不符、不允许提前结束、领奖逾期 | 4220 |
| 次数用完 / 日 / 月 / 总上限（data.reason） | 4225 |
| 抽奖限流 | 4290 |

## 11 测试清单（`code/tests/`，84 个用例）

| 文件 | 覆盖 |
|---|---|
| test_lottery_rules.py | 选奖边界、越界、**10 万次固定种子分布（5σ + 卡方 α=0.001）**、secrets 源、缩减奖池归一、复现、奖池校验（合计、5 万、权重 0、预算、券说明、券模板、谢谢参与、真实奖品、不限量余量）、有利变更矩阵、脱敏、时区日界、配置法定上限 |
| test_lottery_service.py | 发布冻结与审计 / 事件、发布校验、5 万封顶与项目下调、**发布后不可变**、有利变更与公示、草稿编辑 / 版本冲突 / 删除、结束规则与终态、门店范围、审计字段与 secrets、注入随机源、**幂等重放 / 4093 / 缺键**、次数（免费 / 获得 / 日 / 月 / 总）、失败回滚不扣次数、窗口 / 门店 / 人群 / 风控 / 限流、惰性开始与任务、**缺货退化不重抽**、exclude_disclosed、个人上限与预算退化、**发奖事件契约与重试 / failed / 人工重试 / 回执去重**、失败回执重发、实物领奖 / 发货 / 签收 / 逾期 / 明文审计、支付 / 签到 / 退款事件、后台发放幂等、公示脱敏、列表 / 导出 / 统计 / 模拟、记录私有、草稿不公开、**保留期 729 天不删 / 731 天删**、任务注册 |
| test_lottery_concurrency.py | 文件 SQLite + 线程：50 人抢 5 件、单人 50 线程 3 次、每日上限、同键 20 线程、30 个并发支付事件 |
| test_lottery_http.py | 4010、4030、4031、4001（缺键 / 校验 / 未知参数 / 多余字段）、4220、信封、完整流程（详情概率、抽奖幂等、4225、记录、领奖、明文、发货、公示、列表、导出、统计、模拟）、发布后编辑、默认员工解析器拒绝 |

## 12 前端对接

| 页面 | 接口 | 行为 |
|---|---|---|
| 活动页（转盘 / 九宫格 / 刮刮卡） | GET 详情 + chances | 按 `kind` 渲染；展示 disclosure、各奖项概率与数量、变更记录；available = 0 时按钮置灰并显示原因 |
| 抽奖 | POST draw（每次点击生成新 Idempotency-Key，网络重试复用同键） | 动画停在返回的 `prize.seq`；4225 提示次数 / 上限，4290 提示稍后 |
| 我的奖品 | GET records / records/{id} | 虚拟奖显示到账状态；实物奖 awaiting_claim 时引导填写（截止时间） |
| 领奖表单 | POST claim | 发货前可修改 |
| 中奖公示 | GET winners | 滚动展示脱敏名单与兑奖状态 |
| 后台活动 | 列表 / 创建 / 编辑 / 奖池 / 模拟 / 发布 / 结束 | 发布前展示模拟结果；发布后只开放有利变更字段 |
| 后台记录与发货 | records（筛选）/ contact / ship / received / retry-delivery / export | 发货前查看明文（会留痕） |
| 后台统计 | stats | 抽奖数、中奖率、成本、退化原因 |

## 13 定制点

- 配置：§9 全部键（`set_config` 或环境变量）。
- 钩子（`lottery.hooks`）：`set_audience_checker(fn(db, activity, member_id) -> bool)`（如会员等级、排除员工账号）；`set_risk_checker(fn(db, RiskContext))`（黑名单、设备指纹，拒绝即抛 AppError）；`set_member_display(fn(db, member_id) -> 昵称)`（公示）；`set_limiters(member, ip)`（多实例换 Redis 实现，`allow(key)` 接口）；`set_random_source(src)`（只用于测试，生产保持 secrets）。
- 鉴权适配（`lottery.deps`）：`set_staff_resolver(fn(request, db, token, code) -> StaffActor)`；默认用 `admin_rbac.load_staff` 校验权限，不可用时 4010。权限码 `lottery.PERMISSIONS`，导入时向 `admin_rbac.declare_permissions` 登记（可用时）。
- 次数来源：其他模块调用 `lottery.grant_chances(db, activity_id, member_id, chances, source="event", source_ref=唯一业务号)`。
- 任务：`lottery.jobs.JOBS` / `register_jobs(registry)`：advance_activities（60s）、retry_deliveries（60s）、expire_unclaimed（600s）、purge_expired（每天）。

不包含：积分 / 余额作为抽奖消耗（由积分模块扣减后发放次数实现）；现金红包与商家转账；“必中”保底与解锁次数规则树；Redis 预扣库存适配器（高并发时可按 big-market 方案扩展，接口不变）；抽奖消耗与订单退款联动（已用次数不追溯、奖品不追回）；实物物流轨迹；奖品图片上传（只存 URL / media）；虚拟支付相关能力。
