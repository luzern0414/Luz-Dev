---
id: "EXP-0004"
title: "Luz Design 技术方案壳层布局修复"
status: candidate
created: "2026-10-26T13:46:43+08:00"
updated: "2026-10-26T13:46:43+08:00"
task_mode: "implementation"
plan: "D:\\Codes\\skill输出\\_归档\\Luz-Dev\\04-闭环结果页规范页-Luz-Design-视觉重构\\04-闭环结果页规范页-Luz-Design-视觉重构.html"
tags: ["admin-v2", "luz-design", "responsive-layout", "visual-regression"]
evidence: ["test: tests.test_function_catalog 9/9 通过，新增 Admin shell 与重复侧栏偏移回归断言。", "test: tests.test_skill_automation 10/10 通过，新增技术方案 Admin v2 布局契约断言。", "test: tests.test_validate_handoff 8/8 通过。", "build: function-catalog.html 成功重建为 110660 字节，canonical JSON 集成测试通过。", "static: 03、04、技术方案母版和功能目录无重复 ID、无外部资源；内联 JavaScript 均通过 node --check。", "validate: validate_skill_package.py PASS，解析到 D:/Codes/Luz-Design。"]
reusable_assets: ["assets/tech-plan/template.html", "assets/reference/function-catalog.template.html"]
supersedes: ""
superseded_by: ""
---

# Luz Design 技术方案壳层布局修复

## 摘要

修复技术方案页和功能目录模板中侧栏宽度重复计算、标题区被挤压后中文逐字竖排、字号断点偏离 Luz Design Admin v2 的问题。

## 决策与做法

- 把侧栏宽度作为唯一 grid 列计算，移除 workspace 上重复的 margin。
- 标题和内容列显式设置 min-width:0；仅表格容器横向滚动，标题中文保持自然换行。
- 按 Admin v2 使用 240/72 侧栏、64 顶栏、24 内容区、20/16/14/13/12 字号与 8/12 圆角。
- 1440 以下收起侧栏，700 以下切换横向导航；当前技术方案和未来技术方案母版共用该壳层。

## 失败尝试与教训

- 初版只套用了 ai-agent 配色，未核对 Admin v2 布局尺寸，并让 .app 固定侧栏与 .workspace margin-left 同时生效，导致内容被二次挤窄。
- 标题区 flex 子项保持 min-width:auto，右侧统计占宽后中文标题按单字换行；用户截图直接暴露了该问题。
- 调用内置浏览器检查本地 file:// 页面时被安全策略拒绝；没有绕过策略，改为静态布局契约和脚本回归验证。

## 验证证据

<!-- EVIDENCE_START -->
- test: tests.test_function_catalog 9/9 通过，新增 Admin shell 与重复侧栏偏移回归断言。
- test: tests.test_skill_automation 10/10 通过，新增技术方案 Admin v2 布局契约断言。
- test: tests.test_validate_handoff 8/8 通过。
- build: function-catalog.html 成功重建为 110660 字节，canonical JSON 集成测试通过。
- static: 03、04、技术方案母版和功能目录无重复 ID、无外部资源；内联 JavaScript 均通过 node --check。
- validate: validate_skill_package.py PASS，解析到 D:/Codes/Luz-Design。
<!-- EVIDENCE_END -->

## 可复用候选

- 技术方案 HTML 必须经过布局壳层测试；仅套用颜色令牌不能视为遵循 Luz Design。
- 视觉验收被 file:// 安全策略阻断时，保留人工截图复核项，不把静态检查表述为像素级视觉通过。

## 废弃与替代

<!-- DEPRECATION_START -->
_未废弃。_
<!-- DEPRECATION_END -->
