# 调研：用 Luz-Crawl 取得真实资料

技术方案里的外部事实（平台接口、参数限制、状态枚举、法规条款、费用、资质要求）必须来自本次实际打开的原文，不能凭记忆写。调研由 `scripts/tech_plan.py research` 驱动 Luz-Crawl 完成，结果写回方案的“调研结论与来源”章节。

调研覆盖整个系统，分三层：

| 层 | 内容 | 来源 |
|---|---|---|
| 同类系统 | 同类产品的开源实现（模块划分、数据模型、状态机）、整体架构文章 | `systemArchetypes`：mall、food、booking、membership-store、saas-admin 的检索与参考仓库 |
| 业务模块 | 每个模块的设计做法：订单状态机、防超卖、库存预占、优惠叠加、储值、预约名额、售后、定时任务、缓存、幂等与最终一致… | `modules.<id>.queries`，并先读 `references/patterns/` 中对应篇目 |
| 外部平台与法规 | 支付、登录、隐私、内容安全、人脸、预付卡、促销 | `modules.<id>.officialUrls`（直读原文） |
| 必查主题 | Web 与接口安全、后台登录与会话、HTTPS 与服务器加固、容器化部署与发布回滚、云服务器与网络、监控告警、备份恢复、密钥与依赖、数据保护与等级保护 | `baselineTracks`：按任务类型自动加入，与方案选了哪些模块无关 |

## 1. 什么时候必须调研

| 任务类型 | 要求 | 最低阅读量（`references/research-sources.json` → `thresholds`） |
|---|---|---|
| implementation | 必须调研；跳过需要用户明确同意 | 已阅读 6 个，其中官方或法规 2 个、同类系统开源实现 1 个；`officialRequired` 模块各至少 1 个官方来源；没有来源的模块给出警告 |
| operations | 必须调研，可说明原因跳过 | 已阅读 3 个，官方 1 个 |
| analysis | 必须调研，可说明原因跳过 | 已阅读 4 个，官方 1 个 |
| diagnosis | 涉及外部平台或依赖时调研 | 已阅读 2 个 |
| review | 按需；纯代码评审可不调研 | 无 |

`officialRequired` 的模块（登录、支付、退款、对账、人脸、上传与内容安全、订阅消息、隐私、储值、定位、部署、资质、协议，以 `research-sources.json` 为准）没有官方来源时方案不能提交审核。

### 必查主题（baselineTracks）

任何要上线的系统都绕不开安全与运维，所以这部分不靠模块选择触发，而是按任务类型固定加入：

| 主题 | id | 适用任务 | 必须有官方或标准来源 | 方案里要落成的内容 |
|---|---|---|---|---|
| Web 与接口安全 | `web-security` | implementation、operations、analysis、review | 是（OWASP Top 10、API Security Top 10、REST / CSRF / SSRF / 上传） | 每类风险的防护做法：参数校验、越权检查、防重放、限流、上传白名单 |
| 后台登录、会话与操作安全 | `admin-security` | 同上 | 是（OWASP Authentication / Session / Password / MFA） | 密码哈希算法与参数、登录失败锁定阈值、会话时长、二次验证、敏感操作审计 |
| HTTPS 与 Web 服务器加固 | `http-hardening` | implementation、operations、analysis | 是（Mozilla SSL 配置、OWASP HTTP Headers / TLS、nginx） | TLS 版本与套件、HSTS、CSP 等响应头、证书续期、nginx 限流 |
| 容器化部署、CI/CD 与发布回滚 | `container-deploy` | 同上 | 是（Docker 安全与构建、OWASP Docker / CI/CD） | 镜像构建、非 root 运行、流水线密钥、发布与回滚步骤、零停机迁移 |
| 云服务器与网络安全 | `host-security` | 同上 | 否 | SSH 与安全组、系统更新、WAF / DDoS、源站隐藏 |
| 监控、日志与告警 | `observability` | 同上 | 否 | 指标、告警规则与阈值、结构化日志与脱敏、链路追踪 |
| 备份、恢复与容灾 | `backup-dr` | 同上 | 是（PostgreSQL 归档与 PITR、Redis 持久化） | 备份频率、保留期、RPO / RTO、恢复演练 |
| 密钥管理与依赖安全 | `secrets-supply-chain` | implementation、operations、analysis、review | 是（OWASP Secrets / Dependency） | 密钥存放与轮换、依赖漏洞扫描、提交前泄露检查 |
| 数据安全、个人信息保护与等级保护 | `data-protection` | implementation、operations、analysis | 否（法规条款仍以官方原文为准） | 敏感字段加密与脱敏、等保定级、个人信息安全措施 |

- 检索与直读链接由 `research --build / --run` 自动生成，来源打上主题 id，像模块一样计入覆盖。
- 每个适用主题至少一个已读来源，标“是”的主题至少一个官方或标准来源，否则 `submit` 拒绝。
- 结论写成具体配置值并标 `[S#]`，进入“安全与规范”“上线、运维与合规”两章；不要只写“做好安全”。
- 技能维护等不涉及上线系统的方案用 `research NN --no-tracks` 关闭。

## 2. 命令

```powershell
# 1) 创建方案时写明项目类型、系统原型与额外模块；调研路由、模式阅读清单和核对表都由它们决定
python -X utf8 scripts/tech_plan.py new "健身房下单小程序技术方案" --mode implementation `
  --project-type miniprogram,admin,backend --archetype booking --modules face

# 2) 补充本项目特有的官方页面或检索（可选）
python -X utf8 scripts/tech_plan.py research 06 --add-link "face|https://cloud.tencent.com/document/product/867" `
  --add-query "bing=face|健身房 人脸门禁 本地比对 方案"

# 3) 只生成 Luz-Crawl 输入文件，先检查
python -X utf8 scripts/tech_plan.py research 06 --build

# 4) 执行：先直读官方原文，再分批检索；每批完成即导入方案（耗时较长，放到后台运行）
python -X utf8 scripts/tech_plan.py research 06 --run --read 2 --batch 15 --pause 6

# 4b) 因站点冷却失败的检索会记录下来；稍后再次 --run 只补跑失败的部分
python -X utf8 scripts/tech_plan.py research 06 --run --only queries --batch 10 --pause 10

# 5) 导入已有合集、手动登记来源、补模块标签
python -X utf8 scripts/tech_plan.py research 06 --ingest "D:\Codes\skill输出\Luz-Crawl\012_xxx"
python -X utf8 scripts/tech_plan.py research 06 --tag S7=payment,refund
```

`--run` 会在 `raw/` 下写 `research-links.txt`、`research-plan.json` 与 `research-run.log`。Luz-Crawl 的调用顺序以它的 SKILL.md 调用协议为准：`doctor` → `begin "主题" --new`（得到 folder）→ `experience --query` → 每条命令带 `--out <folder>`；`research --run` 每个方案第一次运行时自动执行 `begin "Luz-Dev NN 主题" --new`，返回的目录记在 `plan.json` → `research.crawlFolder`，之后都带 `--out`。已阅读过的页面不会重复打开；知识库里未过期的官方原文直接计入列出它的模块与必查主题。

```powershell
# 只补某些模块或必查主题（tracks 表示全部必查主题）
python -X utf8 scripts/tech_plan.py research 06 --run --topics tracks --batch 12 --pause 5
python -X utf8 scripts/tech_plan.py research 06 --run --only queries --topics host-security,data-protection
```

需要登录或验证的站点按 Luz-Crawl 的读取顺序处理（先静默、再最小化的可见浏览器、最后才请用户登录，结果为 `awaiting_user` 时用 `continue` 续接）；用户不登录时这些检索记为未读，不影响其他站点。

## 3. 来源分级

| 级别 | 例子 | 用法 |
|---|---|---|
| 官方 `official` | pay.weixin.qq.com、developers.weixin.qq.com、云厂商文档、框架官方文档 | 接口、参数、状态、限制以它为准 |
| 法规 / 标准 `standard` | gov.cn、cac.gov.cn、npc.gov.cn、OWASP、RFC | 合规与安全要求以它为准，写明条款号 |
| 开源 `opensource` | GitHub、Gitee | 参考实现与常见坑；不能直接当生产代码 |
| 社区 `community` | CSDN、博客园、掘金、知乎 | 只作经验参考；与官方冲突时以官方为准 |

只有打开原文并确认正文的来源才计为“已阅读”；搜索结果列表、站点外壳、404、需要登录而未完成的页面只算候选。

## 4. 写进方案

- 调研章节先写结论，再列来源。每条结论用 `[S#]` 引用来源编号，渲染后可点击跳到来源表。
- 写明核对日期：外部平台规则会变，例如“2026-10-26 核对：prepay_id 有效期 2 小时 [S3]”。
- 官方与社区说法不一致时写出差异，采用官方说法。
- 查不到可靠依据的数字（价格、费率、阈值）不写，或标“待用户确认”放进待决问题。
- 调研中发现的新通用规则，先补到 `references/standards.json`（带来源与核对日期），再在方案核对表中引用。

## 5. 先查知识库

`knowledge/` 保存以往调研读到的来源、提炼的事实和已执行的检索（SKILL.md “Knowledge base”）。

| 时机 | 自动发生的事 |
|---|---|
| `tech_plan.py new` | 方案模块在复核周期内的知识库来源加入方案（标记“知识库”），相关事实列在调研章节 |
| `research --build / --run` | 已在知识库且未过期的官方原文不再打开；最近成功执行过的检索不再执行 |
| 每次导入合集后 | 新读到的来源与检索记录写回知识库，并重建 `knowledge/index.html` |
| 任务收尾 | 人工提炼事实：`knowledge.py fact …`（必须引用知识库中的来源） |

复核周期：法律法规 365 天、平台文档 90 天、开源实现 180 天、功能做法与工程实践 365 天。过期条目会被下一次调研重新读取并更新核对日期。

## 6. 用 Luz-Crawl 调研的做法

节流、冷却、登录与验证的处理以 Luz-Crawl 的 SKILL.md 为准（按站点自适应节流，限流即停，不换入口规避）。本技能这边的做法：

| 做法 | 原因 |
|---|---|
| 官方文档优先用 `officialUrls` 直读；微信支付文档有 `.md` 纯文本版本与 `llms.txt` 索引 | 请求更少、正文更完整，检索只用于发现清单里没有的页面 |
| 检索按站点轮转、分批并暂停（`--batch`、`--pause`）；被冷却的检索记入 `retryQueries`，下次 `--run` 只补跑这些；成功的记入 `doneQueries` 不再重复 | 同站与同引擎连续请求会触发冷却 |
| 微信开放文档部分旧地址已迁移：从官方页面的导航或索引重新找地址，更新 `officialUrls` | 旧地址显示 unavailable |
| 其他会话同时在用 Luz-Crawl 时，调研期间不并行跑多个大合集；失败的页面下一轮补读 | 节流预算按站点共享 |

调研不可用（扩展未连接、站点全部受限）时，停止并告诉用户具体原因；实施类方案只有在用户明确同意后才能 `--skip … --user-consent`。
