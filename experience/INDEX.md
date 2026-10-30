# Luz Dev Experience Index

按主题、标签和状态检索经验。候选经验不能作为默认工程规则。

## Verified Playbooks

| ID | Playbook | 来源经验 | 状态 |
|---|---|---|---|
| PB-0001 | [Plan-first Luz task delivery](playbooks/PB-0001-plan-first-delivery.md) | EXP-0001 | verified |
| PB-0002 | [Maintain editable function catalog](playbooks/PB-0002-function-catalog-maintenance.md) | EXP-0002 | verified |
| PB-0003 | [业务闭环结果页实施](playbooks/PB-0003-outcome-result-pages.md) | EXP-0003 | verified |


<!-- EXPERIENCE_ROWS_START -->
| ID | 主题 | 任务类型 | 状态 | 标签 | 证据数 | 更新时间 |
|---|---|---|---|---|---|---|
| EXP-0001 | [Luz Dev 技术方案门禁与经验闭环](records/EXP-0001-Luz-Dev-技术方案门禁与经验闭环.md) | implementation | verified | experience, luz-dev, technical-plan | 5 | 2026-10-26T11:49:31+08:00 |
| EXP-0002 | [功能逻辑目录与可视化编辑模板](records/EXP-0002-功能逻辑目录与可视化编辑模板.md) | implementation | verified | function-catalog, luz-design, offline-html, reusable-logic | 5 | 2026-10-26T12:10:56+08:00 |
| EXP-0003 | [业务闭环结果页与恢复路径](records/EXP-0003-业务闭环结果页与恢复路径.md) | implementation | verified | async-job, form, luz-design, payment, recovery, result-flow | 7 | 2026-10-26T13:06:19+08:00 |
| EXP-0004 | [Luz Design 技术方案壳层布局修复](records/EXP-0004-Luz-Design-技术方案壳层布局修复.md) | implementation | deprecated | admin-v2, luz-design, responsive-layout, visual-regression | 6 | 2026-10-28T14:31:38+08:00 |
| EXP-0005 | [方案数据化与审批哈希门禁](records/EXP-0005-方案数据化与审批哈希门禁.md) | implementation | candidate | approval-gate, luz-design, technical-plan | 2 | 2026-10-26T15:58:55+08:00 |
| EXP-0006 | [Luz-Crawl 调研节流与官方原文直读](records/EXP-0006-Luz-Crawl-调研节流与官方原文直读.md) | implementation | candidate | luz-crawl, research, wechat-pay | 2 | 2026-10-26T15:58:55+08:00 |
| EXP-0007 | [微信支付 APIv3 回调验签、签名探测与解密](records/EXP-0007-微信支付-APIv3-回调验签、签名探测与解密.md) | implementation | candidate | callback, payment, security, wechat-pay | 2 | 2026-10-26T15:58:56+08:00 |
| EXP-0008 | [业务逻辑模式库与可复用参考实现](records/EXP-0008-业务逻辑模式库与可复用参考实现.md) | implementation | candidate | after-sales, concurrency, inventory, order, patterns | 2 | 2026-10-26T16:28:07+08:00 |
| EXP-0009 | [全系统调研：系统原型、分批轮转与冷却重试](records/EXP-0009-全系统调研：系统原型、分批轮转与冷却重试.md) | implementation | candidate | archetype, luz-crawl, research | 1 | 2026-10-26T16:28:07+08:00 |
| EXP-0010 | [调研知识库：来源、事实、检索记录与复核周期](records/EXP-0010-调研知识库：来源、事实、检索记录与复核周期.md) | implementation | candidate | knowledge, luz-crawl, research | 2 | 2026-10-26T16:52:19+08:00 |
| EXP-0011 | [接入 Luz Design：按哈希核验批准并检查前端覆盖](records/EXP-0011-接入-Luz-Design：按哈希核验批准并检查前端覆盖.md) | implementation | candidate | frontend, integration, luz-design | 1 | 2026-10-26T16:52:19+08:00 |
| EXP-0012 | [项目文件夹输出与基础 + 定制功能清单](records/EXP-0012-项目文件夹输出与基础-+-定制功能清单.md) | implementation | candidate | features, output-layout, technical-plan | 1 | 2026-10-26T16:52:19+08:00 |
| EXP-0013 | [按 Luz Design 输出规范生成阅读页，并由其检查脚本把关](records/EXP-0013-按-Luz-Design-输出规范生成阅读页，并由其检查脚本把关.md) | implementation | candidate | luz-design, output-rules, technical-plan | 2 | 2026-10-26T17:09:53+08:00 |
<!-- EXPERIENCE_ROWS_END -->

## 偏好、教训与有效信息

由 `record_experience.py note` 按 key 写入：同一 key 再写会替换原条目，不追加。

<!-- NOTES_START -->
| 类型 | key | 内容 | 使用次数 | 最近确认 |
|---|---|---|---|---|
| 偏好 | admin-version-info | 后台显示版本信息：侧栏底部写前后端版本，点开看上线日期、运行环境与更新说明；测试环境顶栏加标识；发现新版本提示刷新但不丢草稿；版本元素类名带 version，检查器按产品内容处理 | 1 | 2026-10-29 |
| 偏好 | agent-model-decides-program-runs | 智能体开发：模型只做决策，程序控制运行；死循环最简单直接限制执行轮次，超过直接报错；工具分级：单一结果工具执行后不再传它的工具定义、权限禁止再调，多种结果工具写清边界；工具参数错误不只回错误信息，要回当前参数、错误原因和怎么改 | 1 | 2026-10-29 |
| 偏好 | agent-must-use-sse | 智能体必须用 SSE 推送回复、阶段与步骤进度、各段耗时、排队位置、审批请求和错误，不轮询、不整段返回；必须有排队等待（并发上限、排队位置与预计时间、队列满 429 加 Retry-After）、防重复提交（client_message_id 幂等）和防失败（只对瞬时错误退避重试、熔断降级、断线续传、断点续跑、保留部分结果、可取消、运行上限），只认终止事件为结束 | 1 | 2026-10-29 |
| 偏好 | base-then-custom-features | 方案页在结论之后先列这类系统的基础功能（逐项标注包含或不适用），再列本项目的定制功能，然后才是调研、设计和架构。 | 1 | 2026-10-28 |
| 偏好 | business-settings-in-admin | 商户日常调整的经营规则（退款手续费、储值扣减顺序、退券策略、补卡费、起送价、配送费）放后台“经营设置”：业务模块用 luzback.business 登记类型、范围、默认、能否按门店、风险；值存 business_settings；高风险键改前重新验证并写原因与审计；业务读到的值写进单据。运行参数才放配置文件 | 1 | 2026-10-30 |
| 偏好 | frontend-copy-from-backend | 小程序与 App 的文字提示和展示配置尽量由后端下发：每处文字一个文案键，后端存当前文案，安装包只带默认兜底；后台“文案与展示”页按页面实时维护、保存即生效；有些项存后端但不在后台展示（admin_visible 为否），开发经种子维护；实现在 23-app-settings（content.json、MP-006、ADMIN-005） | 1 | 2026-10-29 |
| 偏好 | html-tab-icon-required | 每个生成的 HTML 页面都必须有贴合主题的标签页图标，不能空白：模板放 {{FAVICON}}，生成器调用 luz_shell.favicon_for_tokens(kind, tokens) 输出内联 SVG（主题主色线稿 + 浅色圆角底；技术方案=文档、规范=盾牌、知识库=书本、功能目录=宫格），新增页面类型时同步新增图标。 | 1 | 2026-10-28 |
| 偏好 | one-task-one-folder | 一个需求只建一个项目文件夹（skill输出/Luz-Dev/序号-主题/，成品 HTML 加 raw/）；后续修改用 revise 修订同一方案，只告诉用户一个要打开的 HTML。 | 1 | 2026-10-28 |
| 偏好 | photo-upload-no-size-limit | 照片与相册原图上传不限大小、不提示过大，前端不压缩：服务端处理成功后生成高质量原档与清晰度优先的副本（先缩尺寸、后降质量、去元数据），再删除原始上传；有对象存储时预签名直传、服务端异步处理，没有对象存储时上传入口单独放宽到防滥用上限；能换文件的合同与文档才限制大小并在选择前写明；上传参数由服务端下发 | 3 | 2026-10-29 |
| 偏好 | plan-approved-before-build | 任何代码、数据、配置或部署改动之前，技术方案必须得到用户明确批准；批准绑定方案内容哈希，内容变化后要重新提交、重新批准。 | 1 | 2026-10-28 |
| 偏好 | process-info-off-page | 页面只放内容；来源与调研统计、审批状态与哈希、修订记录、时间戳、文件路径留在 raw/plan.json 和对话里，待决问题在对话里直接问用户。 | 1 | 2026-10-28 |
| 偏好 | purchase-rules | 购买规则跟着商品或活动走、后台维护：起售、单笔上限、周期件数与次数限购、共享键、门店范围、手机号维度、新客专享、退款退回、超出部分按原价（over_limit=regular_price 拆成活动价行与原价行，原价行不计名额）；订单模块同事务条件自增计数（order_purchase_quotas）（patterns/purchase-rules.md，ORDER-011/012、MKT-006） | 2 | 2026-10-30 |
| 偏好 | reading-page-check-document | 方案页、规范页和知识库页按 Luz Design 阅读页规则生成，标题在首行、无应用外壳、字号只用 14/16/20/24/32，并通过 Luz Design 的 check_document.py，有 ERROR 不能提交。 | 1 | 2026-10-28 |
| 偏好 | real-backend-data-only | 生产页面只显示真实后端数据；原型里的模拟数据、DEMO 数据和 mock 引用必须全部换成接口数据。 | 1 | 2026-10-28 |
| 偏好 | remote-config-hot-update | 运行参数（上传上限、超时、轮询间隔、限流、功能开关、最低可用版本、智能体上限、日志级别）写在代码仓库的配置文件（config/runtime/*.toml）里由服务端热加载，不在后台展示、不做配置中心页面；改文件走评审，回滚是还原提交；经营内容（营业时间、起送价、公告）仍在后台 | 2 | 2026-10-29 |
| 教训 | audit-by-requirements-parallel | 按历次需求做全面审查时，先把用户原话整理成编号清单，再按模块分给只读审查代理逐条找证据；整改按文件归属分给不同代理并行，后端口径与测试由主会话统一，最后整体回归 | 1 | 2026-10-29 |
| 教训 | check-document-digest-words | 方案页、规范页、模块详设与知识库页会被 check_document 拦下“哈希”“SHA-256”“sha256”这类来源元数据词：页面文字写“内容摘要”，口令类写“口令摘要”或“密码比对”；只给代理读的 md 参考文档不经检查可保留 | 2 | 2026-10-29 |
| 教训 | delivery-mark-only-evidenced | 核对表只把有测试或实测证据的条目标为完成，运维类（恢复演练、HTTPS、回滚、日志、限流）未做就保持计划中并在交付时列为缺口 | 1 | 2026-10-28 |
| 教训 | design-revision-plan-revision | 已批准的设计会话改版（新增页面、动作或事件名）后，先对照绑定表与后端接口列出缺口，写成方案修订：新接口逐个对应动作编号、数据变更、前端改版做法与测试，再请用户批准；不要沿用上一版批准直接开发 | 1 | 2026-10-29 |
| 教训 | element-plus-popper-scoped-style | Element Plus 下拉、弹层内容挂在 body 下，Vue scoped 样式不生效：弹层内自定义样式放进非 scoped 样式块并加专属类名 | 1 | 2026-10-29 |
| 教训 | existing-system-auth-review | 给已有系统补账号体系时，先建 review 方案对照 AUTH/AUTHZ/ADMIN 规则：用户名不存在也做同耗时比对、注册按来源限流防刷额度、附件等上传物核对归属、锁定提示剩余时间 | 1 | 2026-10-28 |
| 教训 | hide-mechanism-from-members | 会员可见的通行记录与提示不要暴露门禁判定方式（鱼眼人数、单人、门斗、门锁反馈等）；已上线的旧客户端无法立刻更新时，在后端会员视图置空这些字段，同时保留客户端判断结果所需的字段，旧版随后端发布即生效 | 1 | 2026-10-29 |
| 教训 | in-process-concurrency-single-instance | 智能体平台的执行位、并发计数与熔断器状态放在进程内时只适用于单实例部署；多实例需要把这些状态移到数据库或缓存 | 1 | 2026-10-29 |
| 教训 | llm-batch-size-real-run | 把多次模型调用合成一批时，批大小要用真实链路定：结构化输出每条带多个字段时，一批 40 条会超出输出上限且单批耗时两分钟以上，20 条稳定；超长时把该批对半拆开重试，不丢数据 | 1 | 2026-10-29 |
| 教训 | more-sources-need-read-priority | 获客类系统扩充数据源后，结果数受阅读原文名额限制而非检索；同一公告应优先读官方平台原文，聚合站转载正文常删减、核验率低 | 1 | 2026-10-29 |
| 教训 | optimize-from-data-evidence | 给已有系统列优化方向前，先查数据库各字段的填充率、评分与状态分布、最近几次任务的分阶段调用次数与用时，再按证据排优先级写进方案输入，不凭印象列清单 | 1 | 2026-10-29 |
| 教训 | plan-spec-sync-readd | spec --file 会按方案模块重新同步库内详设，替换自定义详设后要再逐个 spec --remove 不适用的库内详设并确认 moduleSpecs | 2 | 2026-10-29 |
| 教训 | prod-data-cleanup-transaction | 删除生产数据：先可恢复备份，只读清点全部外键引用与流水和余额一致性，按明确ID清单写单事务脚本，每步ROW_COUNT与清点数比对、不一致时用必然报错的子查询让会话中断回滚，先以ROLLBACK演练再COMMIT，事后核对余额与流水一致、无悬空引用 | 1 | 2026-10-30 |
| 教训 | revise-before-fix-in-e2e | 真实链路验证中发现的缺陷也要先修订方案并经用户确认，再改代码和推送；不要先推送再补修订 | 1 | 2026-10-29 |
| 教训 | standards-rule-id-format | standards.json 的规则编号必须是 前缀-三位数字（如 AGENT-002），build_standards.py 用 ^[A-Z]+-\d{3}$ 校验；AGENT-SSE-001 这类分段编号会被拒，主题写进标题而不是编号 | 1 | 2026-10-29 |
| 教训 | store-close-all-entrances | 门店停业不能只拦新建订单：已建待付单的调起支付（外呼前后各复核一次营业状态）、已授权未通过的入场会话、抽奖都要拦；已调起并付款的回调照常成交，物理退出不拦 | 1 | 2026-10-29 |
| 有效信息 | agent-foundation-pipeline | 智能体服务按 references/agent-foundation.md 的固定流水线实现：接入（SSE、排队、幂等）→ 输入护栏（先于主模型）→ 意图路由（规则→语义路由→小模型，置信度 0.75/0.5，低于阈值追问不猜）→ LangGraph 工作流 → 工具契约（Pydantic 参数与返回、副作用级别、超时、幂等键、错误三分类）→ 结构化输出（校验失败重试 1 次后降级）→ 记忆（检查点 + Store，可看可删）→ 知识库（上下文前缀、向量+BM25+RRF、权限预过滤、引用）→ 输出护栏与审批 → 追踪与评测回归；超时与上限统一见底座 §16 | 1 | 2026-10-29 |
| 有效信息 | browser-in-docker-handover | 容器内需人工验证的浏览器：Xvfb + 有界面 Chromium + x11vnc + noVNC，经应用自身登录的同源 WebSocket 代理暴露，shm 至少 1–2GB，--init，非 root + seccomp；WebRTC（n.eko）延迟低但需 UDP | 1 | 2026-10-28 |
| 有效信息 | config-logging-standard | 配置文件与日志规范在 references/config-logging.md（卡 config-logging，规则 OPS-005、CONF-001~003、OBS-001~008）：网络与安全日志集中留存不少于 6 个月（网络安全法 2025 修正第二十三条）、个人信息处理记录 3 年、交易信息 3 年；来源见 Luz-Crawl 029 | 1 | 2026-10-29 |
| 有效信息 | reasoning-model-intent-max-tokens | 用推理型模型做意图判定时 max_tokens 要给隐藏推理留余量，300 会被截断导致输出不合格 | 1 | 2026-10-29 |
<!-- NOTES_END -->
