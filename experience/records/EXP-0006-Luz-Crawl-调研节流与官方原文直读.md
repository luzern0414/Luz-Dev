---
id: "EXP-0006"
title: "Luz-Crawl 调研节流与官方原文直读"
status: candidate
created: "2026-10-26T15:58:55+08:00"
updated: "2026-10-26T15:58:55+08:00"
task_mode: "implementation"
plan: "D:\\Codes\\skill输出\\_归档\\Luz-Dev\\05-Luz-Dev-调研审核门禁与安全规范体系\\05-Luz-Dev-调研审核门禁与安全规范体系.html"
tags: ["luz-crawl", "research", "wechat-pay"]
evidence: ["run: 合集 009 官方原文直读 17 页读取 14 页，自动导入方案 05（共 48 个已读来源，官方/法规 43 个）", "test: tests.test_tech_plan ResearchIngestTests 3/3 通过"]
reusable_assets: ["references/research.md", "references/research-sources.json"]
supersedes: ""
superseded_by: ""
---

# Luz-Crawl 调研节流与官方原文直读

## 摘要

Luz-Crawl 每站 10 分钟最多 40 次请求（图片计入），站内 site: 检索 4～8 次后冷却；改为先直读官方原文（微信支付 .md 纯文本与 llms.txt 索引），检索只作补充，已读页面不重复打开。

## 决策与做法

- research-sources.json 为模块登记 officialUrls（带标题）、检索模板与阅读门槛；tech_plan.py research --run 先 links 后 queries。

## 失败尝试与教训

- 第一轮 14 组 Bing 检索只有 4 组成功，其余因冷却未执行；部分微信开放文档 OpenApiDoc 旧地址显示 unavailable。
- 其他会话同时使用 Luz-Crawl 时共享站点预算，冷却更快。

## 验证证据

<!-- EVIDENCE_START -->
- run: 合集 009 官方原文直读 17 页读取 14 页，自动导入方案 05（共 48 个已读来源，官方/法规 43 个）
- test: tests.test_tech_plan ResearchIngestTests 3/3 通过
<!-- EVIDENCE_END -->

## 可复用候选

- 尚无复用候选。

## 废弃与替代

<!-- DEPRECATION_START -->
_未废弃。_
<!-- DEPRECATION_END -->
