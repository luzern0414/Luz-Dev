# 调研知识库

每次 Luz-Crawl 调研的结果都沉淀在这里，新项目先复用，只对缺失或过期的部分重新调研。打开 [index.html](index.html) 按关键词和类型浏览。

| 文件 | 内容 | 写入方式 |
|---|---|---|
| `sources.json` | 读过的原文：URL、标题、级别、类型、模块、核对日期、Luz-Crawl 证据目录、摘要 | 调研导入后自动写入（`tech_plan.py research`）或 `knowledge.py harvest` |
| `facts.json` | 从原文提炼的可复用结论，必须引用来源 | `knowledge.py fact` |
| `queries.json` | 已执行的检索与结果 | 调研导入后自动写入 |
| `index.html` | 浏览页 | `knowledge.py build`（写入后自动重建） |

| 类型 | 说明 | 复核周期 |
|---|---|---|
| 法律法规 `law` | 法律、部门规章、标准、合同示范文本 | 365 天 |
| 平台文档 `platform` | 微信支付、小程序、云服务等官方文档 | 90 天 |
| 开源实现 `opensource` | 同类系统的开源仓库 | 180 天 |
| 功能做法 `feature` | 业务功能的设计做法 | 365 天 |
| 工程实践 `practice` | 社区文章中的实践经验 | 365 天 |
| 踩坑记录 `pitfall` | 实际遇到的问题与原因 | 365 天 |

```powershell
python -X utf8 scripts/knowledge.py search 退款
python -X utf8 scripts/knowledge.py for-modules payment refund face
python -X utf8 scripts/knowledge.py stale
python -X utf8 scripts/knowledge.py fact --kind platform --topic "…" --statement "…" --source <URL 或 KS-编号> --module payment
python -X utf8 scripts/knowledge.py check
```

只收录公开信息：不收录商户数据、凭据、个人信息。与其他层的关系：事实是外部世界的结论；`references/standards.json` 是由事实提炼的规则；`references/patterns/` 是设计做法；`experience/` 是每次任务的经验与证据。
