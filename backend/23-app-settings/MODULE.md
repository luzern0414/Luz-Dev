# 模块：23-app-settings（运行配置接口与加载记录 + 文案与展示配置 + 经营设置）`app_settings`

| 项 | 内容 |
|---|---|
| 状态 | 标准：运行配置的内部接口、客户端下发与加载记录；文案库、展示设置、修改历史与恢复、种子同步；测试在 `code/tests/`（另有框架的运行配置测试） |
| 代码 | `23-app-settings/code/app_settings/`，测试 `23-app-settings/code/tests/`，文案种子 `23-app-settings/seed/texts.toml` |
| 依赖 | 00-framework（`luzback.runtime_config` 字段说明、`luzback.runtime_loader` 加载器与热加载）、22-admin-rbac（后台权限 `settings:read` / `settings:write`、门店范围） |
| 装配 | 基础设施：不受 `LUZ_FEATURES` 影响，总是装配（`app.py` 与 `jobs` 的 `ALWAYS_ON`） |
| 详设 | 模块详设 config（`references/module-specs/config.json`）、content（`references/module-specs/content.json`）、business（`references/module-specs/business.json`）；规范 `references/config-logging.md` §1、§2 |

## 0 官方来源

| 主题 | 页面 | 用到的结论 | 核对日期 |
|---|---|---|---|
| 条件请求 | [RFC 9110 §13.1.2 If-None-Match](https://www.rfc-editor.org/rfc/rfc9110#name-if-none-match)、[§15.4.5 304](https://www.rfc-editor.org/rfc/rfc9110#name-304-not-modified) | 版本即 ETag；命中返回 304、不带正文 | 2026-10-29 |
| 文件监听 | [watchfiles · watch](https://watchfiles.helpmanual.io/api/watch/) | `step` 安静多久才产出、`debounce` 最长合并时间、`stop_event` 退出；通知不可用时轮询 | 2026-10-29 |
| 挂载更新 | [Kubernetes · ConfigMaps](https://kubernetes.io/docs/concepts/configuration/configmap/) | 整目录挂载才会更新，子路径与环境变量注入不更新，更新有延迟 | 2026-10-29 |
| 内容安全 | [微信 · msgSecCheck](https://developers.weixin.qq.com/miniprogram/dev/OpenApiDoc/sec-center/sec-check/msgSecCheck.html) | 需要近 2 小时访问过小程序的用户 openid，后台员工改的文案用不了，故留检测钩子 | 2026-10-29 |

## 1 职责与边界

四类东西分开放，判断口径：这个值要不要商户运营日常自己改？

| 类别 | 例子 | 放在哪 | 谁改 | 怎么生效 | 后台展示 |
|---|---|---|---|---|---|
| 运行参数 | 超时、请求体与上传上限、限流阈值、熔断、认证时间窗、功能开关、最低可用版本、日志级别 | `backend/config/runtime/*.toml`，字段说明 `luzback/runtime_config.py` | 开发，经代码评审 | 服务端热加载；客户端经 `GET /api/v1/app/config` 拉可见的键 | 不展示（用户 2026-10-29 决定） |
| 文案与展示 | 提示、空状态、须知、按钮旁说明；首页模块顺序、展示开关、标签名称、数量上限 | 数据库 `ui_texts`、`display_settings` | 运营，在后台“文案与展示”页 | 保存即生效，内容版本加一；小程序下次显示页面时换新 | 展示（只列 `admin_visible` 的键） |
| 隐藏的文案与展示键 | 通用控件词、系统级提示、法规固定文字、技术性展示配置 | 同上，`admin_visible = false` | 开发：文案改种子 `seed/texts.toml`，展示键改 `display.py` 登记的默认值 | 发布时 `python -m app_settings sync` 写入；照常下发小程序 | 不展示，后台接口返回 `content_key_hidden` |
| 经营设置 | 退款手续费、储值扣减顺序、退券策略、补卡费；项目的起送价、配送费、包装费等 | 数据库 `business_settings`；键由各业务模块用 `luzback.business` 登记 | 运营，在后台“经营设置”页 | 保存即生效，写审计；高风险键要求重新验证身份 | 是（`admin_visible = false` 的除外） |

| 负责 | 不负责 |
|---|---|
| `/internal/config`、`/internal/config/reload`、`/api/v1/app/config`、`config_loads` 加载记录 | 字段说明、文件加载、校验、版本、覆盖规则、热加载与目录监听（框架 `luzback.runtime_config` / `runtime_loader`） |
| 文案库、展示设置、修改历史与恢复、小程序拉取 `/api/v1/app/content`；经营设置的存储与后台维护 `/api/v1/admin/settings` | 门店资料与营业时间（门店模块）；经营设置各键的含义与用法（登记它的业务模块dmin/settings`） |
| 文案种子校验与同步 | 运营内容（公告、横幅、券、奖品：各自模块） |
| 内容安全检测钩子 `set_text_checker` | 具体的检测服务 |

## 2 数据模型

数组与 JSON 值按全项目惯例存成 Text（`*_json`），对应详设里的 `TEXT[]` / `JSONB`。

| 表 | 字段 | 约束与索引 | 说明 |
|---|---|---|---|
| `config_loads` | instance_id、config_version、source_commit、trigger（startup / file_change / reload_api / signal）、result（loaded / unchanged / rejected）、changed_keys_json、restart_keys_json、error_summary、created_at | `(instance_id, created_at)`、`(config_version)` | 每个实例每次加载一行，成功与失败都写；只记键名不记值，错误摘要里秘密已打码 |
| `ui_texts` | key（`页面.位置`）、page、slot_label、text（空 = 默认文案）、default_text、max_length、variables_json、allow_empty、admin_visible、sort_order、version、updated_by、created_at、updated_at | `UNIQUE(key)`、`(page, sort_order)` | 默认文案与安装包内置值一致 |
| `display_settings` | store_id（空 = 全局）、key、value_json、admin_visible、version、updated_by、created_at、updated_at | 部分唯一索引 `(key) WHERE store_id IS NULL` + `(store_id, key) WHERE store_id IS NOT NULL`（等价 `UNIQUE(store_id, key) NULLS NOT DISTINCT`） | 没有行时用 `display.py` 登记的默认值；admin_visible 写入时取登记值 |
| `content_revisions` | target_type（text / display）、target_key、store_id、old_value_json、new_value_json、changed_by、source（admin / migration / restore）、created_at | `(target_type, target_key, created_at)` | 只追加；id 即内容版本 |
| `business_settings` | store_id（空 = 全局）、key、value_json、version、updated_by、created_at、updated_at | 部分唯一索引 `(key) WHERE store_id IS NULL` + `(store_id, key) WHERE store_id IS NOT NULL` | 经营设置；没有行时用登记的默认值；修改写审计，不进内容版本 |

迁移：`alembic/versions/0002_config_content.py`（只新增表与可空列，属于 expand 迁移；同时给 `outbox_events` 加 `traceparent`、`trace_id`）；`0004_business_settings.py`（经营设置）。

## 3 运行配置（框架实现，本模块提供接口与记录）

| 项 | 规则 |
|---|---|
| 文件 | `base.toml` 写全部键与默认值，每个键上方一行注释写用途、单位、范围；`dev.toml`、`staging.toml`、`prod.toml` 只写差异；单位写进键名 |
| 字段说明 | `luzback/runtime_config.py`：Pydantic 模型，`extra="forbid"`、默认值也校验、跨字段校验（会员写速率不大于 IP 速率）；元数据 reloadable、client_visible、risk、owner、scope、platforms、client_since |
| 加载 | base + `<LUZ_ENV>`.toml 深度合并 → 整份校验 → 不可变快照；版本 = 规范化 JSON 的 SHA-256 前 12 位；加载串行加锁，读取无锁 |
| 启动失败 | 逐条打印“键、当前值（秘密打码）、原因、允许范围”，以 2 退出，不会带着坏配置就绪 |
| 热加载 | 目录监听（watchfiles，防抖 1.5 秒；不可用时轮询）、`POST /internal/config/reload`、worker / scheduler 收到 SIGHUP；失败保留旧快照、写 `config.reload_rejected`、`luz_config_reload_failures_total` 加一 |
| 待重启 | reloadable = false 的键（`[db] statement_timeout_ms`、`[logging] queue_capacity`、`[breaker]`）变化时保留当前值，列入 `restart_pending` 与 `config_loads.restart_keys_json`，写 `config.restart_required` |
| 请求内一致 | 中间件在请求开始取一次快照（`use_snapshot`），处理中途热加载不影响本请求；定时任务、发件箱投递每次运行各取一次 |
| 覆盖规则 | 环境文件 `[overrides.<名称>]`：stores、channels、client_version_min / max、percent（按用户内部编号与规则名的摘要分桶，同一用户稳定落桶）；只允许写在键 scope 内的维度；每条规则与全部规则同时命中都要合法 |
| 读取 | `current_config()`（全局值）、`config_for(store_id=, user_ref=)`（带覆盖规则，渠道与版本默认取请求头 `X-Client`、`X-Client-Version`） |

## 4 文案与展示规则

1. 文案键为 `页面.位置`（小写蛇形，≤ 80 字）；种子校验 page 是键前缀、位置说明 ≤ 60 字、默认文案不超上限且只用登记的变量。
2. 修改文案：去掉首尾空白；空值表示用默认文案（`allow_empty` 的键存空串，该位置不显示）；超过字数上限返回 `content_text_too_long`，用了未登记的变量返回 `content_variable_unknown`（字数按原文计，变量占位符也算）。
3. 内容安全：`set_text_checker(fn)` 接检测服务，返回 False 或原因字符串时拒绝（`content_unsafe`）；检测服务异常时不保存（5020）。没接时不检测。展示设置里的文字类键（标签名称）同样检测。
4. 隐藏键（`admin_visible = false`）不出现在后台列表与修改历史；后台修改、恢复默认、恢复历史一律返回 `content_key_hidden`（4030）。
5. 条件更新：请求带当前 version，`UPDATE … WHERE version = :v`；不一致返回 `content_conflict`（4090）并附最新内容。展示设置这一级还没有值时 version 为 0，首次写入并发由部分唯一索引兜底。
6. 每次修改、恢复默认、恢复历史：追加 `content_revisions`（id 即新内容版本）、同事务写审计（`content.text.update / reset / restore`、`content.display.update / reset / restore`）、记 `content.*` 日志。恢复默认时已是默认值不生成新版本。
7. 恢复历史 = 撤销那次修改：目标回到那次修改之前的值（展示设置之前没有值 = 删除这一级）；种子同步产生的记录不能恢复（`revision_not_restorable`）。
8. 展示设置生效值：登记的默认值 < 全局值 < 门店值（只对 `store_scope` 的键）；库里的旧值不再符合登记的范围时退回上一级并记日志。
9. 文案对全部门店生效，只有全门店员工能改（门店员工 4031）；展示设置的门店值按员工门店范围校验。
10. 种子同步幂等：新增、更新元数据、删除种子里没有的键；隐藏键不保留自定义文案；新上限或变量容纳不下的自定义文案回到默认并记日志；生效文案变化的键各写一条 `source = migration` 的修改记录。

## 4.1 经营设置规则

1. 键为 `模块.名称`（≤ 64 字），由业务模块在导入时 `declare_business_settings(BusinessSetting(...))` 登记：类型（bool / int / choice）、默认值、单位、范围或选项（带中文名）、能否按门店覆盖、风险级别、是否后台可见；默认值登记时也校验。
2. 生效值：登记的默认值 < 全局值 < 门店值（只对 `store_scope` 的键）；库里的值不合法时用默认值（后台保存时已校验）。
3. 修改按 version 条件更新（这一级还没有值时为 0），冲突返回 `setting_conflict`（4090）并附最新值；值不合法返回 `validation_failed`；不能按门店的键带了门店返回 `store_override_not_allowed`；隐藏键返回 `business_key_hidden`（4030）。
4. 高风险键（影响资金：扣减顺序、手续费、退券、补卡费）修改与恢复默认要求最近完成过完整认证（4011 `reauth_required`），并填写原因写进审计。
5. 每次修改、恢复默认同事务写审计（`business.setting.update / reset`，修改前后的值、门店、原因），记 `business.setting_updated` 日志；只影响之后发生的业务（已生成的退款单、已核销的券按当时的规则）。
6. 全局值只有全门店员工能改；门店值按员工门店范围校验。

## 5 关键流程

| 流程 | 步骤 | 失败分支 |
|---|---|---|
| 改运行配置 | 改 toml → CI `python -m luzback.config_check --env all` → 评审 → 同步到 `/opt/luz/config/runtime/` → 各进程热加载 → `/internal/config` 与 `config_loads` 核对版本（RUNBOOK §5） | 校验失败：保留旧配置、`config.reload_rejected` 告警；不可热加载的键待重启 |
| 运营改文案 | 后台列表 → 行内编辑 → `PUT /api/v1/admin/content/texts/{key}` → 新内容版本 → 小程序下次显示页面拉到新文案 | 超长 / 未知变量 / 不安全：不保存并说明；冲突：显示最新内容重新编辑 |
| 小程序拉取 | 启动用缓存 → `GET /api/v1/app/content?content_version=` → 304 或新内容；`GET /api/v1/app/config?config_version=` 同理 | 拉不到：用缓存或安装包默认值，不提示错误 |
| 开发改隐藏文案 | 改 `seed/texts.toml` → 代码评审 → 发布时 `python -m app_settings sync` | 种子不合法：同步命令逐条列出并以 1 退出 |

## 6 接口

| 方法 | 路径 | 鉴权 | 幂等 | 权限码 | agent | 说明 |
|---|---|---|---|---|---|---|
| GET | `/internal/config` | 内网（含本机回环）+ 运维令牌 | — | — | no | 版本、加载时间、来源提交、待重启的键、最近一次失败；不含值。外网 404，内网缺令牌 403 |
| POST | `/internal/config/reload` | 同上 | 天然幂等 | — | no | 返回 loaded / unchanged；rejected 时 422（4220，reason `config_invalid`）并列出每条错误 |
| GET | `/api/v1/app/config` | 无 | — | — | read | `platform`（不带时只给各端通用的键）、`client_version`、`config_version`、`store_id`；返回 `config_version`、`values`（`分组.键` → 值）；ETag，未变化 304 |
| GET | `/api/v1/app/content` | 无 | — | — | read | `platform`、`client_version`、`content_version`、`store_id`；返回 `content_version`、`texts`（含隐藏键）、`display`（公开键，门店覆盖全局）；ETag，未变化 304 |
| GET | `/api/v1/admin/content/texts` | 后台 | — | `settings:read` | no | 只列 admin_visible；筛选 `page_name`（小程序页面）、`keyword`；分页 20 |
| PUT | `/api/v1/admin/content/texts/{key}` | 后台（全门店员工） | 必填 | `settings:write` | no | `text`、`version`；错误 `content_text_too_long`、`content_variable_unknown`、`content_unsafe`、`content_key_hidden`、`content_conflict` |
| POST | `/api/v1/admin/content/texts/{key}/reset` | 后台（全门店员工） | 必填 | `settings:write` | no | 恢复默认文案 |
| GET | `/api/v1/admin/content/display` | 后台 | — | `settings:read` | no | `store_id` 可选；只列 admin_visible 的键：默认值、全局值、门店值、生效值与来源、这一级的版本 |
| PUT | `/api/v1/admin/content/display/{key}` | 后台（门店范围） | 必填 | `settings:write` | no | `store_id`、`value`、`version`；错误 `validation_failed`、`store_override_not_allowed`、`content_key_hidden`、`content_conflict` |
| POST | `/api/v1/admin/content/display/{key}/reset` | 后台（门店范围） | 必填 | `settings:write` | no | 全局：恢复默认；门店：取消门店覆盖 |
| GET | `/api/v1/admin/content/revisions` | 后台 | — | `settings:read` | no | 只含后台可见的键；筛选 `key`、`target_type`、`store_id`、`source`、`created_at`；门店员工只看全局与本门店 |
| POST | `/api/v1/admin/content/revisions/{id}/restore` | 后台（门店范围） | 必填 | `settings:write` | no | `version`（目标当前版本）；撤销那次修改，生成新版本 |

| GET | `/api/v1/admin/settings` | 后台 | — | `settings:read` | no | `store_id` 可选；只列后台可见的经营设置，按模块分组：说明、单位、范围或选项、默认值、全局值、门店值、生效值与来源、这一级的版本 |
| PUT | `/api/v1/admin/settings/{key}` | 后台（门店范围；高风险键要求重新验证） | 必填 | `settings:write` | no | `store_id`、`value`、`version`、`reason`；错误 `validation_failed`、`store_override_not_allowed`、`business_key_hidden`、`setting_conflict`、`reauth_required` |
| POST | `/api/v1/admin/settings/{key}/reset` | 同上 | 必填 | `settings:write` | no | 全局：恢复登记的默认值；门店：取消门店覆盖 |

## 7 事件

不发、不收发件箱事件。日志事件：`config.loaded`、`config.reloaded`、`config.unchanged`、`config.reload_rejected`、`config.restart_required`、`config.load_record_failed`、`content.text_updated`、`content.display_updated`、`content.seed_synced`、`security.authz_fail`（内部接口被拒）。

## 8 并发与一致性

- 运行配置：加载串行加锁，快照整份替换引用；读取无锁；8 个线程同时重载只有一次 loaded（测试）。
- 文案与展示：条件 UPDATE + rowcount；12 个线程以同一版本改同一文案只有 1 个成功（文件 SQLite 测试）；首次写展示设置的并发由部分唯一索引与保存点兜底。
- 内容版本先取后读：拉取时先取版本再读内容，最坏情况是客户端下次多拉一次，不会把新内容配上旧版本号。
- 加载记录在数据库就绪后由进程入口 `install(store)` 登记并补写启动那次；记录失败只写日志，不影响加载。

## 9 配置

| 项 | 默认 | 说明 |
|---|---|---|
| `LUZ_CONFIG_DIR` | `backend/config/runtime` | 运行配置目录（容器内 `/srv/backend/config/runtime`，compose 整目录只读挂载） |
| `LUZ_CONFIG_WATCH` | `auto` | `auto`（watchfiles，失败改轮询）、`poll`（Docker Desktop 等不发文件通知的挂载）、`off` |
| `LUZ_OPS_TOKEN` | 空 | 内部接口的运维令牌（≥ 32 字符）；为空时内部接口一律拒绝 |
| `COMMIT`（配置目录里的文件） | 无 | 发布同步时写入配置所在的代码提交，进 `config_loads.source_commit` |
| 运行参数 | `config/runtime/base.toml` | 键、单位与范围见文件注释与 `runtime_config.py` |

## 10 错误码

| 场景 | code | reason |
|---|---|---|
| 内部接口：外网 | 4040 | — |
| 内部接口：缺令牌或令牌不对 | 4030 | `ops_token_required` |
| 重载被拒 | 4220 | `config_invalid` |
| 文案超长 / 未知变量 | 4001 | `content_text_too_long` / `content_variable_unknown` |
| 内容不安全 | 4220 | `content_unsafe` |
| 检测服务不可用 | 5020 | — |
| 隐藏键 | 4030 | `content_key_hidden` |
| 版本冲突 | 4090 | `content_conflict`（附 `current`） |
| 展示值不合法 | 4001 | `validation_failed` |
| 键不能按门店覆盖 | 4001 | `store_override_not_allowed` |
| 种子记录不能恢复 / 旧值已不合法 | 4090 | `revision_not_restorable` |
| 门店越界 | 4031 | — |

## 11 测试清单

`code/tests/test_app_settings_runtime.py`（7）：内部接口的内网与令牌判断（外网、经代理的外网、缺令牌、错令牌、未配置令牌）；重载成功、未变化、被拒且旧版本继续生效；`config_loads` 启动补写与每次加载；客户端下发只含可见键、按端与版本过滤、门店与旧版本覆盖、不带端时只给通用键；ETag 与 304；`client_since` 过滤；维护模式只挡顾客端写接口；加载记录失败不影响重载。

`code/tests/test_app_settings_content.py`（15）：仓库种子合法且有隐藏键；坏种子逐条列错；同步幂等、默认文案变化与删键写记录；小程序下发含隐藏键、304；门店值覆盖全局与取消覆盖；后台列表不含隐藏键、按页面与关键词筛选；修改的超长、未知变量、生效、审计与修改记录；隐藏键不能改；展示值校验；版本冲突附最新内容；12 线程并发只有一个成功；恢复默认、恢复历史、种子记录不能恢复；展示恢复删除门店覆盖；内容安全钩子与检测服务不可用；权限与门店范围。

框架的运行配置测试见 `00-framework/tests/test_runtime_config.py`，CI 用它把三个环境文件各加载一遍。

## 12 前端对接

| 页面 | 接口 | 行为 |
|---|---|---|
| 小程序启动与回到前台 | `GET /api/v1/app/config?platform=miniprogram&client_version=&config_version=` | 先用缓存，异步拉；304 不变；低于 `client.min_version_miniprogram` 提示重启更新；`config_refresh_s` 内不重复拉 |
| 小程序每个页面 onShow | `GET /api/v1/app/content?content_version=&store_id=` | 有新版本就替换缓存并刷新本页文字；变量在端上替换；缺键、拉取失败用安装包默认值，不显示空白或键名 |
| 上传文档 | `values["upload.doc_max_mb"]` | 选择文件时预检查并提示上限，服务端按同一个值拒收 |
| 后台“文案与展示” | `/api/v1/admin/content/texts`、`/display`、`/revisions` | 按页面分组、行内编辑；保存后显示“已生效”；冲突时显示最新内容；字数与变量提示在保存按钮旁 |

## 13 定制点与不包含

| 定制点 | 做法 |
|---|---|
| 新增运行参数 | 在 `runtime_config.py` 对应分组用 `setting(...)` 登记，`base.toml` 加键与注释，调用处读 `current_config()` / `config_for()`；测试会检查两边一致 |
| 新增文案键 | 写进 `seed/texts.toml` 并同步安装包默认值；发布时 `python -m app_settings sync` |
| 新增展示键 | 模块导入时 `register_display_key(DisplayKey(...))` |
| 内容安全 | `set_text_checker(fn)` |
| 加载记录 | `install(store)`（进程入口已调用） |

不包含：运行参数的后台页面或配置中心（用户 2026-10-29 决定）；按用户分桶的客户端下发（`/api/v1/app/config` 没有登录用户，百分比覆盖只对服务端 `config_for(user_ref=)` 生效）；内容的定时生效与多语言；门店经营设置。
