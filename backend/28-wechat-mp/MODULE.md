# 模块：28-wechat-mp（小程序服务端接口）`wechat_mp`

小程序服务端接口的唯一共享客户端：登录（code2Session / session_key 校验与重置）、手机号、订阅消息、内容安全（文本同步 / 媒体异步）、小程序码、access_token 中控、消息推送（验签、安全模式加解密、事件分发去重）。使用方：01-identity（登录、手机号、资料审核）、20-notification（订阅消息）、21-media（内容安全）。

## 0 官方来源

`D` = `https://developers.weixin.qq.com`。旧路径 `miniprogram/dev/OpenApiDoc/...` 已 404，现按 `miniprogram/dev/server/API/...` 的 `.md` 原文核对。

| 主题 | 官方页面 | 本模块用到的结论 | 核对日期 |
|---|---|---|---|
| code2Session | D/miniprogram/dev/server/API/user-login/api_code2session.md | GET `/sns/jscode2session`（appid、secret、js_code、grant_type=authorization_code，不需要 access_token）；返回 openid、session_key、unionid（绑定开放平台时）；错误 -1 / 40029 / 40226 / 45011 | 2026-10-28 |
| 登录与 code | D/miniprogram/dev/framework/open-ability/login.md；D/miniprogram/dev/api/open-api/login/wx.login.md | code 5 分钟有效、只能用一次；session_key 不得下发到前端 | 2026-10-28 |
| checkSessionKey / resetUserSessionKey | D/miniprogram/dev/server/API/user-login/api_checksessionkey.md；api_resetusersessionkey.md | signature = hmac_sha256(session_key, "")，sig_method=hmac_sha256；87009 签名无效、87007 不存在或过期；重置后旧 key 失效 | 2026-10-28 |
| getStableAccessToken | D/miniprogram/dev/server/API/mp-access-token/api_getstableaccesstoken.md | 只支持 POST `/cgi-bin/stable_token`；普通模式有效期内返回同一 token 与剩余秒数，平台提前 5 分钟更新；force_refresh 每天 20 次、间隔 ≥ 30 秒；存储至少 512 字符；1 万次/分钟、50 万次/天 | 2026-10-28 |
| access_token 中控 | D/doc/oplatform/developers/dev/AccessToken.md | 中控统一获取与刷新，业务方不各自刷新；新旧 token 5 分钟内都可用；提供被动刷新；40001 / 42001 / 40014 时刷新后重试一次 | 2026-10-28 |
| 通用返回 | D/doc/oplatform/developers/dev/guide.md；D/doc/oplatform/developers/errCode/README.md | 失败固定返回 errcode / errmsg，成功时可能不返回；POST 只把 access_token 放 QueryString；全局错误码（43101 等） | 2026-10-28 |
| 手机号 | D/miniprogram/dev/server/API/user-info/phone-number/api_getphonenumber.md | POST `/wxa/business/getuserphonenumber`，body `{code, openid?}`；传 openid 校验 code 归属；code 一次性、5 分钟；返回 phone_info（phoneNumber、purePhoneNumber、countryCode、watermark） | 2026-10-28 |
| 订阅消息下发 | D/miniprogram/dev/server/API/mp-message-management/subscribe-message/api_sendmessage.md | POST `/cgi-bin/message/subscribe/send`；touser、template_id、page、data、miniprogram_state、lang；关键词类型长度与字符规则；43101 未订阅 / 次数用完、40037、43107、43108、45168、47003 | 2026-10-28 |
| 订阅消息事件 | D/miniprogram/dev/framework/open-ability/subscribe-message.md | JSON 推送中 List 一项为对象、多项为数组 | 2026-10-28 |
| 文本内容安全 | D/miniprogram/dev/server/API/sec-center/sec-check/api_msgseccheck.md | POST `/wxa/msg_sec_check`，version=2，scene 1–4，content ≤ 2500 字，openid 近 2 小时访问过；result.suggest = pass / review / risky；61010、43104、44991、45009 | 2026-10-28 |
| 媒体内容安全 | D/miniprogram/dev/server/API/sec-center/sec-check/api_mediacheckasync.md | POST `/wxa/media_check_async`，media_type 1 音频 / 2 图片，返回 trace_id；结果 30 分钟内以 `wxa_media_check` 事件推送，errcode 为 0 时才有效，-1008 下载失败 | 2026-10-28 |
| 小程序码 | D/miniprogram/dev/server/API/qrcode-link/qr-code/api_getunlimitedqrcode.md | POST `/wxa/getwxacodeunlimit`；scene ≤ 32 个可见字符，字符集 `0-9A-Za-z!#$&'()*+,/:;=?@-._~`；page 不带 `/` 与参数；width 280–1280；成功返回图片二进制、失败返回 JSON；40129 / 40169 / 41030 / 40097 / 85096 | 2026-10-28 |
| 消息推送 | D/miniprogram/dev/framework/server-ability/message-push.md | URL 验证 sha1(sort(token, timestamp, nonce))；安全模式用 msg_signature = sha1(sort(token, timestamp, nonce, Encrypt))；AESKey = Base64(EncodingAESKey + "=")，AES-256-CBC、PKCS#7；明文 = 16B 随机 + 4B 长度（网络序）+ 消息 + appid；回包 "success" | 2026-10-28 |
| 消息推送加解密示例 | https://wximg.gtimg.com/shake_tv/mpwiki/cryptoDemo.zip（消息推送文档内链接） | IV 取 AESKey 前 16 字节；PKCS#7 按 32 字节分块（WXBizMsgCrypt） | 2026-10-28 |

未核实（不作为规则）：code2Session 每用户每分钟 100 次的频率数值；消息推送的超时与重推次数。

## 1 状态与依赖

| 项 | 内容 |
|---|---|
| 状态 | 标准：客户端协议 + HTTP 实现 + 假服务、token 中控、全部接口、消息推送路由，测试 38 项通过 |
| 代码 | `28-wechat-mp/code/wechat_mp/`，测试 `28-wechat-mp/code/tests/` |
| 依赖 | 00-framework（resilient / BreakerRegistry、AppError、db）；cryptography（AES-CBC）；httpx（仅生产传输层） |

## 2 职责与边界

| 负责 | 不负责 |
|---|---|
| 小程序服务端接口调用、errcode → 类型化异常、超时 / 熔断 / 有限重试 | 业务决策（登录建号在 01-identity，模板与重发在 20-notification） |
| access_token 中控（缓存、提前刷新、单飞、被动失效） | 公众号 / 开放平台 / 微信支付（09-wechat-pay） |
| 消息推送：URL 验证、安全模式解密、去重、按事件分发给注册的处理函数 | 各事件的业务处理（由使用方 register_push_handler） |
| 订阅消息 data 本地预检（关键词规则） | 订阅次数记账（20-notification） |

## 3 数据模型

| 表 | 字段 | 约束 / 索引 | 说明 |
|---|---|---|---|
| `wechat_mp_access_tokens` | id, cache_key, token(1024), expires_at, refresh_at, updated_at | `cache_key` 唯一 | `DbTokenStore` 使用：多实例共享 token；token 字段 ≥ 512 字符 |
| `wechat_mp_push_events` | id, dedup_key, event, received_at | `dedup_key` 唯一；event、received_at 索引 | 推送去重；不存正文（含 openid） |

## 4 业务规则

1. 所有接口经 `resilient("wechat_mp.<接口>")`，每个接口一个熔断器；超时默认 3 秒（小程序码 5 秒）。
2. 只有幂等调用重试 1 次：stable_token 普通模式、checksession、msg_sec_check、getwxacodeunlimit。code2Session、getuserphonenumber、subscribe/send、media_check_async、resetusersessionkey、force_refresh 不重试。
3. 判定：`errcode` 缺失或为 0 即成功；-1（系统繁忙）、HTTP 5xx / 429、超时、连接错误为瞬时错误（计入熔断）；其他 errcode 映射为类型化异常（§10），不计入熔断。
4. access_token 只用 getStableAccessToken 普通模式获取；剩余有效期 < 300 秒时刷新；平台尚未换新（剩余 ≤ 余量）时 30 秒后再取，不每次请求都取。
5. 单飞：同一进程只有一个线程刷新；可选分布式锁（`luzback.scheduler` 的 `InMemoryLock` / `RedisLock`），拿不到锁的实例等待对方写入存储。
6. 业务接口返回 40001 / 42001 / 40014：失效缓存后用新 token 重试一次（请求未被执行，非幂等接口也安全）；若普通模式取回的仍是同一 token，才用 force_refresh=true，两次强制刷新间隔 ≥ 30 秒，否则 5020。
7. 刷新失败而旧 token 仍未过期：继续用旧 token（降级）；过期才抛错。
8. token 存储 ≥ 512 字符（DB 列 1024）；日志与异常不含 secret、access_token、session_key、手机号。
9. getuserphonenumber 传 openid 时由平台校验 code 归属，不匹配按 code 无效处理。
10. 订阅消息：page 去掉前导 `/`；miniprogram_state / lang 显式传（默认 formal / zh_CN）；data 本地预检：键为“类型 + 序号”，thing ≤ 20、number / letter / character_string ≤ 32、symbol ≤ 5、phrase ≤ 5 个汉字、phone_number ≤ 17、car_number ≤ 8、amount 为“币种符号 + ≤10 位数字 + 可选元”、name ≤ 10 汉字或 ≤ 20 字母，禁止控制字符；不合规 4001，不发出。
11. msg_sec_check：version=2，scene 1–4，content 1–2500 字；结果 pass / review / risky 原样返回，由调用方决策。
12. media_check_async：media_type 1/2、scene 1–4、URL 必须可下载；返回 trace_id；结果事件 `wxa_media_check` 只有 errcode=0 才有效（`MediaCheckEvent.valid`）。
13. 小程序码：scene 本地校验（≤ 32、字符集）；page 不以 `/` 开头、不带 `?`；width 280–1280；响应 `image/*` 返回字节，否则按 JSON 错误处理。
14. 消息推送 GET：sha1(sort(token, timestamp, nonce)) 等于 signature 才原样返回 echostr，否则 4010。
15. 消息推送 POST：先校验 URL 签名；`encrypt_type=aes` 时校验 msg_signature 后解密并核对 appid；配置了 EncodingAESKey 且 `WECHAT_MP_PUSH_REQUIRE_AES=true` 时拒绝明文推送（4010）；可选时间戳偏差校验。
16. 报文支持 JSON 与 XML；XML 拒绝 DTD / 实体；`normalize_list` 兼容 List 单对象 / 数组。
17. 去重：MsgId 或 (FromUserName, CreateTime, Event, trace_id / MsgID) 的哈希唯一；重复推送直接回 "success" 不再处理。
18. 分发：按 `Event`（普通消息按 `MsgType`）调用注册的处理函数，同一事务提交；处理函数异常回滚并返回 5000，平台重推时可再次处理。

## 5 关键流程

**带 token 的接口调用**
1. `tokens.get()`：缓存新鲜 → 直接用；否则加锁双检后取 stable_token（失败且旧 token 未过期 → 用旧的）。
2. `resilient` 调用（超时 / 熔断 / 幂等才重试）。
3. 返回 40001 / 42001 / 40014 → `tokens.invalidate(旧 token)` → 用新 token 重试一次；仍失败按错误码抛出。
4. 熔断打开 → 5030（`Retry-After`）。

**消息推送**
1. GET 验证 → echostr。
2. POST → 验 signature →（安全模式）验 msg_signature → AES 解密 → 核对 appid → 解析 JSON / XML。
3. 插入去重记录（唯一冲突 → 视为重复，回 success）→ 调处理函数 → 提交 → 回 success。

## 6 接口

| 方法 | 路径 | 鉴权 | 幂等 | agent | 说明 |
|---|---|---|---|---|---|
| GET | `/api/v1/webhooks/wechat-mp/push` | URL 签名 | 是（只读） | no | 服务器地址验证，返回 echostr 纯文本 |
| POST | `/api/v1/webhooks/wechat-mp/push` | URL 签名 + msg_signature | 去重表 | no | 事件推送（JSON / XML，安全模式）；回 `success` |

客户端方法（`MiniProgramClient`）：`access_token`、`code2session`、`check_session_key`、`reset_session_key`、`get_phone_number(code, openid)`、`send_subscribe_message`（返回平台应答 dict）、`msg_sec_check`、`media_check_async`、`get_unlimited_qrcode`。

模块级函数（其他模块只调这些，不持有客户端）：

| 函数 | 说明 | 使用方 |
|---|---|---|
| `send_subscribe_message(touser, template_id, data, page=None, miniprogram_state=None, lang=None) -> dict` | 代理到进程客户端；成功 `{"errcode": 0, "errmsg": "ok", "msgid"?}`，平台错误抛 `WechatError` 子类（`.errcode` / `.errmsg`）；空的 state / lang 用配置 | 20-notification `WechatMpSubscribeSender` |
| `access_token() -> str` | 中控 token（缓存、提前刷新） | 09-wechat-pay 发货信息录入 |
| `invalidate_access_token(bad_token) -> str` | 调用方收到 40001 / 40014 / 42001 时上报，返回新 token（同一缓存，单飞） | 同上 |
| `is_configured() -> bool` | 已注入客户端或配置了真实 AppID / AppSecret | 20-notification 选择默认发送器 |
| `production_problems() -> list[str]` | 生产检查：开发占位 AppID / Secret、进程客户端是假实现、非 https、推送启用但缺 EncodingAESKey、`WECHAT_MP_STATE` ≠ formal；导入时 `register_production_check("wechat_mp", ...)` | app.py 启动（`Settings.assert_production_safe`） |

## 7 事件

| 方向 | 主题 | 说明 |
|---|---|---|
| 发出 | — | 本模块不写发件箱；推送事件直接同步分发给注册的处理函数 |
| 消费（发件箱） | — | `events.py` 的 `SUBSCRIPTIONS = {}`；`jobs.py` 的 `JOBS = ()`（token 按需单飞刷新，无定时任务） |
| 消费（推送） | `wxa_media_check` | 01-identity 已注册（头像审核）；21-media 可注册 |
| 消费（推送） | `subscribe_msg_popup_event` / `subscribe_msg_change_event` / `subscribe_msg_sent_event` | 留给 20-notification 注册 |

## 8 并发与一致性

| 场景 | 机制 |
|---|---|
| 多线程同时取 token | 进程内锁 + 双检（单飞），测试 40 线程只取 1 次 |
| 多实例同时取 token | 可选分布式锁 + `DbTokenStore` 共享；等待方读存储 |
| token 被他处刷新导致 40001 | 被动失效 + 重试一次；同 token 才强制刷新且限 30 秒间隔 |
| 推送重复 | `dedup_key` 唯一约束，重复直接 success |
| 非幂等调用超时 | 不重试，抛 5020，由调用方（任务）决定补偿 |

## 9 配置

| 变量 | 默认 | 说明 |
|---|---|---|
| `WECHAT_APPID` / `WECHAT_SECRET` | 开发占位值 | 与框架共用；占位值时 `get_client()` 报 5020（不发真实请求） |
| `WECHAT_MP_API_BASE` | `https://api.weixin.qq.com` | |
| `WECHAT_MP_TIMEOUT` / `WECHAT_MP_QRCODE_TIMEOUT` | 3 / 5 秒 | |
| `WECHAT_MP_TOKEN_MARGIN` | 300 | 提前刷新余量（秒） |
| `WECHAT_MP_FORCE_REFRESH_INTERVAL` | 30 | 强制刷新最小间隔（秒） |
| `WECHAT_MP_STATE` | formal | 订阅消息跳转版本 developer / trial / formal |
| `WECHAT_MP_LANG` | zh_CN | |
| `WECHAT_MP_ENV_VERSION` | release | 小程序码版本 |
| `WECHAT_MP_PUSH_TOKEN` / `WECHAT_MP_PUSH_AES_KEY` | 空 | 消息推送 Token 与 43 位 EncodingAESKey |
| `WECHAT_MP_PUSH_REQUIRE_AES` | true | 配置了 AESKey 时拒绝明文推送 |
| `WECHAT_MP_PUSH_MAX_SKEW` | 0 | 推送时间戳最大偏差秒数，0 不校验 |

熔断参数沿用框架 BreakerRegistry 默认（60 秒窗口、≥10 次且 ≥50% → 打开 30 秒）。

## 10 错误码

| errcode | 异常 | code |
|---|---|---|
| 40001 / 40014 / 42001 | AccessTokenInvalid（内部重试后仍失败才抛出） | 5020 |
| 40029 / 40163 | InvalidCode | 4010 |
| 40226 | CodeBlocked | 4030 |
| 45009 / 45011 / 44991 / 87020 | RateLimited（Retry-After 60） | 4290 |
| 43101 | SubscriptionRefused | 4220 |
| 40037 / 47003 / 20001 | TemplateInvalid | 4220 |
| 45168 | SensitiveContent | 4220 |
| 61010 | UserNotVisited | 4220 |
| 40003 / 43104 | OpenidMismatch | 4001 |
| 87007 / 87009 | SessionKeyInvalid（check_session_key 返回 False） | 4010 |
| 40097 / 40129 / 40169 / 41030 / 85096 / 40004 / 47001 / 44002 / 40013 / 40002 / 43002 / 41002 / 87008 | InvalidArgument | 4001 |
| 43108 | ConcurrentSend | 4290 |
| 43107 / 50004 / 50007 | CapabilityBanned | 5020 |
| 40125 / 40164 / 40243 / 41004 / 89503 / 89506 / 89507 | ConfigurationError | 5020 |
| -1、超时、5xx | 瞬时错误 → UpstreamError | 5020 |
| 熔断打开 | CircuitOpenError | 5030 |
| 推送验签 / 解密失败 | AppError | 4010 |

## 11 测试清单（`tests/test_wechat_mp.py`，38 项）

| 类别 | 用例 |
|---|---|
| 登录 | code2Session 成功、code 一次性（40163）、无效（40029）、风控拦截（40226）、5 分钟过期、超时不重试；checksession / resetusersessionkey；签名算法 |
| 手机号 | 成功、openid 归属不符 |
| token 中控 | 缓存只取一次且 ≥ 512 字符入库；40 线程单飞；提前 5 分钟刷新；剩余不足时 30 秒后再取；刷新失败降级旧 token；40001 失效重试一次；42001 恢复；同 token 才强制刷新且 30 秒节流；secret 错误；DB 存储共享；分布式锁等待 |
| 订阅消息 | 下发扣次数后 43101；无效模板；data 规则预检；超时不重试；45011 → 4290 |
| 内容安全 | pass / review / risky；参数校验；幂等调用在超时与 -1 时重试；真实超时生效；连续失败熔断打开 5030；异步检测与事件解析（含 -1008） |
| 小程序码 | 成功返回图片；scene 超长 / 非法字符 / page 前导斜杠本地拒绝；errcode 映射 |
| 消息推送 | URL 签名向量；AES 与独立参考实现逐字节一致并可往返；错误 appid / 错误签名 / 篡改密文拒绝；XML / JSON 解析、DTD 拒绝、List 归一；HTTP：GET 验证、安全模式 JSON 重复推送只处理一次、XML 安全模式、拒绝明文 |
| 配置 | 非法 WECHAT_MP_STATE 报错；未配置时 get_client 5020 |

## 12 前端对接

本模块无顾客端接口。小程序侧：`wx.login` 的 code、手机号按钮的 code 交给 01-identity；订阅弹窗结果由 20-notification 记录；小程序码页面读取 `decodeURIComponent(query.scene)`。后台「开发 - 开发管理 - 消息推送配置」：URL 填 `https://<域名>/api/v1/webhooks/wechat-mp/push`，选安全模式、JSON 或 XML 均可。

## 13 定制点

| 类型 | 内容 |
|---|---|
| 配置 | §9 全部环境变量 |
| 注入 | `set_client(client)`：替换进程级客户端（如带 `DbTokenStore` + `RedisLock` 的实例）；`HttpMiniProgramClient(transport=...)` 替换传输层 |
| 扩展钩子 | `register_push_handler(event, fn(db, message))`：任意推送事件的处理函数（不 commit；`dispatch` 去重后统一提交，异常整体回滚）。已登记：01-identity `wxa_media_check` → `handle_media_check_event`，20-notification 三类订阅消息事件 → `handle_push_message`；`TokenStore` 协议：自定义 token 存储（Redis 等） |
| 测试替身 | `FakeWechatServer`（HTTP 层假服务，可注入超时 / 5xx / 任意 errcode）、`FakeMiniProgram`（接假服务的真实客户端 + 造数方法） |
| 不包含 | 旧版 encryptedData 解密（官方推荐新版手机号组件）、getAccessToken（非 stable）、getUserRiskRank、订单发货管理（`/wxa/sec/order/*`）、虚拟支付（`/xpay/*`）、URL Scheme / URL Link、服务通信二次加密（AES256_GCM 签名）、第三方平台代调用 |
