# 路由与网关规范 v2

规定请求如何进入系统、转发到哪个模块、路由层做哪些检查。接口清单见 `API.md`，模块划分见 `MICROSERVICES.md`，限流与弹性数值以 `SPEC.md` §9、§12 为准。模块化单体阶段，本文的检查由框架中间件（`00-framework/code/luzback/app.py`）执行；拆服务后由网关执行同一张表。

## 1 网关职责

| 职责 | 说明 | 不在网关做 |
|---|---|---|
| TLS 终止 | 仅 HTTPS 对外，TLS 1.2+；HTTP 301 | 业务判断 |
| 路由转发 | 按路径前缀转发（§2） | 库存、计价、权限细分 |
| 令牌预检 | 验证签名与过期（不查库） | 权限码、门店范围（模块判断） |
| 限流 | 按 IP、用户、接口（§2 限流列） | — |
| 追踪 | 生成或透传 `X-Trace-Id`、`X-Request-Id`，写访问日志 | — |
| 请求体上限 | 普通接口 256KB；上传走预签名直传，不经网关 | — |
| 灰度与版本 | `X-Client-Version` 或用户分桶 | — |
| 安全响应头 | 统一添加（`SECURITY.md` §5） | — |

## 2 路由表

| 路径前缀 | 模块 | 鉴权 | 限流 | 备注 |
|---|---|---|---|---|
| `/api/v1/app/auth/*`、`/api/v1/app/me*` | 01-identity | 无（login）/ 刷新 / access | 登录 5 次/分钟/设备；短信 1 次/分钟/手机号 | 令牌与资料 |
| `/api/v1/app/stores*` | 02-store | 可选 | 20/秒 | 匿名可读 |
| `/api/v1/app/categories*`、`/products*`、`/set-meals*`、`/search/*` | 03-catalog | 可选 | 50/秒（只读） | 缓存 5 分钟 |
| `/api/v1/app/cart*`、`/favorites*` | 04-cart、03-catalog | access | 写 3/秒 读 20/秒 | — |
| `/api/v1/app/addresses*` | 05-address | access | 写 3/秒 | — |
| `/api/v1/app/geo/*`、`/delivery-check` | 06-geo | access | 10/秒 | 地图代理 |
| `/api/v1/app/orders/preview`、`/orders*`、`/time-slots` | 08-order | access | 写 3/秒 读 20/秒 | 写接口要幂等键 |
| `/api/v1/app/orders/*/pay`、`/api/v1/app/payments*` | 09-wechat-pay | access | 3/秒；查询 10/秒 | — |
| `/api/v1/webhooks/wechat/*` | 09-wechat-pay | **渠道验签，不走令牌** | 渠道 IP 白名单 | 回调 |
| `/api/v1/app/after-sales*` | 10-after-sales | access | 写 3/秒 | — |
| `/api/v1/app/orders/*/delivery*` | 11-delivery | access | 轨迹 6/分钟 | — |
| `/api/v1/app/queue*` | 12-queue | access / 可选（status） | 取号 5/秒 | — |
| `/api/v1/app/reservations*` | 13-reservation | access | 写 3/秒 | — |
| `/api/v1/app/member*`、`/points*` | 14-membership | access | 20/秒 | — |
| `/api/v1/app/recharge*` | 15-recharge | access | 3/秒 | — |
| `/api/v1/app/coupon-templates*`、`/coupons*` | 16-coupon | 可选 / access | 领取 10/秒 | — |
| `/api/v1/app/campaigns*` | 17-campaign | 可选 | 20/秒 | — |
| `/api/v1/app/lottery*` | 18-lottery | access | 抽奖 5 次/分钟/用户 | — |
| `/api/v1/app/orders/*/review`、`/products/*/reviews*`、`/reviews/*` | 19-review | access / 可选 | 提交 5/秒 | — |
| `/api/v1/app/subscribe-consents` | 20-notification | access | 10/秒 | — |
| `/api/v1/app/media/*` | 21-media | access | 10/秒 | 预签名 |
| `/api/v1/app/config` | 23-app-settings | 无 | 50/秒 | 公开配置 |
| `/api/v1/agent/*` | 智能体服务（独立进程） | access / admin | 60 次/分钟/用户 | 见 `agent/` |
| `/api/v1/admin/*` | 各模块后台路由 + 22-admin-rbac | 后台令牌 + 权限码 | 20/秒 | 独立网关实例，内网 / VPN |
| `/api/v1/staff/*` | 12-queue、08-order、13-reservation | 员工令牌 | 20/秒 | 门店 Pad |
| `/api/v1/rider/*` | 11-delivery | 骑手令牌 | 位置 6/分钟，其他 20/秒 | 骑手端 |
| `/healthz`、`/readyz`、`/metrics` | 00-framework | 无（网关不转发） | — | 集群内探测 |

未列出的路径 404，不做默认转发。重叠前缀按最长前缀优先（`/orders/*/pay` 先于 `/orders*`）。

## 3 检查顺序

每个请求依次经过，任一失败即终止：

1. 路径与方法 → 404 / 405
2. 请求体大小 → 413（信封 code 4001）
3. 限流（按 §2 规则匹配路径 + 方法 + 维度）→ 429（4290，`Retry-After`）
4. 令牌 → 401（4010）；小程序收到 4010 刷新一次
5. 幂等键（写接口）→ 400（4001）
6. 参数校验（Pydantic）→ 400（4001，`data.errors[{field,message}]`）
7. 权限码与门店范围（后台）→ 403（4030 / 4031）
8. 服务间签名（带 `X-Service` 的请求）→ 401（4010）
9. 业务处理

## 4 参数约定

路径 id 为正整数；字符串必须声明最大长度；数组声明最大元素数（购物车 ≤ 50、订单行 ≤ 50、图片 ≤ 9）；枚举拒绝未知值；金额只接受整数分；分页 `limit ≤ 20`、`page_size ≤ 100`；排序字段白名单（`SPEC.md` §3.4）。

## 5 版本与灰度

路径版本 `/api/v1/`；破坏性变更升 `/api/v2/` 并行 6 个月。灰度按 `X-Client-Version` 或用户 id 取模。小程序版本低于 `min_supported` 时返回 4001 并带升级提示。

## 6 错误与降级路由

| 情况 | 行为 |
|---|---|
| 下游超时（按 `SPEC.md` §9 的超时表） | 504，`code 5020` |
| 下游熔断打开 | 503，`code 5030`，`Retry-After: 5` |
| 下游 5xx | 透传信封，`code 5000 / 5020`，带 `trace_id` |
| 只读接口有缓存 | 返回缓存，响应头 `X-Degraded: cache` |

## 7 健康检查

`/healthz` 进程存活；`/readyz` 数据库、Redis、迁移版本、熔断状态（框架实现）；负载均衡按 `/readyz` 剔除实例。

## 8 实施与测试

路由表生成网关配置，不手工维护两份；集成测试覆盖 §3 每一步的失败返回；压测验证限流阈值与 `Retry-After`；回调路由验证不走令牌、非白名单 IP 被拒。
