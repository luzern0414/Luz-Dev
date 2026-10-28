# 08-order（通用交易订单）`order`

## 0 官方来源

简写 `DOC/x` = `https://pay.weixin.qq.com/doc/v3/merchant/x`；核对日期均为 2026-10-28。

| 主题 | 官方页面 | 本模块用到的结论 | 核对日期 |
|---|---|---|---|
| 合同成立 / 更正 / 交付时间 | 电子商务法第四十九、五十、五十一条（https://www.cac.gov.cn/2018-09/01/c_1123362506_2.htm ，网信办转载） | 提交订单成功即合同成立，不得以“付款后才成立”的格式条款排除；提交前可更正（试算页）；快递以签收为交付时间（售后窗口起点 = fulfilled_at） | 2026-10-28 |
| 搭售 / 默认勾选 | 消费者权益保护法实施条例第十九条（https://www.gov.cn/zhengce/content/202403/content_6940158.htm） | 购买条款、不适用七日无理由的商品须用户主动确认，不得默认同意（`terms_accepted` 必须为 true） | 2026-10-28 |
| 七日无理由 / 退款口径 | 网络购买商品七日无理由退货暂行办法第六、七、十五、十七条（https://www.gov.cn/zhengce/2020-11/03/content_5723721.htm） | 行级 `returnable` 快照；券按原方式退回（交给 16-coupon 策略）；部分退按各行实付重新计算（行分摊） | 2026-10-28 |
| 下单 time_expire | DOC/4012791897 | time_expire 为用户可支付的最后时限（≥ 1 分钟），不是关单时间；订单把 `pay_deadline_at` 作为稳定截止时间传给 09，便于复用 prepay_id | 2026-10-28 |
| 查单 / 关单 | DOC/4012791900、DOC/4012791901、DOC/4012791911 | NOTPAY 不等于失败；超时先查单，确认未支付再关单，关单成功后才释放库存与券；渠道不可用时保留资源下轮再查 | 2026-10-28 |
| 虚拟支付 | 小程序运营规范 2.5.13、2.14.7（https://developers.weixin.qq.com/miniprogram/product/） | 每行带 `goods_kind`；订单含 virtual_content 时微信支付由 09 拒绝（4220） | 2026-10-28 |
| 发货信息管理 | https://developers.weixin.qq.com/miniprogram/product/jiaoyilei/yunyingguifan.html | 交易类小程序须录入发货信息：快递发货 logistics_type=1，线下服务 / 会员卡按虚拟发货 3，无快递的实物 4（自提） | 2026-10-28 |

参考设计（非官方，只作结构借鉴）：芋道 trade 订单生命周期钩子、`cancel_type`、`updateByIdAndStatus`；002Gym 关单前先查单、过期券不回 available（R7 §2、§B-2）。

## 1 状态与依赖

| 项 | 内容 |
|---|---|
| 状态 | 标准（可复用）；测试 44 项（另有端到端 3 项在 `backend/tests/test_integration.py`） |
| 依赖 | 00-framework；07-inventory（reservation 预占 / 确认 / 释放，`restore` 退货入库）；16-coupon（price_with_coupons / available_coupons / lock / redeem / unlock / return_on_refund）；17-campaign（`pricing` 最大余数法分摊，活动经提供者注入）；09-wechat-pay（支付解析器、支付成功钩子、close_payment、request_shipping_upload）；15-recharge（余额 consume / reverse）；29-member-card（发卡、退卡试算）；01-identity（current_user、手机号）；05-address（地址快照）；22-admin-rbac（权限、门店范围） |
| 被依赖 | 10-after-sales（退款额度与结算）、19-review（Order、COMPLETED）、09-wechat-pay（order.cancelled） |

外部模块全部经 `kinds.py` / `ports.py` 的适配器调用（延迟导入，可替换），模块未启用时退化或给出明确错误。

## 2 职责与边界

| 负责 | 不负责 |
|---|---|
| 试算（服务端价、活动、券、库存与限购提示、报价令牌） | 商品与价目维护（03-catalog / 行种类的价格来源） |
| 下单：快照、分摊、锁券、占库存、限购、条款、单号、截止时间 | 券规则本身（16）、活动规则本身（17） |
| 支付：委托 09 下单 / 复用；余额按行扣款；支付成功流转 | 渠道协议、验签、退款执行（09） |
| 取消 / 超时关单（先查单再关单）、释放库存 / 券 / 限购 | 售后审批与退款流程（10） |
| 履约（发货、履约完成、确认收货、自动完成）与行种类钩子 | 配送调度（11） |
| 退款额度账本（行级占用 / 已退）与退款结算（回库、退券、全退置 refunded） | 会员卡 / 储值的权益本身（29 / 15） |
| 顾客端与后台查询、导出、备注、关单 | 报表（24） |

## 3 数据模型

| 表 | 关键字段 | 索引 / 约束 |
|---|---|---|
| `orders` | order_no, user_id, store_id, status, refund_status, kind, goods_kind, items_cents, campaign_discount_cents, coupon_cents, total_cents, paid_cents, refunded_cents, refund_reserved_cents, pay_method, payment_id, out_trade_no, coupon_ids_json, pay_deadline_at, paid_at, fulfilling_at, fulfilled_at, completed_at, cancelled_at, cancel_type, cancel_reason, express_company, tracking_no, shipping_uploaded_at, remark, address_snapshot_json（AES-GCM 加密）, buyer_phone_masked, buyer_phone_hash, terms_version, terms_accepted_at, quote_token, needs_manual, manual_reason, version | uq order_no；ix (user_id, status)；ix (status, pay_deadline_at)；ix (store_id, created_at) |
| `order_items` | order_id, user_id, kind, goods_kind, product_id, sku_id, name, spec, image, unit_price_cents, qty, gross_cents, campaign_alloc_cents, coupon_alloc_cents, paid_cents, returnable, snapshot_json, fulfil_status, fulfil_ref, fulfilled_at, pay_ref, refund_reserved_qty, refund_reserved_cents, refunded_qty, refunded_cents | ix order_id |
| `order_status_logs` | order_id, from_status, to_status, actor_type, actor_id, reason, created_at | ix (order_id, id)；只追加 |
| `order_quotes` | token, user_id, store_id, request_json, request_hash, result_json, total_cents, expires_at, used_at, order_id | uq token；ix (user_id, id) |
| `order_purchase_counters` | user_id, product_key(kind:product_id), qty | uq (user_id, product_key) |
| `order_notes` | order_id, staff_id, text | ix order_id |

不变量：Σ items.paid_cents = orders.total_cents；refunded ≤ refund_reserved ≤ paid（行级与单级都由条件 UPDATE 保证）。

## 4 业务规则

1. 价格只来自服务端：行种类 `resolve` 取价（goods → SKU，service → 价目提供者，member_card → 卡种）；请求体里的价格字段一律忽略。
2. 试算顺序：活动（`set_promotions_provider` 返回 `pricing.Promotion`）→ 券（16 在活动后金额上计算，门槛按活动后金额）→ 每项优惠只在适用行之间按金额比例、最大余数法分摊；行实付 = 原价 − 活动 − 券。
3. 试算不因券不可用失败：返回 `coupon_error`（原因：below_threshold / expired / not_started / no_scope_items / campaign_conflict / stack_conflict …）并按无券计价；同时列出可用 / 不可用券及原因、推荐券。
4. 试算返回 `quote_token`（ORDER_QUOTE_TTL，默认 600 秒）；库存不足、超出限购写入 `issues`，`submittable=false`。
5. 下单必须带本人未过期的报价令牌；服务端重新计价，任何一行单价 / 数量 / 实付或券集合变化 → 4223 `price_changed`；令牌过期 / 不存在 → 4223；已用 → 4090 `quote_used`（条件 UPDATE `used_at IS NULL`，同一报价并发只成一单）。
6. 购买条款：`terms_accepted` 必须为 true，客户端版本与 ORDER_TERMS_VERSION 不一致 → 4220 `terms_outdated`；订单记录生效版本与同意时间（可换成 27-legal 的同意记录：`set_terms_checker`）。
7. 下单同一事务：写订单与行快照 → 限购计数条件自增（qty + n ≤ limit，超出 4225）→ 行种类 `reserve`（实物：07 预占，ref = `order:{id}`，按 sku_id 升序）→ 锁券（16 lock，同券并发只成一单）→ 状态日志 → `order.created`。任一步失败整单回滚。
8. 单号：前缀(≤4) + 北京时间 yyMMddHHmmss + 毫秒 + 6 位随机数字（≤ 25 位，时间有序，可直接做微信 business_no）；唯一约束兜底，碰撞换号重试 3 次。
9. 支付截止 `pay_deadline_at = 下单时间 + ORDER_PAY_TIMEOUT_MINUTES`（默认 15）；0 元订单提交即成交（`pay_method=free`），不走微信。
10. 微信支付：09 `pay_business("order")` 调用本模块解析器（金额 = total_cents，截止 = pay_deadline_at，goods_kind 汇总：含虚拟 → virtual_content，含实物 → physical，否则 offline_service）；同一订单重复“去支付”复用同一支付单。
11. 余额支付：先查 / 关闭未完成的微信支付单（已付 → 4090 `already_paid`；渠道不可用 → 5020）；再按行 `wallet.consume(ref=order:{id}:item:{item_id})`（余额不足 4224，整笔回滚）并同事务成交。不支持混合支付。
12. 支付成功（09 `on_paid` 钩子，保存点内）：核对金额与付款人 → `pending_payment → paid` 条件 UPDATE → 核销券 → 确认库存预占 → 行种类 `fulfil`（会员卡同事务发卡，issue_ref = `order_item:{id}`）→ `order.paid`；全部行已即时交付 → `fulfilled` + `order.fulfilled`。钩子抛错（订单已关闭、金额不符）→ 09 记 needs_manual 并自动全额退款，订单不变。
13. 用户取消仅限 `pending_payment`；先查单再关单（已付 → 4090；渠道不可用 → 5020，订单保持待支付），然后同事务：释放库存、解锁券（券已过期则 → expired，不回 available）、回退限购计数、`order.cancelled`。后台关单同规则，原因必填并审计。
14. 超时关单任务：`pending_payment` 且 `pay_deadline_at` 已过 → 同规则 13（cancel_type=timeout，状态 closed）；查到已付 → 由支付钩子成交；渠道不可用 → 本轮跳过并保留库存与券；查到渠道已付但订单钩子曾拒绝 → 关闭订单并标 needs_manual（09 已自动退款）。
15. 履约：实物 `ship`（paid → fulfilling，快递公司与单号必填，有处理中售后时拒绝）；`mark_fulfilled`（paid / fulfilling → fulfilled，待交付行全部标记）；顾客 `confirm_receipt`（fulfilling / fulfilled → completed，未记交付时间时以确认时间为交付时间）。
16. 自动完成：fulfilled（或已发货 fulfilling）超过 ORDER_AUTO_COMPLETE_DAYS（默认 7）天且无处理中售后 → completed。
17. 发货信息录入：微信支付订单在发货 / 履约事务提交后调用 09 `request_shipping_upload`（快递 1、无快递实物 4、服务 / 会员卡 3）；失败由 `order.shipping_sync` 补录。
18. 退款额度（供 10）：`refund_quote` 按累计公式 `paid × (已占 + n) // qty − paid × 已占 // qty`，退完剩余数量时 = 剩余实付；行种类可再设上限（会员卡）；`reserve_refund` 条件 UPDATE 保证 Σ占用 ≤ 行实付、数量 ≤ 行数量、单级占用 ≤ 实付；余额支付订单可部分退：`refund_balance(db, order_id, [(item_id, amount_cents)], ref)` 调 `wallet.reverse(..., amount_cents, reversal_ref="{ref}:item:{id}")` 按金额冲回（按 reversal_ref 幂等，累计不超过原消费）。
19. 退款结算 `settle_refund`：计入已退 → 行种类 `on_refunded`（实物按售后决定回库）→ 16 `return_on_refund`（是否退券由券模块策略决定）→ 全部实付已退则主状态 → refunded（终态）→ `order.refunded`。`refund_status` 由占用与已退推导：refunding / partially_refunded / refunded / none。
20. 他人订单一律 4040；后台门店范围外的订单按 4040 处理；列表与导出共用同一 ListSpec；完整手机号关键词按哈希精确匹配，脱敏片段按 LIKE。

## 5 状态机 / 关键流程

主状态（`ORDER_FLOW`，非法流转 4090，并发失败 4090）：

| 从 | 到 | 触发 |
|---|---|---|
| pending_payment | paid | 支付成功（微信钩子 / 余额 / 0 元） |
| pending_payment | cancelled | 用户取消 / 后台关单 |
| pending_payment | closed | 超时关单任务 |
| paid | fulfilling | 发货 |
| paid / fulfilling | fulfilled | 履约完成（人工 / 全部行即时交付） |
| paid / fulfilling / fulfilled | completed | 确认收货（fulfilling / fulfilled）/ 自动完成（fulfilled、fulfilling） |
| paid / fulfilling / fulfilled / completed | refunded | 全部实付已退 |
| cancelled / closed / refunded | — | 终态 |

退款维度 `refund_status`（`REFUND_FLOW`）：none → refunding（有占用）→ partially_refunded / refunded / none（驳回释放）；partially_refunded → refunding；refunded 终态。

关键流程与失败分支：

```
preview ──quote_token──▶ submit（重算；变化 4223）──▶ pending_payment
   ├─ 微信：09 下单 → 回调 / 查单 → on_paid 钩子 → paid（钩子失败 → 09 needs_manual + 自动退款）
   ├─ 余额：查单关单 → consume（不足 4224）→ paid
   ├─ 取消：查单 → 关单 → cancelled（已付 4090 / 渠道不可用 5020）
   └─ 超时：查单 → 关单 → closed（已付 → paid；渠道不可用 → 下轮）
paid ─ship▶ fulfilling ─fulfil▶ fulfilled ─confirm / auto▶ completed
任一已付状态 ─10 售后全额退款▶ refunded
```

## 6 接口

| 方法 | 路径 | 鉴权 | 幂等 | 权限码 | agent | 说明 |
|---|---|---|---|---|---|---|
| POST | `/api/v1/app/orders/preview` | 会员 | — | — | yes（只读） | 试算，返回 quote_token |
| POST | `/api/v1/app/orders` | 会员 | 必须 | — | yes（需确认） | 提交订单 `{quote_token, terms_accepted, terms_version?, remark?, address_id?, expected_total_cents?}` |
| GET | `/api/v1/app/orders` | 会员 | — | — | yes | 我的订单：`tab`=all/unpaid/to_fulfil/to_receive/completed/closed，游标分页，附各页签数量 |
| GET | `/api/v1/app/orders/{id}` | 会员（他人 4040） | — | — | yes | 详情：行、时间线、地址（本人明文）、可操作标记 |
| POST | `/api/v1/app/orders/{id}/cancel` | 会员 | 必须 | — | yes（需确认） | 取消未支付订单 |
| POST | `/api/v1/app/orders/{id}/payments` | 会员 | 必须 | — | no | `{method: wechat|balance}`；wechat 返回调起参数 |
| POST | `/api/v1/app/orders/{id}/pay` | 会员 | 必须 | — | no | 09 提供的同一微信支付入口（兼容） |
| POST | `/api/v1/app/orders/{id}/confirm-receipt` | 会员 | 必须 | — | yes（低风险） | 确认收货 |
| GET | `/api/v1/admin/orders` | 员工 | — | order:read | yes | 列表（见下） |
| GET | `/api/v1/admin/orders/export` | 员工 | — | order:export | no | CSV（同一筛选，脱敏，审计） |
| GET | `/api/v1/admin/orders/{id}` | 员工（范围外 4040） | — | order:read | yes | 详情：行、时间线、地址（脱敏）、备注 |
| POST | `/api/v1/admin/orders/{id}/notes` | 员工 | 必须 | order:remark | no | 后台备注（审计） |
| POST | `/api/v1/admin/orders/{id}/close` | 员工 | 必须 | order:close | no | 关闭未支付订单（原因必填，审计） |
| POST | `/api/v1/admin/orders/{id}/ship` | 员工 | 必须 | order:fulfil | no | 发货（快递公司、单号） |
| POST | `/api/v1/admin/orders/{id}/fulfil` | 员工 | 必须 | order:fulfil | no | 履约完成 / 核销 |

后台列表 ListSpec：筛选 `status`、`refund_status`、`kind`、`goods_kind`、`pay_method`、`store_id`、`user_id`、`needs_manual`、`total_cents_min/max`、`created_at_from/to`、`paid_at_from/to`、`paid_at_null`；关键词：订单号、脱敏手机号片段、完整 11 位手机号（哈希精确匹配）；排序 `total_cents`、`created_at`、`paid_at`；未声明参数 4001。

内部函数（其他模块调用）：preview、submit、pay、cancel、admin_close、close_expired、ship、mark_fulfilled、confirm_receipt、auto_complete、refund_quote、reserve_refund、release_refund、settle_refund、refund_balance、get_owned、admin_get、order_view；v1 兼容 create_order、mark_paid、cancel_order。

## 7 事件

发出（payload 公共字段：order_id, order_no, user_id, store_id, status, total_cents）：

| 主题 | 额外字段 | 时机 |
|---|---|---|
| `order.created` | kind, coupon_ids | 下单 |
| `order.paid` | pay_method, payment_id, paid_cents, kind | 支付成功 |
| `order.fulfilling` | express_company, tracking_no | 发货 |
| `order.fulfilled` | — | 履约完成 |
| `order.completed` | by（user / system） | 确认收货 / 自动完成 |
| `order.cancelled` | cancel_type（user / admin / timeout）, reason | 取消或超时关闭（status 区分 cancelled / closed） |
| `order.refunded` | amount_cents, refunded_cents, full, ref（after_sales:{id}） | 每次退款结算 |

消费（`order/events.py` `SUBSCRIPTIONS`）：

| 主题 | 用到的字段 | 处理 |
|---|---|---|
| `payment.succeeded`（09） | business_type, business_id, amount_cents, payment_id, out_trade_no, paid_at, needs_manual | 兜底：business_type=order、未标人工、订单仍待支付时成交（正常情况下钩子已在同事务完成，空操作）；`mark_processed("order.payment_succeeded")` 去重 |

## 8 并发与一致性

- 所有状态变化 `StateMachine.advance`（`UPDATE … WHERE id AND status = 旧状态`，rowcount）；version 自增。
- 库存：07 条件扣减（不超卖，30 线程抢 5 件只成 5 单）；限购：条件自增 + 唯一计数行；报价：`used_at IS NULL` 条件更新（20 线程同一报价只成 1 单）。
- 退款额度：行级 `refund_reserved_* + n ≤ 行数量 / 行实付` 与单级 `refund_reserved_cents + n ≤ paid_cents` 两条条件 UPDATE（60 线程抢 50 件，恰好 50 成功，占用 = 实付）。
- 渠道调用不在业务事务内：取消 / 关单 / 余额支付先经 09 查单关单（09 内部提交），再在新事务内改订单；支付成功钩子在 09 的保存点内执行，失败不影响支付事实。
- 跨模块副作用：券、库存、钱包、发卡都在订单同一事务内经对方导出函数完成；其余经发件箱事件。
- 补偿：`order.close_expired`、`order.auto_complete`、`order.shipping_sync`（`jobs.JOBS`，逐单提交，可重跑）。

## 9 配置

见 §13 配置表。所有键都有安全默认值，项目启动时 `order.set_config(...)` 覆盖。

## 10 错误码

| 场景 | code | HTTP |
|---|---|---|
| 参数错误、未知行种类、未声明筛选参数、缺幂等键 | 4001 | 400 |
| 未登录 | 4010 | 401 |
| 无权限 | 4030 | 403 |
| 订单 / 商品 / 地址不存在、他人订单、门店范围外 | 4040 | 404 |
| 状态不允许、报价已用、已支付、并发冲突、退款额度变化 | 4090 | 409 |
| 库存不足 | 4091 | 409 |
| 同幂等键处理中 / 幂等键不同请求体 | 4092 / 4093 | 409 / 422 |
| 条款未同意 / 版本过期、券不可用、虚拟商品不能微信支付 | 4220 | 422 |
| 报价过期 / 价格变化 | 4223 | 422 |
| 余额不足 | 4224 | 422 |
| 超出限购 | 4225 | 422 |
| 支付渠道不可用（取消 / 关单 / 余额支付前关单失败） | 5020 | 502 |

## 11 测试清单（44 项，`08-order/code/tests/`）

| 文件 | 覆盖 |
|---|---|
| `test_order_service.py`（18） | 活动 + 券分摊与余数；不可用券原因；参数与库存提示；快照 / 占库存 / 锁券 / order.created；报价令牌（他人、过期、已用）；改价 4223；条款与库存回滚；限购与取消释放；线下服务人工履约；会员卡发卡与退款上限；0 元单；状态机全边；发货 / 确认收货边与日志；自动完成任务；退款累计公式、占用、释放、结算、全退置 refunded 与退券；v1 兼容；地址加密与脱敏 |
| `test_order_payment.py`（18） | 微信支付成功（复用支付单、券核销、库存确认、事件、重复回调、订阅兜底）；拒绝场景（他人、方式、过期、虚拟商品）；钩子拒绝自动退款；余额支付成功 / 不足回滚 / 先关微信单 / 渠道已付；取消（释放、他人 4040、过期券、渠道不可用 5020、已付 4090）；后台关单审计；超时关单四条分支与幂等；渠道故障保留资源；关单后到账自动退款；任务与订阅接线；发货信息录入 |
| `test_order_http.py`（4） | 4010 / 4001 / 信封 / 忽略客户端价格；幂等重放、缺键、短键、4093；我的订单页签、游标、计数、详情 4040、取消、支付、确认收货 4090；后台 4010 / 4030、筛选、手机号哈希与片段关键词、区间、未知参数、导出与审计、备注、关单、门店范围、权限码登记 |
| `test_order_concurrency.py`（4） | 文件 SQLite + 50 线程：不超卖、同一报价一单、限购、退款占用不超过实付 |

## 12 前端对接

| 页面 | 接口 | 行为 |
|---|---|---|
| 确认订单 | `POST /app/orders/preview` | 进入页面与改数量 / 换券时调用；展示行实付、优惠明细、不可用券原因；`issues` 非空时禁用提交；条款勾选框不得默认勾选 |
| 提交 | `POST /app/orders` | 带 Idempotency-Key（一次提交一个键，重试复用）；4223 → 提示价格变化并重新试算；4091 / 4225 → 提示后返回 |
| 收银台 | `POST /app/orders/{id}/payments` | wechat → `wx.requestPayment`，随后进入 09 的支付结果确认页轮询；balance → 直接显示成功 |
| 我的订单 | `GET /app/orders?tab=` | 页签角标用 `counts`；游标翻页 |
| 订单详情 | `GET /app/orders/{id}` | 按 can_pay / can_cancel / can_confirm 显示按钮；倒计时用 pay_deadline_at |
| 后台订单 | `/admin/orders…` | 列表与导出同一组筛选参数；发货填写快递公司与单号 |

## 13 定制点

配置（环境变量 → 默认值）：

| 键 | 默认 | 说明 |
|---|---|---|
| ORDER_PAY_TIMEOUT_MINUTES | 15 | 支付截止（1–1440 分钟），同时作为微信 time_expire |
| ORDER_QUOTE_TTL | 600 | 报价令牌有效秒数（≥ 30） |
| ORDER_AUTO_COMPLETE_DAYS | 7 | 履约 / 发货后自动完成天数 |
| ORDER_MAX_LINES / ORDER_MAX_QTY_PER_LINE | 50 / 99 | 行数与单行数量上限 |
| ORDER_TERMS_REQUIRED / ORDER_TERMS_VERSION | true / purchase-v1 | 购买条款 |
| ORDER_NO_PREFIX | 空 | 单号前缀（≤ 4 位字母数字） |
| ORDER_TIMEZONE | LUZ_TIMEZONE 或 Asia/Shanghai | 单号日期 |
| ORDER_CLOSE_BATCH / ORDER_COMPLETE_BATCH | 100 / 200 | 任务每轮处理量 |
| ORDER_BALANCE_PAYMENT | true | 是否允许余额支付 |
| ORDER_SHIPPING_UPLOAD | true | 是否录入微信发货信息 |

扩展钩子：

| 钩子 | 用途 |
|---|---|
| `register_line_kind(LineKind)` | 新增行种类（resolve / reserve / release / confirm / fulfil / refund_cap / on_refunded），如课程、门票、充值 |
| `GoodsKind(price_source=…)`、`set_stock_port` | 实物价格来源（接 03-catalog）与库存实现 |
| `set_service_catalog(fn)` | 线下服务价目 |
| `set_member_card_port(MemberCardPort)` | 会员卡读取 / 发卡 / 退卡试算 |
| `set_promotions_provider(fn)` | 活动（17-campaign 的 Promotion 列表） |
| `set_coupon_port` / `set_payment_gateway` / `set_wallet_port` | 券、微信支付、余额适配 |
| `set_buyer_contact_provider` / `set_address_provider` / `set_terms_checker` | 下单人手机号、地址快照、条款（可接 27-legal） |

不包含：混合支付（余额 + 微信）、后台改价、拆单 / 合单支付、堂食加菜与结账、取餐码、催单、改地址、配送费与包装费计算、积分抵扣、会员价、预售、充值类行种类（wallet_recharge，走 15 自己的充值单）、小程序虚拟支付。
