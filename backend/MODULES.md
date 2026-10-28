# 技术模块目录（MODULES）

本文是编号模块的**目录**：每个模块覆盖什么、关键规则、事件、权限码与定制点各一屏以内。实现细节（表、状态机、接口、错误码、测试清单）以各模块的 `NN-name/MODULE.md` 为准；本目录与之冲突时以 MODULE.md 和代码为准。结构与铁律见 `MODULE-TEMPLATE.md`，总则见 `SPEC.md`，跨模块调用见 `CONTRACTS.md`。

**状态**：**标准** = 代码 + 测试 + 按 MODULE-TEMPLATE 写成的 MODULE.md，可直接复用；**基础** = 有代码和测试，但范围较窄或文档仍是旧格式；**规划** = 只有设计，没有代码（实现时先补 MODULE.md）。

**全局约定**（各模块不再重复）
1. 金额整数分；状态变更 / 计数 / 扣减一律条件 UPDATE + rowcount；唯一约束是最后一道防线；写接口带 `Idempotency-Key`（`run_idempotent`）。
2. 顾客只能访问本人资源（他人 4040）。后台路由统一 `staff = Depends(admin_rbac.require_permission("<module>:<action>", fresh=...))`，门店范围 `admin_rbac.store_scope(staff)`（None = 全部门店）/ `assert_in_scope`；不再有模块自带的授权适配器，令牌里的声明不算数。资金与敏感操作 `fresh=True`（最近 300 秒内完成认证，否则 4011 `reauth_required`）。
3. 权限码 `<module>:<action>`，各模块导入时 `declare_permissions` 登记；角色可授予精确码或模块通配 `module:*`（前缀匹配，模块以后新增的码自动覆盖）；`*` 只属于超级管理员。
4. 跨模块副作用走发件箱：`enqueue_event` 与业务写同一事务；订阅者在 `<pkg>/events.py` 的 `SUBSCRIPTIONS = {topic: handler(db, payload, event_id)}`，幂等（`mark_processed` 或业务键）、不提交；定时任务在 `<pkg>/jobs.py` 的 `JOBS`（`luzback.scheduler.Job`）。`python -m jobs list` 列出全部任务与订阅，`python -m jobs worker|scheduler` 运行。
5. 生产启动 `Settings.assert_production_safe()`：框架通用项 + 模块用 `luzback.register_production_check(name, fn)` 登记的检查（identity、wechat_pay、wechat_mp），任何一项不过即拒绝启动。
6. 部署：`alembic upgrade head`（基线 `0001_baseline` 覆盖全部模块表）→ `python -m admin_rbac.bootstrap --seed`（同步权限码与内置角色，幂等）；首次部署再 `python -m admin_rbac.bootstrap <username>` 建首个超级管理员。
7. 小程序商品带 `goods_kind`：`physical` / `offline_service` 走普通微信支付；`virtual_content` 一律拒绝（虚拟支付不在基线内）。

| 分类 | 模块 |
|---|---|
| 账号与权限 | 01-identity · 05-address · 22-admin-rbac · 27-legal |
| 交易与资金 | 02-store · 03-catalog · 04-cart · 07-inventory · 08-order · 09-wechat-pay · 10-after-sales · 11-delivery · 15-recharge · 29-member-card |
| 会员与营销 | 14-membership · 16-coupon · 17-campaign · 18-lottery |
| 通知与内容 | 19-review · 20-notification · 21-media · 26-agent-support · 28-wechat-mp |
| 运营与基础设施 | 00-framework · 06-geo · 12-queue · 13-reservation · 23-app-settings · 24-report · 25-jobs |

---

## 账号与权限

### 01-identity 会员账号 `identity` — 标准
- **覆盖**：微信登录与自动注册（协议同意）、access JWT + 服务端会话 + 刷新令牌轮换、短信验证码、手机号绑定、资料与内容安全、注销冷静期与匿名化、后台会员管理。
- **规则**：① access 令牌 HS256，2 小时，每个请求校验会话（登出 / 停用 / 注销即时生效）；② 刷新令牌只存哈希、每次轮换，空闲 14 天 / 绝对 30 天，宽限期外复用 → 吊销整个家族；③ 短信 6 位、5 分钟、≤ 5 次校验、60 秒重发、每号每天 10 条、防枚举；④ 手机号 AES-GCM 加密 + HMAC 哈希（唯一）+ 脱敏展示，`find_user_by_phone(db, phone)` 按哈希等值查找；⑤ 昵称 msgSecCheck、头像 mediaCheckAsync 推送通过才生效；⑥ 注销冷静期 15 天，到期匿名化并发 `member.deleted`。
- **事件**：发 `member.registered` `member.phone_bound` `member.disabled` `member.deleted`；收 wechat_mp 推送 `wxa_media_check`。
- **权限码**：`member:read` `member:write` `member:reveal`（查看完整手机号，fresh + 审计）。
- **配置 / 扩展**：`IDENTITY_*`（TTL、短信频控、冷静期等）；`set_mini_program` `set_sms_sender` `set_agreement_gateway` `register_deletion_checker`；`rate_limit_rules()`（登录 / 短信 / 刷新限流，app.py 自动装配）；生产检查 `identity`（LUZ_FIELD_KEY 为 32 字节 base64）。
- **文档**：[MODULE.md](01-identity/MODULE.md)

### 05-address 收货地址 `address` — 基础
- **覆盖**：收货地址增删改查、默认地址唯一、手机号格式校验；订单保存地址快照。
- **规则**：① 每人上限 20（`MAX_ADDRESSES_PER_USER` 常量）；② 第一个地址自动默认，设默认同事务取消旧默认；③ 他人地址 4040。
- **事件**：无。**权限码**：无（仅顾客端）。
- **文档**：[MODULE.md](05-address/MODULE.md)

### 22-admin-rbac 后台账号与权限 `admin_rbac` — 标准
- **覆盖**：员工账号、口令 + TOTP 登录、服务端会话、RBAC 权限码与门店范围、职责分离、新鲜认证、审计与登录日志查询导出。
- **规则**：① 会话服务端强制：空闲 900 秒、绝对 8 小时、绑定 UA、每人最多 5 个；② 口令 Argon2id（argon2-cffi，缺失时用 cryptography），NIST 800-63B-4：启用 MFA ≥ 8 位、否则 ≥ 15 位，无组成规则，拒绝常见 / 含账号信息的口令，可接泄露库；③ 按账号失败计数与递增锁定，IP 限流在 app.py；④ 默认拒绝；角色授予精确码或 `module:*`；持有 `ADMIN_MFA_REQUIRED_PERMISSIONS` 中任一码（含通配覆盖）必须绑定 MFA；⑤ 防越权：只能授予自己持有的码（授予 `x:*` 需自己持有 `x:*`）、不能授予超出自己的门店、至少保留一名超级管理员；⑥ 内置角色 super_admin / store_manager / front_desk / finance / operator 由 `ensure_seed` 写入（只在首次补齐，之后后台可改）。
- **事件**：发 `staff.created` `staff.access_changed` `staff.disabled` `staff.enabled` `staff.deleted` `staff.locked` `staff.password_changed` `staff.password_reset` `staff.mfa_reset` `staff.recovery_code_used`。
- **权限码**：`staff:read` `staff:write` `staff:sensitive` `role:read` `role:write` `audit:read`；另登记无代码模块的标准码（`dashboard:read` `report:*` `product:*` `stock:*` `points:*` `queue:*` `table:*` `delivery:*` `store:*` `review:*` `kb:*` `agent:*` `campaign:*` `settings:*` 等，见 `seed.py:CATALOG`）。
- **配置 / 扩展**：`ADMIN_*`（会话、锁定、口令、MFA）；`set_breach_checker` `set_captcha_verifier`；依赖 `require_permission(*codes, fresh=False)` `require_fresh_auth` `current_staff`；助手 `store_scope` `apply_store_scope` `assert_in_scope` `assert_not_self` `log_sensitive_read`；测试 `admin_rbac.testing.staff_headers(db, *codes, store_ids=...)`（真实会话）。
- **文档**：[MODULE.md](22-admin-rbac/MODULE.md)

### 27-legal 协议与同意 `legal` — 基础
- **覆盖**：用户协议 / 隐私政策 / 购买条款的版本草稿与发布、当前版本、待同意文档、同意（校验版本与文本哈希）与撤回。
- **规则**：① 发布新版即退役旧版并写审计；② 同意必须带 document_id + version + sha256；③ 购买条款检查器在导入时自动接入 08-order（`install_purchase_terms_checker`）：未同意 4220 `terms_required`、版本不符 4220 `terms_outdated`，无已发布文档时退回订单配置版本。
- **事件**：无。**权限码**：无（后台接口规划中）。
- **文档**：[MODULE.md](27-legal/MODULE.md)

---

## 交易与资金

### 02-store 门店 `store` — 规划（只有模型草稿）
营业时间（含跨午夜、节假日例外、paused）、履约参数（起送、配送费、半径、出餐时间）、附近门店（geohash 粗筛 + Haversine）。目前只有 `store/models.py`，没有服务与接口；其他模块的 `store_id` 只做范围过滤。

### 03-catalog 商品目录 `catalog` — 规划
分类、商品、规格 SKU、套餐；可售校验（状态、渠道、时段、估清、库存）；变更 `catalog_version` 失效缓存。SKU 库存模型在 07-inventory；08-order 经 `GoodsKind(price_source=…)` 接入价格来源。

### 04-cart 购物车 `cart` — 规划
同商品合并、行数 ≤ 50、qty 1–99；读取时按目录重算价格与可售性；登录合并、切店清空。结算报价由 08-order 承担。

### 07-inventory 库存 `inventory` — 标准
- **覆盖**：SKU 库存与上下架、原子扣减与回补、下单预占（reserve / confirm / release / 超时释放）、库存流水。
- **规则**：① 扣减条件 UPDATE `stock >= qty`，不超卖；② `deduct` / `restore(db, sku_id, qty, ref)` 带 ref 时按 (ref, sku, reason) 只生效一次（先查流水，再保存点插入，唯一约束兜底），重放返回 False；③ 部分退货的 ref 每一步唯一（`refund:order:{id}:item:{item}:upto:{n}`）；④ 预占按订单 ref 幂等，`expired_refs` 供关单任务释放；⑤ `Sku` 只有一个模型（`inventory.models`）。
- **事件**：无。**权限码**：后台库存接口规划中（`stock:read` `stock:adjust` 已在种子目录）。
- **文档**：[MODULE.md](07-inventory/MODULE.md)

### 08-order 订单 `order` — 标准
- **覆盖**：服务端报价（活动 + 券）、下单预占、支付钩子成交、履约 / 发货（微信发货信息录入）、自动完成、超时关单、退款额度与结算（供售后）、后台列表导出备注关单。
- **规则**：① 价格只来自服务端，报价令牌 600 秒；② 行种类（实物 / 线下服务 / 会员卡）经 `register_line_kind` 扩展，各自 reserve / fulfil / refund_cap / on_refunded；③ 支付成功钩子在 09 的事务内核对金额与付款人后 `pending_payment → paid`，核销券、确认预占、即时交付行直接 `fulfilled`；④ 超时关单先查支付再关（经 09）并释放库存与券；⑤ 退款额度按累计公式分摊、条件 UPDATE 保证不超实付；余额支付可部分退（`refund_balance(db, order_id, [(item_id, amount)], ref)` → `wallet.reverse` 部分冲回）；⑥ 购买条款由 27-legal 检查。
- **事件**：发 `order.created` `order.paid` `order.fulfilling` `order.fulfilled` `order.completed` `order.cancelled` `order.refunded`；收 `payment.succeeded`（兜底成交）。
- **权限码**：`order:read` `order:export` `order:remark` `order:close` `order:fulfil`。
- **配置 / 扩展**：`ORDER_*`（支付超时 15 分钟、自动完成 7 天、行数上限、条款版本、余额支付开关、发货录入开关）；`register_line_kind` `set_stock_port` `set_service_catalog` `set_member_card_port` `set_promotions_provider` `set_coupon_port` `set_payment_gateway` `set_wallet_port` `set_address_provider` `set_terms_checker`。
- **文档**：[MODULE.md](08-order/MODULE.md)

### 09-wechat-pay 微信支付 `wechat_pay` — 标准
- **覆盖**：JSAPI 下单（APIv3）、回调验签解密、查单补偿、关单、退款与退款查询、对账（交易账单）、发货信息上传、后台查询 / 退款 / 对账 / 导出。
- **规则**：① 金额只来自业务模块注册的 `Payable`；`virtual_content` 拒绝；② 回调只走 APIv3：时间偏差 ≤ 5 分钟、平台公钥 / 证书验签、AES-GCM 解密、商户号与 appid 一致、金额整数比较；验签失败计数 `luz_payment_notify_verify_failed_total`；③ 补偿任务按 5s…30min 节奏查单，关单前先查单；④ 退款同一 `out_refund_no` 重试，额度条件 UPDATE；`find_active_refund(db, business_ref)` 供业务防重复退款；⑤ 未注册业务的支付 → needs_manual 并自动退款；⑥ 发货上传 access_token 默认取 28-wechat-mp，40001 / 42001 刷新后重试一次。
- **事件**：发 `payment.succeeded` `payment.closed` `payment.failed` `refund.succeeded` `refund.failed` `refund.abnormal`（字段见 §7；没有 `payment.refunded`）；收 `order.cancelled`（登记待关单）。
- **权限码**：`payment:read` `payment:refund`（fresh） `payment:reconcile`（关单、差异处理 fresh）。
- **配置 / 扩展**：`WECHAT_PAY_*`（客户端 fake / http、商户密钥与证书、回调地址、超时、补偿节奏、退款与对账参数）；`register_payable` `register_on_paid` `register_on_refunded` `set_client` `set_shipping_client(HttpShippingClient())` `set_openid_resolver`；生产检查 `wechat_pay`；测试 `wechat_pay.testing.wechat_pay_kit()` + `FakeWechatPay`。
- **文档**：[MODULE.md](09-wechat-pay/MODULE.md)

### 10-after-sales 售后 `after_sales` — 标准
- **覆盖**：仅退款 / 退货退款申请、七日无理由与窗口期、审核（职责分离 + 二次验证）、寄回与收货、退款提交与结果处理、人工重试 / 线下结清 / 关闭。
- **规则**：① 可退额度来自 08（不超行实付）；② 原路退款：微信经 09 `create_refund`（提交前 `find_active_refund` 查重）、余额经 08 部分冲回（`after_sales:{id}:item:{item}` 幂等）；③ 退款结果只认事件：`refund.succeeded` → completed（订单计入已退、回库存、按券策略退券）；`refund.failed` → refund_failed；`refund.abnormal` → refund_abnormal；④ 会员卡行完成后由 29 终止卡。
- **事件**：发 `after_sale.applied` `approved` `rejected` `cancelled` `return_shipped` `return_received` `completed`（含 lines、order_full_refund） `refund_failed` `refund_abnormal` `closed`；收 `refund.succeeded` `refund.failed` `refund.abnormal`。
- **权限码**：`after_sale:read` `after_sale:export` `after_sale:create` `after_sale:approve`（fresh） `after_sale:receive` `after_sale:refund`（fresh）。
- **配置 / 扩展**：`AFTER_SALE_*`（无理由 7 天、窗口 15 天、自动同意、寄回期限、凭证上限）；`set_refund_gateway` `set_evidence_checker`。
- **文档**：[MODULE.md](10-after-sales/MODULE.md)

### 11-delivery 配送 `delivery` — 规划
订单 ready 后建配送任务；自配送就近派单（条件 UPDATE 骑手单量）、第三方适配器 `create / cancel / query`；取餐码、送达距离校验、异常改派、订单取消同步取消任务。

### 15-recharge 储值 `wallet` — 标准
- **覆盖**：充值档位与充值单（微信支付）、本金 / 赠送金分批台账、门店线下充值、消费与冲回、余额调整、充值退款（含利息与发票处理）、对账、赠送金过期。
- **规则**：① 余额与批次只经条件 UPDATE；每笔变动写流水，ref 唯一幂等；② 消费按批次顺序分摊本金与赠送金；③ `reverse(db, user_id, ref, amount_cents=None, reversal_ref=None)`：不给金额冲回剩余全部，部分冲回按 `reversal_ref` 幂等、累计不超过原消费、本金 / 赠送按原比例（赠送向下取整，最后一笔补齐）；④ 单用途预付卡规则：余额上限、7 日内未兑付全额退、经营者原因退款按 LPR 计息；⑤ 后台按手机号查会员走 identity 哈希查找。
- **事件**：发 `wallet.recharged` `recharge.succeeded` `balance.consumed` `balance.reverted` `wallet.adjusted` `wallet.refund_*` `recharge.refunded` `wallet.closed` `wallet.bonus_expired` `wallet.payment_mismatch` `wallet.reconcile_mismatch` `wallet.order_closed`；收 `payment.succeeded` `refund.succeeded` `refund.failed` `refund.abnormal`。
- **权限码**：`wallet:read` `wallet:config` `wallet:adjust`（fresh） `wallet:recharge` `wallet:consume`（冲回 fresh） `wallet:refund`（审批 / 线下确认 fresh） `wallet:export` `wallet:reconcile`。
- **配置 / 扩展**：§9 配置；`LUZ_LPR_1Y_BP`（默认 300，示例值，上线前与每月 LPR 公布后更新）；`set_policy_resolver` `custom_policy` `register_outstanding_provider` `set_payment_port` `set_member_resolver` `grant_bonus`。
- **文档**：[MODULE.md](15-recharge/MODULE.md)

### 29-member-card 会员卡 `member_card` — 标准
- **覆盖**：次卡 / 期限卡 / 时段卡的卡种、购买（微信支付）与后台发卡赠卡、激活、核销与冲正、冻结解冻、转让、挂失补办、停用、退卡（报价、审批、原路 / 线下）、到期提醒。
- **规则**：① 次数与天数只经条件 UPDATE；核销按 ref 幂等，冲正只能一次；② 防借卡间隔、适用门店、限购在核销 / 购买时校验；③ 退卡按未使用价值计算，经营者原因按发卡时 `lpr_bp` 计息；④ 订单售后完成（`after_sale.completed` 中 `fulfil_ref = member_card:{id}` 的行）→ 卡终止并发 `member_card.terminated`，已在本模块退卡流程中的卡只发 `member_card.order_refund_conflict` 转人工；⑤ `lottery.prize_won`（prize_kind=member_card）延期并回执抽奖。
- **事件**：发 `member_card.issued` `activated` `used` `use_reversed` `frozen` `unfrozen` `transferred` `lost` `replaced` `refund_*` `refunded` `terminated` `expiring` `expired` `extended` 等；收 `payment.succeeded` `refund.succeeded` `refund.failed` `refund.abnormal` `after_sale.completed` `lottery.prize_won`。
- **权限码**：`member_card:read` `product` `issue` `consume`（冲正 fresh） `adjust`（fresh） `freeze` `transfer` `lost` `disable` `refund`（fresh） `export`。
- **配置 / 扩展**：卡种字段；`LUZ_LPR_1Y_BP`；复用 wallet 的 `set_policy_resolver` `register_outstanding_provider` `set_payment_port`；`set_member_resolver` `check_usable` `consume` `reverse_use` `issue_gift`。
- **文档**：[MODULE.md](29-member-card/MODULE.md)

---

## 会员与营销

### 14-membership 会员与积分 `membership` — 标准（等级只有模型）
- **覆盖**：积分账户与流水、按批次 FIFO 过期、订单完成送积分、退款追回与欠分、赠送、对账、会员画像（供优惠券人群）。
- **规则**：① 余额只经条件 UPDATE，`points_ledger.ref_key` 唯一幂等；② `earn` / `spend` / `gift_points(db, user_id, points, ref, reason="", expires_at=None)` / `revert_spend` / `clawback`；③ 退款按比例追回（向上取整），不足记欠分；④ 积分有效期 `POINTS_VALID_DAYS`（365）；⑤ `member_profile(db, user_id)` 返回注册时间、等级、标签。
- **事件**：发 `points.changed` `points.expired` `points.reconcile_mismatch` `lottery.delivery_result`；收 `order.completed` `order.refunded` `lottery.prize_won`（prize_kind=points，按 delivery_key 幂等并回执）。
- **权限码**：无（后台积分调整接口未包含；种子目录有 `points:read` `points:adjust`）。
- **文档**：[MODULE.md](14-membership/MODULE.md)

### 16-coupon 优惠券 `coupon` — 标准
- **覆盖**：券模板（满减 / 折扣 / 无门槛、适用范围、有效期、人群、库存与限领）、领券中心、兑换码、定向与批量发券、订单锁券 / 核销 / 解锁 / 退券、到期与提醒、后台统计导出。
- **规则**：① 库存与限领条件 UPDATE + 唯一约束（50 线程并发测试）；② 计价经 17 `pricing`，券按真实 sku_id 与 `line_indexes` 限定适用行；③ 退款后是否退券由 `COUPON_RETURN_POLICY`（默认整单全额退款才退）；④ 门店员工只能写本店模板，全局模板只读；⑤ 后台会员券按完整手机号搜索走 identity 哈希查找；⑥ 抽奖发券不受每人限领约束，失败写 `coupon.issue_failed` 并回执抽奖。
- **事件**：发 `coupon.claimed` `locked` `unlocked` `redeemed` `returned` `revoked` `expired` `expiring_soon` `issue_failed` `template_status_changed` `lottery.delivery_result`；收 `member.registered`（注册礼） `lottery.prize_won`（prize_kind=coupon） `coupon.issue_requested`（入站命令：template_id, user_id, ref, channel?, source_ref?）。
- **权限码**：`coupon:read` `coupon:write` `coupon:issue` `coupon:code` `coupon:export`。
- **配置 / 扩展**：§9 `COUPON_*`；`set_member_profile_provider`（默认 `membership.member_profile`） `set_audience_checker` `set_cart_lines_provider`。
- **文档**：[MODULE.md](16-coupon/MODULE.md)

### 17-campaign 活动计价 `pricing` — 基础
- **覆盖**：计价引擎：满减、折扣（`rate_bp`）、优惠券、互斥组与优先级、适用范围、优惠分摊（最大余数法）、部分退款金额。
- **规则**：① 折扣金额向下取整（不是应付取整）；② 分摊到行的合计严格等于优惠总额；③ 适用行按 sku_id 与 `line_indexes` 判断（同一 SKU 多行可区分）；④ 纯函数，不访问数据库。活动配置后台为规划。
- **事件**：无。**文档**：[MODULE.md](17-campaign/MODULE.md)

### 18-lottery 抽奖 `lottery` — 标准
- **覆盖**：活动与奖池（概率、库存、兜底奖）、次数发放（支付、后台、事件）、抽奖、中奖记录与公示、领奖（实物收货信息）、发奖与重试、发货、规则修订记录、审计（随机数与规则哈希）。
- **规则**：① 每日 / 总次数、奖品库存、每人中奖上限全部条件 UPDATE；随机源 `secrets`；② 支付成功按金额发次数（读 payment_id、user_id、amount_cents、business_type、store_id、paid_at）；`refund.succeeded` 按 payment_id 收回该笔支付发放的未用次数；③ 中奖发 `lottery.prize_won`（delivery_key 幂等），券 / 积分 / 会员卡由对应模块发放后回 `lottery.delivery_result`，失败按退避重发；④ 未领取到期作废，实物需收货信息（查看写审计）。
- **事件**：发 `lottery.activity_published` `activity_ended` `chances_granted` `drawn` `prize_won` `prize_shipped` `prize_delivered` `prize_delivery_failed`；收 `payment.succeeded` `refund.succeeded` `lottery.delivery_result`（签到主题 `checkin_topic` 默认关闭）。
- **权限码**：`lottery:read` `lottery:write` `lottery:publish` `lottery:grant` `lottery:fulfil` `lottery:export`。
- **配置 / 扩展**：§9；`lottery.hooks`：`set_audience_checker` `set_risk_checker` `set_member_display` `set_limiters`；`grant_chances(db, activity_id, member_id, chances, source, source_ref)` 供其他模块发次数。
- **文档**：[MODULE.md](18-lottery/MODULE.md)

---

## 通知与内容

### 19-review 评价 `review` — 基础
- **覆盖**：本人已完成订单的评价（每单一评、评分与长度校验、敏感词转审核）。审核后台与商家回复为规划（码 `review:*` 已在种子目录）。
- **文档**：[MODULE.md](19-review/MODULE.md)

### 20-notification 通知 `notification` — 标准
- **覆盖**：通知场景与模板映射、订阅消息额度、发件箱投递与重试（永久失败分类）、静默时段与频控、短信兜底、站内信、后台日志 / 统计 / 导出 / 重发、业务事件 → 场景。
- **规则**：① 事件映射集中在 `consumers.EVENT_BINDINGS`，只绑定生产者真实发出的字段（契约测试保证）：`payment.succeeded`（business_type=order）→ 支付成功、`order.refunded` → 退款成功、`member_card.expiring` → 卡到期、`lottery.prize_won`（实物）→ 中奖；② 每条请求按 dedupe_key 去重；③ 订阅消息默认经 28 `send_subscribe_message`，未配置时生产明确失败、开发用假发送器；④ 订阅消息推送（接受 / 拒绝 / 发送结果）经 28 `register_push_handler` 回写额度。
- **事件**：发 `notification.requested`；收 `notification.requested` `payment.succeeded` `order.refunded` `member_card.expiring` `lottery.prize_won`。
- **权限码**：`notification:read` `notification:export` `notification:resend` `notification:template`。
- **配置 / 扩展**：§9；`set_subscribe_sender` `set_sms_sender` `set_recipient_resolver` `set_store_timezone_resolver` `set_event_binding`；`notify(...)` / `notify_staff(...)` 供业务直接发通知。
- **文档**：[MODULE.md](20-notification/MODULE.md)

### 21-media 媒体上传 `media` — 规划
预签名直传（类型白名单、≤ `UPLOAD_MAX_BYTES`）、完成时服务端校验魔数并剥离 EXIF、未完成 24 小时清理；评价图 ≤ 9、头像 1。目前 10-after-sales 凭证经 `set_evidence_checker` 预留接入。

### 26-agent-support 智能体支撑 `agent_support` — 规划
知识库文档版本与切片（`kb.doc.updated` 供智能体向量化）、检索测试、智能体配置；服务间写接口用 `identity.api.service_call` 签名校验。

### 28-wechat-mp 小程序服务端 `wechat_mp` — 标准
- **覆盖**：唯一共享的小程序服务端客户端：code2Session、手机号、订阅消息、内容安全、小程序码、access_token 中控（stable_token）、消息推送（验签解密、去重、分发）。
- **规则**：① access_token 缓存提前 300 秒刷新，40001 / 42001 作废后重试一次（`invalidate_access_token`）；② 模块级 `send_subscribe_message(touser, template_id, data, page=None, miniprogram_state=None, lang=None) -> dict`、`access_token()`、`is_configured()`；③ 推送 `register_push_handler(event, fn(db, message))`，`dispatch` 去重后统一提交（已登记：identity 头像检测、notification 订阅事件）；④ 平台错误映射为 `WechatError` 子类（带 errcode）。
- **事件**：无（推送经处理函数分发）。**权限码**：无。
- **配置 / 扩展**：`WECHAT_MP_*`；`set_client`、`HttpMiniProgramClient(transport=…)`、`TokenStore`；生产检查 `wechat_mp`（真实 appid / secret、非假客户端、https、推送 AES 密钥、state=formal）；测试 `FakeMiniProgram` / `FakeWechatServer`。
- **文档**：[MODULE.md](28-wechat-mp/MODULE.md)

---

## 运营与基础设施

### 00-framework 框架 `luzback` — 标准
- **覆盖**：配置、数据库（Base / BigId / 时间与软删 mixin）、错误码与信封、幂等、发件箱与 `mark_processed`、审计、弹性（超时 / 重试 / 熔断 / 舱壁）、状态机、列表（`ListSpec`）与 CSV 导出、调度锁、安全（JWT、口令、TOTP、服务签名）、限流、应用工厂（/healthz /readyz /metrics）、测试工具。
- **规则**：① 后台列表一律 `ListSpec`（声明筛选 / 排序 / 关键词）；② `check_password_policy` 按 NIST 800-63B-4：默认 ≥ 15 位（与 MFA 合用传 8），≤ 128，无组成规则，拒绝常见口令与含账号信息；③ 生产禁止开发默认值并执行模块登记的检查；④ /metrics 只对内网或 `LUZ_METRICS_TOKEN` 开放。
- **配置**：`LUZ_*`（JWT、数据库、Redis、字段密钥、服务密钥、CORS、功能开关 `LUZ_FEATURES`）、限流与弹性参数。
- **文档**：[MODULE.md](00-framework/MODULE.md)

### 06-geo 定位与范围 `geo` — 基础
- **覆盖**：两点直线距离（Haversine）、门店配送范围判断、地理编码缓存与地图服务失败降级信号。
- **文档**：[MODULE.md](06-geo/MODULE.md)

### 12-queue 排队与桌台 `queueing` — 规划
取号（计数器条件自增、每人一张有效票）、等待估计、叫号 / 过号 / 重排、入座选桌（桌台条件 UPDATE）、桌台二维码供堂食下单。

### 13-reservation 预约 `booking` — 基础
- **覆盖**：时段名额原子占位、一人一约、时间冲突、候补与自动递补、取消截止、签到窗口、爽约任务（40 并发抢 8 个名额测试）。
- **事件**：发 `booking.promoted`。**文档**：[MODULE.md](13-reservation/MODULE.md)

### 23-app-settings 系统设置 `app_settings` — 规划
按门店 → 全局 → 环境变量默认的业务参数覆盖（分组注册、校验、定时生效、历史与审计）；公开子集给小程序。码 `settings:read` `settings:write` 已在种子目录。

### 24-report 报表与导出 `report` — 规划
营业额口径（现金渠道收款 − 退款，余额消费单列）、日物化、驾驶舱待办、图表、> 5000 行异步导出。码 `dashboard:read` `report:read` `report:export` 已在种子目录。

### 25-jobs 任务与事件进程 `jobs` — 标准
- **覆盖**：发现各模块 `jobs.JOBS` 与 `events.SUBSCRIPTIONS`、调度进程（分布式锁，Redis 可用时 `RedisLock`）、事件 worker（每个订阅者独立会话，失败整体退避重试）、心跳文件、`python -m jobs scheduler|worker|list`。
- **配置**：`--tick`、`LUZ_WORKER_HEARTBEAT_FILE`、`LUZ_FEATURES`。
- **文档**：[MODULE.md](25-jobs/MODULE.md)

---

## 事件总表

有订阅者的主题（生产者 → 消费者）。字段以生产者源码为准，`tests/test_event_contracts.py` 解析每个 `enqueue_event` 并用其字段运行全部订阅者，订阅不存在的主题或读取未发出的字段即失败；`python -X utf8 tests/event_contracts.py` 打印全部主题与字段。

| 主题 | 生产者 | 消费者 |
|---|---|---|
| `payment.succeeded` | 09-wechat-pay | 08-order（兜底成交）、15-recharge、29-member-card、18-lottery（发次数）、20-notification |
| `refund.succeeded` | 09-wechat-pay | 10-after-sales、15-recharge、29-member-card、18-lottery（收回次数） |
| `refund.failed` / `refund.abnormal` | 09-wechat-pay | 10-after-sales、15-recharge、29-member-card |
| `order.cancelled` | 08-order | 09-wechat-pay（待关单） |
| `order.completed` | 08-order | 14-membership（送积分） |
| `order.refunded` | 08-order | 14-membership（追回积分）、20-notification |
| `after_sale.completed` | 10-after-sales | 29-member-card（终止卡） |
| `member.registered` | 01-identity | 16-coupon（注册礼） |
| `member_card.expiring` | 29-member-card | 20-notification |
| `lottery.prize_won` | 18-lottery | 16-coupon、14-membership、29-member-card、20-notification（实物） |
| `lottery.delivery_result` | 16-coupon、14-membership、29-member-card | 18-lottery |
| `notification.requested` | 20-notification（`notify` / `notify_staff`） | 20-notification |
| `coupon.issue_requested` | 项目模块（入站命令，基线无生产者） | 16-coupon |

其余主题目前没有订阅者，供通知绑定、报表与项目扩展使用：`payment.closed` `payment.failed`（09）；`order.created` `order.paid` `order.fulfilling` `order.fulfilled`（08）；`after_sale.*` 其余 9 个（10）；`member.phone_bound` `member.disabled` `member.deleted`（01）；`staff.*`（22）；`wallet.*` `recharge.*` `balance.*`（15）；`member_card.*` 其余（29）；`coupon.*`（16）；`lottery.*` 其余（18）；`points.*`（14）；`booking.promoted`（13）。
