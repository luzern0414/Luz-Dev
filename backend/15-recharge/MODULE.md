# 模块：15-recharge（储值钱包）`wallet`

## 0 官方来源

| 主题 | 官方页面 | 本模块用到的结论 | 核对日期 |
|---|---|---|---|
| 预付式消费司法解释 | 法释〔2025〕4号 https://www.court.gov.cn/zixun/xiangqing/459321.html | 第十四条付款 7 日内请求返还本金应支持；第十五条本金 = 预付款 − 已兑付价款；第十六条经营者原因按合同成立时一年期 LPR 计息；第十八条经营者原因按折扣价，赠送按“实付 /（实付 + 赠送）”折算；第十九、二十条消费者原因按打折前价格、超出实付不追要；第九条排除退款权、记名卡不补办等格式条款无效 | 2026-10-28 |
| 消费者权益保护法实施条例 | 国务院令第778号 https://www.gov.cn/zhengce/content/202403/content_6940158.htm | 第二十二条预付款须书面约定退还方式；重大经营风险时停止收取预付款；第二十一条停业迁址提前 30 日公告 | 2026-10-28 |
| 单用途商业预付卡管理办法（试行） | 商务部令2012年第9号（转载全文 https://www.fadada.com/article/Basics-010635，按未核实处理） | 第十八条记名卡单张 ≤ 5000 元且充值后余额不超限；第十九条记名卡不得设有效期；第二十一条退卡进入本人账户 | 2026-10-28 |
| 上海体育健身预付式消费 | 沪体规文〔2025〕1号（修订说明 https://tyj.sh.gov.cn/styles/img/index/2.《上海市体育健身行业预付式消费经营活动监管实施办法（修订征求意见稿）》修订说明.pdf；新华网 https://www.news.cn/local/20250114/36ca76116dfb476f9fd613ada70eb7a8/c.html） | 储值类一次 ≤ 5000 元；同一消费者各类合计 ≤ 20000 元；续约后仍不超限；修订稿赠送 ≤ 预收 25%（未生效） | 2026-10-28 |
| 北京市单用途预付卡管理条例 | 2022-06-01 施行（转载 https://www.fadada.com/article/Basics-003563） | 第十六条 7 日内未兑付全额退、5 日内退还；第十七条无约定 15 日内退还；第十八条提供余额与消费记录查询 | 2026-10-28 |
| 单用途卡发票 | 税务总局公告2016年第53号 https://fgk.chinatax.gov.cn/zcfgk/c100012/c5194672/content.html | 售卡 / 充值开普通发票，税率“不征税”，编码 601，不得开专票；持卡消费不再开票 | 2026-10-28 |
| 发票红冲 | 发票管理办法实施细则第二十七条 https://fgk.chinatax.gov.cn/zcfgk/c100011/c5221006/content.html | 退款后开具红字发票（本模块只在 `recharge.refunded` 事件给出 `invoice_action=red_reverse`） | 2026-10-28 |
| 微信支付退款 | https://pay.weixin.qq.com/doc/v3/merchant/4012791862 | 支付后 365 天内可原路退款（超期本模块转线下退款） | 2026-10-28 |
| 小程序虚拟支付边界 | 运营规范 2.5.13 / 2.14.7 https://developers.weixin.qq.com/miniprogram/product/ | 门店消费的储值属线下服务：`goods_kind=offline_service`，走普通微信支付 | 2026-10-28 |

## 1 状态与依赖

| 项 | 内容 |
|---|---|
| 状态 | **标准**；测试在 `code/tests/` |
| 依赖 | 00-framework；09-wechat-pay（支付 / 退款 / 钩子，经 `ports.py`）；22-admin-rbac（后台鉴权）；01-identity（顾客令牌、会员查询） |
| 被依赖 | 08-order（余额支付 `consume` / 冲回 `reverse`）、29-member-card（复用 `PrepaidPolicy` 与适配层）、16/17/18（`grant_bonus` 赠送金） |

## 2 职责与边界

| 负责 | 不负责 |
|---|---|
| 充值档位、充值单（= 入账批次）、支付调起与入账 | 支付渠道（09） |
| 本金 / 赠送金分账余额、退款预扣（冻结） | 发票开具（只记录 601 不征税信息与红冲提示） |
| 消费按比例拆分 + 批次 FIFO 归属、冲回 | 提现、余额转赠（不提供） |
| 按批次退款（七日规则、消费者 / 经营者原因公式、LPR 利息）与审批 | 会员卡（29） |
| 后台调整、线下充值、门店扣款、对账、赠送金到期 | 资金存管与监管平台上报 |

## 3 数据模型

| 表 | 关键字段 | 约束 / 索引 |
|---|---|---|
| `wallet_accounts` | user_id, principal_cents, bonus_cents, frozen_principal_cents, frozen_bonus_cents, status(active/closed), version | user_id 唯一；四个余额 CHECK ≥ 0 |
| `wallet_recharge_plans` | store_id?, name, amount_cents, bonus_cents, bonus_valid_days?, status(on/off), sort, sale_start_at/end_at, per_user_daily_limit, terms_text, deleted_at | 软删除 |
| `wallet_recharge_orders`（批次） | order_no, user_id, store_id, plan_id, source(payment/offline/adjust/gift/direct), source_ref, amount_cents, bonus_cents, principal_remaining_cents, bonus_remaining_cents, refunded_cents, bonus_forfeited_cents, bonus_expired_cents, status, payment_id, out_trade_no, paid_at, expires_at, bonus_expires_at, needs_manual, lpr_bp, terms_snapshot, invoice_tax_code(601), invoice_note | order_no、source_ref 唯一；剩余 CHECK ≥ 0；(user_id, status) |
| `wallet_ledger`（只追加） | account_id, user_id, kind, principal_delta, bonus_delta, frozen_principal_delta, frozen_bonus_delta, principal_after, bonus_after, ref, batch_id, store_id, operator_id, note | (ref, kind) 唯一 |
| `wallet_batch_allocations`（只追加） | ledger_id, batch_id, principal_cents, bonus_cents | 一次扣减落到哪些批次 |
| `wallet_refunds` | refund_no, user_id, batch_id, cause, rule, hold_*_cents, refund_cents, interest_cents, fee_cents, bonus_forfeit_cents, retained_cents, route(original/offline), status, breakdown_json, due_at, submitted_at, channel_refund_id, submit_error | refund_no 唯一 |
| `wallet_mismatches` | account_id, user_id, check_date, detail_json, resolved | (account_id, check_date) 唯一 |

## 4 业务规则

1. 档位：金额 > 0；创建 / 修改时校验单笔上限与赠送比例（PrepaidPolicy）；上架后才可购买；售卖时间窗；按门店适用（store_id 空 = 全部门店）；每人每日次数上限可选。
2. 自定义金额默认关闭（`[wallet] custom_amount_enabled`），开启后最低 `[wallet] custom_amount_min_cents`，无赠送。
3. 创建充值单：`[prepaid] stop_prepaid` 开启时拒绝（消保条例第二十二条）；单笔 ≤ `stored_value_max_cents`；充值后余额（本金 + 赠送）≤ `stored_value_max_balance_cents`；同一会员各类预付合计 ≤ `member_total_max_cents`（全部 4225，`data.reason` 区分）。
4. 充值单先提交，再单独调起支付（09 的 `create_payment` 自行提交）；`goods_kind=offline_service`；记录合同条款快照与发票信息（601、不征税、不可开专票）。
5. 入账：09 同事务钩子（主）或 `payment.succeeded` 事件（补偿）；条件 UPDATE `pending|closed → paid`，同一批次只入账一次；金额不符 → `needs_manual`，不入账；事件 `needs_manual=true`（关单后支付、重复支付）不入账（09 自动退款）。
6. 消费：拆分策略 `proportional`（默认，赠送金向下取整）或 `principal_first`；账户条件 UPDATE（四个余额都不为负）；按 FIFO（先入账先用）归属批次并记录分配；同 ref 幂等，ref 属于他人 4090；余额不足 4224。
7. 冲回 `reverse(db, user_id, ref, amount_cents=None, reversal_ref=None, note=None)`：整笔（不带金额，幂等键 ref）冲回尚未冲回的剩余金额；部分（售后部分退款，带金额与 `reversal_ref`，幂等键 reversal_ref，同键不同金额 4090）。累计冲回 ≤ 原消费（consume 流水 `reversed_cents` 条件 UPDATE 封顶，超出 4220 `reverse_exceeds_consumed`）。本金 / 赠送金按原消费比例拆分：累计应退赠送金 = ⌊原赠送 × 累计冲回 ÷ 原消费⌋（向下取整，余数归本金，最后一笔补齐）；按原分配顺序逐批次退回；原批次已在退款 / 已退时，冲回金额进入新的直接入账批次（线下退款路径）。
8. 退款按批次（不是整户）：批次 = 一笔充值的原支付交易，微信只能按原交易退款；FIFO 消费让最近的批次保留最多本金。
9. 退款公式（`rules.quote_batch_refund`）：A = 实付本金、B = 赠送、p / b = 该批剩余；七日内且该批未动 → 退 A；经营者原因 → 可退 = A − ⌊已兑付 × A / (A + B)⌋ + LPR 利息（合同成立时 `lpr_bp`，单利按日）；消费者原因 → 可退 = max(0, A − 已兑付)，可扣手续费 `[wallet] customer_refund_fee_bp`（默认 0）；可退永不超过 p；剩余赠送金 b 全部作废。
10. 申请退款即预扣该批剩余本金与赠送金（移入冻结），批次 `refunding`；顾客申请默认待审批（`[wallet] refund_requires_approval`）；后台发起即审批；驳回 / 失败释放预扣、批次恢复 `paid`。
11. 原路退款在审批事务提交后提交 09 `create_refund`（business_ref = `wallet_refund:<退款单号>`），每笔只提交一次（条件占位）；上游暂时失败等任务重试；规则拒绝（如超 365 天）→ 失败并释放；退款成功清除冻结、批次 `refunded`、事件带 `invoice_action=red_reverse`；`refund.abnormal` 保持处理中并告警。
12. 线下路径：线下 / 调整 / 赠送 / 直接入账批次、超过 365 天的批次、只有利息可退时走线下退款，由财务 `offline-complete` 确认。
13. 退款时限：七日全额 `cooling_off_refund_days`（默认 5 天）；其他 `refund_deadline_days`（默认 15 天）；超期任务发 `wallet.refund_overdue`。
14. 后台调整：原因必填；单次每项 |Δ| ≤ `[wallet] adjust_max_cents`；加款形成 `adjust` 批次，扣款按 FIFO；审计。
15. 赠送金到期：仅当档位设置 `bonus_valid_days`（默认不过期）；本金永不过期（单用途卡办法第十九条）；任务扣减该批剩余赠送金。
16. 对账：每日逐账户校验 余额 = Σ流水、冻结 = Σ冻结流水、可用余额 = Σ可用批次剩余；不一致写 `wallet_mismatches`（每账户每日一条）并发告警事件。
17. 余额不可提现、不可转让；`refund_card`（旧接口）整户线下退剩余本金并关户。

## 5 状态机与流程

批次：`pending →(支付) paid ⇄ exhausted`；`pending →(超时) closed →(迟到支付) paid`；`paid|exhausted →(申请退款) refunding →(成功) refunded`（终态）；`refunding →(驳回 / 失败) paid`。
退款单：`pending_approval →(审批) processing →(成功) succeeded`；`processing →(失败) failed`；`pending_approval →(驳回) rejected`。非法流转 4090。

```
顾客 POST recharge-orders ─(提交)→ start_payment → 09.create_payment(自行提交) → pay_params
支付回调 → 09 同事务钩子 on_payment_succeeded → 批次 paid + 流水 recharge + recharge.succeeded
         └ payment.succeeded 事件（补偿，重复 = duplicate）
申请退款 → 预扣(refund_hold) → [审批] → processing ─(提交后)→ submit_refund → 09.create_refund
退款回调 → 09 钩子 / refund.* 事件 → succeeded: refund_done（清冻结）| failed: refund_release（回补）
```

## 6 接口

顾客端 `/api/v1/app/wallet`（access 令牌）：

| 方法 | 路径 | 幂等 | agent | 说明 |
|---|---|---|---|---|
| GET | `/plans?store_id=` | — | read | 在售档位（游标） |
| POST | `/recharge-orders` | 必须 | write_high | 建充值单并返回 pay_params |
| POST | `/recharge-orders/{id}/pay` | 天然幂等 | no | 继续支付 |
| GET | `/account` | — | read | 余额、冻结 |
| GET | `/ledger` | — | read | 流水（游标，kind 筛选） |
| GET | `/recharge-orders`、`/recharge-orders/{id}` | — | read | 我的充值单 |
| GET | `/recharge-orders/{id}/refund-quote` | — | read | 消费者原因报价 |
| POST | `/recharge-orders/{id}/refund-request` | 必须 | write_high | 申请退款 |
| GET | `/refunds` | — | read | 我的退款 |

后台 `/api/v1/admin/wallet`（直接用 `admin_rbac.require_permission(code, fresh=…)`：真实服务端会话、`module:*` 通配授予；带 † 的 fresh=True，`fresh_auth_seconds` 内完成完整认证，否则 4011 `reauth_required`）：

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET / POST | `/plans` | wallet:read / wallet:config | 列表（status、store_id、amount 区间、关键词 name、排序 seq/amount/created_at）/ 新建 |
| PUT / DELETE | `/plans/{id}` | wallet:config | 修改 / 软删 |
| POST | `/plans/{id}/on-sale`、`/off-sale` | wallet:config | 上下架 |
| GET | `/recharge-orders`、`/recharge-orders/export`、`/recharge-orders/{id}` | wallet:read / wallet:export | 列表（status、source、user、store、plan、needs_manual、金额 / 时间区间、关键词单号）与 CSV |
| GET | `/ledger`、`/ledger/export` | wallet:read / wallet:export | 流水与 CSV |
| GET | `/accounts`、`/accounts/{user_id}` | wallet:read | 账户列表 / 详情 + 近期流水 |
| POST | `/accounts/{user_id}/adjust` † | wallet:adjust | 调整 |
| POST | `/accounts/{user_id}/offline-recharge` † | wallet:recharge | 线下充值 |
| POST | `/accounts/{user_id}/consume`；`/reverse` † | wallet:consume | 门店扣款 / 冲回（冲回动钱，要求新鲜认证） |
| GET | `/recharge-orders/{id}/refund-quote?cause=` | wallet:refund | 报价 |
| POST | `/recharge-orders/{id}/refunds` † | wallet:refund | 发起退款（即审批） |
| GET | `/refunds` | wallet:read | 列表（status、cause、route、到期 / 创建区间） |
| POST | `/refunds/{id}/approve` †、`/reject` †、`/offline-complete` † | wallet:refund | 审批 / 驳回 / 线下确认 |
| POST / GET | `/reconcile`、`/mismatches` | wallet:reconcile | 对账 |

所有写接口 `Idempotency-Key` 必填；导出写审计；门店范围按 admin_rbac（None = 全部，空 = 无门店）。

## 7 事件

发出：

| 主题 | payload |
|---|---|
| `recharge.succeeded` | user_id, batch_id, store_id, amount_cents, bonus_cents, payment_id |
| `wallet.recharged` | user_id, batch_id, amount_cents, bonus_cents, source（直接 / 线下 / 调整 / 赠送入账） |
| `balance.consumed` | user_id, ref, amount_cents, principal_cents, bonus_cents |
| `balance.reverted` | user_id, ref（原消费）, reversal_ref, amount_cents, principal_cents, bonus_cents, reversed_total_cents, consumed_cents, fully_reversed |
| `wallet.refund_created` / `wallet.refund_requested` | refund_id, user_id, batch_id；requested 另含 business_ref, payment_id, out_trade_no, amount_cents, interest_cents, route, reason（通知用，退款由本模块直接调 09） |
| `recharge.refunded` | refund_id, user_id, batch_id, refund_cents, interest_cents, invoice_action=red_reverse |
| `wallet.refund_failed` / `refund_rejected` / `refund_abnormal` / `refund_overdue` | refund_id, user_id?, reason? |
| `wallet.payment_mismatch` | batch_id, expected_cents, paid_cents, payment_id |
| `wallet.order_closed` / `bonus_expired` / `adjusted` / `closed` / `reconcile_mismatch` | 见代码，均只含 id 与金额 |

消费（`wallet/events.py` `SUBSCRIPTIONS`，幂等、不提交）：`payment.succeeded`、`refund.succeeded`、`refund.failed`、`refund.abnormal`（payload 见 09 MODULE.md §7）。同时向 09 注册 `register_on_paid("wallet_recharge")`、`register_on_refunded("wallet_refund")`、`register_payable("wallet_recharge")`。

## 8 并发与一致性

- 余额四列与批次剩余都是条件 UPDATE + rowcount；先锁账户行（UPDATE）再分配批次，同一会员的扣减串行化。
- 流水 (ref, kind) 唯一、批次 source_ref 唯一、充值单状态条件流转：重复回调 / 重放只生效一次。
- 外部调用（create_payment / create_refund）不在业务事务内：先提交业务单，再调用；退款提交用条件占位防重复。
- 50 线程并发消费测试：成功次数精确、余额不为负、对账一致。
- 对账任务覆盖余额 / 冻结 / 批次三套口径。

## 9 配置

运行参数写在 `backend/config/runtime/*.toml`，字段说明（类型、范围、能否热加载、是否下发客户端）在 `00-framework/code/luzback/runtime_config.py`；模块经 `luzback.module_config.ModuleConfig` 读取，热加载后下一次读取即生效。环境变量只放秘密与部署参数；迁移前的环境变量名仍设置着时，启动会打 `config.unknown_env` 警告并指出新键。测试用 `set_config(...)` 或 `luzback.testing.override_runtime(...)` 注入运行参数（写进一份测试用运行配置，测试结束撤销）。

### 9.1 环境变量（秘密与部署参数）

无：本模块没有秘密与部署参数。

### 9.2 运行配置键

| 键 | 默认 | 范围 | 热加载 | 下发客户端 | 说明（含单位） |
|---|---|---|---|---|---|
| `[prepaid] policy` | "conservative" | conservative / shanghai_fitness_2025 / shanghai_fitness_draft_2026 / beijing_2022 / national_single_use_card / unlimited | 是 | 否 | 预付卡监管口径预设（单次、累计上限与冷静期等）；连锁跨城市可按门店另行注入（原 `LUZ_PREPAID_POLICY`） |
| `[prepaid] lpr_1y_bp` | 300 | 0–2000 | 是 | 否 | 合同成立时一年期 LPR（万分比），退款补偿利息按它计算；按公布值更新（原 `LUZ_LPR_1Y_BP`） |
| `[prepaid] stop_prepaid` | false | true / false | 是 | 是 | 停止收取预付款（经营风险时）：储值与会员卡都停售，客户端隐藏入口（原 `LUZ_STOP_PREPAID`） |
| `[wallet] split_policy` | "proportional" | proportional / principal_first | 是 | 否 | 消费时本金与赠送金的扣减顺序（原 `WALLET_SPLIT_POLICY`） |
| `[wallet] pay_timeout_minutes` | 15 | 1–1440 | 是 | 否 | 充值待支付自动关闭（分钟）（原 `WALLET_PAY_TIMEOUT_MINUTES`） |
| `[wallet] refund_requires_approval` | true | true / false | 是 | 否 | 储值退款需要后台审批（原 `WALLET_REFUND_REQUIRES_APPROVAL`） |
| `[wallet] customer_refund_fee_bp` | 0 | 0–10000 | 是 | 否 | 顾客原因退款的手续费（万分比）（原 `WALLET_CUSTOMER_REFUND_FEE_BP`） |
| `[wallet] adjust_max_cents` | 50_000 | 0–10000000 | 是 | 否 | 后台单次手工调整余额上限（分）（原 `WALLET_ADJUST_MAX_CENTS`） |
| `[wallet] custom_amount_enabled` | false | true / false | 是 | 是 | 允许顾客输入自定义充值金额（原 `WALLET_CUSTOM_AMOUNT`） |
| `[wallet] custom_amount_min_cents` | 100 | 1–10000000 | 是 | 是 | 自定义充值最低金额（分）（原 `WALLET_CUSTOM_AMOUNT_MIN_CENTS`） |
| `[wallet] original_refund_max_days` | 365 | 1–365 | 是 | 否 | 支付后多少天内可以原路退回（天）（原 `WALLET_ORIGINAL_REFUND_MAX_DAYS`） |

`[prepaid]` 由储值与 29-member-card 共用，只存一份。按门店不同的监管口径用 `set_policy_resolver` 注入。

## 10 错误码

| 场景 | code | data.reason |
|---|---|---|
| 参数、缺幂等键、未知筛选 | 4001 | — |
| 档位 / 充值单 / 退款单不存在、他人资源 | 4040 | — |
| 状态冲突（退款中、已审批、批次已变化） | 4090 | batch_status |
| 钱包已关闭、暂停收预付、无可退金额 | 4220 | wallet_closed / prepaid_stopped / nothing_refundable |
| 余额不足 | 4224 | — |
| 单笔 / 余额 / 合计 / 赠送比例 / 每日次数 / 调整上限 | 4225 | recharge_amount_cap / balance_cap / member_total_cap / gift_ratio_exceeded / daily_limit / adjust_cap |
| 支付上游失败 | 5020 | — |

## 11 测试清单

| 文件 | 覆盖 |
|---|---|
| `test_wallet.py` | 幂等入账、拆分策略随机校验、冲回、整户退卡、并发不为负（旧接口兼容） |
| `test_wallet_service.py` | 充值单与支付分步、事件幂等与 business_type 别名、金额不符、迟到支付、上游失败后继续支付、档位上下架与售卖窗、每日次数、自定义金额、单笔 / 余额 / 合计 / 赠送比例上限、预设、停止收预付、FIFO 与冲回、冲回到退款中批次、50 线程并发、退款公式（七日 / 消费者 / 经营者 + 利息 / 手续费）、预扣与成功 / 失败 / 驳回 / 异常、提交一次与重试 / 规则拒绝、线下退款、免审批、到期时限与超期、调整、赠送金到期、对账、任务注册、订阅不提交与去重、needs_manual 不入账、部分冲回（多次、比例拆分、封顶、重放、跨批次、20 线程并发不超额） |
| `test_wallet_api.py` | 4010 / 4030 / 4001 / 4093 / 4031、信封、幂等重放、顾客流程、后台列表筛选排序与未知参数、CSV（BOM、601）、空门店范围 = 无门店、游标分页、真实 admin_rbac 会话（伪造令牌 4010、新鲜认证 4011、`wallet:*` 通配） |
| `29-member-card/.../test_with_wechat_pay.py` | 与真实 09 假渠道联调：充值 → 回调钩子入账 → 退款提交 → 退款回调 |

## 12 前端对接

| 页面 | 接口 | 行为 |
|---|---|---|
| 储值充值页 | plans、account | 档位网格（到账 = 本金 + 赠送，服务端计算）；展示条款与“可退规则” |
| 支付 | `POST recharge-orders` → `wx.requestPayment`；失败后 `POST /{id}/pay` | 以 09 支付结果页为准 |
| 余额明细 | ledger | 按 kind 显示文案；退款中显示冻结金额 |
| 申请退款 | refund-quote → refund-request | 显示规则（七日全额 / 按原价扣已用）、可退金额、作废赠送金、预计到账时限 |

## 13 定制点

- 配置项：§9。
- LPR：`[prepaid] lpr_1y_bp` 默认 300（3.00%，示例值，不是实时公布值）；经营者原因退款利息按合同成立时写入批次的 `lpr_bp` 计算，上线前与每月 20 日 LPR 公布后须更新配置。
- 会员查询：`find_member(phone=…)` 默认走 01-identity `find_user_by_phone`（规范化 + 盲索引哈希），`set_member_resolver` 可替换。
- 部分冲回：`reverse(..., amount_cents, reversal_ref)` 供 08-order / 10-after-sales 余额支付订单的部分退款。
- 扩展点：`set_policy_resolver(fn(store_id) -> PrepaidPolicy)`（按门店 / 城市）、`custom_policy(base, **overrides)`、`register_outstanding_provider(name, fn)`（其他预付业务计入合计上限）、`set_payment_port`（替换支付实现，测试用 `FakePaymentPort`）、`set_member_resolver`、`set_store_timezone_resolver`、`set_user_token_decoder`；`grant_bonus` 供营销模块发赠送金。
- 不包含：提现与转赠；整户“退卡”拆分到多笔原交易的自动化（按批次逐笔退）；资金存管比例计算与监管平台上报（上海每日上报、北京存管）；发票开具与红冲执行；赠送金到期前提醒；自动续费；小程序虚拟支付（储值按线下服务处理）。
