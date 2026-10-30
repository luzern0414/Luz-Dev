# 路由与网关规范

规定请求如何进入系统、转发到哪个模块、路由层做哪些检查。接口约定与模块接口索引见 `API.md`，每个接口的细节在所属模块 MODULE.md §6；模块划分见 `MICROSERVICES.md`；超时与熔断默认值以 `SPEC.md` §9 为准；已实现的限流规则与阈值以 `SECURITY.md` §6 和运行配置 `[rate_limit]` 为准。模块化单体阶段，本文的检查由框架中间件（`00-framework/code/luzback/app.py`）执行；拆服务后由网关执行同一张表。

## 1 网关职责

| 职责 | 说明 | 不在网关做 |
|---|---|---|
| TLS 终止 | 仅 HTTPS 对外，TLS 1.2+；HTTP 301 | 业务判断 |
| 路由转发 | 按路径前缀转发（§2） | 库存、计价、权限细分 |
| 令牌预检 | 验证签名与过期（不查库） | 权限码、门店范围、会话是否有效（模块判断） |
| 限流 | 按 IP、用户、接口（§2 限流列） | — |
| 追踪 | 透传或生成 W3C `traceparent` 与 `X-Request-Id`（旧客户端的 `X-Trace-Id` 照常透传），写 JSON 访问日志 | — |
| 请求体上限 | 普通接口 256KB（`[http] max_body_kb`）；文件走预签名直传，不经网关 | — |
| 灰度与版本 | 按运行配置的覆盖规则（门店、渠道、客户端版本、用户分桶） | — |
| 安全响应头 | 统一添加（`SECURITY.md` §5） | — |

## 2 路由表

已实现（前缀与代码一致，接口表见 `API.md` §3）：

| 路径前缀 | 模块 | 鉴权 | 限流 | 备注 |
|---|---|---|---|---|
| `/api/v1/app/auth/*`、`/api/v1/app/me*` | 01-identity | 无（登录）/ 刷新令牌 / access | 登录、短信、刷新按 IP 每分钟（`[rate_limit]`）；短信按手机号与每日上限（`[identity]`） | 令牌与资料 |
| `/api/v1/app/addresses*` | 05-address | access | 会员写 3/秒 | — |
| `/api/v1/app/orders*`（含 `/preview`） | 08-order | access | 会员写 3/秒 | 写接口要幂等键 |
| `/api/v1/app/orders/{id}/pay`、`/api/v1/app/payments*` | 09-wechat-pay | access | 会员写 3/秒 | 比 `/orders*` 长的前缀优先 |
| `/api/v1/app/orders/{id}/review` | 19-review | access | 会员写 3/秒 | — |
| `/api/v1/app/after-sales*` | 10-after-sales | access | 会员写 3/秒 | — |
| `/api/v1/app/points` | 14-membership | access | 按 IP | — |
| `/api/v1/app/wallet*` | 15-recharge | access | 会员写 3/秒 | — |
| `/api/v1/app/member-cards*` | 29-member-card | access | 会员写 3/秒 | — |
| `/api/v1/app/coupon-templates*`、`/api/v1/app/coupons*` | 16-coupon | 可选 / access | 会员写 3/秒 | — |
| `/api/v1/app/lottery*` | 18-lottery | access | 抽奖 10 次/分钟/会员（`[lottery] member_draws_per_minute`）；领奖 5 次/分钟/会员 | 每日次数在活动规则里 |
| `/api/v1/app/notifications*` | 20-notification | access | 按 IP | 订阅额度、站内信 |
| `/api/v1/app/config`、`/api/v1/app/content` | 23-app-settings | 无 | 按 IP | 客户端可见的运行参数、文案与展示配置（ETag / 304） |
| `/api/v1/webhooks/wechat/*` | 09-wechat-pay | **渠道验签，不走令牌** | 只受按 IP 的全局令牌桶；nginx 层不对回调限流（`references/operations.md` §2）；渠道 IP 白名单作第二道防线 | 支付与退款回调；旧 `/api/v1/payments/notify/wechat` 兼容 |
| `/api/v1/webhooks/wechat-mp/*` | 28-wechat-mp | **消息推送验签与解密** | 同上 | 小程序消息推送 |
| `/api/v1/admin/*` | 各模块后台路由 + 22-admin-rbac | 后台会话 + 权限码 | 后台登录、第二因素、重新认证按 IP 每分钟 | 独立网关实例，内网 / VPN |
| `/api/v1/agent/*` | 智能体服务（独立进程） | access / admin | 每用户每分钟运行次数见 `references/agent-foundation.md` §16 | 见 `agent/shared/protocol.md` |
| `/healthz`、`/readyz`、`/metrics` | 00-framework | 无（网关不转发；`/metrics` 只对内网或令牌） | — | 集群内探测 |
| `/internal/*` | 23-app-settings | 内网 + 运维令牌（网关不转发） | — | 运行配置状态与重载，运维在容器内调用 |

规划（模块实现时按此挂载，限流为网关建议值，实现后写进 `[rate_limit]` 与 `SECURITY.md` §6）：

| 路径前缀 | 模块 | 鉴权 | 建议限流 |
|---|---|---|---|
| `/api/v1/app/stores*` | 02-store | 可选 | 20/秒 |
| `/api/v1/app/categories*`、`/products*`、`/set-meals*`、`/search/*` | 03-catalog | 可选 | 50/秒（只读，缓存 5 分钟） |
| `/api/v1/app/cart*`、`/favorites*` | 04-cart | access | 写 3/秒、读 20/秒 |
| `/api/v1/app/geo/*`、`/delivery-check` | 06-geo | access | 10/秒（地图代理） |
| `/api/v1/app/orders/{id}/delivery*`、`/api/v1/rider/*` | 11-delivery | access / 骑手令牌 | 轨迹与位置 6 次/分钟 |
| `/api/v1/app/queue*`、`/api/v1/staff/*` | 12-queue | access / 员工令牌 | 取号 5/秒 |
| `/api/v1/app/slots*`、`/api/v1/app/bookings*` | 13-reservation | access | 写 3/秒 |
| `/api/v1/app/media/*` | 21-media | access | 10/秒（预签名） |

未列出的路径 404，不做默认转发。重叠前缀按最长前缀优先（`/orders/{id}/pay` 先于 `/orders*`）。

## 3 检查顺序

每个请求依次经过，任一失败即终止：

1. 路径与方法 → 404 / 405
2. 请求体大小 → 413（信封 code 4001）
3. 维护模式（`[features] maintenance_mode`，顾客端写接口）→ 503（5030）
4. 限流（按规则匹配路径 + 方法 + 维度）→ 429（4290，`Retry-After`）
5. 令牌与会话 → 401（4010）；小程序收到 4010 刷新一次
6. 幂等键（写接口）→ 400（4001）
7. 参数校验（Pydantic）→ 400（4001，`data.errors[{field,message}]`）
8. 权限码与门店范围（后台）→ 403（4030 / 4031）；需要 MFA 或重新认证 → 401（4011）
9. 服务间签名（带 `X-Service` 的请求）→ 401（4010）
10. 业务处理

## 4 参数约定

路径 id 为正整数；字符串必须声明最大长度；数组声明最大元素数（购物车 ≤ 50、订单行 ≤ 50、图片 ≤ 9）；枚举拒绝未知值；金额只接受整数分；分页 `limit ≤ 20`、`page_size ≤ 100`；筛选与排序字段白名单，未知参数 4001（`SPEC.md` §3.4）。

## 5 版本与灰度

路径版本 `/api/v1/`；破坏性变更升 `/api/v2/` 并行 6 个月。灰度写在运行配置环境文件的覆盖规则里（门店、渠道、客户端版本区间、用户分桶百分比），见 `references/config-logging.md` §2.6。小程序与 App 的最低可用版本是运行参数 `[client] min_version_*`，经 `/api/v1/app/config` 下发，客户端低于它时提示更新（REL-003）。

## 6 错误与降级路由

| 情况 | 行为 |
|---|---|
| 下游超时（按 `SPEC.md` §9 的超时表） | 504，`code 5020` |
| 下游熔断打开 | 503，`code 5030`，`Retry-After: 5` |
| 下游 5xx | 透传信封，`code 5000 / 5020`，带 `request_id` |
| 只读接口有缓存 | 返回缓存，响应头 `X-Degraded: cache` |

## 7 健康检查

`/healthz` 进程存活；`/readyz` 数据库、Redis、迁移版本、配置版本与登记的检查项（框架实现）；负载均衡按 `/readyz` 剔除实例。

## 8 实施与测试

路由表生成网关配置，不手工维护两份；`tests/test_app_wiring.py` 校验模块挂载与路径冲突；集成测试覆盖 §3 每一步的失败返回；压测验证限流阈值与 `Retry-After`；回调路由验证不走令牌、非白名单 IP 被拒。
