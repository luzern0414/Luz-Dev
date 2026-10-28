---
id: "EXP-0013"
title: "按 Luz Design 输出规范生成阅读页，并由其检查脚本把关"
status: candidate
created: "2026-10-26T17:09:53+08:00"
updated: "2026-10-26T17:09:53+08:00"
task_mode: "implementation"
plan: "D:\\Codes\\skill输出\\Luz-Dev\\01-无人健身房系统后端技术方案\\01-无人健身房系统后端技术方案.html"
tags: ["luz-design", "output-rules", "technical-plan"]
evidence: ["check: 方案 08、standards.html、knowledge/index.html 均通过 check_document.py（0 错误 0 警告）", "test: 62 项 skill 测试与 149 项后端测试通过"]
reusable_assets: []
supersedes: ""
superseded_by: ""
---

# 按 Luz Design 输出规范生成阅读页，并由其检查脚本把关

## 摘要

方案页、规范页、知识库页改为阅读型页面：标题在首行、无应用外壳、字号 14/16/20/24/32、只放内容；来源、审批、修订、待决问题留在 raw 与对话中。submit 调用 Luz Design 的 check_document.py，有 ERROR 不能提交。

## 决策与做法

- 尚未补充；记录后按实际决策更新。

## 失败尝试与教训

- 规则正文中的“哈希”“版本号”“由服务端生成”“Admin SPEC”等措辞会被检查器判为过程或设计声明，需改写为同义内容。
- 同一需求生成了多个方案文件夹，用户不知道该看哪个；改为一个任务一个文件夹，后续修改只修订。

## 验证证据

<!-- EVIDENCE_START -->
- check: 方案 08、standards.html、knowledge/index.html 均通过 check_document.py（0 错误 0 警告）
- test: 62 项 skill 测试与 149 项后端测试通过
<!-- EVIDENCE_END -->

## 可复用候选

- 尚无复用候选。

## 废弃与替代

<!-- DEPRECATION_START -->
_未废弃。_
<!-- DEPRECATION_END -->
