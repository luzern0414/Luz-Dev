# 09-wechat-pay（微信支付 APIv3 · 小程序 / JSAPI · 普通商户直连）`wechat_pay`

## 0 官方来源

简写 `DOC/x` = `https://pay.weixin.qq.com/doc/v3/merchant/x`（每页有 `.md` 纯文本版）。核对日期均为 2026-10-28。

| 主题 | 官方页面 | 本模块用到的结论 | 核对日期 |
|---|---|---|---|
| 接口规则 | DOC/4012081709、DOC/4012081717 | HTTPS + 必带 User-Agent；JSON 不允许 null、不支持 4 字节字符；202 用原参数重试，429 降频，5xx 重试；错误不依赖 message | 2026-10-28 |
| 请求签名 | DOC/4012365336、4012365334、4012365337 | 签名串 5 行 `方法\nURL\n时间戳\n随机串\n报文\n`；SHA256withRSA；Authorization 5 字段；官方测试私钥与期望签名（已写成测试向量） | 2026-10-28 |
| 应答 / 回调验签 | DOC/4013053249、DOC/4013053420 | `时间戳\n随机串\n原始报文\n`；偏差 > 5 分钟拒绝；签名探测 `WECHATPAY/SIGNTEST/`；下载账单不验签改验哈希 | 2026-10-28 |
| 公钥 / 平台证书 | DOC/4012153196、DOC/4012154180、DOC/4013038816、DOC/4012551764、DOC/4024350132 | `PUB_KEY_ID_` 用微信支付公钥，否则平台证书；灰度期两种签名随机，必须并存；公钥模式请求带 `Wechatpay-Serial` | 2026-10-28 |
| 解密 | DOC/4012071382、DOC/4012791902 | AEAD_AES_256_GCM，APIv3 密钥 32 字节；associated_data 可能为空 | 2026-10-28 |
| 敏感字段加密 | DOC/4013053257、DOC/4013053264 | OAEP(SHA-1, MGF1-SHA1) + Base64，≤ 214 字节；请求头带 `Wechatpay-Serial` | 2026-10-28 |
| 下单 | DOC/4012791897（小程序）、DOC/4012791856 | out_trade_no 6–32 位 `[0-9A-Za-z_-\|*]`；description ≤ 127；time_expire 1 分钟–15 天；prepay_id 2 小时；同单号同参数可重复下单，参数不同 OUT_TRADE_NO_USED；SYSTEM_ERROR 同参数重试 | 2026-10-28 |
| 调起支付 | DOC/4012791898、DOC/4012365341 | paySign 4 行 `appId\ntimeStamp\nnonceStr\nprepay_id=…\n`，signType=RSA，appid 与下单一致 | 2026-10-28 |
| 支付回调 | DOC/4012791902、DOC/4012075420 | 5 秒内应答；成功 200/204 无体，失败 4xx/5xx + `{"code":"FAIL","message":…}`；15 次重发；不做登录态校验；notify_url https、无查询参数 | 2026-10-28 |
| 查单 / 关单 | DOC/4012791900、DOC/4012791901、DOC/4012791911、DOC/4012791869 | 只有验签成功且 SUCCESS 才算已支付；NOTPAY 不能直接当失败；先查单再关单；关单可重入、成功 204；过 time_expire 关单后换新单号 | 2026-10-28 |
| 最佳实践 | DOC/4012075249、DOC/4014959631 | 主动查单节奏 5s/30s/1m/3m/5m/10m/30m；退款非 200 先查询再用原单重试 | 2026-10-28 |
| 退款申请 / 查询 / 回调 / 异常退款 | DOC/4012791903～4012791906、DOC/4013071031、DOC/4013071200 | 365 天内；最多 50 次部分退款、间隔 1 分钟、换新单号；失败重试沿用原 out_refund_no；受理≠成功；查询每 1 分钟、5 分钟后 5/10/20/30 分钟；REFUND.SUCCESS/ABNORMAL/CLOSED；ABNORMAL 走异常退款 | 2026-10-28 |
| 账单 | DOC/4012791907、DOC/4012791909、DOC/4013071246、DOC/4013071254 | T+1 10 点后；三个月内、不能是当天；GZIP；download_url 5 分钟；SHA1；字段前有反引号；金额单位元；NO_STATEMENT_EXIST 停止、STATEMENT_CREATING 半小时重试 | 2026-10-28 |
| 发货信息管理 | developers.weixin.qq.com …/order-shipping/order-shipping.html、…/order_shipping/api_uploadshippinginfo.md、…/jiaoyilei/yunyingguifan.md | upload_shipping_info；logistics_type 3 虚拟发货；统一发货 1 项；item_desc ≤ 120；系统繁忙 -1/10060012/10060019 可重试；10060002 已发货 | 2026-10-28 |
| 虚拟支付 | 小程序运营规范 2.5.13、2.14.7（developers.weixin.qq.com/miniprogram/product/index.md） | 纯线上虚拟商品必须用小程序虚拟支付；本模块拒绝 `virtual_content` | 2026-10-28 |

未核实（按保守实现）：V3 最短关单间隔、已支付再关单 / 再下单的确切错误码（按 TRADE_ERROR 处理 + 查单兜底）、GZIP 时哈希针对哪份文件（压缩前后任一相符即通过）、虚拟发货是否必须上传与最终期限（默认上传，`WECHAT_PAY_SHIPPING_ENABLED` 可关）。

## 1 状态与依赖

| 项 | 内容 |
|---|---|
| 状态 | 标准（可复用）；测试 145 项 |
| 依赖 | 00-framework（db、errors、idempotency、outbox、audit、resilience、listing、export、scheduler）、identity（current_user、User.openid，延迟导入）、admin_rbac（默认后台授权器，延迟导入）、cryptography、httpx |
| 被依赖 | order、recharge、membership、reservation、after_sales（注册钩子 / 消费事件） |

## 2 职责与边界

做：支付单（任意业务 business_type + business_id）、下单与调起参数、回调、主动查单、关单、补偿、退款与异常退款、交易账单对账、小程序发货信息录入、后台查询 / 导出。
不做：订单状态与库存（业务模块在 `on_paid` / 事件里做）、售后审批（10-after-sales 调 `create_refund`）、资金账单、分账、合单、服务商模式、小程序虚拟支付。

## 3 数据模型

| 表 | 关键字段 | 索引 / 约束 |
|---|---|---|
| `payment_records` | business_type, business_id, business_no, user_id, store_id, goods_kind, out_trade_no, amount_cents, status, description, payer_openid, request_json（首次下单报文）, prepay_id, prepay_at, expires_at, trade_state, channel_txn_id, paid_at, closed_at, close_reason, close_requested_at, fail_code, query_count, next_query_at, last_queried_at, refunded_cents, refund_succeeded_cents, refund_count, last_refund_at, needs_manual, manual_reason | uq out_trade_no；uq channel_txn_id；部分唯一 (business_type, business_id) WHERE status IN (created, paying)；ix (business_type, business_id)；ix (status, next_query_at) |
| `refund_records` | payment_id, business_ref, out_refund_no(≤64), channel_refund_id, amount_cents, reason(≤80 字节), status, channel_status, refund_channel, user_received_account, requested_by, attempts, last_error_code, next_attempt_at, query_count, next_query_at, submitted_at, succeeded_at, finished_at, needs_manual | uq out_refund_no；ix payment_id；ix (status, next_query_at) |
| `pay_notify_logs` | kind(pay/refund), notify_id, event_type, out_trade_no, out_refund_no, channel_txn_id, serial, verified, result, reason, source(notify/query), received_at | 只追加；不存明文与 openid |
| `pay_recon_batches` | bill_date, status, attempts, next_attempt_at, hash_value, remote/local 计数, diff_count, last_error | uq bill_date |
| `pay_recon_diffs` | batch_id, bill_date, diff_type, ref_type, ref_no, out_trade_no, local/remote 金额与状态, resolved, resolved_by, note | uq (batch_id, ref_type, ref_no, diff_type) |
| `pay_shipping_uploads` | payment_id, out_trade_no, logistics_type, payload_json, status, attempts, next_attempt_at, last_errcode, uploaded_at | uq payment_id；ix (status, next_attempt_at) |

## 4 业务规则

1. 金额只来自业务对象（`register_payable` 解析器或 `create_payment` 调用方），整数分，> 0。
2. `goods_kind ∈ physical | offline_service | virtual_content`；`virtual_content` → 4220 `virtual_goods_require_virtual_payment`。
3. out_trade_no = 前缀(≤4) + 北京时间 yyMMddHHmmss + 10 位随机，≤ 26 位；description 去 4 字节字符 ≤ 127。
4. 同一业务同一时刻最多一个未完成支付单；已有 paid → 4090 `already_paid`。
5. 未完成单参数（金额、描述、openid、time_expire、notify_url、attach）完全一致且未过期：prepay_id 获取后 115 分钟内直接复用；之后用 `request_json` 原样重新下单（同一单号）。
6. 参数变化或过 time_expire：先查单 → 已支付则落库并 4090；未支付则关单 → closed(`replaced`) → 新单号下单。渠道不可用时 5020（`payment_unavailable`）。
7. 下单可重试错误（SYSTEM_ERROR / 429 / 超时 / 应答验签失败）：支付单保持 created，用户再点用同单号同参数；不可重试错误 → failed + `payment.failed`；OUT_TRADE_NO_USED / TRADE_ERROR 先查单。
8. 回调：验签（5 分钟、序列号、探测）→ 解密 → appid / mchid / 单号 / 金额 / 币种 / 状态核对 → 条件 UPDATE → 钩子 + 事件 → 204。验签失败 401；核对失败 400 + needs_manual；未知单号 404；重复通知 204。
9. 支付成功同事务调用 `on_paid`（保存点内）；钩子抛错、关单后才支付、同业务第二笔支付 → 支付仍记 paid + needs_manual + 自动全额退款（pending，任务提交）；未注册钩子 → needs_manual，不退款。
10. 查单结果与回调走同一函数；只有 SUCCESS / REFUND 记已支付；NOTPAY 只更新 trade_state。
11. 补偿任务按 5s/30s/1m/3m/5m/10m/30m 查单；过 time_expire 或登记待关单时先查单再关单；渠道无此单且到期 → 本地关闭。
12. 退款一条条件 UPDATE 同时裁决：已支付、累计 ≤ 实付、次数 < 50、距上次 ≥ 60 秒、支付后 ≤ 365 天；违规分别 4220 `exceeds_refundable` / 4225 / 4220 `refund_interval`（带 retry_after_s）/ 4220 `refund_window_expired`。
13. out_refund_no = `R` + out_trade_no + 两位序号；重试永远沿用原单号与原报文。
14. 退款可重试错误（SYSTEM_ERROR、超时、FREQUENCY_LIMITED、NOT_ENOUGH、5xx）→ pending，任务先查询：已受理则采纳，RESOURCE_NOT_EXISTS 才重新提交；5 次仍未受理 → failed 并释放额度。
15. 不可重试拒绝、CLOSED → 释放额度 + `refund.failed`；SUCCESS → `refund.succeeded`；ABNORMAL → needs_manual + `refund.abnormal`，后台发起异常退款（卡号姓名 OAEP 加密，不落库不审计）。
16. 退款查询：受理后每 60 秒，5 分钟后 5/10/20/30 分钟（之后 30 分钟）。
17. 对账：账单日 T+1 本地 10:00 后、三个月内、非当天；GZIP 解压后核对 SHA1；差异四类 missing_local / missing_remote / amount_mismatch / status_mismatch；渠道成功本地未成功 → 唤醒补偿查单；重跑保留已处理差异。
18. 发货上传：仅 paid；默认 logistics_type=3、统一发货 1 项、item_desc ≤ 120；快递必须单号与公司；每笔支付一条（幂等）；系统繁忙 / 网络退避重试，10060002 视为成功，其余失败人工。
19. 外部调用一律 `resilient`（熔断 `wechat_pay`、舱壁 30）；渠道调用前结束事务；任务与回调内不做慢操作。

## 5 状态机 / 关键流程

支付单：

| 从 | 到 | 触发 |
|---|---|---|
| created | paying | 下单成功（prepay_id） |
| created / paying | paid | 回调或查单 SUCCESS / REFUND |
| created / paying | closed | 关单成功、渠道 CLOSED、到期且渠道无单 |
| created / paying | failed | 下单不可重试错误 |
| closed | paid | 关单后仍收到支付成功事实（人工 + 自动退款） |
| paid / failed | — | 终态（退款由退款单表达）；非法流转 4090 |

退款单：`pending → processing | succeeded | abnormal | closed | failed`；`processing → succeeded | abnormal | closed`；`abnormal → processing | succeeded | closed`；succeeded / closed / failed 终态。

流程：顾客 `POST pay` → create_payment →（复用 / 重下单 / 关单换号）→ 返回 paySign 参数 → `wx.requestPayment` → 确认中页轮询 `GET /app/payments/{no}`（节流查单）→ 回调或补偿落库 → `payment.succeeded`。

## 6 接口

| 方法 | 路径 | 鉴权 | 幂等 | 权限码 | agent | 说明 |
|---|---|---|---|---|---|---|
| POST | `/api/v1/app/orders/{order_id}/pay` | 用户 | 必须 | — | no | 订单去支付 / 继续支付 |
| POST | `/api/v1/app/payments` | 用户 | 必须 | — | no | 通用业务支付 `{business_type, business_id}` |
| GET | `/api/v1/app/payments/{out_trade_no}` | 用户（他人 4040） | — | — | yes | 支付结果；未完成节流查单，渠道故障 `degraded=true` |
| POST | `/api/v1/webhooks/wechat/pay`（兼容 `/api/v1/payments/notify/wechat`） | APIv3 验签 | 状态机 | — | — | 支付回调，204 / FAIL |
| POST | `/api/v1/webhooks/wechat/refund` | APIv3 验签 | 状态机 | — | — | 退款回调 |
| GET | `/api/v1/admin/payments`、`/export`、`/{id}` | 员工 | — | payment:read | yes | 列表 / 导出 / 详情（含退款、发货、回调日志） |
| POST | `/api/v1/admin/payments/{id}/sync`、`/close` | 员工 | 必须 | payment:reconcile | no | 人工查单 / 关单 |
| POST | `/api/v1/admin/payments/{id}/refunds` | 员工 | 必须 | payment:refund | no | 人工退款（审计） |
| GET | `/api/v1/admin/refunds`、`/export`、`/{id}` | 员工 | — | payment:read | yes | 退款列表 / 导出 / 详情 |
| POST | `/api/v1/admin/refunds/{id}/abnormal`、`/sync` | 员工 | 必须 | payment:refund | no | 异常退款 / 查询 |
| GET | `/api/v1/admin/reconciliation/batches`、`/diffs` | 员工 | — | payment:read | yes | 对账批次与差异 |
| GET | `/api/v1/admin/reconciliation/diffs/export` | 员工 | — | payment:reconcile | no | 差异导出 |
| POST | `/api/v1/admin/reconciliation/run`、`/diffs/{id}/resolve` | 员工 | 必须 | payment:reconcile | no | 手动对账 / 处理差异 |
| GET / POST | `/api/v1/admin/payment-shipping`、`/{id}/retry` | 员工 | 重试必须 | payment:read / payment:reconcile | no | 发货上传列表 / 重试 |

后台列表筛选（ListSpec）：支付 `status, business_type, business_id, goods_kind, store_id, user_id, needs_manual, amount_cents_min/max, created_at_from/to, paid_at_from/to, paid_at_null`，关键词 out_trade_no / transaction_id / business_no，排序 amount_cents / created_at / paid_at；退款 `status, payment_id, business_type, store_id, needs_manual, amount_cents, created_at, succeeded_at`，关键词 out_refund_no / refund_id / out_trade_no / transaction_id；差异 `diff_type, ref_type, resolved, batch_id, bill_date, created_at`。门店范围外 4040。

内部函数（CONTRACTS）：`create_payment`、`pay_business`、`close_payment`、`query_and_reconcile`、`paid_total`、`get_business_payment`、`handle_pay_notify` / `handle_refund_notify`（`handle_notify` 为别名）、`create_refund`、`get_refund_status`、`request_shipping_upload`、`reconcile_bill`、`register_payable` / `register_on_paid` / `register_on_refunded`。

## 7 事件

发出（payload 只含 id 与必要字段，无 openid）：

| 主题 | payload |
|---|---|
| `payment.succeeded` | payment_id, business_type, business_id, business_no, user_id, store_id, amount_cents, out_trade_no, transaction_id, paid_at, needs_manual, source |
| `payment.closed` | 同上公共字段 + reason（replaced / channel_closed / expired / business_cancel / never_created / admin_close） |
| `payment.failed` | 公共字段 + code |
| `refund.succeeded` / `refund.failed` / `refund.abnormal` | refund_id, payment_id, business_type, business_id, business_ref, amount_cents, out_refund_no, out_trade_no, status（failed 另含 reason） |

消费（`wechat_pay/events.py` `SUBSCRIPTIONS`）：`order.cancelled {order_id}` → 未完成支付单登记待关单并唤醒补偿任务（幂等，不提交，不调外部）。

## 8 并发与一致性

- 状态推进全部 `UPDATE … WHERE status IN (…)` + rowcount；channel_txn_id 唯一兜底并发回调（20 线程只结算一次）。
- 退款额度 / 次数 / 间隔 / 期限在一条条件 UPDATE 中裁决（50 线程抢 1000 分只成功 10 笔 × 100 分）。
- 部分唯一索引保证同业务一个未完成支付单；并发创建由 IntegrityError 重读。
- 渠道调用前 commit，结果用条件 UPDATE 落库；业务钩子在保存点内，失败不影响支付事实。
- 补偿：`wechat_pay.compensate`（30s）、`refund_retry`（60s）、`refund_query`（60s）、`shipping_retry`（300s）、`reconcile`（1800s），`jobs.JOBS`，均幂等可重跑。

## 9 配置

见 §13 配置表；所有密钥只从环境变量 / 密钥服务注入，`assert_production_ready()`（导入时另登记 `luzback.register_production_check("wechat_pay", …)`，app / jobs 启动时由 `Settings.assert_production_safe()` 统一执行）在生产拒绝：假客户端、APIv3 密钥非 32 字节、缺私钥 / 序列号 / 公钥或平台证书、回调地址非 https 或带查询参数。

## 10 错误码

| 场景 | code | HTTP |
|---|---|---|
| 参数错误、金额 ≤ 0、未知筛选参数 | 4001 | 400 |
| 未登录 / 令牌无效 | 4010 | 401 |
| 无权限 | 4030 | 403 |
| 支付单 / 退款单 / 业务对象不存在或非本人、门店范围外 | 4040 | 404 |
| 已支付、状态冲突、非异常退款发起异常退款 | 4090 | 409 |
| 虚拟商品、截止时间过近、超额退款、间隔不足、超 365 天、账单未就绪、核对不符 | 4220 | 422 |
| 退款超过 50 次 | 4225 | 422 |
| 渠道失败 / 超时 | 5020 | 502 |
| 熔断打开 | 5030 | 503 |
| 回调（渠道格式例外） | 204；验签失败 401、报文 / 核对失败 400、未知单号 404、内部错误 500，体 `{"code":"FAIL","message":…}` | — |

## 11 测试清单（149 项）

| 文件 | 覆盖 |
|---|---|
| `tests/test_apiv3.py` | 官方签名测试向量；GET 空行；paySign；验签成功 / 篡改 / 重新序列化 / 探测 / 过期 / 未来 / 未知序列号 / 缺头 / 非 Base64；公钥与平台证书并存；空 associated_data；错密钥 / 算法 / 短密钥；OAEP；事实核对 7 类；状态映射；单号、清洗、goods_kind、截止时间、查询节奏、退款规则、账单解析、状态机 |
| `tests/test_wechat_pay_payment.py` | 创建与 paySign；虚拟商品拒绝；复用 2 小时；原单号重下单；过期关单换号（先查后关）；改价换单；已支付 4090；SYSTEM_ERROR / 超时 / PARAM_ERROR；熔断；回调成功 / 重复 / 篡改 / 探测 / 过期 / 金额不符 / appid 不符 / 未知单 / 非成功事件；钩子拒绝自动退款；未注册钩子；关单后支付；20 线程并发回调；查单；关单（未付 / 已付 / 失败转补偿）；顾客查询归属与降级节流；补偿节奏与到期关单；任务与事件接线 |
| `tests/test_wechat_pay_refund.py` | 受理 + 成功回调 + 钩子；回调篡改 / 金额不符；CLOSED 释放后换号；ABNORMAL + 异常退款加密；超额；1 分钟间隔；50 次；365 天；未支付；50 线程防超退；SYSTEM_ERROR 同单号重试；应答丢失查询采纳；FREQUENCY_LIMITED / NOT_ENOUGH；拒绝释放；重试上限；查询节奏 |
| `tests/test_wechat_pay_bill.py` | 四类差异；重跑幂等保留已处理；哈希不符；非压缩账单；无账单；生成中 30 分钟；日期规则；发货上传成功 / 幂等 / 繁忙重试 / 网络 / 错误码 / 上限与人工重试 / 校验 |
| `tests/test_wechat_pay_http.py` | HTTP 客户端对接假服务器：请求签名被服务端验证、应答验签、错误密钥 SIGN_ERROR、探测应答丢弃并重试、错误分类、同单号异参、超时、备域名、账单下载、异常退款带 Wechatpay-Serial、生产配置检查 |
| `tests/test_wechat_pay_api.py` | 4010 / 4001 / 4093 / 4040 / 4220 / 5020 信封；回调 204 与 FAIL；验签失败指标；后台 4010 / 4030、列表筛选排序关键词区间、未知参数、导出 CSV、门店范围、退款 / 同步 / 关单、对账接口、发货列表 |

## 12 前端对接

| 页面 | 接口 | 行为 |
|---|---|---|
| 确认页 / 收银台 | `POST /app/orders/{id}/pay` 或 `/app/payments` | 防抖；带 Idempotency-Key；拿 `pay_params` 调 `wx.requestPayment`；5020 + `payment_unavailable` 提示稍后支付 |
| 支付结果确认中 | `GET /app/payments/{out_trade_no}` | 任何回调结果都进入此页；1s、2s、3s… 轮询至多 10 次；以 `status` 为准（paid 成功页；closed / failed 失败页）；超时提示“请勿重复支付” |
| 重新支付 | 同收银台 | 服务端决定复用 / 重下单 / 换单号 |
| 后台支付 / 退款 / 对账 | `/admin/...` | 列表筛选与导出同一参数；退款前展示可退金额与次数 |

## 13 定制点

配置（环境变量 → 默认值）：

| 键 | 默认 | 说明 |
|---|---|---|
| WECHAT_PAY_CLIENT | fake | 生产必须 http |
| WECHAT_PAY_APPID / WECHAT_PAY_MCHID | 取 WECHAT_APPID / WECHAT_MCH_ID | 小程序 appid 与商户号 |
| WECHAT_PAY_APIV3_KEY | 空 | 32 字节 |
| WECHAT_PAY_PRIVATE_KEY_PATH / _PEM | 空 | 商户 API 私钥 |
| WECHAT_PAY_CERT_SERIAL | 空 | 商户 API 证书序列号 |
| WECHAT_PAY_PUBLIC_KEY_ID / _PATH | 空 | 微信支付公钥（推荐） |
| WECHAT_PAY_PLATFORM_CERTS | 空 | `序列号=路径,…` 平台证书（可与公钥并存） |
| WECHAT_PAY_NOTIFY_URL / WECHAT_PAY_REFUND_NOTIFY_URL | example.com 占位 | https、无查询参数 |
| WECHAT_PAY_BASE_URL / _BACKUP_BASE_URL | api.mch / api2.mch | 主备域名 |
| WECHAT_PAY_TIMEOUT_PREPAY / QUERY / CLOSE / REFUND / BILL | 8 / 5 / 5 / 10 / 30 秒 | SPEC §9 |
| WECHAT_PAY_QUERY_ATTEMPTS / CLOSE_ATTEMPTS / BULKHEAD | 3 / 2 / 30 | 下单与退款不在调用内重试 |
| WECHAT_PAY_TIMESTAMP_SKEW | 300 | 验签时间容差（秒） |
| WECHAT_PAY_OUT_TRADE_NO_PREFIX | P | ≤ 4 位字母数字 |
| WECHAT_PAY_EXPIRE_MINUTES | 15 | 未给截止时间时的默认 |
| WECHAT_PAY_PREPAY_REUSE_SECONDS | 6900 | prepay_id 复用窗口（< 2 小时） |
| WECHAT_PAY_QUERY_SCHEDULE | 5,30,60,180,300,600,1800 | 补偿查单节奏 |
| WECHAT_PAY_APP_SYNC_INTERVAL | 3 | 顾客查询触发查单的最小间隔 |
| WECHAT_PAY_AUTO_REFUND_ORPHANS | true | 异常支付自动退款 |
| WECHAT_PAY_REFUND_MAX_TIMES / MIN_INTERVAL / WINDOW_DAYS | 50 / 60 / 365 | 官方限制 |
| WECHAT_PAY_REFUND_MAX_ATTEMPTS | 5 | 未受理退款提交上限 |
| WECHAT_PAY_REFUND_QUERY_FAST / _FAST_WINDOW / _BACKOFF | 60 / 300 / 5,10,20,30 | 退款查询节奏 |
| WECHAT_PAY_BILL_READY_HOUR / BILL_RETRY_MINUTES / BILL_MAX_DAYS | 10 / 30 / 90 | 对账 |
| WECHAT_PAY_SHIPPING_ENABLED / SHIPPING_MAX_ATTEMPTS | true / 8 | 发货信息上传 |
| WECHAT_PAY_TZ | Asia/Shanghai | 单号日期与账单日 |

扩展钩子：`register_payable(business_type, fn(db, user_id, business_id) -> Payable)`；`register_on_paid(business_type, fn(db, PaymentInfo))`；`register_on_refunded(key, fn(db, RefundInfo))`（key = business_ref 前缀或 business_type）；`set_openid_resolver`；`set_client(WechatPayClient)`；`set_shipping_client(ShippingClient)`（生产注入 `HttpShippingClient()`：access_token 默认取 28-wechat-mp `get_client().access_token()`，40001 / 42001 作废缓存后重试一次；也可传 `access_token=` / `invalidate=`）；后台鉴权为 22-admin-rbac `require_permission`（发起退款、异常退款、关单、对账差异处理 fresh=True），无本地授权器；对外查询 `find_active_refund(db, business_ref) -> RefundRecord | None`（pending / processing / succeeded / abnormal，最新一笔；10-after-sales 重提前查重）；`set_config(WechatPayConfig)`。`adapters.install_defaults` 只在 order 模块未注册时为 v1 订单兜底。测试：`wechat_pay.testing.wechat_pay_kit()` + `FakeWechatPay`（可作 httpx MockTransport 假服务器）。

不包含：小程序虚拟支付（`virtual_content` 一律拒绝）、服务商 / 合单 / 付款码 / H5 / Native / APP 支付、分账、资金账单、平台证书自动下载轮换（需配置文件热更新）、电子发票、代金券明细核算、合单发货与特殊发货报备、发货事件推送（trade_manage_*）接收。
