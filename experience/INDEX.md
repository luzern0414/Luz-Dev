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
| 偏好 | base-then-custom-features | 方案页在结论之后先列这类系统的基础功能（逐项标注包含或不适用），再列本项目的定制功能，然后才是调研、设计和架构。 | 1 | 2026-10-28 |
| 偏好 | html-tab-icon-required | 每个生成的 HTML 页面都必须有贴合主题的标签页图标，不能空白：模板放 {{FAVICON}}，生成器调用 luz_shell.favicon_for_tokens(kind, tokens) 输出内联 SVG（主题主色线稿 + 浅色圆角底；技术方案=文档、规范=盾牌、知识库=书本、功能目录=宫格），新增页面类型时同步新增图标。 | 1 | 2026-10-28 |
| 偏好 | one-task-one-folder | 一个需求只建一个项目文件夹（skill输出/Luz-Dev/序号-主题/，成品 HTML 加 raw/）；后续修改用 revise 修订同一方案，只告诉用户一个要打开的 HTML。 | 1 | 2026-10-28 |
| 偏好 | plan-approved-before-build | 任何代码、数据、配置或部署改动之前，技术方案必须得到用户明确批准；批准绑定方案内容哈希，内容变化后要重新提交、重新批准。 | 1 | 2026-10-28 |
| 偏好 | process-info-off-page | 页面只放内容；来源与调研统计、审批状态与哈希、修订记录、时间戳、文件路径留在 raw/plan.json 和对话里，待决问题在对话里直接问用户。 | 1 | 2026-10-28 |
| 偏好 | reading-page-check-document | 方案页、规范页和知识库页按 Luz Design 阅读页规则生成，标题在首行、无应用外壳、字号只用 14/16/20/24/32，并通过 Luz Design 的 check_document.py，有 ERROR 不能提交。 | 1 | 2026-10-28 |
| 偏好 | real-backend-data-only | 生产页面只显示真实后端数据；原型里的模拟数据、DEMO 数据和 mock 引用必须全部换成接口数据。 | 1 | 2026-10-28 |
| 教训 | delivery-mark-only-evidenced | 核对表只把有测试或实测证据的条目标为完成，运维类（恢复演练、HTTPS、回滚、日志、限流）未做就保持计划中并在交付时列为缺口 | 1 | 2026-10-28 |
| 教训 | plan-spec-sync-readd | spec --file 会按方案模块重新同步库内详设，替换自定义详设后要再逐个 spec --remove 不适用的库内详设并确认 moduleSpecs；方案页面避免 哈希、sha256、数据来源、输出目录 等被 check_document 拦截的词 | 1 | 2026-10-28 |
| 教训 | research-source-recheck-before-publish | 平台驱动 Luz-System 会话时，调研来源可能在调研后被站点临时封锁（如 HTTP 423），发布前要复核来源、失败重试一次、仍不可达则从经验库替换同类来源再跑 strict 校验 | 1 | 2026-10-28 |
<!-- NOTES_END -->
