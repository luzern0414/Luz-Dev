# 后端安全规范

安全基线按 OWASP ASVS 5.0 的章节组织，并对照 OWASP API Security Top 10（2023）；补充微信支付 v3、小程序与智能体的特定要求。实现状态见 §11；外部依赖的超时与熔断以 `SPEC.md` §9 为准，限流规则与阈值只在本文 §6。

## 1 身份认证（数值为运行配置默认值，写在 `config/runtime/base.toml` 的 `[identity]`、`[admin_rbac]`、`[auth]` 等分组，改文件热加载）

| 项 | 规定 | 实现 |
|---|---|---|
| 小程序登录 | `wx.login` code 换 openid（28-wechat-mp 共享客户端，熔断 + 超时）；code 一次性；首次登录自动注册并记录协议同意 | 01-identity |
| 会员访问令牌 | JWT HS256，2 小时（`[identity] access_ttl_seconds=7200`），含 `aud`、`sid`、`jti`、`kind=access`；leeway 30 秒；每次请求校验服务端会话仍有效（登出 / 停用 / 注销立即失效） | 01-identity `current_user` |
| 会员刷新令牌 | 只存哈希；每次使用轮换；空闲 14 天、绝对 30 天（NIST AAL1 上限）；轮换后 10 秒宽限，宽限外重放 → 4010 并吊销整个会话 | 01-identity |
| 新鲜认证（会员） | 注销等敏感操作要求令牌 15 分钟内签发（`LUZ_FRESH_WINDOW=900`），否则 4010 `fresh_required` | `identity.require_fresh` |
| 短信验证码 | 只存哈希；5 分钟有效；≤ 5 次尝试；60 秒重发间隔；每号 10 次/天，每 IP 20 次/时、50 次/天；接口另按 IP 限频 | 01-identity |
| 后台登录 | 账号 + 口令（Argon2id，m=19456 KiB、t=2、p=1）+ TOTP（RFC 6238，±1 步，防重放）/ 恢复码；按账号失败锁定：窗口 15 分钟内 5 次 → 锁定 15 分钟起递增翻倍、封顶 24 小时；3 次失败后可要求验证码；首登临时口令 72 小时内必须改密 | 22-admin-rbac |
| 后台会话 | 服务端会话（`admin_sessions`），令牌 `kind=admin`；空闲 15 分钟、绝对 8 小时；绑定 UA；每人最多 5 个；改密、停用、角色变更即时生效（每请求从库加载权限） | `admin_rbac.require_permission` |
| 后台 MFA | 超级管理员、财务角色，以及持有 `staff:write / role:write / after_sale:approve / after_sale:refund / payment:refund / report:export / wallet:adjust / wallet:refund / member_card:adjust / member_card:refund / member:reveal`（含 `module:*` 覆盖）的员工必须绑定 MFA，未绑定时会话受限（只能改密 / 绑定） | 22-admin-rbac |
| 新鲜认证（后台） | 资金与敏感操作 `require_permission(code, fresh=True)`：5 分钟内完成过完整认证（`[admin_rbac] fresh_auth_seconds=300`），否则 4011 `reauth_required`，用 `/api/v1/admin/auth/reauth` 重新验证。用于：退款发起 / 异常退款、对账差异处理、关单、售后审批 / 重试退款 / 线下结清、储值调整 / 冲回 / 退款审批、会员卡调整 / 冲正 / 退款、查看会员完整手机号等 | 各模块 api |
| 令牌隔离 | 会员（`kind=access`, aud）、后台（`kind=admin` + 服务端会话）、MFA 挑战（`kind=admin_mfa`）、服务间（签名）互不通用；伪造的声明（如自带 `perms`）一律无效 | 各依赖 |
| 口令策略 | NIST SP 800-63B-4：启用 MFA ≥ 8 位、仅口令 ≥ 15 位，上限 128；不设组成规则、不定期强制改密；拒绝常见口令与含账号 / 姓名的口令，可接泄露库检查；旧哈希登录后重哈希 | `admin_rbac.passwords.check_policy`、`luzback.security.check_password_policy` |

## 2 授权

| 项 | 规定 |
|---|---|
| 对象级 | 按 id 访问的资源必须校验归属（`user_id` 或门店范围）；不属于时 4040（不泄露存在性） |
| 功能级 | 后台路由一律 `admin_rbac.require_permission(code)`；默认拒绝 4030；权限码 `<module>:<action>` 由各模块 `declare_permissions` 登记；角色可授予精确码或模块通配 `module:*`（前缀匹配，覆盖以后新增的码）；`*` 只属于超级管理员；每请求从库加载，无缓存 |
| 防越权授予 | 只能授予自己持有的码（授予 `module:*` 需自己持有 `module:*` 或 `*`）、自己范围内的门店；超级管理员角色只能由超级管理员授予，至少保留一名 |
| 数据级 | 多门店按 `store_scope(staff)` / `apply_store_scope` 过滤（None = 全部门店，[] = 无门店）；越界 4031；条件由服务端注入，不接受客户端 `user_id / store_id` |
| 职责分离 | `assert_not_self`：退款等审批人不能是发起人 |
| 服务间 | `X-Service` + 时间戳 + nonce + HMAC-SHA256 签名；偏差 ≤ 5 分钟；nonce 去重 |
| 智能体 | 工具层 `user_id` 只来自令牌；模型输出中的身份字段丢弃；高风险操作经用户确认 |
| 内置角色 | super_admin、store_manager、front_desk、finance、operator（`admin_rbac/seed.py`）；发布时 `python -m admin_rbac.bootstrap --seed` 同步权限码与角色 |

## 3 输入与输出

| 项 | 规定 |
|---|---|
| 校验 | Pydantic 模型声明类型、长度、范围、枚举；未知字段忽略；4001 带 `errors[{field,message}]` |
| 大小 | JSON 体 ≤ 256KB（`[http] max_body_kb`）；文件预签名直传，类型白名单（jpg / png / webp 等）并按魔数校验；拍照与相册图片不按大小拒收，前端上传原图、后台统一压缩，对象存储只设防滥用上限（100 MB）；文档类文件上限 `[upload] doc_max_mb`（`references/module-specs/upload.json`） |
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
| 密钥 | 不入仓库；JWT、字段密钥、商户私钥、APIv3Key、小程序 AppSecret、服务密钥定期轮换；`Settings.assert_production_safe()` 拒绝开发默认值，并执行各模块登记的生产检查（identity 字段密钥、wechat_pay、wechat_mp） |
| 备份 | 加密；每季度恢复演练 |
| 保留 | 日志与审计的留存期限按 `references/config-logging.md` §3.9：网络与安全日志不少于 6 个月；审计记录建议 1 年，涉及个人信息的批量导出与越权操作建议 3 年；交易信息 3 年。后台登录日志默认保留 186 天，不少于 6 个月（`[admin_rbac]`）；会员注销冷静期满后匿名化（01-identity）；有奖销售资料保存 2 年（18-lottery） |

## 5 HTTP 安全响应头与 CORS

框架统一添加：`X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、`Referrer-Policy: no-referrer`、`Cache-Control: no-store`、`Strict-Transport-Security: max-age=31536000; includeSubDomains`、`Permissions-Policy: geolocation=(), camera=(), microphone=()`。CORS 只允许配置的后台域名（`LUZ_CORS_ORIGINS`）；小程序不需要 CORS。生产关闭 `/docs`。

## 6 限流与防刷

| 对象 | 策略 |
|---|---|
| 接口 | 多规则令牌桶 / 固定窗口（`luzback.ratelimit`，装配在 `backend/app.py` 与 `identity.api.rate_limit_rules`），阈值取运行配置 `[rate_limit]` 与模块分组、热加载生效；Redis 不可用时放行并计数。默认：每 IP 访问 `/api/` 20 次/秒（突发 40）；会员写操作 3 次/秒；登录每 IP 5 次/分钟；后台第二因素与重新认证每 IP 10 次/分钟；刷新令牌每 IP 30 次/分钟 |
| 登录与验证码 | 发码每 IP 5 次/分钟；同一手机号 60 秒重发、每天 10 条；每 IP 每小时 20 条、每天 50 条（`[identity]`）；后台按账号失败锁定（§1） |
| 下单、领券、抽奖、评价 | 按会员；抽奖 10 次/分钟（`[lottery] member_draws_per_minute`）、领奖 5 次/分钟（`[rate_limit] lottery_claim_per_min`），每日次数在 18-lottery 内计数 |
| 异常行为 | 同设备短时大量下单 / 领券 → 风控队列，要求验证码或人工审核 |
| 爬虫 | 商品接口返回缓存；异常 UA 与高频访问降级为验证码 |

## 7 支付与回调

| 项 | 规定 |
|---|---|
| 金额来源 | 只取服务端业务对象金额；客户端金额一律忽略 |
| 回调验签（v3） | 时间戳偏差 ≤ 5 分钟 → 平台证书序列号匹配 → RSA-SHA256 验签 `timestamp\nnonce\nbody\n` → APIv3Key AES-256-GCM 解密 → `mchid / appid / trade_state / currency / amount / out_trade_no` 逐项校验；任一失败不改数据并记录 `pay_notify_logs` |
| 重放 | 渠道单号唯一 + 支付单条件 UPDATE；重复回调返回成功不重复处理 |
| 来源 | 渠道 IP 白名单作为第二道防线 |
| 退款 | 经售后审批（`after_sale:approve`，新鲜认证）或 `payment:refund`；金额 ≤ 实付 − 已退（09 按支付单累计占用）；同一 `business_ref` 先查 `find_active_refund` 防重复；退款回调同样验签解密 |
| 证书与密钥 | 商户私钥只在内存；回调只走 APIv3 验签（微信支付公钥或平台证书，按序列号匹配），没有 HMAC 联调通道；生产缺密钥、仍用假客户端、回调地址不合规时 `wechat_pay.production_problems` 经 `register_production_check` 拒绝启动 |
| 日志 | 不记录支付参数、签名原文、证书内容 |

## 8 日志、审计与隐私

| 项 | 规定 |
|---|---|
| 日志 | 字段、事件名、脱敏与留存见 `references/config-logging.md` §3 与 `SPEC.md` §13；登录失败、越权、令牌重放、验签失败、限流命中、输入校验失败写 `security.*` 事件（`category=security`） |
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
| 失控调用 | 每次运行决策轮次、工具调用、同名同参重复、token 与每日预算都有上限，超过即以 `STEP_LIMIT` 等错误结束，不让模型收尾（`references/agent-foundation.md` §14、§16） |
| 编造 | 核验节点：数字必须能在工具结果或引用中找到 |

## 10 供应链与运行环境

依赖锁版本；CI 执行 `pip-audit`、`bandit`、`trivy`，高危阻断（`assets/ci/github-actions.yml`）；镜像非 root、只读根文件系统；秘密来自密钥服务或受限文件，运行配置来自代码仓库的配置文件；`/docs` 生产关闭；`/metrics` 与 `/readyz` 只在内网暴露。

## 11 实施状态

| 项 | 状态 | 位置 |
|---|---|---|
| JWT 签发校验（iss / jti / kind / leeway） | 已实现 | `00-framework/code/luzback/security.py` |
| 口令哈希与策略、TOTP | 已实现 | `22-admin-rbac/code/admin_rbac/passwords.py`、`totp.py` |
| 服务间签名与 nonce 去重 | 已实现 | `security.py`、`01-identity/code/identity/api.py` `service_call` |
| 字段加密与哈希查询 | 已实现 | `luzback/crypto.py`、01-identity |
| 刷新轮换、登出、冻结、注销（新鲜令牌）、短信限额 | 已实现 | 01-identity |
| 幂等（作用域含用户、指纹、僵尸接管） | 已实现 | `luzback/idempotency.py` |
| 多规则限流、请求体上限、安全头、CORS、全局异常 | 已实现 | `luzback/ratelimit.py`、`app.py` |
| 审计表 | 已实现 | `luzback/audit.py` |
| 熔断 / 重试 / 超时 | 已实现 | `luzback/resilience.py` |
| 后台登录、MFA、锁定、服务端会话、权限码（含 `module:*`）、门店范围；全部模块后台路由统一接入 | 已实现 | 22-admin-rbac |
| 微信支付 APIv3 签名、验签、解密、对账 | 已实现 | 09-wechat-pay |
| 生产配置启动检查（框架 + 模块登记） | 已实现 | `luzback.config.register_production_check` |
| 对象级归属校验 | 已实现（订单、地址、支付、评价）；新模块按模板 | 各模块 |
| 依赖与镜像漏洞扫描 | 已接入 CI | `assets/ci/github-actions.yml` |

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
| API9 | 资产管理不当 | `API.md` §3 索引、各 MODULE.md §6 与 `ROUTING.md`；未列出路径 404 |
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
