# 模块：16-coupon（优惠券）`coupon`

## 0 官方来源

| 主题 | 官方页面 | 本模块用到的结论 | 核对日期 |
|---|---|---|---|
| 促销规则公示 | 规范促销行为暂行规定（总局令第32号）https://www.gov.cn/zhengce/zhengceku/2020-11/06/content_5557730.htm | 第六条优惠承诺应当履行；第七、二十条公示规则、期限与不利限制条件；第十四条礼券须公布兑换规则、使用范围、有效期；第二十二条折价办法须公开 → 模板生成 `rules_text` / `validity_text`；已发券保留规则快照；结束发放后已发券照常可用 | 2026-10-28 |
| 按约定折抵 | 明码标价和禁止价格欺诈规定（总局令第56号）https://www.gov.cn/gongbao/content/2022/content_5699926.htm | 第十九条：以礼券、代金券折抵价款时拒不按约定折抵属价格欺诈 → 核销按领取时快照计算，报价与锁券同一函数；第十六条划线价规则属 03-catalog，不在本模块 | 2026-10-28 |
| 退货时券的处理 | 网络购买商品七日无理由退货暂行办法 https://www.gov.cn/zhengce/2020-11/03/content_5723721.htm | 第十五条：以优惠券支付的部分按原方式退回，另有约定除外 → 默认策略 `full_refund` 退券；第十七条：部分退货按商品价格重新计算 → 锁券时保存行分摊，售后按行实付退款 | 2026-10-28 |
| 小程序退款方式 | 微信小程序运营规范 §2.14 第 6 条 https://developers.weixin.qq.com/miniprogram/product/ | 退款原路退回，不得强制退为余额、储值卡、积分、代金券 → 本模块不提供“退款转券”；补偿券（compensation 渠道）不能替代退款 | 2026-10-28 |
| 有奖销售 | 反不正当竞争法（2025）第十一条 https://www.cnipa.gov.cn/art/2026/5/20/art_104_206437.html | 券作抽奖奖品时按价值计入最高奖 5 万元（由 18-lottery 校验）；本模块只负责按中奖记录发券 | 2026-10-28 |
| 并发控制 | PostgreSQL 事务隔离 https://www.postgresql.org/docs/current/transaction-iso.html | Read Committed 下条件 UPDATE 会对新版本重新求 WHERE → 库存、状态一律条件 UPDATE + rowcount | 2026-10-28 |
| 发件箱 | Transactional Outbox https://microservices.io/patterns/data/transactional-outbox.html | 至少一次投递，消费者必须幂等 → `mark_processed` + 业务键 ref_key | 2026-10-28 |
| CSV 注入 | OWASP CSV Injection https://owasp.org/www-community/attacks/CSV_Injection | 导出单元格转义（框架 export_csv / safe_cell） | 2026-10-28 |

参考设计（非官方，R7）：芋道（规则快照、使用时复核有效期、退回时已过期置 expired、总量 `take_count + n ≤ total`）、CRMEB（领取期与使用期分开、限领 ≤ 总量）、mall（多券按行分摊）、002Gym（关单释放未判断过期的缺陷）。三家开源的“每人限领”都是先查后插，本模块用槽位唯一约束修复。

## 1 状态与依赖

| 项 | 内容 |
|---|---|
| 状态 | 标准（可复用）；测试在 `code/tests/`（规则、领取、并发 50 线程、订单锁券 / 核销 / 解锁 / 退券、任务、事件、HTTP） |
| 代码 | `16-coupon/code/coupon/`：models / rules / service / events / jobs / api / deps / config |
| 依赖 | luzback（db、errors、idempotency、outbox、audit、listing、export、statemachine、scheduler、crypto）；17-campaign `pricing`（计价与分摊，只读使用） |
| 依赖 | 22-admin-rbac（后台鉴权：`admin_rbac.require_permission` 正式依赖 + 门店范围，必备）；可选：01-identity（会员令牌与会话校验、`find_user_by_phone` 手机号查找）、14-membership（`member_profile` 人群画像）、18-lottery（`ack_delivery` 回执） |

## 2 职责与边界

| 负责 | 不负责 |
|---|---|
| 券模板（满减 / 无门槛、折扣封顶、兑换券）、适用范围、叠加组、有效期、库存、限领、人群、渠道 | 订单金额汇总（08-order 调 `price_with_coupons` / `lock`） |
| 领取、后台批量发放、事件发放（注册礼、抽奖、补偿）、兑换码 | 活动满减计算（17-campaign，作为 `campaign_promotions` 传入） |
| 下单可用券与原因、推荐券、锁定 / 核销 / 解锁 / 退券、来源收回 | 会员折扣与券的取舍（订单按 `stack_with_member` 决定） |
| 过期任务、到期提醒事件、统计、后台列表与导出 | 推送 / 订阅消息（20-notification 消费 `coupon.expiring_soon`） |

## 3 数据模型

| 表 | 关键字段 | 约束 / 索引 |
|---|---|---|
| `coupon_templates` | store_id?, name, description, type(amount_off/percent_off/exchange), amount_cents, rate_bp(实付比例，100 的倍数), max_discount_cents?, exchange_qty, threshold_cents, scope_type(all/categories/products/product_kinds), scope_ids_json, exclude_product_ids_json, stack_group, stack_with_campaign, stack_with_member, valid_type(fixed/relative), use_start_at/use_end_at, valid_days, start_delay_days, claim_start_at/claim_end_at, total_stock?(空=不限), issued_count, used_count, per_member_limit(0=不限), per_member_daily_limit(0=不限), audience_json, channels_json, trigger_topic?, status, version, published_at, created_by/updated_by, deleted_at | CHECK issued ≥ 0、used ≥ 0、`total_stock IS NULL OR issued ≤ total_stock`；ix status、trigger_topic |
| `user_coupons` | user_id, template_id, template_version, name, rules_json(快照), slot, claim_day(门店时区日期), day_slot, status, channel, source_ref, claimed_at, valid_from, valid_to, order_id, discount_cents, alloc_json(行分摊), locked_at, used_at, returned_at, expired_at, revoked_at, revoke_reason, reminded_at | 唯一 (template_id, user_id, slot)、(template_id, user_id, claim_day, day_slot)；ix (user_id, status)、order_id、(status, valid_to) |
| `coupon_issue_logs` | ref_key, template_id, user_id, channel, source_ref, user_coupon_id | ref_key 唯一；ix source_ref |
| `coupon_issue_batches` / `_items` | template_id, channel, reason, ignore_limit, total/issued/failed, status；明细 batch_id, user_id, status, error | 明细 (batch_id, user_id) 唯一 |
| `coupon_code_batches` / `coupon_codes` | 批次 name、count、exported_at；码 code_hash(HMAC)、code_enc(密文)、status(unused/used/void)、used_by_user_id | code_hash 唯一；不存明文 |
| `coupon_code_attempts` | user_id, window, failures | (user_id, window) 唯一 |

## 4 业务规则

1. 模板校验：percent_off 的 `rate_bp` 为 100–9900 且为 100 的倍数（8500 = 八五折）；兑换券必须限定范围；固定有效期 `use_start < use_end` 且 `claim_end ≤ use_end`；相对有效期 1–3650 天、延迟生效 0–365 天；每日限领 ≤ 每人限领；`trigger_topic` 需含 event 渠道。不合法 4001。
2. 领取 / 发放顺序：ref_key 去重 → 模板 active → 渠道允许 → 自助渠道（claim_center、code）检查领取期 → 固定有效期未结束 → 人群（claim_center、code、event）→ 每人限领、自助渠道每日限领 → 条件扣库存 → 插入（槽位唯一裁决并发）→ 发放日志 → `coupon.claimed`。
3. 库存：`UPDATE … SET issued_count + 1 WHERE status='active' AND (total_stock IS NULL OR issued_count < total_stock)`；0 行 → 模板仍 active 则 4225 sold_out，否则 4220 not_active。
4. 每人限领：slot = 已有张数 + 1，超过上限 4225；并发抢同一槽位由唯一约束拒绝（4225 或可重试的 4090），调用方回滚后库存一并回滚。每日限领同理（claim_day 为门店时区日期）。
5. 有效期：相对 = 领取时刻 + 延迟天数起，持续 valid_days 天；固定 = 模板区间。区间为 [valid_from, valid_to)。
6. 人群：all；new_members（注册 N 天内）、levels、tags 需要会员画像钩子；未配置钩子时拒绝（audience_unavailable）。后台、补偿、抽奖渠道不检查人群。
7. 规则快照：领取时把使用规则写入 rules_json；之后修改模板只影响新发的券。
8. 发布后编辑：类型、门店、有效期类型不可改；库存不得低于已发；每人 / 每日限领只能放宽；领取期只能延长、开始时间不能推后；使用规则可改（version + 1）。`apply_to_issued=true` 时仅当所有修改对顾客有利（面额不减、折扣不升、封顶不降、门槛不升、范围不缩、叠加不收紧、固定有效期不缩短）才同步给 unused 的券，否则 4220 并列出字段。编辑带 `version` 乐观锁，冲突 4090。
9. 状态：draft → active（发布，校验领取期与有效期未结束）→ paused ↔ active → ended（终态）。只有草稿可删除（软删）。结束发放后已发出的券照常可用。
10. 抵扣（经 17-campaign `pricing.quote`）：先算活动，再算券；门槛按活动后、范围内行的金额判断；amount_off 抵扣 = min(面额, 范围余额)；percent_off = 范围余额 − 范围余额 × 折扣 // 100（应付向下取整，优惠向上取整到分），封顶 max_discount_cents；exchange = 范围内最便宜的 exchange_qty 件单价之和。`stack_with_campaign=false` 的券跳过已享活动的行。抵扣按行金额用最大余数法分摊，合计严格相等。
11. 叠加：stack_group 为空的券不与任何券同用；同组互斥；不同非空组可叠加；一单最多 `[coupon] max_coupons_per_order` 张。冲突 4220 stack_conflict。
12. 可用券：unused 且在有效期内，逐张计算抵扣；不可用给出原因（not_started、expired、no_scope_items、campaign_conflict、below_threshold + “还差X元可用”、no_discount）。推荐：抵扣最大，同额先到期。
13. 锁定：下单事务内 `unused → locked`（条件 UPDATE，WHERE 含 user_id 与有效期），写 order_id、discount_cents、alloc_json；同一订单重复锁定幂等；其他订单 4220 unavailable 或 4090。
14. 核销：支付成功 `locked → used`，模板 used_count + n；锁定时已校验有效期，支付时不因到期拒绝；重复回调幂等。
15. 解锁：关单 / 超时 `locked → unused`；若此时已过有效期则 `locked → expired`（修复 002Gym 把过期券放回可用的问题）。
16. 退券：策略 full_refund（默认，仅整单全额退款）/ any_refund / never；`[coupon] return_window_hours > 0` 时核销超过该时长不退；仍在有效期 → unused，已过期 → expired；used_count − n；重复调用幂等。只退券本身，退款资金一律原路退回。
17. 来源收回：`revoke_by_source(source_ref)` 把该来源发出的 unused 券置 revoked；锁定 / 已用的保留并返回，由来源模块决定是否阻止退款。后台作废单张 unused 券须填原因（审计）。
18. 兑换码：12 位（去掉 I O 0 1 的 32 字符字母表，约 60 位熵），只存 HMAC 与密文；一码一次（条件 UPDATE）；不存在 / 已用 / 作废统一 4040；同一会员每窗口失败达上限后 4290（含 Retry-After），失败计数在业务事务回滚后单独提交；发券失败（限领、售罄）时码保持未使用。生成数量不超过剩余库存减未兑换码数。
19. 批量发放：创建时校验模板 active、渠道（admin / compensation）、剩余库存，原因必填（审计）；任务逐个会员一个事务，ref_key `batch:{批次}:{会员}` + 明细唯一约束保证幂等；失败写原因；`ignore_limit` 仅用于补偿。
20. 事件发放：见 §7；业务原因失败写 `coupon.issue_failed`，不重试；并发冲突抛出由 worker 重试。
21. 过期任务：unused 且 valid_to ≤ now → expired（批量 UPDATE … RETURNING，每张写事件）；锁定中的券不处理（由解锁处理）。提醒任务：unused、未提醒、将在 N 小时内到期 → `coupon.expiring_soon`（reminded_at 去重）。
22. 统计：发放数、各状态数、各渠道数、核销数、核销抵扣总额、带动订单数、核销率（万分比）、剩余库存。
23. 事务：service 用例不 commit / rollback；接口层 `run_idempotent` 提交；订单在自己的事务里调用 lock / redeem / unlock / return_on_refund。

## 5 状态机

用户券（COUPON_FLOW，非法流转 4090，终态不可变）：

| 从 | 到 | 触发 | 失败分支 |
|---|---|---|---|
| unused | locked | 下单 lock | 已被其他订单锁定 / 已过期 → 4220 或 4090，订单事务回滚 |
| locked | used | 支付成功 redeem | 已解锁 → 0 行，忽略（返回实际核销的券） |
| locked | unused | 关单 unlock（仍有效） | 已核销 → 0 行，不动 |
| locked | expired | 关单 unlock（已过期） | — |
| used | unused / expired | 退款 return_on_refund（按策略与有效期） | 策略不退 → kept |
| unused | expired | 过期任务 | — |
| unused | revoked | 来源收回 / 后台作废 | 非 unused → 4090 / kept |

终态：expired、revoked。模板（TEMPLATE_FLOW）：draft → active → paused ↔ active → ended（终态）。

下单流程：报价 `price_with_coupons`（与下单同一计算）→ 下单事务 `lock`（同事务扣库存、写订单）→ 支付回调 `redeem` → 关单 `unlock` / 退款 `return_on_refund`。并发：支付与关单同时到达时条件 UPDATE 只让一个生效。

## 6 接口

| 方法 | 路径 | 鉴权 | 幂等 | 权限码 | agent | 说明 |
|---|---|---|---|---|---|---|
| GET | `/api/v1/app/coupon-templates` | 可选 | — | — | read | 领券中心，游标（type、store_id，sort=published_at）；登录后带 claimed_by_me / can_claim |
| GET | `/api/v1/app/coupon-templates/{id}` | — | — | — | read | 模板详情（规则与有效期文案） |
| POST | `/api/v1/app/coupon-templates/{id}/claim` | 会员 | 必须 | — | write | 领取；4225 限领 / 售罄 |
| GET | `/api/v1/app/coupons?tab=available\|used\|expired` | 会员 | — | — | read | 我的券，游标（template_id，sort=valid_to / claimed_at） |
| GET | `/api/v1/app/coupons/counts` | 会员 | — | — | read | 各页签数量 |
| GET | `/api/v1/app/coupons/{id}` | 会员 | — | — | read | 他人的券 4040 |
| GET | `/api/v1/app/coupons/available-for-cart` | 会员 | — | — | read | 按服务端购物车给可用 / 不可用（原因）/ 推荐；未配置购物车钩子 5030 |
| POST | `/api/v1/app/coupons/redeem-code` | 会员 | 必须 | — | no | 兑换码；4040 无效，4290 限流 |
| GET | `/api/v1/admin/coupon-templates` | 员工 | — | coupon:read | read | 列表：status、type、store_id(_null)、created_at / published_at 区间、issued_count / used_count 区间，keyword=名称，排序 created_at / published_at / issued_count / used_count |
| GET | `/api/v1/admin/coupon-templates/export` | 员工 | — | coupon:export | no | CSV（同筛选，审计） |
| POST | `/api/v1/admin/coupon-templates` | 员工 | 必须 | coupon:write | no | 创建草稿（审计） |
| GET | `/api/v1/admin/coupon-templates/{id}` | 员工 | — | coupon:read | read | 详情 |
| PATCH | `/api/v1/admin/coupon-templates/{id}` | 员工 | 必须 | coupon:write | no | 编辑（version 乐观锁，apply_to_issued） |
| POST | `/api/v1/admin/coupon-templates/{id}/publish\|pause\|resume\|end` | 员工 | 必须 | coupon:write | no | 状态流转（审计、事件） |
| DELETE | `/api/v1/admin/coupon-templates/{id}` | 员工 | 必须 | coupon:write | no | 仅草稿 |
| GET | `/api/v1/admin/coupon-templates/{id}/stats` | 员工 | — | coupon:read | read | 统计 |
| POST | `/api/v1/admin/coupon-issue-batches` | 员工 | 必须 | coupon:issue | no | 批量 / 补偿发放（异步，原因必填） |
| GET | `/api/v1/admin/coupon-issue-batches` · `/{id}` | 员工 | — | coupon:read | read | 批次列表（status、template_id、created_at，keyword=原因）/ 详情（含失败明细） |
| POST | `/api/v1/admin/coupon-code-batches` | 员工 | 必须 | coupon:code | no | 生成兑换码 |
| GET | `/api/v1/admin/coupon-code-batches` | 员工 | — | coupon:read | read | 批次列表 |
| GET | `/api/v1/admin/coupon-code-batches/{id}/export` | 员工 | — | coupon:code | no | 导出明文码（审计） |
| POST | `/api/v1/admin/coupon-code-batches/{id}/void` | 员工 | 必须 | coupon:code | no | 作废未使用的码 |
| GET | `/api/v1/admin/user-coupons` | 员工 | — | coupon:read | read | 会员券：status、template_id、user_id、channel、order_id、claimed_at / used_at / valid_to 区间、discount_cents 区间；keyword=券名 / 会员 id / 来源；keyword 为 11 位手机号时经 identity 哈希查找会员后按 user_id 过滤 |
| GET | `/api/v1/admin/user-coupons/export` | 员工 | — | coupon:export | no | CSV（审计） |
| POST | `/api/v1/admin/user-coupons/{id}/revoke` | 员工 | 必须 | coupon:write | no | 作废（原因必填，审计） |

### 6.1 模块间调用（`from coupon import ...`，全部**不 commit**，在调用方事务内执行，失败抛 AppError 由调用方回滚）

| 函数 | 调用方 / 时机 | 说明 |
|---|---|---|
| `price_with_coupons(db, user_id, coupon_ids, lines, campaign_promotions=(), shipping_cents=0, now=None, order_id=None) -> CouponPricing` | 08-order 报价 | 返回 coupon_cents、line_coupon_cents（行分摊）、coupons[{user_coupon_id, discount_cents, lines}]；不可用 4220（data.reason） |
| `lock(db, user_id, order_id, coupon_ids, lines, campaign_promotions=(), shipping_cents=0, now=None) -> CouponPricing` | 08-order 下单事务 | 与报价同一计算；写 order_items.coupon_alloc_cents 用 `line_coupon_cents`；同订单重复调用幂等 |
| `redeem(db, order_id, now=None) -> list[int]` | 支付成功处理（payment.succeeded 消费方） | locked → used，幂等 |
| `unlock(db, order_id, now=None) -> {"unlocked", "expired"}` | 取消 / 超时关单 | 已过期的直接 expired |
| `return_on_refund(db, order_id, full_refund, now=None, policy=None) -> {"returned", "expired", "kept"}` | 10-after-sales / 08-order 在 `refund.succeeded` 后、确认整单是否全额退款时调用 | 按 `[coupon] return_policy` |
| `revoke_by_source(db, source_ref, reason, now=None)` | 赠券来源订单退款 | 返回 revoked 与 kept（已用的） |
| `issue(db, template_id, user_id, ref_key, channel=..., source_ref=None, enforce_limits=True, now=None)` | 会员、补偿、生日礼 | ref_key 幂等 |
| `available_coupons / best_coupon(db, user_id, lines, campaign_promotions=(), ...)` | 确认订单页 | 可用 / 不可用原因 / 推荐 |
| `CartLine(sku_id, product_id, qty, unit_price_cents, category_id=None, product_kind="", campaign_priced=False)`、`from_quote_line(QuoteLine)` | 构造行 | 价格必须来自服务端 |

权限码声明：`coupon.deps.PERMISSIONS`（coupon:read / write / issue / code / export），导入时登记到 admin_rbac；角色可授予 `coupon:*`。

门店范围（admin_rbac.store_scope）：门店员工只能新建 / 修改 / 发放 / 生成兑换码 / 作废本门店模板及其券（越界 4031）；全局模板（store_id 为空）只有全门店员工能改，门店员工可查看；模板与会员券列表 / 导出只含本门店与全局模板。

## 7 事件

发出（payload 只含 id 与必要字段）：

| 主题 | payload |
|---|---|
| coupon.claimed | user_coupon_id, user_id, template_id, channel, source_ref, valid_to |
| coupon.locked | user_coupon_id, user_id, order_id, discount_cents |
| coupon.redeemed | user_coupon_id, user_id, order_id, template_id, discount_cents |
| coupon.unlocked | user_coupon_id, user_id, order_id |
| coupon.expired | user_coupon_id, user_id, template_id?/order_id?, reason(validity_ended / expired_before_unlock) |
| coupon.returned | user_coupon_id, user_id, order_id, status(unused / expired), full_refund |
| coupon.revoked | user_coupon_id, user_id, source_ref? |
| coupon.expiring_soon | user_coupon_id, user_id, template_id, name, valid_to |
| coupon.issue_failed | template_id, user_id, ref, topic, reason |
| coupon.template_status_changed | template_id, from, status |
| lottery.delivery_result | draw_id, delivery_key, ok, ref, error（仅抽奖模块未装配、无法直接回执时） |

本模块不直接订阅退款事件（`refund.succeeded` / `refund.failed` / `refund.abnormal`，09-wechat-pay §7）：是否整单全额退款由订单 / 售后判断后调用 `return_on_refund`。

消费（`events.SUBSCRIPTIONS`，handler(db, payload, event_id)，不提交）：

| 主题 | 用到的字段 | 处理 |
|---|---|---|
| member.registered（01-identity） | user_id | 发 trigger_topic 为注册事件的模板；ref `registered:{模板}:{会员}` |
| lottery.prize_won | draw_id, member_id, prize_kind, ref_id, quantity, delivery_key | prize_kind=coupon：发 quantity 张模板 ref_id，ref `lottery:{delivery_key}:{n}`，不受限领约束；成功 `lottery.ack_delivery(ok=True, ref=券id列表)`，失败 ok=False + 原因 |
| coupon.issue_requested | template_id, user_id, ref, channel?, source_ref? | 通用发券入口（补偿、生日礼；基础包内暂无生产方，项目模块按此载荷发布） |

## 8 并发与一致性

| 场景 | 机制 |
|---|---|
| 超发 | 库存条件 UPDATE + CHECK；测试：50 线程、库存 10、25 人各点两次 → 恰好 10 张、每人 ≤ 1 |
| 同一会员并发多领 | 槽位 / 日槽位唯一约束；测试：20 线程、限领 2 → 恰好 2 张 |
| 同券并发用于两单 | 条件 UPDATE unused → locked；测试 20 单只成功 1 单 |
| 支付与关单竞争 | 两者都以 `status='locked' AND order_id=?` 为条件；测试 10 轮均只有一方生效，used_count 一致 |
| 模板暂停与领取竞争 | 扣库存 WHERE 带 status='active' |
| 重复请求 | 写接口 run_idempotent；发放 ref_key 唯一；批次明细唯一；事件 mark_processed |
| 调用方事务 | 用例不提交；测试：claim / lock 后调用方回滚，什么都不留下 |
| 后台并发编辑 | version 乐观锁 + 条件 UPDATE |

## 9 配置

运行参数写在 `backend/config/runtime/*.toml`，字段说明（类型、范围、能否热加载、是否下发客户端）在 `00-framework/code/luzback/runtime_config.py`；模块经 `luzback.module_config.ModuleConfig` 读取，热加载后下一次读取即生效。环境变量只放秘密与部署参数；迁移前的环境变量名仍设置着时，启动会打 `config.unknown_env` 警告并指出新键。测试用 `set_config(...)` 或 `luzback.testing.override_runtime(...)` 注入运行参数（写进一份测试用运行配置，测试结束撤销）。

### 9.1 环境变量（秘密与部署参数）

| 变量 | 默认 | 说明 |
|---|---|---|
| `COUPON_TIMEZONE` | LUZ_TIMEZONE / Asia/Shanghai | 每日限领与文案的时区 |
| `LUZ_FIELD_KEY` | 框架 | 兑换码 HMAC 与加密密钥 |

### 9.2 运行配置键

| 键 | 默认 | 范围 | 热加载 | 下发客户端 | 说明（含单位） |
|---|---|---|---|---|---|
| `[coupon] return_policy` | "full_refund" | never / full_refund / any_refund | 是 | 否 | 退款后是否退券：never 不退、full_refund 整单全额退款才退、any_refund 任何退款都退（原 `COUPON_RETURN_POLICY`） |
| `[coupon] return_window_hours` | 0 | 0–8760 | 是 | 否 | 退券只在下单后多少小时内（小时，0 = 只看券的有效期）（原 `COUPON_RETURN_WINDOW_HOURS`） |
| `[coupon] code_length` | 12 | 8–32 | 是 | 否 | 兑换码长度（位）（原 `COUPON_CODE_LENGTH`） |
| `[coupon] code_max_failures` | 5 | 1–100 | 是 | 否 | 兑换码在窗口内输错几次后锁定（原 `COUPON_CODE_MAX_FAILURES`） |
| `[coupon] code_window_seconds` | 600 | 60–86400 | 是 | 否 | 兑换码输错计数窗口（秒）（原 `COUPON_CODE_WINDOW_SECONDS`） |
| `[coupon] code_batch_max` | 10_000 | 1–100000 | 是 | 否 | 一批最多生成多少个兑换码（原 `COUPON_CODE_BATCH_MAX`） |
| `[coupon] issue_batch_max` | 10_000 | 1–100000 | 是 | 否 | 一次批量发券最多多少人（原 `COUPON_ISSUE_BATCH_MAX`） |
| `[coupon] expire_batch_size` | 500 | 1–10000 | 是 | 否 | 过期任务每批几张（原 `COUPON_EXPIRE_BATCH`） |
| `[coupon] remind_before_hours` | 48 | 1–720 | 是 | 否 | 到期前多少小时提醒（小时）（原 `COUPON_REMIND_BEFORE_HOURS`） |
| `[coupon] issue_job_batch` | 200 | 1–10000 | 是 | 否 | 批量发券任务每批几人（原 `COUPON_ISSUE_JOB_BATCH`） |
| `[coupon] max_coupons_per_order` | 3 | 1–10 | 是 | 是 | 一单最多叠加几张券（原 `COUPON_MAX_PER_ORDER`） |

定时任务（`jobs.JOBS`）：coupon.expire 300 秒、coupon.remind_expiring 3600 秒、coupon.issue_batches 30 秒。

## 10 错误码

| 场景 | code | HTTP |
|---|---|---|
| 参数 / 模板配置不合法、缺幂等键、未知筛选参数 | 4001 | 400 |
| 未登录 / 令牌类型不对 | 4010 | 401 |
| 无权限码 | 4030 | 403 |
| 模板 / 券不存在、他人的券、兑换码无效 | 4040 | 404 |
| 非法状态流转、版本冲突、并发锁券、同券并发 | 4090 | 409 |
| 同幂等键处理中 / 同键不同请求体 | 4092 / 4093 | 409 / 422 |
| 模板未上架、不在领取期、渠道不允许、人群不符、券不可用（带 reason）、发布后收紧、券不能同用 | 4220 | 422 |
| 每人 / 每日限领、售罄、兑换码或批次超过库存 | 4225 | 422 |
| 兑换码失败过多 | 4290 | 429 |
| 购物车钩子未配置 | 5030 | 503 |

## 11 测试清单（`16-coupon/code/tests/`）

| 文件 | 覆盖 |
|---|---|
| test_coupon_rules.py | 模板校验 13 种非法配置、有效期计算、规则文案、门槛只算范围行 + 还差金额、折扣封顶与取整、兑换券、会员卡种类范围与排除、活动后门槛与不同享、最大余数分摊、多组叠加、叠加冲突、发布后编辑约束、有利修改判断、兑换码 |
| test_coupon_claim.py | 扣库存与快照、不提交调用方事务、限领与售罄、每日限领跨日、状态与领取期、渠道、人群钩子、ref_key 幂等、抽奖忽略限领、50 线程不超发、同一会员并发限领 |
| test_coupon_orders.py | 可用券原因与推荐、校验（他人 4040、叠加、张数、门槛）、多组叠加、锁券与报价一致 + 分摊、过期 / 未生效不可锁、调用方回滚、核销 / 解锁状态边与幂等、解锁时已过期 → expired、全额 / 部分 / never / 时间窗退券、退时过期、来源收回与作废、同券两单竞争、支付与关单竞争 |
| test_coupon_admin_jobs.py | 模板状态机合法 / 非法、发布校验、删除仅草稿、发布后编辑与版本冲突、规则只影响新券、有利修改同步、兑换码一次性 / 不存明文 / 作废 / 限流 / 失败不消耗码 / 库存校验、批量发放幂等与失败原因、补偿批次、注册礼事件去重、抽奖奖品数量 / delivery_key 去重 / 回执 / 回执事件退路、发件箱分发、订阅不提交、过期任务幂等与分批、提醒去重、任务登记、统计 |
| test_coupon_http.py | 4010 / 4030 / 4001 / 防批量赋值 / 信封、真实 admin_rbac 会话 / 伪造声明 4010 / 门店范围 4031 / `coupon:*` 通配、手机号关键词、模板 CRUD + 幂等重放 + 4093 + 乐观锁 + 列表筛选排序关键词 + 导出 + 统计、领券中心游标、领取与重放、我的券页签与数量、他人券 4040、兑换码导出与 4290、购物车可用券、批量发放 + 会员券列表 / 关键词 / 作废 / 导出、日期区间校验 |

## 12 前端对接

| 页面 | 接口 | 行为 |
|---|---|---|
| 领券中心 | GET coupon-templates | 展示 rules_text、validity_text；sold_out 置灰；can_claim=false 显示“已领取” |
| 领券按钮 | POST …/claim | 每次点击生成新幂等键、网络重试复用；4225 按 data.reason 提示“已领完 / 已达上限 / 今日已领” |
| 我的券 | GET coupons?tab= + counts | 页签数量；display_status=not_started 显示“X 日生效”；locked 显示“使用中” |
| 兑换码 | POST coupons/redeem-code | 4040 统一提示“兑换码无效或已使用”；4290 按 Retry-After 倒计时 |
| 确认订单 → 优惠券 | GET coupons/available-for-cart | 可用按抵扣额排序并默认选中 best；不可用灰显 unusable.message（如“还差20元可用”） |
| 后台模板 | admin coupon-templates | 编辑页带 version；已发布时提示“修改只影响之后领取的券”，勾选“同步给已领未用的券”仅限有利修改 |
| 后台发券 | coupon-issue-batches | 提交后轮询详情看 issued / failed 与失败原因 |

## 13 定制点

| 类型 | 名称 | 说明 |
|---|---|---|
| 配置 | §9 | 运行参数在 `[coupon]`；`coupon.set_config(...)` 只给启动与测试 |
| 钩子 | `set_member_profile_provider(fn(db, user_id) -> MemberProfile)` | 新会员、等级、标签人群；未注入时默认用 `membership.member_profile`（可导入时） |
| 钩子 | `set_audience_checker(fn(db, user_id, audience, now) -> 原因或 None)` | 完全自定义人群判断 |
| 钩子 | `set_cart_lines_provider(fn(db, user_id) -> lines 或 (lines, campaign_promotions))` | 购物车可用券（接 04-cart / 03-catalog / 17-campaign） |
| 钩子 | `set_member_resolver(fn(token, db) -> user_id)` | 会员令牌解析（默认 01-identity） |
| 接入 | `price_with_coupons / lock / redeem / unlock / return_on_refund / revoke_by_source / issue` | 订单、售后、抽奖、会员模块调用 |
| 接入 | `events.SUBSCRIPTIONS`、`jobs.JOBS` | 25-jobs 装配 |

不包含：运费券（free_delivery，需配送费参与计价后再加）；积分兑换券；满额赠券（订单支付后发券由订单发 `coupon.issue_requested`）；按手机号定向发放（后台先按手机号查会员再按 id 发放）；微信支付商家券 / 代金券对接；券的转赠；虚拟支付（小程序虚拟支付）相关处理；会员关键词按昵称搜索（支持会员 id、完整手机号、券名、来源）。
