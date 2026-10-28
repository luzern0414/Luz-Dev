# 模块：payment（微信支付 · 小程序 / JSAPI）

| 项 | 内容 |
|---|---|
| 状态 | 支付单、回调幂等、金额校验已实现（`wechat_pay/__init__.py`，本地联调用 HMAC 验签）；APIv3 请求签名、调起支付签名、回调验签、签名探测识别、AES-256-GCM 解密、事实核对、交易状态映射已实现为纯函数（`wechat_pay/apiv3.py`，测试 17 项）；渠道 HTTP 客户端（下单、查单、关单、退款、账单）待接入 |
| 代码 | `09-wechat-pay/code/wechat_pay/`、测试 `09-wechat-pay/code/tests/` |
| 依赖 | framework（幂等、Outbox、限流、审计）、order（订单状态机）、identity（openid） |
| 规范 | `references/standards.json` 中 PAY-001～PAY-015、RFD-001～RFD-005、MP-005、MONEY-*、ORDER-001～ORDER-006；方案核对表自动带出 |

本文件规定支付的**接口、状态、安全与成功 / 失败后的逻辑**。外部平台细节以官方原文为准，下表是 2026-10-26 经 Luz-Crawl 读取官方页面后的核对结果；接入前重新打开这些页面核对，有变化时先改 `references/standards.json` 再改本文件。

## 0 官方来源

| 主题 | 官方页面 | 本文用到的结论 |
|---|---|---|
| 文档索引 | [商户文档 llms.txt](https://pay.weixin.qq.com/doc/v3/merchant/llms.txt) | 小程序支付 API 列表；每页都有 `.md` 纯文本版本 |
| 下单 | [JSAPI下单](https://pay.weixin.qq.com/doc/v3/merchant/4012791856)（小程序：[4012791897](https://pay.weixin.qq.com/doc/v3/merchant/4012791897)） | `POST /v3/pay/transactions/jsapi`；out_trade_no 6–32 位；金额为分；prepay_id 有效 2 小时；time_expire 规则 |
| 调起支付 | [JSAPI调起支付](https://pay.weixin.qq.com/doc/v3/merchant/4012791857)（小程序：[4012791898](https://pay.weixin.qq.com/doc/v3/merchant/4012791898)）、[wx.requestPayment](https://developers.weixin.qq.com/miniprogram/dev/api/payment/wx.requestPayment.html) | 前端参数由服务端签名 |
| 支付回调 | [支付成功回调通知](https://pay.weixin.qq.com/doc/v3/merchant/4012791861)（小程序：[4012791902](https://pay.weixin.qq.com/doc/v3/merchant/4012791902)） | 验签头、签名探测、5 秒应答、200/204、重发频次、trade_state 枚举、必须结合查单 |
| 解密 | [如何解密回调报文](https://pay.weixin.qq.com/doc/v3/merchant/4012071382) | AEAD_AES_256_GCM，APIv3 密钥 32 字节 |
| 签名 | [签名认证](https://pay.weixin.qq.com/doc/v3/merchant/4012365342)、[如何应对签名探测流量](https://pay.weixin.qq.com/doc/v3/merchant/4013053249) | 请求与应答都要签名 / 验签 |
| 开发指引 | [小程序支付开发指引 4012791911](https://pay.weixin.qq.com/doc/v3/merchant/4012791911) | 官方流程（调起后查单）、订单状态流转、防抖、交易类小程序发货管理要求 |
| 查单 / 关单 | 小程序：[商户订单号查询订单 4012791900](https://pay.weixin.qq.com/doc/v3/merchant/4012791900)、[关闭订单 4012791901](https://pay.weixin.qq.com/doc/v3/merchant/4012791901)；JSAPI：[关闭订单 4012791860](https://pay.weixin.qq.com/doc/v3/merchant/4012791860) | 关单 `POST …/out-trade-no/{out_trade_no}/close`，成功 204 |
| 退款 | [退款申请 4012791862](https://pay.weixin.qq.com/doc/v3/merchant/4012791862)（小程序：[4012791903](https://pay.weixin.qq.com/doc/v3/merchant/4012791903)～[4012791906](https://pay.weixin.qq.com/doc/v3/merchant/4012791906)） | `POST /v3/refund/domestic/refunds`；365 天内；最多 50 次部分退款；重试沿用原退款单号；状态枚举 |
| 账单 | 小程序：[申请交易账单 4012791907](https://pay.weixin.qq.com/doc/v3/merchant/4012791907)、[下载账单 4012791909](https://pay.weixin.qq.com/doc/v3/merchant/4012791909) | 每日对账 |
| 密钥 | [证书密钥概览 4024350132](https://pay.weixin.qq.com/doc/v3/merchant/4024350132)、[微信支付公钥 4012153196](https://pay.weixin.qq.com/doc/v3/merchant/4012153196) | 微信支付公钥一次申请永久有效；平台证书有效期 5 年，可切换 |

## 1 支付主路径

| 步骤 | 执行方 | 动作 | 接口 / 位置 | 结果状态 |
|---|---|---|---|---|
| 1 | 小程序 | 用户在确认页点击“确认支付”，携带 `Idempotency-Key` | — | — |
| 2 | 后端 | 校验订单属于当前用户、状态为待支付；金额取订单快照（不信任前端） | `POST /api/v1/app/orders/{id}/pay` | 订单 `pending_payment` |
| 3 | 后端 | 复用该订单未过期的待支付渠道单；没有则创建支付单 `pending`，生成 out_trade_no | `create_payment` | 支付单 `pending` |
| 4 | 后端 | 调用 JSAPI 下单，`time_expire` 与本地支付时限一致；`SYSTEM_ERROR` / 超时用相同参数有限重试 | 渠道客户端（待接入）+ `apiv3.build_authorization` | 保存 prepay_id 与获取时间 |
| 5 | 后端 | 用商户私钥生成 timeStamp、nonceStr、package、signType=RSA、paySign 返回 | `apiv3.jsapi_pay_params` | — |
| 6 | 小程序 | `wx.requestPayment`；任何回调结果都进入“支付结果确认中”页 | — | 不改变业务状态 |
| 7 | 小程序 | 轮询服务端结果（1s、2s、3s…，最多 10 次），从服务端状态重建结果页 | `GET /api/v1/app/payments/{out_trade_no}` | 以服务端为准 |
| 8 | 微信支付 | 支付成功后 POST 回调 | `POST /api/v1/payments/notify/wechat` | — |
| 9 | 后端 | 验签 → 解密 → 核对 appid / mchid / 单号 / 金额 / 状态 → 同一事务条件更新支付单与订单并写 Outbox → 立即应答 204 | `apiv3.parse_notify` + `apiv3.check_transaction` + `handle_notify` | 订单 `paid` |
| 10 | 后端 | Outbox 消费者执行成功后的副作用（§5） | Outbox | — |

**原则**：订单是否已支付只以第 9 步（或 §6 主动查单）的服务端事实为准。小程序的 success / fail 只决定界面（PAY-001）。

## 2 微信支付 API 清单（普通商户 · 小程序支付）

| 用途 | 方法与路径 | 关键约束 | 本地封装 |
|---|---|---|---|
| 下单 | `POST /v3/pay/transactions/jsapi` | appid 与 mchid 有绑定关系；`payer.openid` 来自同一 appid；`amount.total` 为大于 0 的整数分；`out_trade_no` 6–32 位 `[0-9A-Za-z_-\|*]` 且商户号下唯一；`time_expire` 不早于下单后 1 分钟、不晚于 15 天，不传时 7 天后无法支付；prepay_id 有效 2 小时，过期后用原下单参数重新下单获取新的 prepay_id；前端下单按钮防抖 | 待接入；签名用 `build_authorization` |
| 调起支付参数 | 小程序 `wx.requestPayment` | 签名串 `appId\n timeStamp\n nonceStr\n package\n`（每项后换行），SHA256withRSA，`signType=RSA` | `jsapi_pay_params` |
| 查单 | 按商户订单号查询（小程序支付 API 列表 4012791900）；也可按微信支付订单号查询（4012791899） | 官方流程：`requestPayment` 返回后由后端查单确认再展示结果；回调丢失、关单前也调用；结果交给与回调相同的处理逻辑 | 待接入；状态映射 `TRADE_STATE_MAP` |
| 关单 | `POST /v3/pay/transactions/out-trade-no/{out_trade_no}/close`，body `{"mchid": …}` | 成功 204 无应答体；适用于未支付订单（用户取消或超时） | 待接入 |
| 支付回调 | 商户 `notify_url`（HTTPS，公网可达，按官方注意事项填写） | 5 秒内验签并应答；`event_type=TRANSACTION.SUCCESS`；`resource_type=encrypt-resource` | `parse_notify`、`check_transaction` |
| 退款申请 | `POST /v3/refund/domestic/refunds` | transaction_id 与 out_trade_no 二选一；`out_refund_no` ≤ 64 字节 `[0-9A-Za-z_-\|*@]`，同一单号多次请求只退一笔；支付成功后 365 天内；最多 50 次部分退款，换单号且间隔 1 分钟；失败重试沿用原单号；成功仅代表受理 | 待接入 |
| 查询退款 | `GET /v3/refund/domestic/refunds/{out_refund_no}` | 提交后每 1 分钟查一次，超过 5 分钟仍处理中则按 5、10、20、30 分钟衰减；零钱一般 5 分钟内到账，银行卡 1–3 个工作日 | 待接入 |
| 异常退款 | `POST /v3/refund/domestic/refunds/{refund_id}/apply-abnormal-refund` | `ABNORMAL` 状态的人工处理入口 | 待接入（后台人工） |
| 退款回调 | 退款结果回调通知（4012791906） | `event_type`：`REFUND.SUCCESS` / `REFUND.ABNORMAL` / `REFUND.CLOSED`；与支付回调相同的验签、解密、幂等规则 | 复用 `parse_notify` |
| 账单 | `GET /v3/bill/tradebill`、`GET /v3/bill/fundflowbill` → `download_url` | 次日 9 点开始生成、10 点后获取；仅三个月内；金额单位为**元**；交易账单只含支付成功订单；下载地址 5 分钟有效，下载后核对 SHA1；资金账单 3 QPS | 待接入 |

主域名 `https://api.mch.weixin.qq.com`，备域名 `https://api2.mch.weixin.qq.com`（异地接入点）。错误码：`SYSTEM_ERROR`（500，用相同参数重试）、`FREQUENCY_LIMITED`（429，退避）、`OUT_TRADE_NO_USED`（403，不重试，核对订单号）、`SIGN_ERROR`（401，检查签名）、`APPID_MCHID_NOT_MATCH`、`PARAM_ERROR`（400，不重试）。

## 3 状态

### 3.1 本地支付单

| 状态 | 进入条件 | 允许流转 | 守卫 |
|---|---|---|---|
| `pending` | 创建支付单 | → `success`（回调 / 查单 SUCCESS 或 REFUND）；→ `closed`（关单成功或查单 CLOSED）；→ `failed`（渠道明确失败） | `UPDATE … WHERE status='pending'`；同一业务同时最多一个 pending（部分唯一索引） |
| `success` | 核对通过的支付成功事实 | 终态；退款由退款单表达 | `channel_txn_id` 唯一 |
| `closed` | 关单成功或渠道已关闭 | 终态；若之后收到 SUCCESS 事实，记录支付成功并进入“已关闭仍支付”人工处理与自动退款 | — |
| `failed` | 渠道明确失败（付款码场景） | 终态 | — |

### 3.2 交易状态映射（`apiv3.TRADE_STATE_MAP`）

官方状态流转：下单 → `NOTPAY`；用户支付失败时仍为 `NOTPAY`；支付成功 → `SUCCESS`；商户 7 天内关单或超过 7 天微信自动关单 → `CLOSED`；支付成功后 1 年内申请退款成功 → `REFUND`。`CLOSED`、`SUCCESS`、`REFUND` 为终态。在可支付期内需要关单时，先查单确认仍为 `NOTPAY` 再关单，关单后按失败终态处理。

| trade_state | 含义 | 本地 | 说明 |
|---|---|---|---|
| `SUCCESS` | 支付成功 | `success` | 核对金额后推进订单 |
| `REFUND` | 转入退款 | `success` | 支付事实成立，退款状态看退款单 |
| `NOTPAY` | 未支付 | `pending` | 继续等待；到期关单 |
| `CLOSED` | 已关闭 | `closed` | — |
| `REVOKED` / `USERPAYING` / `PAYERROR` | 仅付款码支付返回 | `closed` / `pending` / `failed` | 小程序支付不应依赖这些状态 |

### 3.3 退款单

| 微信退款状态 | 本地 | 处理 |
|---|---|---|
| 申请受理（接口成功） | `refund_pending` | 界面显示“退款处理中” |
| `PROCESSING` | `refund_pending` | 定时查询 |
| `SUCCESS` | `refund_success` | Outbox 执行回滚策略（§7） |
| `CLOSED` | `closed` | 记录原因，允许重新发起（新退款单号） |
| `ABNORMAL` | `refund_pending` + `needs_manual` | 原路退回失败（银行卡作废或冻结），后台人工处理或发起异常退款 |

## 4 支付安全

| 规则 | 做法 | 实现 |
|---|---|---|
| PAY-005 验签 | 验签串 = `Wechatpay-Timestamp\n Wechatpay-Nonce\n 原始请求体\n`；用 `Wechatpay-Serial` 对应的公钥验签（`PUB_KEY_ID_` 开头为微信支付公钥，否则为平台证书）；未知序列号、时间戳偏差 > 5 分钟、签名无效一律拒绝 | `verify_signature` |
| 原始报文 | 验签必须用收到的原始字节；先解析 JSON 再序列化会导致验签失败 | `test_reserialized_json_fails_verification` |
| PAY-006 签名探测 | `Wechatpay-Signature` 以 `WECHATPAY/SIGNTEST/` 开头时按验签失败应答，不处理业务 | `is_sign_probe` |
| PAY-007 解密 | AES-256-GCM，APIv3 密钥 32 字节，`nonce` + `associated_data`；算法不符或解密失败拒绝 | `decrypt_resource` |
| PAY-008 核对事实 | appid、mchid、out_trade_no、amount.total、currency、trade_state、transaction_id 全部核对；不一致不改订单，记录并告警 | `check_transaction` |
| PAY-009 应答 | 成功 HTTP 204（或 200）无应答体；失败 4XX/5XX + `{"code":"FAIL","message":"…"}`；重复通知识别后继续应答成功 | `failure_response` |
| PAY-015 密钥 | 商户私钥、APIv3 密钥、微信支付公钥从密钥服务注入；不进仓库、镜像、日志；生产不存在 `LUZ_PAY_NOTIFY_SECRET` | 配置检查 |
| PAY-013 回调地址 | HTTPS、公网可达；服务器有防火墙时放通微信支付回调；IP 白名单不能替代验签 | 部署检查 |
| 日志 | 只记录 out_trade_no、transaction_id、序列号、结果与拒绝原因；不记录明文资源、openid 全量、密钥 | `PayNotifyLog` |

回调重发：未在 5 秒内收到成功应答时，微信支付按 15s、15s、30s、3m、10m、20m、30m、30m、30m、60m、3h、3h、3h、6h、6h 重发，最多 15 次。

## 5 支付成功后的逻辑

在验签与核对通过的同一事务内：

1. 支付单 `pending → success`（条件更新），写入 `channel_txn_id`、`paid_at`。
2. 订单 `pending_payment → paid`（条件更新）。
3. 写入 Outbox 事件 `payment.succeeded`（业务单号、金额、支付时间）。
4. 提交后应答 204。

Outbox 消费者（各自按事件 ID 幂等，失败重试，进入死信时告警）：

| 消费者 | 动作 |
|---|---|
| 库存 / 名额 | 预占转为确认 |
| 优惠券 | 锁定转为核销 |
| 会员 / 积分 | 发放积分、成长值、权益（写流水） |
| 履约 | 生成核销码、预约确认、发货任务 |
| 通知 | 订阅消息（未授权不影响业务） |
| 统计 | 销售报表 |

结果页：从服务端读取订单与支付状态，显示订单号、金额与一个主要下一步动作；路由使用替换导航，返回键不回到收银台（UI-002）。

## 6 支付失败、取消与未知的逻辑

| 情况 | 服务端 | 界面 |
|---|---|---|
| 用户取消（errMsg 含 cancel） | 不改变状态；订单保持待支付直到时限 | 显示“已取消支付”，可重新支付 |
| 前端返回其他失败 | 立即查单一次；仍为 NOTPAY 则保持待支付 | 进入确认中页，查单后显示失败与“重新支付” |
| 确认中超时（轮询结束仍 pending） | 定时查单继续补偿 | 显示“支付结果确认中，请勿重复支付”，提供刷新与联系客服 |
| 重新支付 | prepay_id 未过期且未到 time_expire：复用；prepay_id 过期但未到 time_expire：用原下单参数重新下单取新 prepay_id；已过 time_expire：先关单，再用新 out_trade_no 下单 | 同一订单不会出现两笔待支付 |
| 订单超时 | 关单 → 查单确认未支付 → 取消订单 → 释放库存与券（ORDER-004） | 订单显示已取消 |
| 关单后又收到 SUCCESS | 记录支付成功事实，订单进入人工处理并自动发起退款，告警 | 订单详情显示“退款处理中” |
| 同一订单两笔 SUCCESS | 第二笔自动退款，告警（PAY-003） | — |
| 回调核对失败（金额、商户号不符） | 不改变订单；记录 `PayNotifyLog` 并告警人工对账 | — |

## 6.1 上线前置条件（MP-005）

在小程序内收款的交易类小程序，必须按《交易类小程序运营规范》接入订单发货管理功能；不满足时可能被限制在正式环境调起支付。履约完成（核销、开课、发货）后由 Outbox 录入发货信息。

## 7 退款与回滚

| 步骤 | 动作 | 规则 |
|---|---|---|
| 1 | 有 `order:refund` 权限的账号发起，填写原因；大额需二次确认 | RFD-001 |
| 2 | 计算可退金额（按分摊后的行金额），条件更新 `refunded_cents` 防超退，生成退款单 `refund_pending` 与 `out_refund_no` | RFD-002 |
| 3 | 调用退款申请；超时或 `SYSTEM_ERROR` 用**原退款单号**重试；多次部分退款需换单号并间隔 1 分钟，最多 50 次 | RFD-002 |
| 4 | 退款回调或定时查询（每 1 分钟，5 分钟后衰减）确认终态 | RFD-003 |
| 5 | `SUCCESS` 后 Outbox 执行回滚：库存 / 名额回补（按策略）、券退回（已过期按策略）、积分与成长值回扣（不足时按方案规则）、会员权益收回 | RFD-004 |
| 6 | 每日对账核对退款 | RFD-005 |

## 8 对账（设计）

每日 10 点后申请前一天的交易账单（必要时资金账单），5 分钟内下载并核对 SHA1；账单金额为“元”，转换为分后与本地支付单、退款单逐笔核对，差异分为：本地无渠道有、本地有渠道无、金额不符、状态不符。差异写入对账差异表，财务在后台处理并留痕；对账任务幂等、可重跑。

## 9 本系统接口

| 方法 | 路径 | 鉴权 | 幂等 | 说明 |
|---|---|---|---|---|
| POST | `/api/v1/app/orders/{order_id}/pay` | access（归属） | `Idempotency-Key` 必须 | 创建或复用支付单，返回调起支付参数 |
| GET | `/api/v1/app/payments/{out_trade_no}` | access（归属） | — | 查询支付结果；pending 超过阈值时触发查单 |
| POST | `/api/v1/payments/notify/wechat` | 渠道签名 | 按 out_trade_no / transaction_id | 支付回调；不要求用户登录 |
| POST | `/api/v1/payments/notify/wechat/refund` | 渠道签名 | 按 out_refund_no | 退款回调（设计） |
| POST | `/api/v1/admin/orders/{id}/refund` | admin `order:refund` | `Idempotency-Key` 必须 | 发起退款（设计） |
| GET | `/api/v1/admin/reconciliation` | admin `finance:reconcile` | — | 对账结果与差异（设计） |

## 10 配置

| 变量 | 说明 |
|---|---|
| `WECHAT_PAY_APPID` | 小程序 appid（与商户号绑定） |
| `WECHAT_PAY_MCHID` | 商户号 |
| `WECHAT_PAY_APIV3_KEY` | APIv3 密钥（32 字节，从密钥服务注入） |
| `WECHAT_PAY_PRIVATE_KEY_PATH` | 商户 API 私钥文件路径（受限权限） |
| `WECHAT_PAY_CERT_SERIAL` | 商户 API 证书序列号 |
| `WECHAT_PAY_PUBLIC_KEYS` | 微信支付公钥 / 平台证书：`序列号=文件路径`，可配置多个以便轮换 |
| `WECHAT_PAY_NOTIFY_URL` | 支付回调地址（HTTPS，公网可达） |
| `WECHAT_PAY_REFUND_NOTIFY_URL` | 退款回调地址（不能携带参数） |
| `LUZ_PAY_NOTIFY_SECRET` | 仅本地联调的 HMAC 密钥；生产配置中不得存在 |

## 11 错误码

| 场景 | code | HTTP |
|---|---|---|
| 订单不存在或不属于用户 | 4040 | 404 |
| 订单不是待支付 | 4090 | 409 |
| 金额非法 | 4001 | 400 |
| 回调验签失败 / 签名探测 | 4010 | 401（回调应答体 `{"code":"FAIL"}`） |
| 回调事实不一致 | 4220 | 422 |
| 渠道下单、查单或退款失败 | 5020 | 502 |

## 12 测试

| 用例 | 文件 | 断言 |
|---|---|---|
| 有效回调、重复回调、伪造签名、金额不一致 | `tests/test_payment.py` | 订单只流转一次；错误签名与金额不符不改订单 |
| 官方格式回调验签与解密 | `tests/test_apiv3.py` | 通过并映射为 success |
| 篡改报文、重新序列化、签名探测、未知序列号、过期时间戳、缺少签名头 | `tests/test_apiv3.py` | 拒绝，原因码稳定 |
| 错误密钥、错误算法、短密钥 | `tests/test_apiv3.py` | 解密拒绝 |
| 金额、商户号、appid、单号、状态不符 | `tests/test_apiv3.py` | 核对拒绝 |
| trade_state 映射 | `tests/test_apiv3.py` | SUCCESS / REFUND / NOTPAY / CLOSED |
| 请求签名与 paySign | `tests/test_apiv3.py` | 用商户公钥可验证签名串格式 |
| 待补：并发回调、关单与回调竞态、重复支付自动退款、退款重试沿用单号 | — | 接入渠道客户端时补齐 |

## 13 可复用代码

- `wechat_pay/apiv3.py`：`build_authorization`、`jsapi_pay_params`、`verify_signature`、`is_sign_probe`、`decrypt_resource`、`parse_notify`、`check_transaction`、`TRADE_STATE_MAP`、`failure_response`。依赖 `cryptography`。生产优先使用官方 SDK；本模块可用于自研客户端或核对 SDK 行为。
- `wechat_pay/__init__.py`：`PaymentRecord`、`create_payment`、`handle_notify`（条件更新与幂等）。接入真实回调时，在 `handle_notify` 之前调用 `parse_notify` 与 `check_transaction`，HMAC 验签只保留在本地联调。

## 14 前端对接

| 页面 | 接口 | 行为 |
|---|---|---|
| 确认页 / 收银台 | 支付接口 | 携带 `Idempotency-Key`；拿到参数后调用 `wx.requestPayment` |
| 支付结果确认中 | 查询接口 | 按 §1 第 7 步轮询；成功进入支付成功页，失败进入支付失败页，超时显示“请勿重复支付” |
| 支付成功页 | 查询接口 | 从服务端重建；替换导航 |
| 支付失败页 | 支付接口 | “重新支付”沿用订单，服务端决定复用或替换渠道单 |
