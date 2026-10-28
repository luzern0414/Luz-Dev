# Luz Dev

把已批准的产品设计做成**可上线、合规的生产系统**的技能（Agent Skill）：后端服务、接口、数据库、业务逻辑、与 Luz Design 前端的对接、部署运维、资质与协议，直到上线验收。

## 能做什么

- **先方案、后动手**：任何实现、排查、分析或运维任务都先写一份技术方案（调研依据、改动范围、风险、验收），你批准后才改代码、数据或线上环境。
- **常见业务逻辑**：订单与库存防超卖、价格与优惠券、储值与积分、预约、售后、支付与退款、定时任务、缓存、多门店隔离等，附带经过测试的参考实现。
- **前端对接**：读取 Luz Design 交付的系统设计与原型，接入小程序、管理后台或应用，保持页面、动作和状态与设计一致。
- **安全与规范基线**：接口与登录安全、HTTPS、容器部署、持续集成与回滚、监控告警、备份恢复、密钥与依赖安全、数据保护与等级保护，每项都要求有调研来源。
- **上线准备**：运维手册、经营资质与备案、软件许可、用户协议与隐私政策、上线验收清单。
- **知识与经验**：调研过的来源、事实和搜索记录存进知识库，项目经验沉淀下来供下次复用。

## 典型用法

把本目录放进 Agent 的技能目录后，直接提需求，例如：

- “按批准的设计，把健身房小程序和后台的后端做出来。”
- “给订单模块加上库存防超卖和超时关单。”
- “帮我检查一下这个系统上线前还缺哪些安全配置和资质。”
- “写一份部署到云服务器的方案，包含备份和回滚。”

## 工作方式

调研 → 技术方案 → 你批准 → 实现 → 与前端对接 → 上线 → 记录经验。方案批准之前不做任何代码、数据、配置或部署上的改动。

## 目录结构

```text
SKILL.md       技能入口：顺序、路由、工程规则
WORKFLOW.md    各阶段流程
backend/       业务模式的参考实现与测试
references/    业务模式、模块详细设计、规范基线、运维、合规与协议说明
contracts/     与 Luz Design 的交接契约（字段定义与校验）
knowledge/     调研知识库（来源、事实、搜索记录）
experience/    项目经验
assets/        部署模板与协议模板
scripts/       方案、调研、知识库、交接校验、技能注册等脚本
agent/         智能体运行时与协议
tests/         测试
```

## 依赖

- Python 3.11 及以上，依赖见 `requirements.txt`（FastAPI、SQLAlchemy、Pydantic、LangGraph 等）。
- 调研依赖 [Luz-Crawl](https://github.com/luzern0414/Luz-Crawl)；前端与所有 HTML 页面依赖 [Luz-Design](https://github.com/luzern0414/Luz-Design)；协议与上线文案依赖 [Luz-Writing](https://github.com/luzern0414/Luz-Writing)。

## 相关技能

| 技能 | 分工 |
|---|---|
| [Luz-Design](https://github.com/luzern0414/Luz-Design) | 用户看到的界面与原型，经批准后交给 Luz Dev |
| [Luz-Crawl](https://github.com/luzern0414/Luz-Crawl) | 真实浏览器调研 |
| [Luz-Writing](https://github.com/luzern0414/Luz-Writing) | 用户协议、隐私政策与上线文案 |
| [HardwareMatch](https://github.com/luzern0414/HardwareMatch) | 需要硬件时的选型与接口说明 |

## 许可

本仓库采用 [PolyForm Noncommercial 1.0.0](LICENSE)：个人学习、研究、教学、公益组织与政府机构等**非商业用途**可以自由使用、复制、修改和分发；**禁止任何商业用途**。如需商用，请先联系作者取得授权。分发时请保留 LICENSE 与 NOTICE 中以 “Required Notice:” 开头的版权声明。协议模板仅供参考，正式使用前请由法务审核。
