# 智能体（agent）

Luz Dev 智能体服务的参考实现与设计说明：为小程序、App 与管理后台实现业务助手（LangGraph + Pydantic）；对话界面由 Luz System 定义。

**唯一规范是 `../references/agent-foundation.md`（智能体底座）。** 本目录的文档与代码按它对齐；代码与底座的差距列在本文“差距待办”，按优先级补齐。

```
agent/
  README.md                 本文件：目录、现状、差距待办
  00-core/                  契约（Pydantic 模型）、配置、错误码
  03-intent/                意图表、规则预筛、模型分类、阈值、追问
  04-memory/                裁剪、运行摘要、长期记忆、查看与删除
  05-knowledge/             分块、检索、RRF、阈值、引用、缓存
  08-resilience/            超时、重试、熔断、回退链、预算、降级梯度
  09-verify/                结果核验与模板回答
  10-llm/                   模型适配（OpenAI 兼容）、结构化输出
  11-observability/         trace_id、指标、脱敏事件日志
  backend/SPEC.md           门店业务助手应用规范 v2（意图表、工具清单、文案、核验细则；文首列出被底座取代的条目）
  backend/code/             v1 参考图（plan / run_tool / confirm / unknown_tool 四节点，10 项测试）
  shared/protocol.md        前后端协议 v3（请求、SSE 事件、卡片、审批、错误）
  requirements/_template.md 项目定制清单模板（填好即项目的 AGENT.md）
```

底座 §15 规定的完整目录还包括 `01-app`（接入与 SSE）、`02-guardrails`、`06-tools`、`07-graph`、`12-evals`，以及项目内生成的 `config/` 与 `contracts/`；它们尚未建立，见差距待办。

## 阅读顺序

1. `../references/agent-foundation.md`：§1 流水线 → 按环节读 §2–§14 → §16 上限与超时 → §17 检查清单
2. `backend/SPEC.md`：门店业务助手的意图、工具、审批与降级文案、核验细则（与底座冲突处按底座）
3. `shared/protocol.md` v3
4. Luz System `references/agent-design.html`（界面）
5. 新项目：复制 `requirements/_template.md` 填写 → 生成项目的 `config/intents.yaml`、`config/tools.yaml` → 按下面差距待办的顺序实现

## 职责边界

| 智能体做 | 智能体不做 |
|---|---|
| 理解意图、追问缺失信息、按意图选工具 | 直接发起支付、退款、改价、改库存、改权限 |
| 只读查询直接执行；低风险写执行后回显并可撤销 | 绕过审批执行高风险操作 |
| 高风险写：服务端预览 → 用户审批 → 带幂等键执行 | 自行决定身份（`user_id` 只来自令牌） |
| 用已审核的知识库回答规则类问题并给引用，找不到明确说 | 编造订单号、金额、时间（核验拦截） |
| 解释失败、给页面入口；必要时转人工 | 替代页面：每个回答都带“去页面”入口 |

## 与后端的关系

- 工具只调用 `backend/API.md` 标注 `agent: yes` 且已实现的接口；“设计 / 待建”接口对应的工具注册为 `disabled`。
- 高风险操作由智能体服务在审批后执行；幂等键 UUID v5，满足后端 36 位要求。
- 后端需提供下单报价等只读预览接口与服务间签名校验。

## 差距待办（代码 vs 底座）

P0 = 任何智能体项目上线前必须完成；P1 = 第一个项目交付前完成；P2 = 按需。建议顺序：G04 → G06 → G05 → G01–G03 → G07 → G09 → G08，再做 P1。

| # | 底座要求 | 现状 | 待办 | 位置 | 优先级 |
|---|---|---|---|---|---|
| G01 | §2 运行接口、SSE 事件日志、`Last-Event-ID` 续传、终止后 204、心跳、取消、从失败处续跑 | 无（只有 `sse_ping_interval_s` 配置项） | 新建接入层 | `01-app` | P0 |
| G02 | §2 三级并发上限、`run.queued` 位置与预计时间、队列满 429 + `Retry-After`、会话内待发送 | 无；v2 用 `409 AGENT_BUSY`（`00-core/errors.py`） | 队列与调度；去掉 `AGENT_BUSY` | `01-app`、`00-core` | P0 |
| G03 | §2 `client_message_id` 去重返回同一 `run_id`，内容不同 409 | 只校验 UUID 格式，无去重存储 | 幂等登记（24 小时） | `01-app` | P0 |
| G04 | §2–§10 v3 契约：`RunEvent` 及事件数据、v3 错误码、`ToolSpec`、`IntentSpec`、`ApprovalRequest`；契约基类 `extra="forbid"`；导出 JSON Schema | `AgentEvent(seq, turn_id)`、v2 错误码、卡片类型 `confirm`；`StrictModel` 为 `extra="ignore"` | 改模型、错误码与卡片类型（`confirm` → `approval`），加导出脚本 | `00-core`、`contracts/` | P0 |
| G05 | §6 工具契约与注册校验、每工具一模块、参数与返回模型、副作用三级、幂等键、错误三分类、参数纠正上限、按意图暴露 | v1 注册表用手写 JSON Schema，风险只有 read / high，只过滤未声明参数 | 新建工具层，迁移 v1 工具 | `06-tools` | P0 |
| G06 | §5 编排图接入各模块；轮次级重置；`add_messages`；Postgres 检查点；`recursion_limit`；审批前预览与过期检查 | v1 四节点图；`messages` 整表替换；无轮次重置；只有内存检查点 | 新建编排图，迁移 v1 | `07-graph` | P0 |
| G07 | §3 输入护栏（阻塞、遮蔽、注入检测、只读模式、越权意图）；§10 输出遮蔽与转义 | 无独立模块；遮蔽规则散在 `04-memory`、`11-observability` | 新建护栏模块，收拢规则 | `02-guardrails` | P0 |
| G08 | §11 五类评测集、回归脚本、CI 门槛 | 无 | 新建评测模块 | `12-evals` | P0 |
| G09 | 各模块单元测试 | `00`–`11` 没有测试；`pytest -q agent` 只跑 v1 的 10 项 | 每个模块补测试（含失败路径） | 各模块 `tests/` | P0 |
| G10 | §4 意图表配置化、语义路由层、`IntentResult` 增加 `router / needs_clarification / candidates`、`< 0.5` 不猜 | 意图表写在 `taxonomy.py`；`< 0.5` 走 `general` 交给模型自由回答 | 配置 + 校验 + 路由层 | `03-intent` | P1 |
| G11 | §6–§7 模型适配：处理全部并行工具调用、参数解析失败回给模型、流式参数拼接、Claude 格式、缓存断点、529 与花费上限 429 映射、工具输出方式的结构化输出、结构化重试 1 次 | 只取第一个工具调用；参数 JSON 解析失败静默变成 `{}`；只有非流式 OpenAI 兼容；结构化输出用提示词 JSON，重试 2 次 | 扩展适配层 | `10-llm` | P1 |
| G12 | §8 记忆保留上一版、记录来源（会话、运行）、全部删除、删除同步向量索引、接口接线 | 合并时直接覆盖；来源只有 `source_turn` | 补字段与接口 | `04-memory`、`01-app` | P1 |
| G13 | §9 上下文前缀、重排、按门店与角色预过滤、两路并行、pgvector 实现、入库审核与旧版本保留 | 有标题前缀分块、内存 BM25、RRF（k=60）、阈值、引用、缓存；只按租户与有效期过滤；两路串行 | 补入库与检索能力 | `05-knowledge` | P1 |
| G14 | §14 遵守 `Retry-After`、全抖动、过载识别、熔断打开时长递增与手动覆盖、整轮时长、图步数、同名同参重复检测 | 有超时、有界退避（固定幅度抖动）、熔断（固定 30 秒）、回退链、token 与日轮次预算、降级梯度 | 补齐策略与上限 | `08-resilience`、`07-graph` | P1 |
| G15 | §11 OpenTelemetry 导出、span 类型齐全、首事件与首 token 时延、队列与费用指标、告警 | 进程内计数器与直方图、节点计时、脱敏事件日志 | 导出与指标 | `11-observability` | P1 |
| G16 | §16 默认值落进配置：`AGENT_MAX_RUNNING`、`USER_MAX_RUNNING`、`QUEUE_*`、`RUN_TIMEOUT_S`、`MAX_GRAPH_STEPS`、`MAX_SAME_CALL`、`SMALL_LLM_TIMEOUT_S`、`STRUCTURED_RETRIES`、`BREAKER_OPEN_MAX_S` 等 | `config.py` 缺这些键 | 补运行配置 `[agent]` 表的键与字段说明（小写蛇形，单位写进键名） | `00-core` | P1 |
| G17 | §6 MCP 客户端接入（白名单、契约包装、熔断分组） | 无 | 新建 | `06-tools` | P2 |
| G18 | §9 引用带文档版本，核验对照 v3 `Citation` | `Citation` 没有 `version` | 补字段 | `00-core`、`09-verify` | P2 |
| G19 | 底座 §18 列出的补采：DeepSeek 工具调用（官网需人工验证，暂按 OpenAI 兼容格式）、OpenTelemetry 生成式 AI 属性名（官网为迁移页） | 未采 | 用 Luz-Crawl 补采后更新底座 | `references/agent-foundation.md` | P2 |

文档侧已对齐：底座文档（§0 含原 `references/agent.md` 的职责边界）、本文件、`shared/protocol.md` v3、`backend/SPEC.md` 文首的取代说明、`references/standards.json` 的 `AGENT-002`–`AGENT-020`。

## 运行测试

```powershell
# 从 Luz-Dev 根目录运行
python -m pip install langgraph langchain-core pydantic httpx
python -m pytest -q agent
```
