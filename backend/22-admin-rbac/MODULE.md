# 模块：22-admin-rbac（后台账号、认证、权限与审计）`admin_rbac`

## 0 官方来源

| 主题 | 官方页面 | 本模块用到的结论 | 核对日期 |
|---|---|---|---|
| 口令长度与策略 | NIST SP 800-63B-4 §3.1.1.2 https://pages.nist.gov/800-63-4/sp800-63b.html | 仅口令时 ≥15 位，作为多因素之一时 ≥8 位；最长至少 64；禁止组成规则；不强制定期改密；比对常见 / 泄露口令；NFC 规范化 | 2026-10-28 |
| 口令要求 | OWASP ASVS 5.0 V6 https://github.com/OWASP/ASVS/blob/master/5.0/en/0x15-V6-Authentication.md | 6.2.3 改密需当前口令；6.2.4 比对常见口令；6.2.8 不截断；6.3.2 禁用默认账号名；6.4.1 系统下发初始口令随机、短期、首次使用后失效；6.5.1/6.5.2 恢复码一次性、<112 bit 用口令哈希加盐存储 | 2026-10-28 |
| 口令哈希 | OWASP Password Storage Cheat Sheet https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html | Argon2id 最低 m=19 MiB、t=2、p=1；PHC 串存参数；登录成功时按新参数重哈希 | 2026-10-28 |
| 防暴力破解 | NIST SP 800-63B-4 §3.2.2；OWASP Authentication Cheat Sheet https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html | 按账号计数（不按 IP）；连续失败上限 ≤100；递增等待；成功清零；验证码只作纵深防御、可在若干次失败后要求 | 2026-10-28 |
| 防账号枚举 | OWASP Authentication Cheat Sheet | 账号不存在 / 口令错 / 锁定 / 停用返回同一提示；账号不存在也执行一次哈希保持耗时一致 | 2026-10-28 |
| TOTP | RFC 6238 https://www.rfc-editor.org/rfc/rfc6238 ；RFC 4226 https://www.rfc-editor.org/rfc/rfc4226 | X=30 s、T0=0、HMAC-SHA1；漂移建议 ≤1 步；同一 OTP 不得二次接受；附录 B 测试向量；密钥 160 bit（RFC 4226 R6） | 2026-10-28 |
| MFA | OWASP Multifactor Authentication Cheat Sheet https://cheatsheetseries.owasp.org/cheatsheets/Multifactor_Authentication_Cheat_Sheet.html | 管理员与高权限账号必须 MFA；首次绑定下发一次性恢复码；更换 / 重置因素需重新认证并通知 | 2026-10-28 |
| 会话 | OWASP Session Management Cheat Sheet https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html ；ASVS V7 https://github.com/OWASP/ASVS/blob/master/5.0/en/0x16-V7-Session-Management.md | 令牌 ≥128 bit CSPRNG；空闲 15–30 分钟、绝对 4–8 小时，服务端强制；登录 / 重新认证 / 改密后换发令牌（7.2.4）；停用即终止会话（7.4.2）；用户可查看并终止自己的会话（7.5.2）；管理员可终止他人会话（7.4.5）；敏感属性修改前重新认证（7.5.1） | 2026-10-28 |
| 授权 | OWASP Authorization Cheat Sheet https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html ；ASVS V8 https://github.com/OWASP/ASVS/blob/master/5.0/en/0x17-V8-Authorization.md | 默认拒绝；每请求服务端校验；最小权限；多租户（门店）隔离（8.4.1） | 2026-10-28 |
| 日志 | OWASP Logging Cheat Sheet https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html ；ASVS V16 https://github.com/OWASP/ASVS/blob/master/5.0/en/0x25-V16-Security-Logging-and-Error-Handling.md | 记录全部认证尝试（含因素）、授权失败、权限变更、管理员操作、敏感数据访问、导出；不记口令 / 令牌 | 2026-10-28 |
| 日志留存 | 《网络安全法》第二十一条 https://www.cac.gov.cn/2016-11/07/c_1119867116_2.htm | 网络日志留存不少于六个月 → 登录日志默认 180 天 | 2026-10-28 |
| 操作权限 | 《个人信息保护法》第五十一条 https://www.cac.gov.cn/2021-08/20/c_1631050028355286.htm | 合理确定个人信息处理的操作权限 → 手机号明文需 `staff:sensitive` 并审计 | 2026-10-28 |
| 列表与导出 | 本仓库 SPEC §3.4 / `luzback.listing`（依据 Google AIP-158/132、Azure REST 指南，见 R5 调研） | 声明式筛选 / 排序 / 关键词；未知参数 4001；导出复用同一声明并记审计 | 2026-10-28 |

## 1 状态与依赖

| 项 | 内容 |
|---|---|
| 状态 | **标准**（代码 + 测试齐全） |
| 依赖 | 00-framework（db、errors、audit、outbox、idempotency、listing、export、crypto、scheduler）；`cryptography ≥ 44`（Argon2id，未装 argon2-cffi 时使用） |
| 被依赖 | 所有带后台接口的模块（见下表，全部从包 `admin_rbac` 导入） |

**对外导出（签名固定）**

| 名称 | 签名 | 说明 |
|---|---|---|
| `require_permission` | `require_permission(*codes: str) -> Depends 可用的依赖，返回 Staff` | 需持有全部码，否则 4030；未登记的码自动登记 |
| `current_staff` | `current_staff(...) -> Staff`（FastAPI 依赖） | 完整会话；受限会话 4011 |
| `authenticated_staff` | 同上 | 允许受限会话（仅 me / 改密 / MFA 绑定 / 登出用） |
| `require_fresh_auth` / `require_fresh` | FastAPI 依赖 `-> Staff` | 300 秒内完成过完整认证，否则 4011 `reauth_required` |
| `load_staff` | `load_staff(db, staff_id: int, **context) -> Staff` | 无会话加载在职员工（任务 / 服务间）；停用 / 删除 4010 |
| `load_active_staff` | `load_active_staff(db, staff_id: int, rv=None) -> Staff` | 旧适配器用：`rv`（= 令牌 `sid`）存在时完整校验服务端会话；受限 4011 |
| `decode_access` / `authenticate` | `decode_access(token) -> dict`；`authenticate(db, token, ip=None, user_agent=None, trace_id=None) -> Staff` | 令牌校验 |
| `declare_permissions` / `declare_permission` | `declare_permissions(module: str, items: Iterable[tuple[code, 描述]])`；`declare_permission(code, description="", module=None)` | 权限码登记 |
| `assert_permission` | `assert_permission(staff, *codes)`（兼容 `assert_permission(db, staff, code)`） | 4030 |
| `store_scope` / `grants_of` | `store_scope(staff) -> list[int] \| None`；`grants_of(db, staff) -> (frozenset[str], list[int] \| None)` | None = 全部门店；`[]` = 无门店 |
| `apply_store_scope` | `apply_store_scope(stmt, staff, column) -> Select` | 查询加门店条件 |
| `assert_in_scope` | `assert_in_scope(staff, store_id)` | 4031 |
| `assert_not_self` | `assert_not_self(staff, other_staff_id, action="approve")` | 职责分离 4030 |
| `assert_fresh` | `assert_fresh(staff, now=None, window_seconds=None)` | 4011 |
| `log_sensitive_read` | `log_sensitive_read(db, staff, resource, resource_id, fields, reason=None)` | 敏感查看审计（调用方 commit） |
| `Staff` | 冻结数据类：`id, username, name, permissions, all_stores, store_ids, role_ids, is_super_admin, mfa_enabled, mfa_required, must_change_password, session_id, auth_at, amr, ip, user_agent, trace_id`；`has(code)`、`store_scope`、`restricted_reason` | — |

## 2 职责与边界

| 负责 | 不负责 |
|---|---|
| 后台员工账号（建 / 改 / 停用 / 启用 / 软删除 / 重置口令 / 解锁） | 小程序顾客登录（01-identity） |
| 口令登录、TOTP 二次验证、恢复码、登录节流与锁定 | 短信 / 邮件通知发送（消费本模块事件，20-notification） |
| 服务端会话（空闲 / 绝对超时、撤销、会话列表）、新鲜认证 | 业务对象的具体数据权限规则（各模块用本模块助手声明） |
| 角色、权限码注册表、角色授权、门店范围 | 审计表本身（框架 `audit_logs`，只追加） |
| 防越权、职责分离助手、敏感查看留痕 | 人机验证服务、泄露口令库（钩子注入） |
| 审计日志与登录日志的查询、导出 | 员工排班、考勤 |

## 3 数据模型

| 表 | 关键字段 | 索引 / 约束 | 说明 |
|---|---|---|---|
| `admin_users` | username, password_hash(PHC), must_change_password, temp_password_expires_at, password_changed_at, name, phone_enc, phone_hash, status(active\|disabled), all_stores, mfa_enabled, mfa_secret_enc, mfa_pending_secret_enc, mfa_last_step, mfa_enrolled_at, last_login_at/ip/ua, created_by, updated_by, deleted_at | username 唯一；phone_hash；status | 软删除后账号名不复用 |
| `admin_roles` | name, builtin_code, description, builtin, mfa_required | name 唯一；builtin_code 唯一 | 内置：super_admin / store_manager / front_desk / finance / operator |
| `admin_permissions` | code(PK), module, description | — | 由注册表同步 |
| `admin_role_permissions` | (role_id, code) | 复合主键 | 超级管理员不存行，运行时视为全集 + `*` |
| `admin_user_roles` | (user_id, role_id) | 复合主键；role_id | — |
| `admin_store_scopes` | (user_id, store_id) | 复合主键；store_id | all_stores=False 时生效 |
| `admin_sessions` | staff_id, token_hash, stage(mfa_pending\|active), amr(pwd\|pwd+otp\|pwd+recovery), created_at, auth_at, last_seen_at, expires_at, revoked_at, revoke_reason, ip, user_agent | token_hash 唯一；staff_id | 令牌明文不落库 |
| `admin_login_throttles` | username(PK, 规范化), failures, window_started_at, locked_until, lock_count, last_failure_at | 主键 | 不存在的账号名同样计数 |
| `admin_login_logs` | user_id?, username, factor, success, reason, ip, user_agent, created_at | (username, created_at)；user_id；created_at | 保留 180 天 |
| `admin_recovery_codes` | staff_id, code_hash(Argon2id), used_at, created_at | staff_id | 一次性 |
| `audit_logs`（框架） | actor_type, actor_id, action, resource, resource_id, before/after_json, reason, ip, user_agent, trace_id | — | 只追加 |

## 4 业务规则

**R1 口令**
1. 规范化 NFC，不截断；>1024 字节直接判失败。
2. 最短：已启用或角色要求 MFA 时 8 位，否则 15 位；最长 128 位；不设组成规则。
3. 拒绝：内置常见口令表、重复字符（≤2 种字符）、包含账号名 / 姓名 / 系统名 / `ADMIN_PASSWORD_BLOCKLIST` 词；配置了泄露检查钩子时拒绝命中口令（钩子异常默认放行，可配置为 5020 拒绝）。
4. 哈希 Argon2id（m=19456 KiB, t=2, p=1，可调高）；旧格式（框架 `argon2$` / `pbkdf2_sha256$`）登录成功后自动重哈希。
5. 管理员新建员工与重置口令时只由系统生成临时口令（20 位随机），仅本次响应返回一次（不写入幂等记录），72 小时内有效，首次登录只能改密。
6. 改密必须提交当前口令；新口令不能与当前相同；改密后撤销其他会话并轮换当前会话。

**R2 登录**
1. 账号不存在 / 口令错误 / 停用 / 临时口令过期：同为 4010 `invalid username or password`；账号不存在时执行一次假哈希。
2. 节流按规范化账号名：`login_failure_window_seconds`（900）内失败 `login_max_failures`（5）次 → 锁定 `login_lock_seconds`（900）秒，再次锁定时长翻倍（封顶 86400）；锁定期间任何口令都返回 4290 + Retry-After；成功登录清零（含锁定级别）。
3. 配置了人机验证钩子时，窗口内失败 ≥ `captcha_after_failures`（3）次后必须带 `captcha_token`，否则 4010 + `captcha_required=true`。
4. 每次尝试写登录日志（因素、成功、内部原因、IP、UA）；锁定时发 `staff.locked`。
5. 已绑定 MFA：口令通过只签发 5 分钟 `mfa_token`；第二步失败同样计入节流；口令阶段不清零计数。

**R3 MFA**
1. TOTP：SHA1、6 位、30 秒、允许 ±1 步；成功时条件 UPDATE `mfa_last_step < step`，同一或更早时间步的码一律拒绝（防重放，并发下只有一个成功）。
2. 密钥 160 bit，加密存储；绑定先生成待确认密钥，用一个有效码确认后启用，并生成 10 个恢复码（`XXXXX-XXXXX`，约 50 bit，Argon2id 存储，只返回一次）。
3. 恢复码一次性（条件 UPDATE `used_at IS NULL`）；可重新生成（旧码全部失效）；使用后发 `staff.recovery_code_used`。
4. 必须 MFA：角色 `mfa_required`（种子：超级管理员、财务）或持有 `ADMIN_MFA_REQUIRED_PERMISSIONS` 中任一码（默认 staff:write、role:write、after_sale:approve、after_sale:refund、payment:refund、report:export、wallet:adjust、wallet:refund、member_card:adjust、member_card:refund、member:reveal；授予的 `module:*` 覆盖其中的码也算），或 `ADMIN_MFA_REQUIRED_ALL=true`。未绑定的员工登录后是受限会话，只能 me / 改密 / 绑定 MFA / 登出，其他接口 4011 `mfa_enrollment_required`。
5. 自行关闭 MFA：需新鲜认证 + 当前 TOTP；角色要求 MFA 时 4220。
6. 重置他人 MFA：必须是另一名管理员（自己 4030）、必须填原因、需新鲜认证；清密钥与恢复码、撤销其全部会话、发 `staff.mfa_reset`。

**R4 会话**
1. 令牌为框架 HS256 JWT（`kind=admin`，`sub`=员工 id，`sid`=`rv`=会话 id，`exp`=绝对过期），只作为会话行的签名引用；库里存令牌 SHA-256，每请求按 `sid` 取会话行并比对哈希——会话有效性完全由服务端决定（撤销立即生效）。MFA 挑战令牌 `kind=admin_mfa`，不能当会话令牌用。`Authorization: Bearer`。
2. 空闲超时 900 秒、绝对超时 8 小时，服务端强制；活动时最多每 60 秒刷新一次 last_seen。
3. 默认绑定 User-Agent，不一致即撤销（4010 `ua_mismatch`）。
4. 每人最多 5 个有效会话，超出撤销最早的。
5. 撤销：登出（当前）、改密（其他）、MFA 绑定 / 关闭（其他）、停用 / 删除 / 重置口令 / 重置 MFA / 角色或门店范围变更 / 管理员强制下线（全部）。
6. 登录 MFA 通过、重新认证、改密、绑定 MFA 后换发新令牌，旧令牌立即失效。
7. 新鲜认证：`auth_at` 在 `fresh_auth_seconds`（300）内；否则 4011 `reauth_required`；`POST /auth/reauth`（口令 + 已绑定时 TOTP / 恢复码）后轮换会话。终止自己的其他会话也要求新鲜认证。

**R5 授权**
1. 权限码 `<module>:<action>`；模块用 `declare_permissions` 登记；`require_permission` 遇到未登记码自动登记；角色只能授予已登记的码或模块通配 `module:*`（该模块至少登记了一个码），否则 4001；`*` 不能授予。
2. 默认拒绝：无授予即 4030；超级管理员持有全集 + `*`。`module:*` 按冒号前缀匹配（`wallet:*` 覆盖 `wallet:adjust` 与以后新增的 wallet 码，不覆盖 `wallet_x:read`）；授予别人 `module:*` 要求自己持有 `module:*` 或 `*`（只持有现有精确码不够）。权限每请求从库加载（无缓存，变更立即生效）。
3. 门店范围：`all_stores=True` 为全部；否则为列表（空 = 无门店）。`apply_store_scope` 给查询加条件；`assert_in_scope` 越界 4031。
4. 职责分离：`assert_not_self(staff, initiator_id)`（例如不能审批自己的退款）4030。
5. 防越权：不能改自己的角色 / 门店、不能停用 / 删除 / 重置自己；只能授予自己持有的权限码与门店；只有超级管理员能授予超级管理员角色；只能管理权限与门店范围都不超过自己的员工；范围外员工 4040。
6. 超级管理员角色不可编辑、不可删除；任何操作后至少保留一名在职超级管理员（锁超级管理员角色行后复核，4220）。
7. 内置角色（除超级管理员）可改权限但不可删除；角色仍有员工使用时删除 4090。

**R6 审计**
1. 全部后台写操作与本人安全操作同事务写审计（含 before / after / reason / IP / UA / trace_id）。
2. 敏感查看（员工手机号明文）`staff:sensitive` + 原因必填 + `staff.sensitive_read` 审计；其他模块用 `log_sensitive_read`。
3. 审计 / 登录日志列表按 ListSpec 声明筛选；导出与列表同一声明，导出行为本身写审计（筛选条件、大小）；CSV 防公式注入，手机号脱敏。

## 5 关键流程

**登录**
```
POST /auth/login ─ 锁定? ──是──> 4290 Retry-After
        │否
   需要验证码且未通过? ──是──> 4010 captcha_required
        │否
   账号存在 & 口令对 & 在职 & 临时口令未过期? ──否──> 失败+1（可能锁定）→ 4010 通用提示 / 4290
        │是
   已绑定 MFA? ──是──> mfa_token(5 分钟) ─ POST /auth/mfa ─ TOTP/恢复码 ─失败+1→ 4010/4290
        │否                                                └─通过→ 换发会话令牌
   清零节流 → 会话令牌（must_change_password / mfa_enrollment_required 时为受限会话）
```

**每请求**：令牌哈希查会话 → 撤销 / 阶段不对 → 4010；绝对 / 空闲超时、UA 不符、员工停用 → 撤销并 4010 `reason`；加载权限与门店 → 受限会话 4011 → 权限不足 4030 → 需要新鲜认证 4011 `reauth_required`。

**员工状态**：`active ⇄ disabled`（停用 / 启用，条件 UPDATE，重复操作 4090）；任意 → `deleted`（软删除，终态，不可恢复，账号名不复用）。

## 6 接口

前缀 `/api/v1/admin`；写接口（员工 / 角色）必带 `Idempotency-Key`（作用域 `staff:{id}` + 操作）；“新鲜”= 需 300 秒内完成认证；agent 标记一律 no。

| 方法 | 路径 | 鉴权 / 权限码 | 新鲜 | 幂等 | 说明 |
|---|---|---|---|---|---|
| POST | `/auth/login` | 无 | — | — | 口令登录；返回会话令牌或 `mfa_token` |
| POST | `/auth/mfa` | mfa_token | — | — | TOTP `code` 或 `recovery_code` |
| POST | `/auth/logout` | 会话（含受限） | — | — | 撤销当前会话 |
| GET | `/auth/me` | 会话（含受限） | — | — | 角色、权限码、门店、MFA 状态、受限原因、会话时限 |
| POST | `/auth/password` | 会话（含受限） | — | — | 改密（旧 + 新），返回新令牌 |
| POST | `/auth/reauth` | 会话（含受限） | — | — | 新鲜认证，返回新令牌 |
| GET | `/auth/sessions` | 会话 | — | — | 本人有效会话 |
| DELETE | `/auth/sessions/{id}` | 会话 | 是 | — | 终止本人某会话（他人的 4040） |
| POST | `/auth/sessions/revoke-others` | 会话 | 是 | — | 终止本人其他会话 |
| POST | `/auth/mfa/enroll` | 会话（含受限） | 是 | — | 生成密钥与 otpauth URI |
| POST | `/auth/mfa/confirm` | 会话（含受限） | — | — | 确认绑定，返回 10 个恢复码 + 新令牌 |
| POST | `/auth/mfa/recovery-codes` | 会话 | 是 | — | 重新生成恢复码 |
| POST | `/auth/mfa/disable` | 会话 | 是 | — | 关闭本人 MFA（角色要求时 4220） |
| GET | `/staff` | staff:read | — | — | 列表：status, mfa_enabled, all_stores, username, created_at_from/to, last_login_at_from/to/_null, role_id, store_id, phone, keyword(账号/姓名), sort |
| GET | `/staff/export` | staff:read | — | — | CSV（手机号脱敏，写审计） |
| GET | `/staff/{id}` | staff:read | — | — | 详情（范围外 4040） |
| POST | `/staff` | staff:write | 是 | 是 | 新建，返回一次性 `temporary_password`（重放返回 null + replayed） |
| PUT | `/staff/{id}` | staff:write | — | 是 | 姓名 / 手机号 |
| PUT | `/staff/{id}/access` | staff:write | 是 | 是 | 角色、all_stores、store_ids；撤销其会话 |
| POST | `/staff/{id}/disable` `/enable` `/unlock` | staff:write | — | 是 | 停用 / 启用 / 解除锁定 |
| POST | `/staff/{id}/reset-password` | staff:write | 是 | 是 | 一次性临时口令 |
| POST | `/staff/{id}/reset-mfa` | staff:write | 是 | 是 | 原因必填，不能是自己 |
| POST | `/staff/{id}/sessions/revoke` | staff:write | — | 是 | 强制下线 |
| GET | `/staff/{id}/sessions` | staff:read | — | — | 该员工有效会话 |
| POST | `/staff/{id}/phone/reveal` | staff:sensitive | — | — | 明文手机号，原因必填，写审计 |
| DELETE | `/staff/{id}?reason=` | staff:write | 是 | 是 | 软删除 |
| GET | `/roles` | role:read | — | — | 列表：builtin, mfa_required, name, created_at_from/to, keyword, sort |
| GET | `/roles/{id}` | role:read | — | — | 详情 |
| POST | `/roles` | role:write | 是 | 是 | 新建（只能授予自己持有的码） |
| PUT | `/roles/{id}` | role:write | 是 | 是 | 修改（超级管理员角色 4220） |
| DELETE | `/roles/{id}?reason=` | role:write | 是 | 是 | 删除（内置 4220，有员工 4090） |
| GET | `/permissions` | role:read | — | — | 权限码注册表 |
| GET | `/audit-logs` | audit:read | — | — | 筛选：actor_type, actor_id, action, resource, resource_id, trace_id, created_at_from/to, keyword(动作/资源ID/原因), sort |
| GET | `/audit-logs/export` | audit:read | — | — | CSV，写 `audit.export` |
| GET | `/audit-logs/{id}` | audit:read | — | — | 详情 |
| GET | `/login-logs` | audit:read | — | — | 筛选：user_id, username, success, factor, reason, ip, created_at_from/to, keyword |
| GET | `/login-logs/export` | audit:read | — | — | CSV，写 `login_log.export` |

## 7 事件

| 发出（发件箱） | payload | 用途 |
|---|---|---|
| `staff.created` / `staff.disabled` / `staff.enabled` / `staff.deleted` | staff_id | 通知、同步 |
| `staff.access_changed` | staff_id | 通知 |
| `staff.password_reset` / `staff.password_changed` | staff_id | 安全通知（ASVS 6.3.7） |
| `staff.mfa_reset` | staff_id, by | 安全通知 |
| `staff.locked` | staff_id, seconds | 安全通知 |
| `staff.recovery_code_used` | staff_id, remaining | 安全通知 |

不消费事件（`events.py: SUBSCRIPTIONS = {}`）。

## 8 并发与一致性

| 场景 | 做法 |
|---|---|
| 失败计数 / 锁定 | `INSERT … ON CONFLICT DO NOTHING` 建行 + `failures = failures + 1` 原子更新；锁定 UPDATE 以 `lock_count` 与“未锁定”为条件，并发下只锁一次（测试：20 线程） |
| TOTP 重放 | `UPDATE … WHERE mfa_last_step < step`，rowcount=1 才通过（测试：8 线程同码只成功 1 次） |
| 恢复码 | `UPDATE … WHERE used_at IS NULL`（测试：8 线程只成功 1 次） |
| 最后一名超级管理员 | 锁超级管理员角色行（Postgres `FOR UPDATE`），写后复核在职超管数（测试：两人互相停用至少留一人） |
| 状态变更 | 停用 / 启用 / 删除 / MFA 确认均为条件 UPDATE，0 行 → 4090 / 4040 |
| 唯一 | username、角色名、builtin_code、token_hash 唯一索引兜底（4090） |
| 写接口 | `run_idempotent`；业务写、审计、事件同一事务；临时口令不进幂等记录 |

## 9 配置

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `ADMIN_SESSION_IDLE_SECONDS` | 900 | 空闲超时 |
| `ADMIN_SESSION_ABSOLUTE_SECONDS` | 28800 | 绝对超时 |
| `ADMIN_SESSION_TOUCH_SECONDS` | 60 | last_seen 刷新粒度 |
| `ADMIN_SESSION_BIND_UA` | true | 会话绑定 UA |
| `ADMIN_MAX_SESSIONS` | 5 | 每人有效会话上限 |
| `ADMIN_FRESH_AUTH_SECONDS` | 300 | 新鲜认证窗口 |
| `ADMIN_MFA_CHALLENGE_SECONDS` | 300 | mfa_token 有效期 |
| `ADMIN_LOGIN_MAX_FAILURES` | 5（回落 `LUZ_LOGIN_MAX_FAILURES`） | 窗口内失败阈值 |
| `ADMIN_LOGIN_FAILURE_WINDOW_SECONDS` | 900 | 计数窗口 |
| `ADMIN_LOGIN_LOCK_SECONDS` | 900（回落 `LUZ_LOGIN_LOCK_SECONDS`） | 首次锁定时长 |
| `ADMIN_LOGIN_LOCK_MAX_SECONDS` | 86400 | 锁定封顶；也是锁定级别衰减时间 |
| `ADMIN_CAPTCHA_AFTER_FAILURES` | 3 | 需验证码的失败次数（需注入钩子） |
| `ADMIN_PASSWORD_MIN_MFA` / `ADMIN_PASSWORD_MIN_NO_MFA` / `ADMIN_PASSWORD_MAX` | 8 / 15 / 128 | 口令长度 |
| `ADMIN_PASSWORD_BLOCKLIST` | 空 | 业务禁用词（逗号分隔） |
| `ADMIN_PASSWORD_BREACH_FAIL_CLOSED` | false | 泄露检查异常时是否拒绝 |
| `ADMIN_ARGON2_MEMORY_KIB` / `ADMIN_ARGON2_TIME_COST` / `ADMIN_ARGON2_PARALLELISM` | 19456 / 2 / 1 | 只可调高 |
| `ADMIN_TEMP_PASSWORD_TTL_SECONDS` | 259200 | 临时口令有效期 |
| `ADMIN_TOTP_ISSUER` | `LUZ_SERVICE_NAME` | otpauth issuer |
| `ADMIN_TOTP_DRIFT_STEPS` | 1 | 允许漂移步数 |
| `ADMIN_RECOVERY_CODE_COUNT` | 10 | 恢复码数量 |
| `ADMIN_MFA_REQUIRED_ALL` | false | 所有员工强制 MFA |
| `ADMIN_MFA_REQUIRED_PERMISSIONS` | staff:write,role:write,after_sale:approve,after_sale:refund,payment:refund,report:export,wallet:adjust,wallet:refund,member_card:adjust,member_card:refund,member:reveal | 持有（含通配覆盖）即强制 MFA |
| `ADMIN_LOGIN_LOG_RETENTION_DAYS` | 180 | 登录日志保留 |
| `ADMIN_EXPORT_MAX_ROWS` | 50000 | 同步导出上限（超出 4225） |
| `LUZ_FIELD_KEY` | 框架 | 手机号 / TOTP 密钥加密 |

定时任务（`jobs.py: JOBS`，`luzback.scheduler.Job` 元组，25-jobs 读取）：`admin_rbac.purge_sessions`（1 小时，删 30 天前撤销 / 过期会话）、`admin_rbac.purge_login_logs`（1 天）、`admin_rbac.purge_throttles`（1 小时）。

部署：每次发布在 `alembic upgrade head` 后执行 `python -m admin_rbac.bootstrap --seed`（导入全部已启用模块登记权限码，再同步 `admin_permissions` 与缺失的内置角色，幂等）；首次部署 `python -m admin_rbac.bootstrap <账号> [姓名]` 创建首个超级管理员（打印一次性临时口令；已有在职超管时拒绝）。

## 10 错误码

| 场景 | code | data |
|---|---|---|
| 账号或口令错误、停用、临时口令过期、MFA 码错误、会话无效 / 超时 | 4010 | `captcha_required` / `reason` |
| 账号锁定 | 4290 + Retry-After | `retry_after` |
| 受限会话（首登改密 / 必须绑定 MFA）、需要新鲜认证 | 4011 | `reason`: password_change_required / mfa_enrollment_required / reauth_required |
| 无权限、越权授予、对自己操作、职责分离 | 4030 | `required` / `reason`: privilege_escalation / self_action / separation_of_duties |
| 门店越界 | 4031 | `store_id` |
| 员工 / 角色 / 会话不存在或不在范围 | 4040 | — |
| 状态冲突、账号名 / 角色名重复、角色仍在使用、MFA 已启用 | 4090 | — |
| 口令策略、参数、未知权限码、缺原因 | 4001 | `errors[{field,message}]` |
| 超级管理员角色不可改、内置角色不可删、最后一名超管、角色要求 MFA 不能关闭 | 4220 | `reason` |
| 导出超限 | 4225 | `total` |
| 泄露检查不可用（fail-closed） | 5020 | — |

## 11 测试清单（`code/tests/`，共 115 个）

| 文件 | 覆盖 |
|---|---|
| `test_admin_rbac_passwords_totp.py` | Argon2id 参数与 PHC、加盐、NFC、不截断、超长、重哈希、旧格式；口令最短 8/15、最长、无组成规则、常见 / 重复 / 上下文词、泄露钩子与 fail-open/closed；RFC 6238 附录 B 全部向量（SHA1/256/512）、RFC 4226 HOTP 向量、160 bit 密钥、±1 漂移、±2 拒绝、重放、otpauth URI、恢复码格式；锁定时长递增、会话超时、新鲜判断、账号名 / 手机号校验 |
| `test_admin_rbac_auth.py` | 登录成功与日志；三种失败不可区分；5 次锁定 / 锁定期正确口令也拒 / FakeClock 到期解锁；不存在账号同样锁定；窗口重置；递增锁定与成功清零；可配置阈值；验证码钩子；旧哈希升级；临时口令首登改密与过期；角色 / 权限码强制 MFA；MFA 绑定、登录、重放、漂移、挑战过期、MFA 失败计入锁定；恢复码一次性与重新生成；关闭 / 重置 MFA 规则；空闲 / 绝对超时；UA 绑定；登出、改密、停用、角色变更撤销会话；会话列表与上限；新鲜认证与重新认证（含二次因素）；me |
| `test_admin_rbac_rbac.py` | 种子幂等；模块通配 `module:*`（前缀匹配、后来登记的码、授予校验与防越权、内置角色存通配、MFA 按通配覆盖）；注册表与自动登记；默认拒绝 4030；超管通配；权限变更立即生效；门店范围过滤 / 4031；职责分离；敏感查看审计；越权授予权限 / 角色 / 门店；不能管理更高权限员工；不能对自己操作；最后一名超管 4220；bootstrap；角色规则；手机号加密 / 脱敏 / 明文审计 / 哈希查询；建号校验；停用 / 启用 / 解锁 / 软删除与账号名不复用；重置口令；强制下线；员工列表筛选与范围；导出脱敏与公式注入 |
| `test_admin_rbac_http.py` | 信封；4010；4001；4290 Retry-After；4030；受限会话 4011；HTTP 上的 MFA 绑定与登录；新鲜认证 4011 → reauth；幂等重放、4093、缺键 4001、临时口令不进幂等记录；员工 / 角色全部接口；审计筛选（操作者、动作、资源、日期区间、关键词）、详情、导出与导出审计；登录日志与导出；本人会话；改密；定时任务 |
| 兼容层（在 auth / http 文件中） | 旧适配器流程：`decode_token(kind=admin)` → `load_active_staff(sid)` → `assert_permission(db, staff, code)` / `grants_of`；登出与空闲超时立即生效；受限会话 4011；MFA 挑战令牌不能当会话令牌；伪造 / 篡改令牌 4010；`JOBS` / `SUBSCRIPTIONS` / 包导出 |
| `test_admin_rbac_concurrency.py` | 文件 SQLite + 线程：并发失败只锁一次且日志不丢；恢复码只用一次；同一 TOTP 只成功一次；两名超管互相停用至少留一人 |

## 12 前端对接

| 页面 | 接口 | 行为 |
|---|---|---|
| 登录 | `POST /auth/login` → `POST /auth/mfa` | 4010 显示通用提示；`captcha_required` 时显示验证码；4290 按 Retry-After 倒计时；`mfa_required` 进入验证码页（支持“使用恢复码”） |
| 首登 / 安全设置 | `GET /auth/me` → `/auth/password`、`/auth/mfa/enroll` → `/auth/mfa/confirm` | `restricted_reason` 决定引导；绑定页渲染 otpauth 二维码；恢复码只展示一次，提示保存；返回的新令牌替换旧令牌 |
| 全局 | 任意接口 | 4010 → 回登录页；4011 `reauth_required` → 弹出重新验证（口令 + 验证码）后重试；4011 其他原因 → 跳安全设置；4030 → 403 页；4031 → 404 页 |
| 菜单 | `GET /auth/me` | 按 `permissions` 隐藏菜单（后端仍逐请求校验）；`*` 表示全部 |
| 我的会话 | `/auth/sessions`、`DELETE /auth/sessions/{id}`、`/auth/sessions/revoke-others` | 标出当前会话 |
| 员工管理 | `/staff*` | 新建 / 重置后弹窗展示一次性临时口令；手机号默认脱敏，“查看”需填原因 |
| 角色管理 | `/roles*`、`/permissions` | 按模块分组展示权限码；超级管理员角色只读 |
| 审计 / 登录日志 | `/audit-logs*`、`/login-logs*` | 日期区间、操作者、动作筛选；导出按当前筛选 |

## 13 定制点

| 类型 | 内容 |
|---|---|
| 配置项 | §9 全部环境变量；代码中 `admin_rbac.set_config(**overrides)` |
| 权限码 | 业务模块 `declare_permissions("module", [(code, 描述)])`；`seed.py:CATALOG` 只登记尚无代码模块的标准码（报表、配送、桌台排队、商品、库存、积分、活动、评价、门店、知识库、员工、设置） |
| 内置角色 | `seed.py:BUILTIN_ROLES`：super_admin（`*`）、store_manager、front_desk、finance、operator，模式为精确码或 `module:*`（原样存储，不依赖模块导入顺序），覆盖 member / order / payment / after_sale / wallet / coupon / lottery / notification / member_card 等模块；只在首次种子时写入，之后在后台编辑 |
| 钩子 | `set_breach_checker(fn(pw)->bool)`（泄露口令库，如 Pwned Passwords k-匿名）；`set_captcha_verifier(fn(token, ip)->bool)`（人机验证）；`set_clock(fn)`（测试） |
| 接入 | 业务模块后台路由：`staff: Staff = Depends(require_permission("payment:refund", fresh=True))`（或另加 `Depends(require_fresh_auth)`）；门店范围 `store_scope(staff)`；不要自行解析令牌、不要信任令牌里的权限声明 |
| 测试 | `admin_rbac.testing.staff_headers(db, *codes, store_ids=None)` / `staff_token` / `staff_actor`：建员工与真实会话（MFA 视为已绑定、刚完成认证），其他模块的 HTTP 测试都用它 |
| 助手 | `store_scope` / `apply_store_scope` / `assert_in_scope`、`assert_not_self`、`assert_fresh`、`log_sensitive_read`、`load_staff` |
| 不包含 | 自助找回口令（后台由管理员重置）；短信 / 邮件 OTP；WebAuthn / 通行密钥；SSO（OIDC / 企业微信）；IP 白名单；密码历史；异步大批量导出（交 24-report）；权限缓存（每请求查库） |
