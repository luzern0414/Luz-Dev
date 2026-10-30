# 模块：01-identity（会员账号）`identity`

小程序会员账号：微信登录与自动注册（记录同意的协议版本）、令牌与会话（短期 access JWT + 不透明刷新令牌轮换 / 复用检测 / 家族吊销）、短信验证码（可选登录 / 绑定）、手机号（微信快速验证 / 短信）、资料（白名单 + 内容安全）、注销（阻断检查 + 冷静期 + 匿名化）、后台会员管理。

## 0 官方来源

`D` = `https://developers.weixin.qq.com`；R3 来源为 OWASP / ASVS 5.0 / NIST SP 800-63B-4 / RFC。

| 主题 | 官方页面 | 本模块用到的结论 | 核对日期 |
|---|---|---|---|
| 小程序登录 | D/miniprogram/dev/framework/open-ability/login.md；D/miniprogram/dev/server/API/user-login/api_code2session.md | code 一次性、5 分钟；code2Session 换 openid / unionid / session_key；session_key 不得下发 | 2026-10-28 |
| session_key | D/miniprogram/dev/framework/open-ability/signature.md | 有效期由微信决定、wx.login 可能刷新；服务端及时更新存储 | 2026-10-28 |
| UnionID | D/miniprogram/dev/framework/open-ability/union-id.md | 绑定开放平台后 code2Session 直接返回 | 2026-10-28 |
| 手机号 | D/miniprogram/dev/server/API/user-info/phone-number/api_getphonenumber.md；D/miniprogram/dev/framework/open-ability/getPhoneNumber.md | 用按钮 code 调 getuserphonenumber 并传 openid 校验归属；code 一次性 5 分钟；返回 purePhoneNumber + countryCode | 2026-10-28 |
| 内容安全 | D/miniprogram/dev/server/API/sec-center/sec-check/api_msgseccheck.md；api_mediacheckasync.md；D/miniprogram/dev/framework/operation.md | 昵称 scene=1（资料）同步检测；头像异步检测 30 分钟内推送；review 需人工确认 | 2026-10-28 |
| 用户数据删除 | D/miniprogram/product/index.md（运营规范 2.15）；D/miniprogram/product/reject.md（3.4.1） | 提供删除方式，注销后删除相关数据且不可恢复 | 2026-10-28 |
| 隐私与手机号使用 | D/miniprogram/product/index.md；D/miniprogram/dev/framework/user-privacy/PrivacyAuthorize.md | 不得强制要求手机号才能使用；隐私协议须用户主动同意 | 2026-10-28 |
| 注销时限 | 《App违法违规收集使用个人信息行为认定方法》第六部分（https://www.nbd.com.cn/articles/2019-12-30/1397100.html 转载） | 必须提供注销；不得设不合理条件；人工处理不超过 15 个工作日；后台须真正完成 | 2026-10-28 |
| 删除义务 | 《个人信息保护法》第十九、四十七条（https://www.cac.gov.cn/2021-08/20/c_1631050028355286.htm） | 最短保存；法定留存期内只存储不处理 | 2026-10-28 |
| 刷新令牌 | RFC 9700 §4.14.2（https://www.rfc-editor.org/rfc/rfc9700） | 每次刷新轮换；已作废令牌再出现 → 吊销当前有效令牌（家族）；不活动后过期 | 2026-10-28 |
| JWT | RFC 8725 §3.1 / §3.9；RFC 7519 §4.1；ASVS 9.1.1 / 9.1.2 / 9.2.1 / 9.2.3 | 算法白名单；先验签；exp / iat 小偏差；必须校验 aud；jti 唯一 | 2026-10-28 |
| 会话 | ASVS 7.2.3、7.4.1、7.4.2、7.4.5；OWASP Session Management Cheat Sheet；NIST 800-63B-4 AAL1 | 引用令牌 ≥128 bit CSPRNG；登出服务端作废；停用 / 删除终止全部会话；管理员可终止会话；总时长 ≤ 30 天 | 2026-10-28 |
| 短信验证码 | NIST 800-63B-4 §3.1.3.2；ASVS 6.5.1 / 6.5.3 / 6.5.5 / 6.6.2 / 6.6.3；OWASP MFA Cheat Sheet | ≥ 6 位、CSPRNG、≤ 10 分钟、只能用一次、绑定请求、限次数限速、哈希存储 | 2026-10-28 |
| 短信频控 | https://help.aliyun.com/zh/document_detail/2881383.html；https://cloud.tencent.com/document/product/382/37809 | 云厂商默认同号 1 条/分钟、10 条/天；服务端频控不高于此 | 2026-10-28 |
| 防枚举 | OWASP Authentication Cheat Sheet；ASVS 6.3.8 | 发送 / 校验失败的响应不区分号码是否注册 | 2026-10-28 |
| 敏感操作再认证 | ASVS 7.5.1 | 注销前要求新鲜令牌 | 2026-10-28 |
| 审计 | OWASP Logging Cheat Sheet；ASVS 16.3.1 | 认证事件、管理员操作、敏感数据访问、导出都要记录；不记令牌与 PII | 2026-10-28 |

未核实（不作为规则）：注销冷静期的法定时长（行业惯例 7–15 天，本模块默认 15 天且上限 15 天，保证 15 个工作日内完成）。

## 1 状态与依赖

| 项 | 内容 |
|---|---|
| 状态 | 标准：登录注册、令牌会话、短信、手机号、资料与内容安全、注销与匿名化、后台管理、定时任务全部实现 |
| 代码 | `01-identity/code/identity/`（models / service / admin / rules / ports / config / jobs / api），测试 `01-identity/code/tests/` |
| 依赖 | 00-framework；28-wechat-mp（code2Session、手机号、内容安全、推送）；27-legal（协议同意，可替换）；22-admin-rbac（后台权限，可替换） |

## 2 职责与边界

| 负责 | 不负责 |
|---|---|
| 微信登录、自动注册、协议版本同意记录（经 27-legal） | 协议文本与版本管理（27-legal） |
| access / refresh 令牌、会话、登出、复用检测 | 后台员工账号与权限（22-admin-rbac） |
| 短信验证码的生成、频控、校验 | 短信通道实现（项目提供 `SmsSender`） |
| 手机号绑定（加密 / 哈希 / 脱敏） | 会员等级、积分（14-membership） |
| 资料白名单与内容安全 | 头像上传存储（21-media） |
| 注销：阻断检查注册表、冷静期、匿名化、`member.deleted` 事件 | 其他模块的数据清理（消费 `member.deleted`） |
| 后台会员列表 / 导出 / 详情 / 查看手机号 / 停用启用 / 强制下线 | 订阅消息（20-notification） |

## 3 数据模型

| 表 | 字段 | 约束 / 索引 | 说明 |
|---|---|---|---|
| `users` | id, member_no, openid, unionid, wx_session_key_enc, nickname, avatar_url, gender, birthday, phone_enc, phone_hash, phone_masked, phone_verified_at, status, disabled_reason, disabled_at, register_channel, agreed_document_ids, content_review, last_login_at, deletion_requested_at, deletion_due_at, deletion_reason, deletion_hold, created_at, updated_at, deleted_at | `member_no` 唯一；`openid` 唯一；`phone_hash` 唯一；`unionid` 索引；(status, created_at) | status：active / disabled / deleting / deleted；session_key 与手机号 AES-GCM 加密 |
| `user_sessions` | id, sid, user_id, status, channel, created_at, last_seen_at, absolute_expires_at, revoked_at, revoke_reason, ip, user_agent | `sid` 唯一；user_id 索引 | 会话 = 刷新令牌家族；sid 写入 access 令牌 |
| `user_refresh_tokens` | id, session_id, user_id, token_hash, parent_id, status, created_at, expires_at, used_at | `token_hash` 唯一；session_id 索引 | status：active / rotated / revoked；只存 SHA-256 |
| `sms_codes` | id, request_id, phone_hash, purpose, code_hash, attempts, status, send_slot, ip_hash, expires_at, used_at, created_at | `request_id` 唯一；(phone_hash, purpose, send_slot) 唯一；(phone_hash, purpose)、ip_hash、created_at 索引 | status：active / used / superseded / failed；code_hash = HMAC(请求 + 号码 + 用途 + 码) |
| `user_content_checks` | id, user_id, field, value, trace_id, status, label, created_at, decided_at | `trace_id` 唯一；user_id、created_at 索引 | 头像异步检测：pending / pass / risky / review / failed / timeout / superseded |

## 4 业务规则

**登录与注册**
1. 先调 code2Session（事务外），再取回或创建会员；openid 唯一索引兜底并发首次登录（并发 20 次只建 1 个）。
2. 新会员注册时记录同意的协议版本（`agreements: [{document_id, version, sha256}]` → 27-legal accept）；仍有必需协议未同意 → 4220 `agreement_required`（带 pending 列表），不建号。老会员登录返回 `pending_agreements`（协议更新需重新同意）。
3. 会员号 `M` + 10 位随机数字，不按注册顺序。
4. session_key 只在服务端加密保存（`session_key_of` 供内部用），任何响应不含 session_key。
5. 停用的账号登录 → 4030 `{"reason": "account_disabled"}` 并写审计；已匿名化账号的 openid 已替换，同一微信再次登录成为新会员。
6. 冷静期（deleting）内登录 → 自动撤销注销（`deletion_cancelled: true`）。
7. 成功登录开新会话；新会员发 `member.registered`。

**令牌与会话**
8. access JWT：HS256（框架只接受 HS256，拒绝 none 等）、iss / aud / sub / sid / jti / iat / exp，默认 2 小时，时钟偏差 30 秒；先验签再读声明。
9. 每个请求校验会话：sid 对应会话 active、未过绝对期限、会员 active 或 deleting；否则 4010（登出、停用、复用检测、注销即时生效）。
10. 刷新令牌：256 bit 随机、不透明、只存哈希；每次刷新轮换（旧 → rotated，新令牌 parent_id 指向旧）；空闲期 14 天、绝对期限 30 天（会话创建起算）。
11. 已轮换的令牌在宽限（默认 10 秒）内再次出现：4010 `refresh_rotated`，不吊销（同一客户端并发刷新）；超过宽限：吊销整个家族（会话 + 全部令牌）并审计 `member.session.reuse_detected`。
12. 并发刷新同一令牌：条件 UPDATE（status = active）只有一个成功，其余 4010。
13. 登出：`/auth/logout` 结束当前会话，`/auth/logout-all` 结束全部会话；服务端作废。

**短信验证码**（`[identity] sms_enabled`）
14. 6 位 CSPRNG 数字，有效期默认 5 分钟（配置上限 10 分钟），只能使用一次，存 HMAC。
15. 绑定 手机号 + 用途（login / bind）+ 请求（发送时返回 `request_id`，校验必须带回）。
16. 每次校验先条件计数，最多 5 次（含正确那次）；超过后正确码也无效。
17. 重发间隔 60 秒（同号同用途，4290 带剩余秒）；新码使旧码作废；每号每天 10 条；每 IP 每小时 20 条、每天 50 条（4290）；同一时间片唯一约束兜底并发（20 并发只发 1 条）。
18. 防枚举：发送响应与号码是否注册无关；所有校验失败返回同一提示 `verification code is incorrect or expired`。短信登录只登录已绑定手机号的账号，未绑定在验证码通过后才返回 4010 `phone_not_registered`。
19. 先落库再调用短信通道（事务外，不重试）；通道失败 → 5020，记录标 failed，不占重发间隔与每日次数。

**手机号**
20. 微信快速验证：getuserphonenumber 带 openid 校验 code 归属；大陆号存 11 位，境外号存 `+区号号码`。
21. 存储：phone_enc（AES-GCM）+ phone_hash（HMAC，唯一）+ phone_masked（`138****8000`）；接口只返回脱敏值。
22. 号码已被其他账号绑定 → 4090 `{"reason": "PHONE_IN_USE"}`（唯一索引兜底并发）；换绑写审计并发 `member.phone_bound`。

**资料**
23. 可改字段白名单：nickname（1–20 字）、avatar_url（https）、gender（0/1/2）、birthday（真实日期、不晚于今天）；其他字段 4001。
24. 昵称改动先调 msgSecCheck（scene=1）：risky → 4220 `content_risky`；review → 保存并标 `content_review` 待人工复核。
25. 头像改动提交 mediaCheckAsync，不立即生效（`avatar_pending`）；`wxa_media_check` 推送 pass 才生效，risky / review / 失败不生效；新上传使旧检测 superseded；超过 60 分钟未回推记 timeout。
26. 内容安全接口不可用时默认拒绝（5020）；`[identity] content_check_fail_open=true` 时放行并记降级日志。

**注销**
27. 申请要求新鲜令牌（15 分钟内登录，否则 4010 `fresh_required`）。
28. 阻断检查：各模块 `register_deletion_checker(name, fn)` 返回 `Blocker(code, message)`（未完成订单、储值余额、有效会员卡…）；有阻断 → 4220 `deletion_blocked` 带清单。
29. 通过后 active → deleting（条件 UPDATE），冷静期默认 15 天（配置 0–15）；立即结束其他会话，保留当前会话以便撤销；冷静期内登录或主动撤销 → active。
30. 到期任务（每小时）重新检查阻断：有阻断则挂起（`deletion_hold`）下次再试；否则匿名化：openid 换成随机值（不可逆）、清空昵称 / 头像 / 生日 / 手机号 / unionid / session_key、会话 IP 与 UA 清空、吊销全部会话、删除验证码与检测记录、`deleted_at` 置位，发 `member.deleted` 让其他模块清理；member_no 保留。
31. 时限：冷静期 ≤ 15 天 + 任务每小时运行，保证 15 个工作日内完成。

**后台**
32. 列表（`member:read`）：ListSpec 声明 status（多值）、created_at / last_login_at 区间与排序、member_no、content_review，额外参数 has_phone、include_deleted；keyword 匹配昵称 / 脱敏手机号 / 会员号；默认不含已匿名化会员。
33. 导出（`member:read`）：与列表同一筛选，手机号脱敏，≤ 5000 行同步（超过 4225，转 24-report 异步），写审计。
34. 详情（`member:read`）：脱敏手机号、活跃会话数、注销与停用信息，不返回 openid。
35. 查看完整手机号（`member:reveal`）：必须填原因，每次写审计（只记脱敏值），不进幂等缓存。
36. 停用 / 启用 / 强制下线（`member:write`）：必须填原因、幂等键；停用 active → disabled 并吊销全部会话、发 `member.disabled`；启用 disabled → active；非法流转 4090；全部审计。

## 5 状态机与关键流程

```
active ──停用──► disabled ──启用──► active
active ──申请注销──► deleting ──登录 / 撤销──► active
deleting ──到期匿名化──► deleted（终态）
deleting ──到期有阻断──► deleting（deletion_hold，下次重试）
```

| 从 | 到 | 触发 | 失败 |
|---|---|---|---|
| active | disabled | 后台停用 | 非 active → 4090 |
| disabled | active | 后台启用 | 非 disabled → 4090 |
| active | deleting | 本人申请（新鲜令牌、无阻断） | 已 deleting → 4090；阻断 → 4220 |
| deleting | active | 登录 / 撤销 | 非 deleting 撤销 → 4090 |
| deleting | deleted | 任务到期且无阻断 | 有阻断 → 挂起 |

会话：`active → revoked`（登出、全部登出、停用、复用检测、注销、管理员下线）；刷新令牌：`active → rotated`（刷新）/ `revoked`（家族吊销）。

**微信登录失败分支**：code 无效 / 已用 4010；风控拦截 4030；微信超时 / 故障 5020（不重试，code 一次性）；频率 4290；熔断 5030；协议未同意 4220；停用 4030。

## 6 接口

| 方法 | 路径 | 鉴权 | 幂等 | 权限码 | agent | 说明 |
|---|---|---|---|---|---|---|
| POST | `/api/v1/app/auth/login` | 无 | 否（code 一次性；响应含令牌，不缓存） | — | no | `{code, agreements?}` → 令牌 + member_no、is_new_user、phone_bound、deletion_cancelled、pending_agreements |
| POST | `/api/v1/app/auth/refresh` | 刷新令牌 | 否（轮换语义） | — | no | `{refresh_token}` → 新令牌对 |
| POST | `/api/v1/app/auth/logout` | access | 天然幂等 | — | no | 结束当前会话 |
| POST | `/api/v1/app/auth/logout-all` | access | 天然幂等 | — | no | 结束全部会话 |
| POST | `/api/v1/app/auth/sms/send` | 无 | 否（频控） | — | no | `{phone, purpose}` → `{request_id, expires_in, retry_after}` |
| POST | `/api/v1/app/auth/sms/login` | 无 | 否（码一次性） | — | no | `{phone, request_id, code}` → 令牌 |
| GET | `/api/v1/app/me` | access | — | — | read | 本人资料（脱敏） |
| PUT | `/api/v1/app/me` | access | 是 | — | no | 白名单字段；昵称 / 头像内容安全 |
| POST | `/api/v1/app/me/phone/wechat` | access | 是（先查重放再调微信） | — | no | `{phone_code}` |
| POST | `/api/v1/app/me/phone/sms` | access | 是 | — | no | `{phone, request_id, code}` |
| GET | `/api/v1/app/me/deletion` | access | — | — | no | 状态、到期时间、阻断清单 |
| POST | `/api/v1/app/me/deletion` | access（新鲜） | 是 | — | no | 申请注销 `{reason?}` |
| POST | `/api/v1/app/me/deletion/cancel` | access | 是 | — | no | 撤销注销 |
| GET | `/api/v1/admin/members` | 员工 | — | member:read | no | 列表（§4.32） |
| GET | `/api/v1/admin/members/export` | 员工 | — | member:read | no | CSV（脱敏、审计） |
| GET | `/api/v1/admin/members/{id}` | 员工 | — | member:read | no | 详情 |
| POST | `/api/v1/admin/members/{id}/reveal-phone` | 员工 | 否（不缓存明文） | member:reveal | no | `{reason}` → 完整手机号，审计 |
| POST | `/api/v1/admin/members/{id}/disable` | 员工 | 是 | member:write | no | `{reason}` |
| POST | `/api/v1/admin/members/{id}/enable` | 员工 | 是 | member:write | no | `{reason}` |
| POST | `/api/v1/admin/members/{id}/logout` | 员工 | 是 | member:write | no | 强制下线 `{reason}` |

推送回调 `wxa_media_check` 经 28-wechat-mp `/api/v1/webhooks/wechat-mp/push` 进入 `handle_media_check_event`。

限流声明 `identity.api.rate_limit_rules()`（按 IP）：login 5 次/分钟、sms/login 5 次/分钟、sms/send 5 次/分钟、refresh 30 次/分钟；按手机号与每日上限在服务层用数据库计数（§4.17）。

## 7 事件

| 方向 | 主题 | payload | 说明 |
|---|---|---|---|
| 发出 | `member.registered` | user_id, member_no, channel | 新会员 |
| 发出 | `member.phone_bound` | user_id, source | 绑定 / 换绑 |
| 发出 | `member.disabled` | user_id | 后台停用 |
| 发出 | `member.deleted` | user_id, deleted_at | 匿名化完成；各模块据此清理个人数据 |
| 消费（推送） | `wxa_media_check` | — | 头像审核结果（经 28-wechat-mp，不走发件箱） |
| 消费（发件箱） | — | — | `identity/events.py` 的 `SUBSCRIPTIONS = {}`：当前不消费发件箱主题 |

## 8 并发与一致性

| 场景 | 机制 |
|---|---|
| 同一微信并发首次登录 | openid 唯一约束 + 回滚重读（测试 20 并发 1 个会员） |
| 同一刷新令牌并发 | 条件 UPDATE rowcount；宽限内不吊销（测试 20 并发 1 成功、家族保持） |
| 令牌复用 | 宽限外吊销家族（会话 + 令牌） |
| 同号并发发码 | (phone_hash, purpose, send_slot) 唯一（测试 20 并发只发 1 条） |
| 验证码爆破 | 条件计数 attempts < 上限，正确码也计数 |
| 手机号并发绑定 | phone_hash 唯一索引 → PHONE_IN_USE |
| 状态流转 | 条件 UPDATE（WHERE status = 源状态） |
| 外部调用 | code2Session / 手机号 / 内容安全在写事务外；短信先提交记录再发送 |
| 幂等 | 写接口 run_idempotent；先做外部调用的接口先查已完成的幂等记录再调用；响应时间统一 ISO 8601，重放字节一致 |

## 9 配置

运行参数写在 `backend/config/runtime/*.toml`，字段说明（类型、范围、能否热加载、是否下发客户端）在 `00-framework/code/luzback/runtime_config.py`；模块经 `luzback.module_config.ModuleConfig` 读取，热加载后下一次读取即生效。环境变量只放秘密与部署参数；迁移前的环境变量名仍设置着时，启动会打 `config.unknown_env` 警告并指出新键。测试用 `set_config(...)` 或 `luzback.testing.override_runtime(...)` 注入运行参数（写进一份测试用运行配置，测试结束撤销）。

### 9.1 环境变量（秘密与部署参数）

| 变量 | 默认 | 说明 |
|---|---|---|
| `IDENTITY_JWT_AUDIENCE` | luz-app | access 令牌 aud |
| `LUZ_JWT_SECRET / LUZ_JWT_ISSUER` | 框架 | 令牌签名密钥与签发方 |
| `LUZ_FIELD_KEY` | 框架 | 手机号 AES-GCM 加密与盲索引（32 字节 base64） |
| `LUZ_TIMEZONE` | Asia/Shanghai | 每日计数的时区 |

### 9.2 运行配置键

| 键 | 默认 | 范围 | 热加载 | 下发客户端 | 说明（含单位） |
|---|---|---|---|---|---|
| `[identity] access_ttl_seconds` | 7200 | 60–86400 | 是 | 否 | 会员 access 令牌有效期（秒）；新签发的令牌按新值（原 `IDENTITY_ACCESS_TTL`） |
| `[identity] jwt_leeway_seconds` | 30 | 0–300 | 是 | 否 | 校验令牌时允许的时钟偏差（秒，RFC 7519：最多几分钟）（原 `IDENTITY_JWT_LEEWAY`） |
| `[identity] refresh_idle_days` | 14 | 1–30 | 是 | 否 | 刷新令牌空闲超时（天），不大于绝对期限（原 `IDENTITY_REFRESH_IDLE_DAYS`） |
| `[identity] refresh_absolute_days` | 30 | 1–30 | 是 | 否 | 刷新令牌绝对期限（天，NIST AAL1 不超过 30）（原 `IDENTITY_REFRESH_ABSOLUTE_DAYS`） |
| `[identity] refresh_reuse_grace_seconds` | 10 | 0–60 | 是 | 否 | 刷新令牌轮换后旧令牌的宽限（秒），并发刷新不误判为重放（原 `IDENTITY_REFRESH_REUSE_GRACE`） |
| `[identity] session_check` | true | true / false | 是 | 否 | 每个请求校验服务端会话（登出、停用、注销即时生效）（原 `IDENTITY_SESSION_CHECK`） |
| `[identity] sms_enabled` | true | true / false | 是 | 是 | 短信验证码登录与绑定开关；关闭后客户端隐藏短信入口（原 `IDENTITY_SMS_ENABLED`） |
| `[identity] sms_ttl_seconds` | 300 | 60–600 | 是 | 否 | 短信验证码有效期（秒，不超过 10 分钟）（原 `IDENTITY_SMS_TTL`） |
| `[identity] sms_max_attempts` | 5 | 1–10 | 是 | 否 | 同一验证码最多校验几次（原 `IDENTITY_SMS_MAX_ATTEMPTS`） |
| `[identity] sms_resend_seconds` | 60 | 10–600 | 是 | 是 | 同一手机号重发间隔（秒）；客户端倒计时用同一个值（原 `IDENTITY_SMS_RESEND_SECONDS`） |
| `[identity] sms_daily_per_phone` | 10 | 1–100 | 是 | 否 | 每个手机号每天最多发几条验证码（原 `IDENTITY_SMS_DAILY_PER_PHONE`） |
| `[identity] sms_hourly_per_ip` | 20 | 1–10000 | 是 | 否 | 每个 IP 每小时最多发几条验证码（原 `IDENTITY_SMS_HOURLY_PER_IP`） |
| `[identity] sms_daily_per_ip` | 50 | 1–100000 | 是 | 否 | 每个 IP 每天最多发几条验证码（原 `IDENTITY_SMS_DAILY_PER_IP`） |
| `[identity] content_check` | true | true / false | 是 | 否 | 昵称与头像走内容安全检测（原 `IDENTITY_CONTENT_CHECK`） |
| `[identity] content_check_fail_open` | false | true / false | 是 | 否 | 检测服务不可用时是否放行（默认不放行）（原 `IDENTITY_CONTENT_CHECK_FAIL_OPEN`） |
| `[identity] media_check_timeout_minutes` | 60 | 5–1440 | 是 | 否 | 头像异步检测多久没有结果按失败处理（分钟）（原 `IDENTITY_MEDIA_CHECK_TIMEOUT_MINUTES`） |
| `[identity] deletion_cooling_days` | 15 | 0–15 | 是 | 否 | 注销冷静期（天，个人信息须在 15 个工作日内删除）（原 `IDENTITY_DELETION_COOLING_DAYS`） |
| `[identity] sms_retention_days` | 7 | 1–365 | 是 | 否 | 短信验证码记录保留（天）（原 `IDENTITY_SMS_RETENTION_DAYS`） |
| `[identity] session_retention_days` | 180 | 30–3650 | 是 | 否 | 已失效会话保留（天）（原 `IDENTITY_SESSION_RETENTION_DAYS`） |
| `[rate_limit] login_per_min` | 5 | 1–1000 | 是 | 否 | 登录接口每个 IP 每分钟次数（会员登录与后台登录各自计数） |
| `[rate_limit] sms_send_per_min` | 5 | 1–1000 | 是 | 否 | 发送短信验证码接口每个 IP 每分钟次数（按手机号与每日上限见 [identity]） |
| `[rate_limit] refresh_per_min` | 30 | 1–1000 | 是 | 否 | 刷新令牌接口每个 IP 每分钟次数 |
| `[auth] fresh_window_s` | 900 | 60–3600 | 是 | 否 | 资金与敏感操作要求最近认证的时间窗（秒） |
| `[auth] service_signature_skew_s` | 300 | 30–900 | 是 | 否 | 服务间签名允许的时钟偏差（秒） |

定时任务（`identity.jobs.JOBS`，Job 元组；也可 `register(registry)`）：`identity.anonymize_due`（每小时）、`identity.expire_content_checks`（10 分钟）、`identity.purge_auth_records`（每天）。

## 10 错误码

| 场景 | code | data.reason |
|---|---|---|
| 参数错误、字段不可改、验证码错误 / 过期 / 用尽 | 4001 | `sms_code_invalid`（验证码） |
| code 无效、令牌无效 / 过期 / aud 错误、会话已吊销、刷新令牌无效 | 4010 | `session_revoked` / `refresh_invalid` / `refresh_rotated` / `session_expired` |
| 注销缺新鲜令牌 | 4010 | `fresh_required` |
| 短信登录号码未绑定（验证码通过后） | 4010 | `phone_not_registered` |
| 账号停用、微信风控拦截、后台无权限 | 4030 | `account_disabled` |
| 会员不存在 | 4040 | — |
| 手机号被占用 | 4090 | `PHONE_IN_USE` |
| 状态不允许（重复注销、撤销不存在的注销、非法停用 / 启用） | 4090 | — |
| 协议未同意 | 4220 | `agreement_required` |
| 昵称内容违规 | 4220 | `content_risky` |
| 注销被阻断 | 4220 | `deletion_blocked` |
| 导出超过同步上限 | 4225 | — |
| 发码过频 / 每日上限 / IP 上限、微信频率 | 4290 | — |
| 微信 / 短信 / 内容安全不可用 | 5020 | — |
| 熔断打开 | 5030 | — |

## 11 测试清单

| 文件 | 覆盖 |
|---|---|
| `test_identity_auth.py` | 注册记录协议版本与加密 session_key；未同意协议拒绝且不建号；旧版本冲突；老会员复用并返回新协议；错误码映射（无效 code / 风控 / 超时 / 频率 / 空 code）；旧式注入；停用登录 4030；20 并发首次登录 1 会员；JWT 声明、伪造签名、alg=none、错误 aud、缺 sid、错误 kind、刷新令牌当 access、过期、偏差容忍；登出后会话失效；轮换只存哈希；宽限内复用不吊销；宽限外复用吊销家族且不影响其他设备；零宽限；空闲期与绝对期限；伪造刷新令牌；20 并发刷新 1 成功；当前 / 全部登出；短信：只存哈希、重发间隔与旧码作废、每号每日与每 IP 上限、5 次后正确码也无效、过期 / 用途 / 号码 / 请求不符 / 只能用一次、防枚举响应一致、短信登录、通道故障不占额度、参数与配置上限、20 并发发码只发 1 条；手机号：加密哈希脱敏与事件、PHONE_IN_USE、code 归属、境外号码、HMAC 键；27-legal 真实协议联动 |
| `test_identity_profile.py` | 白名单与字段校验；昵称 risky / review；内容安全默认拒绝与放行配置；头像 pass 后生效、重复推送幂等、非本模块 trace 忽略；risky / 下载失败不生效、新上传取代；检测超时任务幂等；关闭检测；注销：新鲜令牌、阻断清单、保留当前会话与撤销、冷静期登录撤销、匿名化全量清理与事件与重新注册、到期出现阻断挂起后完成、冷静期配置与上限；任务注册与清理幂等；停用吊销会话、事件、非法流转、freeze 兼容 |
| `test_identity_http.py` | 测试工具 `identity.testing.member_token` 签发的令牌可通过 current_user；JOBS / SUBSCRIPTIONS 接线约定； 登录 / 资料 / 刷新 / 登出 HTTP；4010 / 4001 / 多余字段；停用登录 4030 信封；PUT /me 缺幂等键 4001、重放一致、同键异体 4093、非白名单字段；微信手机号重放（code 已用）；短信绑定与登录；注销流程；登录限流 4290；后台列表筛选 / 关键词 / 排序 / 未知参数 / 鉴权；详情脱敏、查看手机号需 member:reveal 且审计不含明文；导出脱敏与审计；停用幂等、4030 / 4001 / 4090、启用、强制下线；推送回调到头像生效；current_claims 直接调用（CONTRACTS）；权限码与事件声明 |

## 12 前端对接

| 页面 | 接口 | 行为 |
|---|---|---|
| 启动 / 登录 | `wx.login` → `/auth/login` | 首次登录先展示协议弹窗，用户勾选后把协议 `{document_id, version, sha256}` 一并提交；4220 `agreement_required` → 弹协议；`pending_agreements` 非空 → 弹更新后的协议 |
| 任意接口 | `Authorization: Bearer` | 4010 → 调 `/auth/refresh` 一次（串行，避免并发刷新）；`refresh_rotated` → 用另一个请求刚拿到的令牌；仍失败 → 静默 `wx.login` 重新登录 |
| 我的 → 绑定手机号 | 按钮 `getPhoneNumber` → `/me/phone/wechat` | 4090 `PHONE_IN_USE` → 提示联系客服或换号 |
| 短信验证码 | `/auth/sms/send` → `/me/phone/sms` 或 `/auth/sms/login` | 保存 `request_id`；按 `retry_after` 倒计时；错误统一提示“验证码错误或已失效” |
| 编辑资料 | `/me`（PUT，幂等键） | 头像返回 `avatar_pending` → 显示“审核中”；昵称 4220 → 就近提示 |
| 设置 → 注销账号 | `/me/deletion`（GET / POST）、`/me/deletion/cancel` | 先展示阻断清单；4010 `fresh_required` → 静默重新登录后重试；冷静期显示到期时间与撤销按钮 |
| 退出登录 | `/auth/logout` / `/auth/logout-all` | 清本地令牌 |
| 后台会员管理 | `/admin/members*` | 列表筛选与导出；“查看手机号”需填原因；停用 / 启用需填原因 |

## 13 定制点

| 类型 | 内容 |
|---|---|
| 配置 | §9：环境变量只有令牌受众与框架密钥；运行参数在 config/runtime 的 `[identity]`（测试用 `identity_testkit.set_config` 或 `override_runtime`） |
| 注入（`identity.api`） | `set_mini_program(client)`（默认 `wechat_mp.get_client()`）；`set_code2session(fn)` / `set_phone_resolver(fn)`（旧式兼容）；`set_sms_sender(SmsSender)`；`set_agreement_gateway(AgreementGateway)`（默认 27-legal）。后台路由鉴权直接用 22-admin-rbac `require_permission`（member:read / member:write；member:reveal 另要求新鲜认证 fresh=True），不再有本地授权器 |
| 对外查询 | `find_user_by_phone(db, phone) -> User \| None`：规范化（去空格 / 短横线 / +86）后按 phone_hash 等值查询，不合法 / 未绑定 / 已注销返回 None；供优惠券、储值、会员卡等后台按手机号找会员 |
| 生产检查 | 导入时 `luzback.register_production_check("identity", production_problems)`：LUZ_FIELD_KEY 必须是 32 字节 base64、需安装 cryptography（运行参数范围由运行配置加载时校验）；`Settings.assert_production_safe()` 启动时执行 |
| 扩展钩子 | `register_deletion_checker(name, fn(db, user_id) → [Blocker])`：注销前置检查；消费 `member.deleted` 清理本模块数据 |
| 测试替身 | `FakeSmsSender`、`FakeAgreements`、`wechat_mp.FakeMiniProgram`；`identity.testing.member_token(db, user_id=None)` / `member_tokens` 供其他模块测试签发真实可用的会员令牌（含会话）；`identity.current_user_id(token)` 保持可导入 |
| 不包含 | 用户名 / 密码登录、找回密码；多因素认证（会员端）；会员自助查看与终止会话列表（ASVS 7.5.2，仅提供全部登出）；设备指纹与风险分级（getUserRiskRank）；旧版 encryptedData 手机号解密；跨小程序 / 公众号的 unionid 账号合并；昵称 review 的人工复核界面（仅标记 `content_review`，后台可按其筛选） |
