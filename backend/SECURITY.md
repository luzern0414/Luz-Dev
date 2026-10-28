# 后端安全规范 v2

安全基线按 OWASP ASVS 5.0 的章节组织，并对照 OWASP API Security Top 10（2023）；补充微信支付 v3、小程序与智能体的特定要求。实现状态见 §11；数值（限流、超时）以 `SPEC.md` §9、§12 为准。

## 1 身份认证

| 项 | 规定 | 实现 |
|---|---|---|
| 小程序登录 | `wx.login` code 换 openid，code 一次性；code2session 经熔断与超时 | 01-identity |
| 访问令牌 | JWT HS256（生产 RS256），2 小时；载荷 `iss, sub, iat, exp, jti, kind, role, pv` | `luzback.security` |
| 刷新令牌 | 30 天；每次使用轮换（`refresh_version` 条件 UPDATE）；重放 → 4010 并视为泄露（撤销全部会话） | 01-identity |
| 登出 / 冻结 / 注销 | `refresh_version + 1`；访问令牌自然过期（≤ 2 小时） | 01-identity |
| 新鲜令牌 | 注销、改手机号、退款审批、权限变更要求 15 分钟内登录（`fresh`） | `require_fresh` |
| 短信验证码 | 6 位随机，只存哈希；5 分钟有效；≤ 5 次尝试；1 次/分钟、10 次/天 | 01-identity |
| 后台登录 | 账号 + 口令（argon2id / PBKDF2 600k）+ TOTP；5 次失败锁 15 分钟；首登改密；会话 2 小时滑动 + 刷新 7 天 | 22-admin-rbac |
| 员工 / 骑手 / 后台 / 智能体 | 令牌 `kind` 不同，不可互用 | 各依赖 |
| 口令策略 | ≥ 10 位，含字母与数字；旧哈希登录成功后重哈希 | `check_password_policy` |

## 2 授权

| 项 | 规定 |
|---|---|
| 对象级 | 按 id 访问的资源必须校验归属（`user_id` 或门店范围）；不属于时 4040（不泄露存在性） |
| 功能级 | 后台按权限码（`22-admin-rbac/MODULE.md` §3）；`require_permission` 每请求校验，缓存 60 秒 |
| 数据级 | 多门店按 `store_scope` 过滤；条件由服务端注入，不接受客户端 `user_id / store_id` |
| 服务间 | `X-Service` + 时间戳 + nonce + HMAC-SHA256 签名；偏差 ≤ 5 分钟；nonce 10 分钟去重；写接口只对合法签名开放 `agent: yes` |
| 智能体 | 工具层 `user_id` 只来自令牌；模型输出中的身份字段丢弃；高风险操作经用户确认 |
| 最小权限 | 默认角色只读；不能给自己升权或停用自己；数据库账号只拥有所需权限 |

## 3 输入与输出

| 项 | 规定 |
|---|---|
| 校验 | Pydantic 模型声明类型、长度、范围、枚举；未知字段忽略；4001 带 `errors[{field,message}]` |
| 大小 | JSON 体 ≤ 256KB（框架中间件）；上传预签名直传 ≤ 5MB，类型白名单 jpg / png / webp，按魔数校验 |
| 输出 | 只返回约定字段；手机号、地址默认脱敏；完整值需 `*:sensitive` 并审计 |
| SQL | ORM 或参数化；排序字段白名单；禁止字符串拼接 |
| 富文本 | 存储前白名单清洗；后端不返回 HTML |
| 文件 | 文件名服务端生成（UUID）；EXIF 剥离 |
| SSRF | 外部 URL 只允许白名单域名，禁止内网地址 |
| 反序列化 | 只接受 JSON |
| 批量赋值 | `user_id`、`status`、`price`、`amount` 不接受客户端写入；价格与金额只在服务端计算 |

## 4 传输与存储

| 项 | 规定 |
|---|---|
| 传输 | 全站 HTTPS，TLS 1.2+，HSTS；内部服务 mTLS |
| 口令 | argon2id（有库时）或 PBKDF2-HMAC-SHA256 600k 轮；不可逆 |
| 敏感字段 | 手机号、身份证：AES-256-GCM 加密列 + HMAC 哈希列用于查询（`luzback.crypto.FieldCipher`）；密钥 `LUZ_FIELD_KEY` 由密钥服务注入，生产必填 |
| 密钥 | 不入仓库；JWT、字段密钥、商户私钥、APIv3Key、服务密钥定期轮换；`assert_production_safe` 拒绝开发默认值 |
| 备份 | 加密；每季度恢复演练 |
| 保留 | 登录日志 180 天；审计 2 年；骑手轨迹 7 天；注销账户 30 天内删除个人数据 |

## 5 HTTP 安全响应头与 CORS

框架统一添加：`X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、`Referrer-Policy: no-referrer`、`Cache-Control: no-store`、`Strict-Transport-Security: max-age=31536000; includeSubDomains`、`Permissions-Policy: geolocation=(), camera=(), microphone=()`。CORS 只允许配置的后台域名（`LUZ_CORS_ORIGINS`）；小程序不需要 CORS。生产关闭 `/docs`。

## 6 限流与防刷

| 对象 | 策略 |
|---|---|
| 接口 | 多规则令牌桶 / 固定窗口（`luzback.ratelimit`），维度 IP / 用户 / 手机号 / 设备；数值见 `SPEC.md` §12；Redis 不可用时放行并计数 |
| 登录与验证码 | 按设备与手机号双维度 |
| 下单、领券、抽奖、评价 | 按用户；抽奖 5 次/分钟 |
| 异常行为 | 同设备短时大量下单 / 领券 → 风控队列，要求验证码或人工审核 |
| 爬虫 | 商品接口返回缓存；异常 UA 与高频访问降级为验证码 |

## 7 支付与回调

| 项 | 规定 |
|---|---|
| 金额来源 | 只取服务端业务对象金额；客户端金额一律忽略 |
| 回调验签（v3） | 时间戳偏差 ≤ 5 分钟 → 平台证书序列号匹配 → RSA-SHA256 验签 `timestamp\nnonce\nbody\n` → APIv3Key AES-256-GCM 解密 → `mchid / appid / trade_state / currency / amount / out_trade_no` 逐项校验；任一失败不改数据并记录 `pay_notify_logs` |
| 重放 | 渠道单号唯一 + 支付单条件 UPDATE；重复回调返回成功不重复处理 |
| 来源 | 渠道 IP 白名单作为第二道防线 |
| 退款 | 经售后审批（`aftersales:approve`）；金额 ≤ 实付 − 已退；大额双人复核（设置项）；退款回调同样验签解密 |
| 证书与密钥 | 商户私钥只在内存；平台证书定期拉取并校验序列号；开发联调的 HMAC 签名在生产被 `assert_production_safe` 禁止 |
| 日志 | 不记录支付参数、签名原文、证书内容 |

## 8 日志、审计与隐私

| 项 | 规定 |
|---|---|
| 日志 | JSON；`trace_id`、`request_id`、用户 id、接口、状态码、耗时、错误码；手机号 / 令牌 / 身份证自动脱敏（`luzback.logging.redact`） |
| 禁止记录 | 完整手机号、地址、令牌、口令、支付签名、回调明文 |
| 审计 | 后台写操作、敏感查看、导出、登录与权限变更、价格 / 库存 / 退款；`audit_logs` 只追加 |
| 个人信息 | 最小化收集；可查看、导出、注销；隐私政策版本与同意记录留存 |
| 小程序合规 | 定位、订阅消息、手机号授权按微信规范申请，拒绝后提供替代路径 |

## 9 智能体安全

| 风险 | 对策 |
|---|---|
| 提示词注入 | 工具结果与知识片段作为数据包裹；高风险工具必须确认；摘要数字只来自预览接口 |
| 越权 | 工具层不接受身份参数；后端按令牌与服务签名授权 |
| 数据外泄 | 工具结果进模型前脱敏；日志不含对话正文 |
| 失控调用 | 单轮工具 ≤ 5；同参数 3 次中止；预算与熔断（`agent/backend/SPEC.md` §8） |
| 编造 | 核验节点：数字必须能在工具结果或引用中找到 |

## 10 供应链与运行环境

依赖锁版本；CI 执行 `pip-audit`，高危阻断；镜像非 root、只读根文件系统；密钥与配置来自密钥服务；`/docs` 生产关闭；`/metrics` 与 `/readyz` 只在内网暴露。

## 11 实施状态

| 项 | 状态 | 位置 |
|---|---|---|
| JWT 签发校验（iss / jti / kind / leeway） | 已实现 | `00-framework/code/luzback/security.py` |
| 口令哈希与策略、TOTP | 已实现 | 同上 |
| 服务间签名与 nonce 去重 | 已实现 | `security.py`、`01-identity/code/identity/api.py` `service_call` |
| 字段加密与哈希查询 | 已实现 | `luzback/crypto.py`、01-identity |
| 刷新轮换、登出、冻结、注销（新鲜令牌）、短信限额 | 已实现 | 01-identity |
| 幂等（作用域含用户、指纹、僵尸接管） | 已实现 | `luzback/idempotency.py` |
| 多规则限流、请求体上限、安全头、CORS、全局异常 | 已实现 | `luzback/ratelimit.py`、`app.py` |
| 审计表 | 已实现 | `luzback/audit.py` |
| 熔断 / 重试 / 超时 | 已实现 | `luzback/resilience.py` |
| 后台登录、MFA、锁定、权限码、门店范围 | 按 `MODULES.md` 22 实现 | 22-admin-rbac |
| 微信 v3 验签解密 | 按 `MODULES.md` 09 实现 | 09-wechat-pay |
| 对象级归属校验 | 已实现（订单、地址、支付、评价）；新模块按模板 | 各模块 |
| 依赖漏洞扫描 | CI 待接入 | — |

## 12 OWASP API Security Top 10（2023）对照

| 编号 | 风险 | 对策 |
|---|---|---|
| API1 | 对象级授权失效 | 归属校验；不存在与无权限统一 4040 |
| API2 | 失效的身份认证 | 令牌有效期、刷新轮换、MFA、锁定、回调验签、令牌类型隔离 |
| API3 | 对象属性级授权失效 | 请求模型显式字段；敏感字段不接受客户端写入 |
| API4 | 无限制的资源消耗 | 限流、请求体上限、分页上限、数组上限、预算 |
| API5 | 功能级授权失效 | 权限码、门店范围、服务签名 |
| API6 | 敏感业务流程滥用 | 领券 / 下单 / 抽奖按用户限流；风控队列；幂等 |
| API7 | SSRF | 外部 URL 白名单；禁止内网地址 |
| API8 | 安全配置错误 | 安全头；生产禁用开发密钥；关闭 docs；只 HTTPS |
| API9 | 资产管理不当 | `API.md` 附录 A 与 `ROUTING.md`；未列出路径 404 |
| API10 | 不安全的 API 消耗 | 超时、重试上限、熔断、回调九项校验 |

## 13 安全测试清单

| 类别 | 测试 |
|---|---|
| 认证 | 无令牌 / 过期 / 篡改 / 错 kind / 错 issuer → 4010；刷新重放 → 4010；锁定 → 4290 |
| 授权 | A 的令牌访问 B 的订单、地址、支付单、评价、预约、排号 → 4040；无权限码 → 4030；越门店 → 4031 |
| 服务签名 | 篡改 body、过期时间戳、重放 nonce、未知服务 → 4010 |
| 注入 | 请求体中的 `user_id`、`status`、`price`、`amount` 不生效 |
| 限流 | 超阈值 4290 带 `Retry-After`；登录与短信双维度 |
| 回调 | 伪造签名、错序列号、过期时间戳、金额篡改、重复回调 → 不改订单且有日志 |
| 头部 | 响应含安全头；非法 `X-Request-Id` 被替换 |
| 敏感信息 | 列表不返回完整手机号；日志无令牌与手机号 |
| 依赖 | CI 漏洞扫描高危阻断 |
