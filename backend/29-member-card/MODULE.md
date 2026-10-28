# 模块：member-card（会员卡：次卡 / 期限卡 / 时段卡）`member_card`

## 0 官方来源

| 主题 | 官方页面 | 本模块用到的结论 | 核对日期 |
|---|---|---|---|
| 预付式消费司法解释 | 法释〔2025〕4号 https://www.court.gov.cn/zixun/xiangqing/459321.html | 第十四条付款 7 日内返还本金；第十五条本金 = 预付款 − 已兑付；第十六条经营者原因加一年期 LPR 利息；第十八条经营者原因按折扣价、赠送按“实付 /（实付 + 赠送）”；第十九、二十条消费者原因按打折前价格、超出实付不追要；第二十二条消费者自身原因未在期限内使用不予返还；第九条“不补办 / 不退 / 限制转让”格式条款无效；第十一条可转让、更名；第二十四条记名卡可挂失补办 | 2026-10-28 |
| 消费者权益保护法实施条例 | 国务院令第778号 https://www.gov.cn/zhengce/content/202403/content_6940158.htm | 第二十二条书面约定退款方式（卡上保存条款快照）、经营风险时停止收预付款；第十七条不得限制解除合同（冻结中仍可退卡） | 2026-10-28 |
| 上海体育健身预付式消费 | 沪体规文〔2025〕1号（修订说明 https://tyj.sh.gov.cn/styles/img/index/2.《上海市体育健身行业预付式消费经营活动监管实施办法（修订征求意见稿）》修订说明.pdf；新华网 https://www.news.cn/local/20250114/36ca76116dfb476f9fd613ada70eb7a8/c.html） | 会籍类 ≤ 5000 元且 ≤ 24 个月；课时类 ≤ 20000 元且 ≤ 60 节；合计 ≤ 20000 元；续约后仍不超限；修订稿会籍 ≤ 12 个月、赠送 ≤ 25%（未生效） | 2026-10-28 |
| 北京市单用途预付卡管理条例 | 2022-06-01（转载 https://www.fadada.com/article/Basics-003563） | 7 日未兑付全额退、5 日内退还；无约定 15 日内退还；挂失补办转让方式须载明；提供消费记录 | 2026-10-28 |
| 单用途卡发票 | 税务总局公告2016年第53号 https://fgk.chinatax.gov.cn/zcfgk/c100012/c5194672/content.html | 售卡开普通发票“不征税”编码 601、不得开专票；用卡消费不再开票；转让手续费为应税服务 | 2026-10-28 |
| 发票红冲 | 实施细则第二十七条 https://fgk.chinatax.gov.cn/zcfgk/c100011/c5221006/content.html | 退卡后开红字发票（事件给出 `invoice_action=red_reverse`） | 2026-10-28 |
| 小程序虚拟支付边界 | 运营规范 2.5.13 / 2.14.7 https://developers.weixin.qq.com/miniprogram/product/ | 到店使用的会员卡属线下服务：`goods_kind=offline_service`，普通微信支付（线上内容会员不在本模块） | 2026-10-28 |
| 微信支付退款 | https://pay.weixin.qq.com/doc/v3/merchant/4012791862 | 365 天内原路退款，超期转线下 | 2026-10-28 |

## 1 状态与依赖

| 项 | 内容 |
|---|---|
| 状态 | **标准**；测试 50 项（`code/tests/`） |
| 依赖 | 00-framework；15-recharge `wallet`（PrepaidPolicy、支付 / 鉴权 / 会员适配层）；09-wechat-pay（支付、退款、钩子）；22-admin-rbac；01-identity |
| 被依赖 | 门禁 / 预约 / 课程（调用 `consume`、`reverse_use`、`check_usable`）、16/17/18（`issue_gift`、`lottery.prize_won` 延期） |

## 2 职责与边界

| 负责 | 不负责 |
|---|---|
| 卡种（次卡 count、期限卡 period、时段卡 time_slot）与预付限额 | 储值卡（15 wallet） |
| 购卡单、支付成功发卡、后台发卡 / 赠卡、活动赠卡 | 支付渠道（09） |
| 使用（入场 / 上课）与冲正、使用记录与只追加流水 | 门禁设备授权下发、课程排期 |
| 冻结（请假）、转让、挂失补办、停用、后台调整 | 发票开具 |
| 退卡报价、申请、审批、原路 / 线下退款、到期与提醒 | 资金存管与监管上报 |

## 3 数据模型

| 表 | 关键字段 | 约束 / 索引 |
|---|---|---|
| `member_card_products` | store_id, name, kind, price_cents, original_price_cents, sessions, gift_sessions, period_days, gift_days, valid_days, slot_windows_json, activation_mode(on_purchase/on_first_use/on_date), activation_date, activation_deadline_days, renew_stack, store_scope(single/multi/all), store_ids_json, store_ids_key, per_member_limit, sale_start_at/end_at, status, goods_kind, transferable, transfer_fee_cents, max_transfers, transfer_cooling_days, freezable, freeze_min/max_days, freeze_max_times, freeze_total_max_days, refund_fee_bp, entry_cooldown_minutes, terms_text, sort, deleted_at | 软删除 |
| `member_card_orders` | order_no, kind(purchase/transfer_fee), user_id, store_id, product_id, card_id, target_user_id, amount_cents, status(pending/paid/closed), payment_id, out_trade_no, paid_at, expires_at, needs_manual, product_snapshot_json, goods_kind, invoice_tax_code(601 / 转让费为空), invoice_note | order_no 唯一 |
| `member_card_cards` | card_no, user_id, product_id, store_id, kind, status, prev_status, source(purchase/manual/gift), issue_ref, order_id, payment_id, paid_cents, original_price_cents, paid_at, lpr_bp, paid_units, gift_units, total_units, sessions_remaining, sessions_used, use_count, starts_at, ends_at, activated_at, activation_deadline_at, 适用门店 / 时段 / 防借卡间隔快照, freeze_count, frozen_days_total, transfer_count, last_transfer_at, last_used_at, holder_name, holder_phone_masked, holder_phone_hash, product_snapshot_json, terms_snapshot, version | card_no、issue_ref 唯一；剩余 / 已用 CHECK ≥ 0；(user_id, status)、(status, ends_at) |
| `member_card_usages` | card_id, user_id, store_id, ref, units, sessions_after, status(done/reversed), operator, used_at, reversed_at | ref 唯一 |
| `member_card_ledger`（只追加） | card_id, user_id, kind, delta_sessions, delta_days, sessions_after, ends_at_after, status_after, ref, store_id, operator, note | (ref, kind) 唯一 |
| `member_card_freezes` | card_id, start_at, planned_end_at, planned_days, actual_end_at, actual_days, status(active/ended), reason, operator | — |
| `member_card_transfers` | card_id, from_user_id, to_user_id, fee_cents, fee_order_id, fee_collected(none/online/offline/waived), sessions_remaining, ends_at | — |
| `member_card_refunds` | refund_no, card_id, user_id, cause, rule, refund_cents, interest_cents, fee_cents, route, status, breakdown_json, due_at, submitted_at, channel_refund_id, submit_error | refund_no 唯一 |
| `member_card_reminders` | card_id, days_before, ends_at_key | 三列唯一（提醒去重） |

## 4 业务规则

1. 卡种：kind ∈ count / period / time_slot（储值 → 15）；原价 ≥ 实付价；次卡用 sessions，期限 / 时段卡用 period_days；时段卡至少一个窗口（周几 + HH:MM，结束 ≤ 开始表示跨午夜）；single 门店恰一个、multi ≥ 1、all 不限。
2. 预付限额（PrepaidPolicy，按门店）：期限 / 时段卡 ≤ `period_max_cents` 且 总天数 ≤ `period_max_days`；次卡 ≤ `count_max_cents` 且 总次数 ≤ `count_max_sessions`；赠送 / 付费单位 ≤ `gift_ratio_max_bp`；购买时再校验续约后同类剩余单位 ≤ 上限与会员合计（储值本金 + 卡未兑付价值）≤ `member_total_max_cents`。
3. 购卡：在售、售卖窗、门店适用、限购（持有 + 未过期待支付单）、免费卡种只能门店发放、`LUZ_STOP_PREPAID`；购卡单先提交再调起支付；保存卡种与条款快照。
4. 发卡：09 同事务钩子（主）/ `payment.succeeded` 事件（补偿）；`issue_ref` 唯一（`order:{id}` / 后台键 / `gift:{ref}`）保证只发一张；金额不符 → 人工；`needs_manual=true` 的支付事件不发卡。
5. 生效：购买即生效（门店时区本地日算第 1 天，到期 = 第 N 天结束）；首次使用激活（`activation_deadline_days` 到期自动以截止时刻激活）；指定日期；期限卡续购（`renew_stack`）接在同卡种最晚到期之后，未到开始时间使用 → `card_not_activated`。
6. 使用 `consume(card, store, at, ref)`：同 ref 返回首次结果（ref 属于其他卡 4090）；检查顺序与原因：冻结 / 挂失 / 停用 / 退款中 / 已退 / 过期 / 用完（4224）→ 未到开始 → 过期 → 门店不适用 → 时段外（4221，门店时区）→ 防借卡间隔；次卡条件扣减 `remaining >= 1`，用完转 `used_up`；只追加流水。
7. 冲正 `reverse_use(ref)`：使用记录 done → reversed 条件流转；次卡回补并 `used_up → active`；退款中 / 已退的卡不可冲正；重复冲正返回原记录。
8. 冻结：仅 active、已生效、有到期日的卡；天数 ∈ [最小, 最大]；次数 < 上限；累计天数 ≤ 上限；冻结即到期日顺延计划天数；提前解冻按实际天数（不足 1 天按 1 天、至少 1 天）退回未用天数；到期任务自动解冻（保留全部顺延）。
9. 转让：卡种允许、状态 active / inactive、未过期、次数 < 上限、冷却期（购买或上次转让起 N 天）、受让人须为已注册会员（会员 id 或手机号）、不能转给自己、受让人合计限额；顾客发起且有手续费 → 生成手续费单（应税服务，不用 601）支付后完成；后台发起手续费线下收取；就地更名（卡号、权益、流水不变）。
10. 挂失补办：active / inactive / used_up / expired 可挂失；挂失中不可使用；补办换新卡号并恢复原状态，权益全部保留；可解挂。
11. 停用 / 启用：后台，原因必填，审计；冻结中、退款中、已退不可停用。
12. 退卡报价（`rules.quote_card_refund`）：七日内且未使用（use_count = 0）→ 全额实付；经营者原因：已兑付 = ⌊实付 × 已用单位 / 总单位（含赠送）⌋，可退 = 实付 − 已兑付 + LPR 利息；消费者原因：已兑付 = min(实付, ⌊原价 × 已用单位 / 付费单位⌋)，可扣手续费 `refund_fee_bp`；期限卡已用天数 = 总天数 − ⌈剩余⌉（扣除冻结中未过去的天数，偏向消费者）；赠送单位不折现；已过期的卡消费者原因不予退款（第二十二条），经营者原因可退。
13. 冻结中的卡可申请退卡（不得限制解除合同）：先按当天结束冻结再报价。申请即 `refunding`（不可使用、不可冲正），顾客申请默认待审批；后台发起即审批；驳回 / 失败恢复原状态；成功 `refunded`（终态）、剩余次数清零。
14. 原路退款在审批事务提交后提交 09 `create_refund`（business_ref = `member_card_refund:<退款单号>`，金额 ≤ 实付，利息另行线下支付），每笔只提交一次；后台发卡 / 赠卡、转让过的卡、超过 365 天、只有利息可退 → 线下退款，财务确认。
15. 退款时限：七日全额 5 天、其他 15 天（PrepaidPolicy），超期发 `member_card.refund_overdue`。
16. 后台调整：原因必填、审计；次数 |Δ| ≤ 50、天数 |Δ| ≤ 366（可配）；不得为负；计入赠送单位；自动修正 used_up / expired / active。
17. 到期任务：active / used_up 且到期 → expired；提醒：剩余天数进入 7 / 3 / 1 天档各发一次（到期日变化后重新计）。
18. 抽奖奖品 `member_card_days`：给 `ref_id` 卡（须属该会员且可用）或会员当前到期最晚的卡延长 quantity 天；按 delivery_key 去重；结果回执 `lottery.ack_delivery` 或事件 `lottery.delivery_result`。

## 5 状态机与流程

```
inactive ─(首次使用 / 激活截止)→ active ⇄ frozen（请假）
active ⇄ used_up（用完 / 调整、冲正回补）     active|used_up ─(到期)→ expired ─(调整延期)→ active
active|inactive|used_up|expired ⇄ lost（挂失 / 解挂 / 补办）
非终态（除 frozen/refunding）⇄ disabled（停用 / 启用，恢复 prev_status）
active|inactive|frozen*|used_up|expired|lost ─(申请退卡)→ refunding ─(成功)→ refunded（终态）
                                               refunding ─(驳回 / 失败)→ prev_status
```
（* 冻结先结束再进入 refunding。）非法流转 4090；终态不可变。

购卡：`POST orders`（提交）→ `start_payment` → 09.create_payment（自行提交）→ 回调 → 09 钩子发卡（或事件补偿）。退卡：申请 → 审批 → `submit_refund` → 09.create_refund → 钩子 / `refund.*` 事件回写。

## 6 接口

顾客端 `/api/v1/app/member-cards`：

| 方法 | 路径 | 幂等 | agent | 说明 |
|---|---|---|---|---|
| GET | `/products?store_id=&kind=` | — | yes | 门店可购卡种（游标） |
| GET | `/products/{id}` | — | yes | 卡种详情（含条款） |
| POST | `/orders` | 必须 | 需确认 | 购卡单 + pay_params |
| POST | `/orders/{id}/pay` | 天然幂等 | — | 继续支付（购卡 / 转让手续费） |
| GET | `/cards`、`/cards/{id}`、`/cards/{id}/ledger` | — | yes | 我的卡（游标）、详情（条款、退款）、流水（游标） |
| POST | `/cards/{id}/freeze`、`/unfreeze` | 必须 | 需确认 | 请假 / 提前恢复（`MEMBER_CARD_APP_FREEZE`） |
| GET | `/cards/{id}/refund-quote` | — | yes | 消费者原因报价 |
| POST | `/cards/{id}/refund-request` | 必须 | 需确认 | 申请退卡 |
| POST | `/cards/{id}/transfer` | 必须 | 需确认 | 转让（to_member_id 或 to_phone 二选一） |
| POST | `/cards/{id}/report-lost` | 必须 | — | 挂失 |

后台 `/api/v1/admin/member-cards`（经 `wallet.require_permission` 薄别名直接用 `admin_rbac.require_permission(code, fresh=…)`；† = fresh=True，否则 4011 `reauth_required`）：

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET / POST | `/products`；GET / PUT / DELETE `/products/{id}`；POST `/products/{id}/on-sale|off-sale` | member_card:read / member_card:product | 卡种 CRUD 与上下架（列表：status、kind、store_id、价格区间、关键词、排序） |
| GET | `` （卡列表）、`/export` | member_card:read / member_card:export | 筛选 status、kind、product_id、store_id、user_id、source、ends_at 区间、`expiring_within_days`、`phone`（哈希精确）、keyword（卡号 / 姓名 / 脱敏手机号）；CSV 脱敏 |
| GET | `/{id}`、`/{id}/ledger` | member_card:read | 详情（快照、流水、退款）/ 流水分页 |
| POST | `/issue` † | member_card:issue | 后台发卡（manual，线下实付）/ 赠卡（gift） |
| POST | `/{id}/consume`、`/consume-by-no`；`/usages/reverse` † | member_card:consume | 核销 / 扫卡号核销 / 冲正 |
| POST | `/{id}/adjust` † | member_card:adjust | 调次数 / 天数 |
| POST | `/{id}/freeze`、`/unfreeze` | member_card:freeze | 冻结 / 解冻 |
| POST | `/{id}/transfer` | member_card:transfer | 转让（可标手续费线下已收） |
| POST | `/{id}/report-lost`、`/cancel-lost`、`/replace` | member_card:lost | 挂失 / 解挂 / 补办 |
| POST | `/{id}/disable`、`/enable` | member_card:disable | 停用 / 启用 |
| GET | `/{id}/refund-quote?cause=` | member_card:refund | 报价 |
| POST | `/{id}/refunds` † | member_card:refund | 发起退卡（即审批） |
| GET | `/refunds`、`/orders` | member_card:read | 退款单 / 购卡单列表 |
| POST | `/refunds/{id}/approve` †、`/reject` †、`/offline-complete` † | member_card:refund | 审批 / 驳回 / 线下确认 |

写接口 `Idempotency-Key` 必填；员工门店范围外的卡 4040、门店参数越界 4031；后台写操作写审计。

## 7 事件

发出：

| 主题 | payload |
|---|---|
| `member_card.issued` | card_id, user_id, product_id, source, store_id, paid_cents |
| `member_card.activated` / `used` / `use_reversed` | card_id, user_id?, store_id?, ref?, sessions_remaining? |
| `member_card.frozen` / `unfrozen` | card_id, user_id, days / actual_days, until / auto |
| `member_card.transferred` | card_id, from_user_id, to_user_id, fee_cents |
| `member_card.lost` / `replaced` / `expired` / `extended` | card_id, user_id（extended 另含 days, source, draw_id） |
| `member_card.expiring` | card_id, user_id, days_left, tier, ends_at |
| `member_card.refund_created` / `refund_requested` | refund_id, card_id, user_id, refund_cents；requested 另含 business_ref, biz_type, biz_id, payment_id, amount_cents, interest_cents, route, reason（通知用） |
| `member_card.refunded` | refund_id, card_id, user_id, refund_cents, interest_cents, invoice_action=red_reverse |
| `member_card.refund_failed` / `refund_rejected` / `refund_abnormal` / `refund_overdue` | refund_id, card_id?, reason? |
| `member_card.payment_mismatch` / `order_closed` / `transfer_fee_orphan` | order_id 及金额 |
| `member_card.terminated` | card_id, user_id, reason=order_refund, after_sale_id, order_id, order_item_id, refund_cents, status_before |
| `member_card.order_refund_conflict` | card_id, user_id, after_sale_id, order_id（卡正在本模块退卡流程中，售后又退了购卡订单 → 人工） |
| `lottery.delivery_result`（无 `lottery.ack_delivery` 时） | draw_id, delivery_key, ok, ref, error |

消费（`member_card/events.py` `SUBSCRIPTIONS`，幂等、不提交）：`payment.succeeded`、`refund.succeeded`、`refund.failed`、`refund.abnormal`（payload 见 09 MODULE.md §7）、`lottery.prize_won`（draw_id, member_id, prize_kind, ref_id, quantity, delivery_key …）、`after_sale.completed`（10-after-sales：lines[].fulfil_ref = `member_card:{id}` 的行 → 外部退款已完成，终止该卡：状态 refunded、剩余次数清零、卡流水 kind=order_refund ref=`after_sale:{售后单}:{卡}`；幂等 = 事件去重 + 流水唯一 + 状态条件 UPDATE）。经订单售卡的退款走 08/10（金额上限由 `quote_refund` 按已用量给出），本模块自己的退卡流程用于门店直售 / 线下卡。另向 09 注册 `register_on_paid("member_card")`、`register_on_refunded("member_card_refund")`、`register_payable("member_card")`。

## 8 并发与一致性

- 次卡扣减：`UPDATE … SET sessions_remaining = sessions_remaining - 1 WHERE status='active' AND sessions_remaining >= 1 AND 有效期内`；50 线程并发扣 10 次卡恰好成功 10 次、不为负、使用记录 10 条。
- 其他卡状态变化：按 version 乐观条件 UPDATE + 状态条件；冻结次数 / 天数上限写在 WHERE 里。
- 唯一约束兜底：usages.ref、ledger(ref, kind)、cards.issue_ref / card_no、reminders 三列。
- 外部调用不在业务事务内（先提交购卡单 / 退款审批，再调 09）；退款提交条件占位只提交一次。
- 事件消费者先登记 processed_events（不回滚会话）再处理；任务按业务键幂等。

## 9 配置

| 键 | 默认 | 说明 |
|---|---|---|
| `LUZ_PREPAID_POLICY` | conservative | 预设见 15-recharge §9 |
| `LUZ_STOP_PREPAID` | false | 停止售卡 |
| `LUZ_LPR_1Y_BP` | 300 | 一年期 LPR（合同成立时写入卡） |
| `MEMBER_CARD_PAY_TIMEOUT_MINUTES` | 15 | 待支付超时 |
| `MEMBER_CARD_APP_FREEZE` / `_APP_TRANSFER` | true / true | 顾客端自助冻结 / 转让 |
| `MEMBER_CARD_REFUND_REQUIRES_APPROVAL` | true | 顾客退卡需审批 |
| `MEMBER_CARD_REMINDER_DAYS` | 7,3,1 | 到期提醒档 |
| `MEMBER_CARD_ORIGINAL_REFUND_MAX_DAYS` | 365 | 超过转线下退款 |
| `MEMBER_CARD_ADJUST_MAX_SESSIONS` / `_DAYS` | 50 / 366 | 单次调整上限 |
| `MEMBER_CARD_REPLACE_FEE_CENTS` | 0 | 补卡工本费（只记录） |

卡种级规则（冻结、转让、手续费、防借卡、激活、适用门店、限购）在卡种上配置，并随卡快照（改卡种不影响已售卡）。

## 10 错误码

| 场景 | code | data.reason |
|---|---|---|
| 参数、缺幂等键、未知筛选、转让对象二选一 | 4001 | — |
| 卡 / 卡种 / 订单 / 会员不存在、他人或范围外的卡 | 4040 | — |
| 状态不允许（冻结中再冻结、非挂失补办等）、并发变化、ref 属于其他卡 | 4090 | card_status / card_changed / card_refunding |
| 冻结 / 过期 / 挂失 / 停用 / 退款中 / 已退 | 4220 | card_frozen / card_expired / card_lost / card_disabled / card_refunding / card_refunded |
| 未到生效、门店不适用、防借卡、不支持冻结 / 转让、冷却期、转给自己、过期消费者原因退款、无可退金额、暂停收预付 | 4220 | card_not_activated / store_not_applicable / entry_cooldown / freeze_not_allowed / freeze_not_applicable / freeze_days_out_of_range / transfer_not_allowed / transfer_cooling / transfer_to_self / expired_customer_no_refund / nothing_refundable / prepaid_stopped |
| 时段外 | 4221 | outside_time_slot |
| 次数用完、调整后为负 | 4224 | card_used_up / sessions_negative |
| 限购、冻结次数 / 天数、转让次数、预付限额、调整上限 | 4225 | per_member_limit / freeze_times_exceeded / freeze_days_exceeded / transfer_times_exceeded / prepaid_amount_cap / prepaid_units_cap / prepaid_units_cap_after_renewal / member_total_cap / gift_ratio_exceeded / adjust_cap |
| 支付上游失败 | 5020 | — |

## 11 测试清单（52 项）

| 文件 | 覆盖 |
|---|---|
| `test_member_card.py` | 卡种校验与限额；上下架审计；购卡分步支付与 Payable；事件发卡幂等；金额不符与迟到支付；限购 / 门店 / 停收 / 上游失败；续约与合计上限；续购叠加与未生效；后台发卡 / 赠卡幂等；次卡扣减、重放、用完、冲正；ref 归属；六种状态拒绝原因；多门店与时段卡（门店时区、跨午夜）；防借卡；首次使用激活与截止自动激活；50 线程并发扣次；冻结顺延与提前解冻重算；冻结上限；自动解冻；转让（手机号、自己、不存在、次数、冷却、不可转）；手续费支付后转让、钩子路径抛错触发自动退款、事件路径标人工；挂失补办；停用；退卡公式（七日、原价、折扣 + 赠送摊薄 + 利息、手续费）；期限卡已用天数；过期退款规则；退卡成功 / 驳回 / 失败 / 异常、冻结中退卡；转让卡线下退；免审批；退款提交一次、重试、规则拒绝；调整；到期与 7/3/1 提醒；超期与任务注册；订阅不提交与去重；抽奖延期（事件回执与 ack_delivery）；售后完成终止订单售卡（幂等、退卡流程中冲突转人工） |
| `test_member_card_api.py` | 4010 / 4030 / 4001、信封、幂等重放、顾客端全流程、后台列表筛选（即将到期、关键词、手机号、未知参数）、CSV 脱敏、核销 / 冲正 / 调整 / 挂失补办 / 退卡 / 发卡、门店范围隐藏 |
| `test_with_wechat_pay.py` | 与真实 09 假渠道联调：购卡 → 回调钩子发卡 → 事件重复 → 退卡提交 → 退款回调；储值同样流程 |

## 12 前端对接

| 页面 | 接口 | 行为 |
|---|---|---|
| 门店卡种列表 / 详情 | products | 显示价格、原价、次数 / 天数（含赠送）、适用门店与时段、条款全文与退款规则 |
| 购卡 | `POST orders` → `wx.requestPayment`；`/orders/{id}/pay` 重试 | 支付结果以 09 结果页为准，成功后跳我的卡 |
| 我的卡 | cards、cards/{id}、ledger | 状态文案按 status；显示剩余次数 / 天数、到期日、冻结记录 |
| 请假 | freeze / unfreeze | 天数选择受卡种 min/max；显示顺延后的到期日 |
| 转让 | transfer | 输入对方手机号；有手续费时进入支付 |
| 退卡 | refund-quote → refund-request | 展示规则（七日全额 / 按原价扣已用）、可退金额、预计到账时限 |
| 后台核销 | consume-by-no / usages/reverse | 4220/4221/4224 的 `data.reason` 直接映射提示 |

## 13 定制点

- 配置项：§9；卡种字段（冻结、转让、手续费、防借卡、激活方式、续购叠加、适用门店、限购、退款手续费）。
- LPR：`LUZ_LPR_1Y_BP` 默认 300（3.00%，示例值）；经营者原因退卡利息按发卡时写入的 `lpr_bp`，上线前与每月 LPR 公布后更新配置。
- 扩展点：`wallet.set_policy_resolver`（按门店的预付限额，含上海 / 北京预设）、`register_outstanding_provider`（本模块已注册会员卡未兑付价值）、`wallet.set_payment_port`、`set_member_resolver`（会员与手机号查询；默认 01-identity `find_user_by_phone`）、`set_store_timezone_resolver`（门店时区）、`check_usable` / `consume` / `reverse_use` 供门禁与预约模块调用、`issue_gift` 供营销模块调用、`lottery.prize_won` 延期。
- 不包含：储值卡（15）；线上内容会员与小程序虚拟支付；未来日期开始的预约冻结与冻结审批流；卡密兑换卡；按次结算给教练的分账；门禁设备授权同步；资金存管与监管平台上报；自动续费；发票开具；借转让之名多人共用的行为识别（只提供防借卡间隔）。
