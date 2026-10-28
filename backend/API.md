# 后端 API 目录与约定

本文件是全部对外接口的总目录，来源于各模块 `MODULE.md`。接口变更时先改模块文档，再同步本表。状态列：**已实现** 表示有代码与测试；**设计** 表示仅有文档。

> 标准模块（`SPEC.md` §2）的接口、权限码与鉴权以各自 `MODULE.md` §6 为准（本表下方旧行未逐条同步时以 MODULE.md 为准）；挂载与路径冲突由 `tests/test_app_wiring.py` 校验。后台接口一律 `admin_rbac.require_permission`（`SECURITY.md` §1–2）。

## 1 通用约定

### 1.1 基础路径与版本

| 端 | 前缀 | 说明 |
|---|---|---|
| 小程序 | `/api/v1/app/` | 用户端，access 令牌 |
| 管理后台 | `/api/v1/admin/` | 后台账号，RBAC 权限码 |
| 员工端 | `/api/v1/staff/` | 门店员工（叫号、入座），员工令牌 |
| 骑手端 | `/api/v1/rider/` | 骑手 App，骑手令牌 |
| 回调 | `/api/v1/payments/notify/`、`/api/v1/webhooks/` | 第三方回调，渠道签名，不使用用户令牌 |
| 系统 | `/healthz`、`/readyz`、`/metrics` | 仅内网可访问（网关不对外暴露） |

版本变更规则：向后兼容的新增字段不升版本；删除字段、改变语义必须升到 `/api/v2/`，旧版至少保留 6 个月。

### 1.2 请求头

| 头 | 必填 | 说明 |
|---|---|---|
| `Authorization: Bearer <token>` | 用户接口必填 | access 令牌 |
| `Idempotency-Key` | 写操作（创建、支付、领取、取消、评价、退款）必填 | 客户端生成的 UUID（36 位），同一次操作的重试沿用 |
| `X-Request-Id` | 可选 | 由网关或客户端传入，≤ 64 位字母数字与横线；未传则服务端生成 |
| `X-Client-Version` | 小程序必填 | 版本号，用于灰度与强制升级 |
| `X-Device-Id` | 可选 | 设备标识（风控用，不作为身份依据） |

### 1.3 响应

成功与失败统一为信封：

```json
{"code": 0, "message": "ok", "data": {}}
```

响应头固定包含：`X-Request-Id`、`X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、`Cache-Control: no-store`（用户接口）。

### 1.4 分页

| 类型 | 请求 | 响应 `data` |
|---|---|---|
| 页码分页（管理后台） | `page`（从 1），`page_size`（默认 20，最大 100） | `{items, total, page, page_size}` |
| 游标分页（小程序列表） | `cursor`（不透明字符串：base64 的 `(sort_value, id)`，由上一页 `next_cursor` 原样回传），`limit`（默认 10，最大 20） | `{items, next_cursor, has_more, total?}`（`total` 仅首页且 ≤ 10000 时返回） |

筛选与排序的通用写法见 `SPEC.md` §3.4：`status=a,b`、`from=&to=`、`keyword=`、`sort=-created_at,amount`（字段白名单）。

小程序商品列表默认 `limit=10`，与前端“每页 10 条”一致（见 Luz System `references/miniprogram-design.html` 与 handoff 页面规范）。

### 1.5 排序与筛选

- 排序参数 `sort`：`default`、`sales_desc`（月销量）、`price_asc`、`price_desc`、`new`。
- 筛选参数使用字段名直传：`category_id`、`price_min`、`price_max`、`spicy`、`prep_max_minutes`、`keyword`。
- 不支持任意字段排序（防止慢查询）；未知参数直接忽略并在日志记录。

### 1.6 金额与时间

- 金额：整数分（`*_cents`）。
- 时间：ISO 8601，带时区；服务端存 UTC。

### 1.7 限流

| 维度 | 默认 | 说明 |
|---|---|---|
| 单 IP 单接口 | 20 次 / 秒（突发 40） | 框架 `TokenBucketLimiter`，生产换 Redis |
| 登录与验证码 | 5 次 / 分钟 / 手机号 | 超限返回 4290 |
| 下单与支付 | 3 次 / 秒 / 用户 | 防刷单 |
| 管理后台登录 | 5 次失败锁定 15 分钟 | 见 admin-rbac |

超限返回 HTTP 429、`code = 4290`，响应头 `Retry-After`。

## 2 错误码

见 `backend/SPEC.md` §4（框架 `ErrorCode` 为唯一来源）。摘要：0 成功；4001 参数；4010 未登录或令牌失效；4030 无权限；4040 不存在；4090 状态冲突；4091 库存不足；4092 幂等冲突；4220 业务规则；4290 限流；5000 内部错误；5020 上游失败。

## 3 接口目录

### 3.1 系统

| 方法 | 路径 | 鉴权 | 状态 | 说明 |
|---|---|---|---|---|
| GET | `/healthz` | 无 | 已实现 | 存活检查 |
| GET | `/readyz` | 无 | 设计 | 就绪检查（数据库、依赖） |

### 3.2 登录（auth-login）

| 方法 | 路径 | 鉴权 | 幂等 | 状态 | 说明 |
|---|---|---|---|---|---|
| POST | `/api/v1/app/auth/login` | 无 | — | 已实现 | body `{code}`，返回令牌对 |
| POST | `/api/v1/app/auth/refresh` | 刷新令牌 | — | 已实现 | 轮换刷新令牌 |
| POST | `/api/v1/app/auth/logout` | access | — | 设计 | 撤销全部刷新令牌 |
| POST | `/api/v1/app/auth/sms/send` | 无 | — | 设计 | 发送验证码，限流 |
| POST | `/api/v1/app/auth/sms/verify` | 无 | — | 设计 | 校验验证码 |

### 3.3 商品与购物车（catalog，设计）

| 方法 | 路径 | 鉴权 | 幂等 | 状态 | 说明 |
|---|---|---|---|---|---|
| GET | `/api/v1/app/categories` | 可选 | — | 设计 | 分类树 |
| GET | `/api/v1/app/products` | 可选 | — | 设计 | 商品列表：`category_id`、`sort`、`keyword`、筛选项、`cursor`、`limit`（默认 10）；返回 `monthly_sales`、价格、是否多规格、是否售罄 |
| GET | `/api/v1/app/products/{id}` | 可选 | — | 设计 | 商品详情（含规格组、加料、库存提示、30 天销量） |
| GET | `/api/v1/app/products/{id}/specs` | 可选 | — | 设计 | 规格组与可售状态（规格弹层用） |
| GET | `/api/v1/app/cart` | access | — | 设计 | 购物车 |
| POST | `/api/v1/app/cart/items` | access | 必须 | 设计 | 加入购物车：`sku_id`、`qty`、`spec_codes[]`；同规格组合合并数量 |
| PATCH | `/api/v1/app/cart/items/{id}` | access | — | 设计 | 修改数量 |
| DELETE | `/api/v1/app/cart/items/{id}` | access | — | 设计 | 删除 |

### 3.4 库存（inventory-oversell）

| 方法 | 路径 | 鉴权 | 幂等 | 状态 | 说明 |
|---|---|---|---|---|---|
| PATCH | `/api/v1/admin/skus/{id}/stock` | admin `sku:stock` | 必须 | 设计 | 增减库存 + 原因（不允许直接覆盖为最终值） |

库存扣减不对外开放接口，只在下单事务内发生。

### 3.5 地址与地图（address、map-location）

| 方法 | 路径 | 鉴权 | 幂等 | 状态 | 说明 |
|---|---|---|---|---|---|
| GET | `/api/v1/app/addresses` | access | — | 已实现 | 列表，默认地址在前 |
| POST | `/api/v1/app/addresses` | access | 建议 | 已实现 | 新增 |
| PUT | `/api/v1/app/addresses/{id}` | access（归属） | — | 设计 | 修改 |
| POST | `/api/v1/app/addresses/{id}/default` | access（归属） | — | 已实现 | 设为默认 |
| DELETE | `/api/v1/app/addresses/{id}` | access（归属） | — | 已实现 | 删除，删默认后自动补位 |
| GET | `/api/v1/app/geo/reverse` | access | — | 设计 | 逆地理，参数 `lat`、`lng` |
| GET | `/api/v1/app/geo/geocode` | access | — | 设计 | 地址解析 |
| GET | `/api/v1/app/stores/{id}/delivery-check` | access | — | 设计 | 配送范围校验，返回 `in_range`、`distance_m` |

### 3.6 订单（order）

| 方法 | 路径 | 鉴权 | 幂等 | 状态 | 说明 |
|---|---|---|---|---|---|
| POST | `/api/v1/app/orders` | access | 必须 | 已实现 | 创建订单（扣库存、写明细、写事件） |
| GET | `/api/v1/app/orders` | access | — | 设计 | 列表，`status`、`cursor`、`limit` |
| GET | `/api/v1/app/orders/{id}` | access（归属） | — | 已实现 | 详情（状态、明细、金额） |
| POST | `/api/v1/app/orders/{id}/cancel` | access（归属） | 必须 | 已实现 | 仅待支付可取消，回补库存 |
| POST | `/api/v1/app/orders/{id}/confirm-receipt` | access（归属） | 必须 | 设计 | 确认收货 |
| GET | `/api/v1/admin/orders` | admin `order:read` | — | 设计 | 管理列表（门店范围过滤） |
| POST | `/api/v1/admin/orders/{id}/ship` | admin `order:ship` | 必须 | 设计 | 发货 |

### 3.7 支付（payment）

| 方法 | 路径 | 鉴权 | 幂等 | 状态 | 说明 |
|---|---|---|---|---|---|
| POST | `/api/v1/app/orders/{id}/pay` | access（归属） | 必须 | 已实现 | 创建支付单，返回支付参数 |
| GET | `/api/v1/app/payments/{out_trade_no}` | access（归属） | — | 已实现 | 查询支付结果 |
| POST | `/api/v1/webhooks/wechat/pay` · `/api/v1/webhooks/wechat/refund`（旧路径 `/api/v1/payments/notify/wechat` 兼容） | APIv3 验签 | 按单号幂等 | 已实现 | 支付 / 退款回调 |
| POST | `/api/v1/admin/orders/{id}/refund` | admin `order:refund` | 必须 | 设计 | 发起退款 |

### 3.8 会员与积分（membership）

| 方法 | 路径 | 鉴权 | 幂等 | 状态 | 说明 |
|---|---|---|---|---|---|
| GET | `/api/v1/app/points` | access | — | 已实现 | 余额与最近 20 条流水 |
| GET | `/api/v1/app/points/ledger` | access | — | 设计 | 流水分页 |
| GET | `/api/v1/app/member` | access | — | 设计 | 会员状态与权益 |
| POST | `/api/v1/app/member/subscribe` | access | 必须 | 设计 | 开通会员（走支付） |

### 3.9 营销与优惠券（marketing-coupon）

| 方法 | 路径 | 鉴权 | 幂等 | 状态 | 说明 |
|---|---|---|---|---|---|
| GET | `/api/v1/app/coupon-templates` | 可选 | — | 设计 | 可领取的活动券 |
| POST | `/api/v1/app/coupon-templates/{id}/claim` | access | 必须 | 已实现 | 领取 |
| GET | `/api/v1/app/coupons` | access | — | 设计 | 我的券，`status`、`applicable_amount` |
| GET | `/api/v1/app/campaigns` | 可选 | — | 设计 | 活动列表（专题页、Banner） |
| GET | `/api/v1/admin/coupon-templates` | admin `coupon:read` | — | 设计 | 管理列表 |
| POST | `/api/v1/admin/coupon-templates` | admin `coupon:write` | 必须 | 设计 | 创建模板 |
| PATCH | `/api/v1/admin/coupon-templates/{id}/stock` | admin `coupon:write` | 必须 | 设计 | 库存增减 |

### 3.10 评价（review）

| 方法 | 路径 | 鉴权 | 幂等 | 状态 | 说明 |
|---|---|---|---|---|---|
| POST | `/api/v1/app/orders/{id}/review` | access（归属） | 必须 | 已实现 | 评价（仅已完成订单，一单一评） |
| GET | `/api/v1/app/products/{id}/reviews` | 可选 | — | 设计 | 评价列表（仅 visible） |
| GET | `/api/v1/app/products/{id}/review-summary` | 可选 | — | 设计 | 评分汇总 |
| GET | `/api/v1/admin/reviews` | admin `review:read` | — | 设计 | 待审核列表 |
| POST | `/api/v1/admin/reviews/{id}/moderate` | admin `review:moderate` | 必须 | 设计 | 通过或隐藏 |

### 3.11 配送（delivery-dispatch，设计）

| 方法 | 路径 | 鉴权 | 状态 | 说明 |
|---|---|---|---|---|
| GET | `/api/v1/app/orders/{id}/delivery` | access（归属） | 设计 | 配送进度与骑手（脱敏） |
| GET | `/api/v1/app/orders/{id}/delivery/track` | access（归属） | 设计 | 骑手位置（10 秒轮询） |
| GET | `/api/v1/admin/delivery/tasks` | admin `delivery:read` | 设计 | 待派单 / 已派单任务列表 |
| GET | `/api/v1/admin/delivery/riders` | admin `delivery:read` | 设计 | 候选骑手与当前负载 |
| POST | `/api/v1/admin/delivery/tasks/{id}/assign` | admin `delivery:dispatch` | 设计 | 指派骑手；幂等键、状态和版本校验 |
| POST | `/api/v1/admin/delivery/tasks/{id}/reassign` | admin `delivery:dispatch` | 设计 | 改派；幂等键与审计 |
| POST | `/api/v1/admin/delivery/tasks/{id}/flag` | admin `delivery:dispatch` | 设计 | 标记异常并进入人工处理 |
| POST | `/api/v1/rider/tasks/{id}/accept` | 骑手令牌 | 设计 | 确认已派单任务 |
| POST | `/api/v1/rider/tasks/{id}/pickup` | 骑手令牌 | 设计 | 取餐（取餐码核对） |
| POST | `/api/v1/rider/tasks/{id}/deliver` | 骑手令牌 | 设计 | 送达（范围校验） |
| POST | `/api/v1/rider/location` | 骑手令牌 | 设计 | 位置上报（10 秒） |

### 3.12 排队（queue，设计）

| 方法 | 路径 | 鉴权 | 状态 | 说明 |
|---|---|---|---|---|
| POST | `/api/v1/app/queue/tickets` | access | 设计 | 取号（幂等） |
| GET | `/api/v1/app/queue/tickets/current` | access | 设计 | 当前排队状态 |
| POST | `/api/v1/app/queue/tickets/{id}/cancel` | access（归属） | 设计 | 取消 |
| POST | `/api/v1/app/queue/tickets/{id}/arrive` | access（归属） | 设计 | 叫号后到店确认 |
| POST | `/api/v1/staff/queue/call-next` | 员工令牌 | 设计 | 叫下一位 |
| POST | `/api/v1/staff/queue/tickets/{id}/seat` | 员工令牌 | 设计 | 入座 |

### 3.13 充值（recharge，设计）

| 方法 | 路径 | 鉴权 | 幂等 | 状态 | 说明 |
|---|---|---|---|---|---|
| GET | `/api/v1/app/recharge/plans` | access | — | 设计 | 档位 |
| POST | `/api/v1/app/recharge/orders` | access | 必须 | 设计 | 创建充值单，返回支付参数 |
| GET | `/api/v1/app/recharge/account` | access | — | 设计 | 余额与流水 |

### 3.14 通知（notification，设计）

| 方法 | 路径 | 鉴权 | 状态 | 说明 |
|---|---|---|---|---|
| POST | `/api/v1/app/subscribe-consents` | access | 设计 | 订阅消息授权上报 |
| GET | `/api/v1/admin/notifications` | admin `notification:read` | 设计 | 失败列表 |
| POST | `/api/v1/admin/notifications/{id}/resend` | admin `notification:resend` | 设计 | 人工重发 |

### 3.15 管理后台账号（admin-rbac，设计）

| 方法 | 路径 | 鉴权 | 状态 | 说明 |
|---|---|---|---|---|
| POST | `/api/v1/admin/auth/login` | 无 | 设计 | 账号密码 + 验证码 |
| POST | `/api/v1/admin/auth/mfa` | 无 | 设计 | 二次验证 |
| POST | `/api/v1/admin/auth/logout` | 登录 | 设计 | 注销 |
| GET | `/api/v1/admin/me` | 登录 | 设计 | 账号、角色、权限码、门店范围 |
| GET/POST/PUT | `/api/v1/admin/staff` | `staff:manage` | 设计 | 账号管理 |
| GET/POST/PUT | `/api/v1/admin/roles` | `role:manage` | 设计 | 角色管理 |
| GET | `/api/v1/admin/audit-logs` | `audit:read` | 设计 | 审计查询（只读） |

### 3.16 智能体（agent，设计；由智能体服务提供，协议见 `agent/shared/protocol.md`）

| 方法 | 路径 | 鉴权 | 状态 | 说明 |
|---|---|---|---|---|
| POST | `/api/v1/agent/conversations` | access / admin | 设计 | 新建会话 |
| POST | `/api/v1/agent/conversations/{id}/turns` | access / admin | 设计 | 发一轮；H5 / 后台返回 SSE，小程序返回 `202 {turn_id}` |
| GET | `/api/v1/agent/conversations/{id}/events?after_seq=` | access / admin | 设计 | 回填 / 轮询事件 |
| POST | `/api/v1/agent/conversations/{id}/confirm` | access / admin | 设计 | 确认或拒绝高风险操作 |
| GET | `/api/v1/agent/conversations/{id}/messages` | access / admin | 设计 | 历史分页 |
| POST | `/api/v1/agent/conversations/{id}/feedback` | access / admin | 设计 | 点赞 / 点踩 |
| GET / DELETE | `/api/v1/agent/memory` | access | 设计 | 查看 / 删除长期记忆 |
| POST | `/api/v1/agent/ws-ticket` | access | 设计 | 换取 WebSocket 一次性票据 |
| GET | `/api/v1/agent/health` | 内网 | 设计 | 模型 / 知识库 / 后端依赖与熔断状态 |

后端为智能体提供的支撑接口（`agent-support` 模块）：`POST /api/v1/app/orders/preview`（报价预览，`agent: yes`，read）、知识库文档 CRUD（见附录 A 的"内容"段）。

## 4 接口统计

| 状态 | 数量 |
|---|---|
| 已实现（含测试） | 16 |
| 设计 | 全部其余接口 |

已实现的 16 个接口：`/healthz`、`auth/login`、`auth/refresh`、`addresses`（列表、新增、设默认、删除）、`orders`（创建、详情、取消）、`orders/{id}/pay`、`payments/{no}`、`payments/notify/wechat`、`points`、`coupon-templates/{id}/claim`、`orders/{id}/review`。

## 5 文档与代码的一致性

- 每个已实现接口的路径、方法、鉴权与 `MODULE.md` 一致；代码中的 `api.py` 是唯一的路由定义来源，文档随代码更新。
- 设计接口在实现时必须补上测试，并把状态改为“已实现”。
- `scripts/check_api_catalog.py`（待补）导出 OpenAPI 并与本表对比；CI 中不一致即失败。

---

## 附录 A：v2 接口全集（按生成阶段，`SPEC.md` §17）

状态：已实现 / 设计（有 MODULE.md）/ 待建（模块目录未建）。`agent` 列：yes(read) / yes(low) / yes(high) / no。所有写接口 `Idempotency-Key` 必填（§1.2）。

### A.1 P1 身份与门店

| 方法 | 路径 | 鉴权 | agent | 状态 |
|---|---|---|---|---|
| POST | `/api/v1/app/auth/login` · `/refresh` · `/logout` | — / 刷新 / access | no | 已实现 / 已实现 / 设计 |
| POST | `/api/v1/app/auth/phone/bind`（一键手机号） · `/sms/send` · `/sms/verify` | access / 无 / 无 | no | 设计 |
| GET / PUT | `/api/v1/app/me`（资料、头像、昵称） | access | yes(read) | 待建 |
| POST | `/api/v1/app/me/deactivate`（注销，需 `fresh`） | access | no | 待建 |
| GET | `/api/v1/app/stores` · `/api/v1/app/stores/{id}`（信息、营业状态、履约参数、距离） | 可选 | yes(read) | 待建 |
| GET | `/api/v1/app/stores/nearby?lat=&lng=` | 可选 | yes(read) | 待建 |
| GET / PUT | `/api/v1/admin/stores` · `/{id}` · `/{id}/hours` · `/{id}/fulfillment` · `POST /{id}/copy-config` | `store:*` | no | 待建 |
| GET / PUT | `/api/v1/admin/settings` · `/{group}` | `settings:*` | no | 待建 |

### A.2 P2 商品与购物车

| 方法 | 路径 | 鉴权 | agent | 状态 |
|---|---|---|---|---|
| GET | `/api/v1/app/categories` | 可选 | yes(read) | 设计 |
| GET | `/api/v1/app/products`（筛选 / 排序 / 游标） · `/{id}` · `/{id}/specs` | 可选 | yes(read) | 设计 |
| GET | `/api/v1/app/set-meals` · `/{id}` | 可选 | yes(read) | 待建 |
| GET | `/api/v1/app/search/suggest?q=` · `/api/v1/app/search/hot` | 可选 | yes(read) | 待建 |
| GET / POST / PATCH / DELETE | `/api/v1/app/cart` · `/items` · `/items/{id}` · `POST /api/v1/app/cart/merge`（登录合并） · `DELETE /api/v1/app/cart`（清空） | access | yes(low) | 设计 / 待建 |
| GET / POST | `/api/v1/app/favorites` · `DELETE /{product_id}` | access | yes(low) | 待建 |
| POST | `/api/v1/app/media/presign`（上传预签名） | access | no | 待建 |
| 全部 | `/api/v1/admin/products*` · `/categories*` · `/set-meals*` · `/stock*`（见 Luz System `references/admin-design.html`） | `product:*` 等 | no | 待建 |

### A.3 P3 地址与定位

| 方法 | 路径 | 鉴权 | agent | 状态 |
|---|---|---|---|---|
| GET / POST / PUT / DELETE | `/api/v1/app/addresses` · `/{id}` · `POST /{id}/default` | access | yes(read) / yes(high 新建与删除) | 已实现（PUT 设计） |
| GET | `/api/v1/app/geo/reverse` · `/geo/geocode` · `/api/v1/app/delivery-check?address_id=|lat=&lng=` | access | yes(read) | 设计 |

### A.4 P4 交易主链

| 方法 | 路径 | 鉴权 | agent | 状态 |
|---|---|---|---|---|
| POST | `/api/v1/app/orders/preview`（报价：商品、券、积分、会员价、配送费、时段可用性；返回 `quote_token` 10 分钟） | access | yes(read) | 待建 |
| POST | `/api/v1/app/orders`（body 含 `quote_token`、`channel`、`address_id?`、`table_id?`、`coupon_id?`、`points_cents?`、`expected_at?`、`remark?`） | access | yes(high) | 已实现（扩展待建） |
| GET | `/api/v1/app/orders?status=&cursor=` · `/{id}`（含时间线） | access | yes(read) | 设计 / 已实现 |
| POST | `/api/v1/app/orders/{id}/cancel`（原因） · `/urge` · `/address` · `/confirm-receipt` · `/pickup-code` | access | high / low / high / low / read | 已实现（扩展待建） |
| POST | `/api/v1/app/orders/{id}/pay` · `GET /api/v1/app/payments/{out_trade_no}` · `POST /api/v1/webhooks/wechat/pay` · `/wechat/refund` | access / 渠道 | no | 已实现 / 已实现 / 已实现（路径迁移）/ 设计 |
| GET | `/api/v1/app/time-slots?date=&channel=`（可约时段） | 可选 | yes(read) | 待建 |

### A.5 P5 履约与售后

| 方法 | 路径 | 鉴权 | agent | 状态 |
|---|---|---|---|---|
| POST | `/api/v1/admin/orders/{id}/accept` · `/reject` · `/prepare` · `/ready` · `/ship` · `/complete` · `/cancel` · `/note` · `/verify-pickup` · `POST /api/v1/admin/orders/batch/print` | `order:*` | no | 待建 |
| GET | `/api/v1/admin/orders` · `/{id}` · `/api/v1/admin/orders/board`（工作台看板） | `order:read` | no | 设计 / 待建 |
| POST | `/api/v1/app/orders/{id}/add-items`（堂食加菜） · `/checkout`（堂食结账） | access | low / high | 待建 |
| POST | `/api/v1/app/after-sales`（申请） · `GET /api/v1/app/after-sales` · `/{id}` · `POST /{id}/close` | access | high / read / read / low | 待建 |
| GET / POST | `/api/v1/admin/after-sales` · `/{id}` · `/{id}/approve` · `/reject` · `/reship` · `/retry-refund` | `after_sale:*`（审批 / 重试需新鲜认证） | no | 已实现（见 10-after-sales/MODULE.md §6） |
| POST | `/api/v1/app/subscribe-consents` · `GET / PUT /api/v1/admin/notifications/templates` · `GET /api/v1/admin/notifications?status=failed` · `POST /{id}/resend` · `GET /stats` | access / `notification:*` | no | 已实现（见 20-notification/MODULE.md §6） |

### A.6 P6 会员与营销

| 方法 | 路径 | 鉴权 | agent | 状态 |
|---|---|---|---|---|
| GET | `/api/v1/app/member` · `/member/levels` · `/points` · `/points/ledger` | access | yes(read) | 设计 / 已实现（points） |
| POST | `/api/v1/app/member/subscribe`（开通 / 续费，走支付） | access | yes(high) | 设计 |
| GET / POST | `/api/v1/app/recharge/plans` · `/orders` · `/account` · `/ledger` · `POST /api/v1/app/recharge/orders/{id}/refund-request` | access | read / 跳转页面 / read / read / high | 设计 |
| GET / POST | `/api/v1/app/coupon-templates` · `/{id}/claim` · `/api/v1/app/coupons?status=` · `/api/v1/app/coupons/applicable?quote_token=` | access | read / low / read / read | 设计 / 已实现 / 设计 / 待建 |
| GET | `/api/v1/app/campaigns` · `/{id}`（专题页内容与商品） | 可选 | yes(read) | 设计 / 待建 |
| 全部 | `/api/v1/admin/members*` · `/member-levels` · `/recharge*` · `/stored-ledger` · `/coupon-templates*` · `/campaigns*`（见后台模块清单） | 对应权限码 | no | 设计 / 待建 |

### A.7 P7 行业能力

| 方法 | 路径 | 鉴权 | agent | 状态 |
|---|---|---|---|---|
| POST / GET | `/api/v1/app/queue/tickets` · `/current` · `/{id}/cancel` · `/{id}/arrive` · `GET /api/v1/app/queue/status`（各区域等待） | access / 可选 | high / read / high / low / read | 设计 |
| 全部 | `/api/v1/staff/queue/call-next` · `/tickets/{id}/call|miss|seat|cancel` · `/api/v1/admin/queue/config` · `/api/v1/admin/tables*` | 员工 / `queue:*` `table:*` | no | 设计 |
| GET | `/api/v1/app/orders/{id}/delivery` · `/delivery/track` | access | yes(read) | 设计 |
| 全部 | `/api/v1/rider/*` · `/api/v1/admin/delivery/*` | 骑手 / `delivery:*` | no | 设计 |
| POST / GET | `/api/v1/app/orders/{id}/review` · `/api/v1/app/products/{id}/reviews` · `/review-summary` · `/api/v1/app/reviews/mine` | access / 可选 | low / read / read / read | 已实现 / 设计 / 设计 / 待建 |
| GET / POST | `/api/v1/admin/reviews` · `/{id}/moderate` · `/{id}/reply` | `review:*` | no | 设计 / 设计 / 待建 |

### A.8 P8 后台

| 方法 | 路径 | 鉴权 | agent | 状态 |
|---|---|---|---|---|
| POST / GET | `/api/v1/admin/auth/login` · `/mfa` · `/refresh` · `/logout` · `GET /api/v1/admin/me` | — / 登录 | no | 设计 |
| 全部 | `/api/v1/admin/staff*` · `/roles*` · `GET /api/v1/admin/permissions` · `GET /api/v1/admin/audit-logs` · `/{id}` | `staff:manage` `role:manage` `audit:read` | no | 设计 |
| GET | `/api/v1/admin/dashboard/summary?range=` · `/todo` · `/charts?type=` | `dashboard:read` | yes(read, admin) | 待建 |
| GET / POST | `/api/v1/admin/reports/daily` · `/range` · `POST /api/v1/admin/exports` · `GET /exports` · `/{id}/download` | `report:*` | yes(read, admin) | 待建 |

### A.9 P9 智能体支持

| 方法 | 路径 | 鉴权 | agent | 状态 |
|---|---|---|---|---|
| 全部 | `/api/v1/admin/kb/docs*` · `/{id}/reindex` · `POST /api/v1/admin/kb/test` | `kb:*` | no | 待建 |
| GET / PUT | `/api/v1/admin/agent/config` · `GET /api/v1/admin/agent/conversations` | `agent:*` | no | 待建 |
| — | 服务间签名校验（`X-Service: agent`，`SPEC.md` §5） | — | — | 待建 |

### A.10 系统

| 方法 | 路径 | 鉴权 | 状态 |
|---|---|---|---|
| GET | `/healthz` · `/readyz` · `/metrics` | 内网 | 已实现 / 待建 / 待建 |
