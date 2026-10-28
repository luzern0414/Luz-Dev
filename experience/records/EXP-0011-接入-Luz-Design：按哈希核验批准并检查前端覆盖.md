---
id: "EXP-0011"
title: "接入 Luz Design：按哈希核验批准并检查前端覆盖"
status: candidate
created: "2026-10-26T16:52:19+08:00"
updated: "2026-10-26T16:52:19+08:00"
task_mode: "implementation"
plan: "D:\\Codes\\skill输出\\_归档\\Luz-Dev\\07-Luz-Dev-开发到上线全流程、前端接入与调研知识库\\07-Luz-Dev-开发到上线全流程、前端接入与调研知识库.html"
tags: ["frontend", "integration", "luz-design"]
evidence: ["test: tests.test_design_intake 5/5；真实健身房会话 inspect：21 页面、61 操作、10 实体，未批准被正确拒绝"]
reusable_assets: ["scripts/design_intake.py"]
supersedes: ""
superseded_by: ""
---

# 接入 Luz Design：按哈希核验批准并检查前端覆盖

## 摘要

Luz Design 会话以 APPROVALS.json 记录各阶段批准与 SHA-256；design_intake.py 核验后生成操作 / 实体 / 集成 / 页面绑定表，并检查前端覆盖、原型数据残留与未设计操作。

## 决策与做法

- 尚未补充；记录后按实际决策更新。

## 失败尝试与教训

- Luz Design 在同一时段被另一会话重构（目录迁移、交接 schema 变化），原先依赖其内部路径的校验全部失效；改为只依赖 SKILL.md、主题文件与会话产物。
- Luz Design 的 HANDOFF.md 写的是 PAGE-/ACT- 前缀，实际校验器与产物使用 P-/A-；以实际产物为准。

## 验证证据

<!-- EVIDENCE_START -->
- test: tests.test_design_intake 5/5；真实健身房会话 inspect：21 页面、61 操作、10 实体，未批准被正确拒绝
<!-- EVIDENCE_END -->

## 可复用候选

- 尚无复用候选。

## 废弃与替代

<!-- DEPRECATION_START -->
_未废弃。_
<!-- DEPRECATION_END -->
