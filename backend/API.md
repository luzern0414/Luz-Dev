# 后端接口约定与索引

本文件只写所有接口共用的约定，以及“模块 → 接口表”的索引。每个接口的方法、路径、鉴权、幂等、权限码、agent 标记与说明只写在所属模块的 `MODULE.md` §6（旧格式模块在 §3 或 §5），代码 `api.py` 是路由的唯一定义；挂载与路径冲突由 `tests/test_app_wiring.py` 校验。接口变更的顺序：先改 MODULE.md §6，再改代码与测试，跨模块导出变化同时改 `CONTRACTS.md`。后台接口一律 `admin_rbac.require_permission`（`SECURITY.md` §1–§2）。

## 1 通用约定

### 1.1 基础路径与版本

| 端 | 前缀 | 说明 |
|---|---|---|
| 小程序 / App / H5 | `/api/v1/app/` | 会员端，access 令牌 |
| 管理后台 | `/api/v1/admin/` | 后台账号，权限码与门店范围 |
| 员工端、骑手端（规划） | `/api/v1/staff/`、`/api/v1/rider/` | 门店员工（叫号、入座）、骑手，各自的令牌 |
| 回调 | `/api/v1/webhooks/<渠道>/…` | 第三方回调，渠道签名，不使用用户令牌；旧的 `/api/v1/payments/notify/wechat` 只为已部署的商户兼容保留 |
| 运维 | `/internal/*` | 只对内网并校验运维令牌，网关不转发 |
| 系统 | `/healthz`、`/readyz`、`/metrics` | 只在内网探测（网关不对外暴露） |

版本变更规则：向后兼容的新增字段不升版本；删除字段、改变语义必须升到 `/api/v2/`，旧版至少保留 6 个月。

### 1.2 请求头

| 头 | 必填 | 说明 |
|---|---|---|
| `Authorization: Bearer <token>` | 用户接口必填 | access 令牌或后台会话令牌 |
| `Idempotency-Key` | 产生副作用的写接口必填 | 客户端生成的 UUID（36 位），同一次操作的重试沿用；语义见 `SPEC.md` §6 第 5 条 |
| `X-Request-Id` | 可选 | 由网关或客户端传入，≤ 64 位字母数字与横线；未传则服务端生成 |
| `traceparent` | 可选 | W3C 链路头；合法时沿用其 trace（服务端生成新 span），不合法时忽略；不放任何个人信息 |
| `X-Trace-Id` | 可选 | 旧的链路编号（1–64 位字母数字、横线、下划线），没有 traceparent 时沿用 |
| `X-Client` | 建议 | `miniprogram`、`app`、`h5`、`admin`、`staff`、`agent`；运行配置的渠道覆盖规则按它命中 |
| `X-Client-Version` | 小程序必填 | 版本号，用于灰度与强制升级 |
| `X-Config-Version` | 建议 | 客户端当前的运行配置版本，写进请求汇总日志便于排查 |
| `X-Device-Id` | 可选 | 设备标识（风控用，不作为身份依据） |

### 1.3 响应

成功与失败统一为信封：

```json
{"code": 0, "message": "ok", "data": {}}
```

响应头固定包含：`X-Request-Id`、`X-Trace-Id`、`traceparent`、`X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、`Cache-Control: no-store`（用户接口；配置与文案拉取接口为 `no-cache` + `ETag`）。

### 1.4 分页、筛选与排序

| 类型 | 请求 | 响应 `data` |
|---|---|---|
| 页码分页（管理后台） | `page`（从 1），`page_size`（默认 20，最大 100；后台界面提供 10 / 20 / 50 / 100） | `{items, total, page, page_size}` |
| 游标分页（小程序列表） | `cursor`（不透明字符串，由上一页 `next_cursor` 原样回传），`limit`（默认 10，最大 20） | `{items, next_cursor, has_more}` |

筛选、区间、关键词、排序与导出的通用写法只有一套，见 `SPEC.md` §3.4（`luzback.listing.ListSpec`）：字段白名单，未知参数返回 4001。商品列表这类业务排序（销量、价格）在对应模块实现时用 ListSpec 声明排序字段。

### 1.5 金额与时间

- 金额：整数分（`*_cents`）。
- 时间：ISO 8601，带时区；服务端存 UTC；只写日期的区间按门店时区整天计算。

### 1.6 限流

限流规则与阈值见 `SECURITY.md` §6，阈值写在运行配置 `[rate_limit]` 与各模块分组（热加载）。超限返回 HTTP 429、`code = 4290`，响应头 `Retry-After`。

## 2 错误码

唯一来源是框架 `ErrorCode`，完整表见 `SPEC.md` §4。常用：4001 参数；4010 未登录或令牌失效；4011 需要 MFA 或重新认证；4030 无权限；4031 门店范围外；4040 不存在；4090 状态冲突；4091 库存不足；4092 同幂等键处理中（409）；4093 同幂等键不同请求体（422）；4220 业务规则；4225 次数或数量上限；4290 限流；5000 内部错误；5020 上游失败；5030 熔断或维护。

## 3 模块接口索引

| 模块 | 前缀 | 接口表 |
|---|---|---|
| 00-framework | `/healthz`、`/readyz`、`/metrics` | `00-framework/MODULE.md` §2（`app.py`） |
| 01-identity | `/api/v1/app/auth/*`、`/api/v1/app/me*`、`/api/v1/admin/members*` | `01-identity/MODULE.md` §6 |
| 05-address | `/api/v1/app/addresses*` | `05-address/MODULE.md` §3 |
| 08-order | `/api/v1/app/orders*`（含 `POST /preview`）、`/api/v1/admin/orders*` | `08-order/MODULE.md` §6 |
| 09-wechat-pay | `/api/v1/app/orders/{id}/pay`、`/api/v1/app/payments*`、`/api/v1/webhooks/wechat/pay`、`/api/v1/webhooks/wechat/refund`、`/api/v1/admin/payments*`、`/api/v1/admin/refunds*`、`/api/v1/admin/reconciliation/*`、`/api/v1/admin/payment-shipping*` | `09-wechat-pay/MODULE.md` §6 |
| 10-after-sales | `/api/v1/app/after-sales*`、`/api/v1/admin/after-sales*` | `10-after-sales/MODULE.md` §6 |
| 14-membership | `/api/v1/app/points` | `14-membership/MODULE.md` §6 |
| 15-recharge | `/api/v1/app/wallet*`、`/api/v1/admin/wallet*` | `15-recharge/MODULE.md` §6 |
| 16-coupon | `/api/v1/app/coupon-templates*`、`/api/v1/app/coupons*`、`/api/v1/admin/coupon-templates*`、`/api/v1/admin/coupon-code-batches*`、`/api/v1/admin/coupon-issue-batches*`、`/api/v1/admin/user-coupons*` | `16-coupon/MODULE.md` §6 |
| 18-lottery | `/api/v1/app/lottery/*`、`/api/v1/admin/lottery/*` | `18-lottery/MODULE.md` §6 |
| 19-review | `/api/v1/app/orders/{id}/review` | `19-review/MODULE.md` §3 |
| 20-notification | `/api/v1/app/notifications/*`、`/api/v1/admin/notifications/*` | `20-notification/MODULE.md` §6 |
| 22-admin-rbac | `/api/v1/admin/auth/*`、`/api/v1/admin/staff*`、`/api/v1/admin/roles*`、`/api/v1/admin/permissions`、`/api/v1/admin/audit-logs*`、`/api/v1/admin/login-logs*` | `22-admin-rbac/MODULE.md` §6 |
| 23-app-settings | `/api/v1/app/config`、`/api/v1/app/content`、`/api/v1/admin/content/*`、`/internal/config`、`/internal/config/reload` | `23-app-settings/MODULE.md` §6 |
| 28-wechat-mp | `/api/v1/webhooks/wechat-mp/push` | `28-wechat-mp/MODULE.md` §6 |
| 29-member-card | `/api/v1/app/member-cards*`、`/api/v1/admin/member-cards*` | `29-member-card/MODULE.md` §6 |

没有路由的模块：07-inventory、17-campaign、25-jobs 只提供函数或进程（`CONTRACTS.md`）；06-geo、13-reservation、27-legal 有函数，接口是各自 MODULE.md 里的设计稿；02-store、03-catalog、04-cart、11-delivery、12-queue、21-media、24-report、26-agent-support 为规划，接口在实现时写进各自 MODULE.md §6。

## 4 智能体接口

智能体服务的接口（`/api/v1/agent/*`：会话、运行、事件流、审批、取消、续跑、历史、反馈、记忆）与事件、错误码见 `agent/shared/protocol.md` 与 `references/agent-foundation.md` §2。智能体工具能调用哪些后端接口，看各模块 MODULE.md §6 的 agent 列（`SPEC.md` §16）。
