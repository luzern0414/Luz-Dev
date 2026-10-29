# 智能体底座（Luz 智能体服务的唯一规范）

底座 v1 · 2026-10-29。本文规定一个 Luz 智能体服务从接入到回答的固定流水线：每个环节的规则、契约、默认值和依据，以及代码放在哪里、上线前逐项核对什么。凡是与其他智能体文档冲突的地方，以本文为准。

## 0 地位与读法

| 文档 | 角色 |
|---|---|
| `references/agent-foundation.md`（本文） | 底座：流水线、契约、默认值、上限、检查清单。唯一权威来源 |
| `agent/backend/SPEC.md` v2 | 门店业务助手的应用规范：意图表、工具清单、确认与降级文案、核验规则。文首列出了被本文取代的条目 |
| `agent/shared/protocol.md` v3 | 前后端协议细节：请求体、卡片、审批面板数据；事件名与 SSE 规则来自本文 §2 |
| `agent/README.md` | 代码现状与差距待办表 |
| `references/agent.md` | 入口与职责边界摘要 |
| `references/standards.json` → `AGENT-*` | 可核对规则；`agent` 类型方案自动带入核对表 |
| Luz-System `references/agent-design.html` | 界面：排队、逐步耗时、审批、记忆入口、引用、错误与降级的表达 |

读法：§1 看全貌；§2–§14 每个环节固定四段——**规则**（可核对）、**契约**（Pydantic）、**默认值**、**依据**（〔E编号 来源〕，全文与链接见 §18）；§15 是代码目录，§16 是全部超时与上限，§17 是检查清单。

改动纪律：
- 改契约（意图表、工具契约、状态、事件、错误码）要升 `schema_version`，并跑 §11 评测回归。
- 改超时或上限，同时改 §16 与 `agent/00-core/code/agent_core/config.py`；方案里改默认值要写理由。
- 新环节、新事件名、新错误码先改本文，再改代码。

## 1 固定流水线

```text
客户端 ─POST /runs─► §2 接入层：鉴权 · 限流 · 排队 · 幂等 · request_id ─────SSE 事件流────► 客户端
                         │
                         ▼
                 §3 输入护栏：校验 · 敏感信息遮蔽 · 注入检测 · 越权意图拦截（阻塞，先于主模型）
                         │
                         ▼
                 §4 意图识别与路由：规则 → 语义路由 → 小模型；置信度阈值；低于阈值追问
                         │
                         ▼
                 §5 编排（LangGraph 工作流；只有 plan ↔ 工具 之间有受限循环）
                   ├─► §6 工具层：契约 · 超时 · 幂等 · 错误分类 · MCP · 各家模型格式适配
                   ├─► §8 记忆：短期检查点 · 长期 Store
                   └─► §9 知识库：混合检索 · 重排 · 权限过滤 · 引用
                         │
                         ▼
                 §7 结构化输出：Pydantic 校验，失败重试 1 次后降级
                         │
                         ▼
                 §10 输出护栏与人工审批：事实核对 · 遮蔽 · 转义 · interrupt 审批
                         │
                         ▼
                 回答：message.delta … → run.completed

贯穿全程：§11 可观测与评测 · §12 性能 · §13 安全（OWASP 十大风险） · §14 稳定（超时 · 重试 · 熔断 · 降级 · 续跑 · 取消 · 上限）
```

**总原则：模型只做决策，程序控制运行。** 模型每一步只输出一个经 §7 校验的决策（`ToolCall` 列表、追问、审批请求或最终回答）；要不要执行、执行几次、重试、审批、何时停止、出错怎么办，全部由程序判定，写在代码与配置里，不写进提示词等模型遵守。授权在下游系统里完成，不依赖模型判断能否执行〔E54〕。下面每个环节都按这条原则分工。

| 环节 | 输入 | 输出契约 | 失败时 | 代码目录（§15） |
|---|---|---|---|---|
| §2 接入层 | HTTP 请求 + 令牌 | `RunAccepted`、`RunEvent` 流 | 422 / 409 / 429 + `Retry-After`，不入队 | `01-app`（待建） |
| §3 输入护栏 | 用户文本、附件 | `GuardrailVerdict` | 拒答模板或只读模式，不进主模型 | `02-guardrails`（待建） |
| §4 意图路由 | 文本、最近对话、意图表 | `IntentResult` | 规则意图或追问 | `03-intent` |
| §5 编排 | `AgentState` | 节点更新、检查点 | 从检查点续跑 | `07-graph`（待建） |
| §6 工具层 | `ToolCall` | `ToolResult` | 错误型结果回给模型；重试或熔断 | `06-tools`（待建） |
| §7 结构化输出 | 模型原始输出 | 各契约模型 | 重试 1 次 → 降级 | `10-llm` |
| §8 记忆 | 会话、用户 | `MemoryItem` | 跳过记忆，不影响回答 | `04-memory` |
| §9 知识库 | 问题、权限 | `RetrievedContext` | 跳过检索（降级 L1），说明影响 | `05-knowledge` |
| §10 输出护栏与审批 | 草稿回答、高风险调用 | `FinalAnswer`、`ApprovalRequest` | 重生成 1 次 → 模板回答；审批过期不执行 | `09-verify` + `02-guardrails` |
| §11 观测与评测 | 全程 | trace、指标、评测报告 | 告警；不达标不发布 | `11-observability` + `12-evals`（待建） |

## 2 接入层：鉴权、SSE、排队、幂等

### 规则

1. **身份只来自令牌。** 网关从令牌解析 `user_id`、`tenant_id`、`store_ids`、`role`，放进 `config["configurable"]`；请求体、模型输出、工具参数里出现的身份字段一律忽略。
2. **编号贯穿全程。** 每个 HTTP 请求生成 `request_id`（响应头 `X-Request-Id`）；每次运行有 `run_id` 和 `trace_id`，贯穿事件、工具调用、后端请求（`X-Trace-Id`）和日志。错误事件带可复制的 `request_id`。
3. **必须 SSE。** 回复文字、阶段与步骤进度、耗时、排队位置、审批请求、结果与错误，一律用 SSE（`text/event-stream`，UTF-8）推送。不轮询，不等整段生成完再一次返回。只有确需双向实时（例如语音）时才用 WebSocket，并在技术方案里写明理由。
4. **两步接入。** `POST .../runs` 只做校验、幂等登记、入队，立即返回 `202 {run_id, events_url}`；事件从 `GET /api/v1/agent/runs/{run_id}/events` 读取，首次连接与断线重连是同一个接口。网页端用 `fetch` 流式读取（可带 `Authorization` 头）；用原生 `EventSource` 时只能靠 Cookie 或一次性短期票据鉴权。
5. **小程序同样消费 SSE 格式。** 小程序没有 `EventSource`：用 `wx.request` 设 `enableChunked: true`（基础库 2.20.2 起；开启后该请求走 HTTP/1，不享受 HTTP/2、HTTP/3 优化），在 `RequestTask.onChunkReceived` 里把收到的 `ArrayBuffer` 按 UTF-8 解码、按空行切分事件，按同一规则解析 `id` / `event` / `data`，断线后手动带 `Last-Event-ID` 重连；低于 2.20.2 的基础库退到 `GET /runs/{run_id}/events?after_id=` 读取同一份事件日志。服务端不为小程序另做协议。
6. **事件名固定**为下方契约里的 14 个。每个事件带 `id`（本次运行内从 1 起单调递增）、`run_id`、`ts`、`elapsed_ms`；`ping` 不带 `id`、不入事件日志。新增事件名要升 `schema_version`；客户端遇到不认识的事件名忽略，不报错。
7. **断线续传。** 服务端按运行保存事件日志；流开头发 `retry: 3000`；客户端带 `Last-Event-ID` 重连时，从该 id 之后补发。运行已结束且客户端已收到终止事件（`Last-Event-ID` ≥ 终止事件 id）时返回 `204`，客户端停止重连；还没收到终止事件的，补发到终止事件后关闭。刷新页面或切回前台按 `run_id` 恢复显示，绝不重新执行。
8. **心跳与网关。** 每 15 秒发一次心跳（`event: ping`，不带 `id`，不会改变客户端记住的最后事件 id）；响应头 `Content-Type: text/event-stream; charset=utf-8`、`Cache-Control: no-cache`、`X-Accel-Buffering: no`；Nginx 在事件路径上 `proxy_buffering off`、`proxy_http_version 1.1`，`proxy_read_timeout` 必须大于心跳间隔（默认 60 秒即可）。
9. **结束判定只认终止事件**：`run.completed` / `run.failed` / `run.cancelled`。连接关闭不等于成功；HTTP 200 之后仍可能出现 `run.failed`（例如模型过载），客户端必须处理，已显示的半截回复标为“未完成”。客户端收到终止事件后主动关闭连接（`EventSource` 默认会自动重连）。
10. **首个事件 1 秒内到达**（`run.queued` 或 `run.started`）。做不到时界面先显示“已收到，正在准备”。
11. **耗时随事件下发。** `step.*`、`stage.completed` 带 `duration_ms`；`run.completed` 带 `timings`（排队、审批等待、执行、总计分开）。排队时间与审批等待单独计时，不算进执行耗时。
12. **排队削峰。** 设全局、每用户、每会话三级并发上限（§16）；超出的运行入队并推 `run.queued`（位置、预计开始秒数；变化时更新，最多每秒一次）。队列满或预计等待超过上限时返回 `429` + `Retry-After`（秒）和可读说明；界面显示“预计 X 秒后可用”，不显示“失败”。监控队列深度；生产速度长期大于消费速度时在入口限流，而不是无界扩容（扩容只会把压力推给模型和后端）。
13. **一个会话同时只跑一个运行。** 运行中再次发送：进入本会话的待发送队列（`run.queued`，`reason: "conversation_busy"`，显示“当前回复完成后处理”），或客户端选择“停止并发送”（先 `cancel` 再新建）。两个运行不得同时写同一会话。
14. **幂等。** 每条用户消息带客户端生成的 `client_message_id`（UUID v4，重发同一条消息时复用）。服务端 24 小时内对 `(tenant_id, user_id, conversation_id, client_message_id)` 去重：重复请求返回同一个 `run_id`，绝不新建运行；同一键但消息内容不同 → `409 IDEMPOTENCY_KEY_REUSED`；校验失败的请求不登记，修正后可用同一键重发。发送后按钮变为“停止”。
15. **限流与预算在入口检查**（每用户每分钟、每日运行数，租户每日费用），超出返回 `429` + `Retry-After` 与明确文案。

### 契约

HTTP 接口（路径前缀 `/api/v1/agent`）：

| 方法 | 路径 | 作用 | 成功 | 失败 |
|---|---|---|---|---|
| POST | `/conversations` | 新建会话 | `201 {conversation_id}` | 401 / 403 |
| POST | `/conversations/{cid}/runs` | 发一条消息：校验、幂等、入队 | `202 RunAccepted`；重复 `client_message_id` → `200` 同一 `run_id` | `422 VALIDATION_FAILED`、`409 IDEMPOTENCY_KEY_REUSED`、`429 QUEUE_FULL / RATE_LIMITED / BUDGET_EXCEEDED` + `Retry-After` |
| GET | `/runs/{run_id}/events` | SSE 事件流；首次与重连同一接口（`Last-Event-ID`） | `200 text/event-stream`；终止事件已送达 → `204` | 401 / 404 |
| GET | `/runs/{run_id}/events?after_id=` | 不能分块接收时的轮询，读同一份事件日志 | `200 {events, next_id, done}` | 401 / 404 |
| POST | `/runs/{run_id}/cancel` | 停止 | `202`；已结束 → `200` 返回现状 | 404 |
| POST | `/runs/{run_id}/approvals/{approval_id}` | 审批（同意 / 拒绝），事件在原流继续 | `202` | `409 APPROVAL_MISMATCH`、`410 APPROVAL_EXPIRED` |
| POST | `/runs/{run_id}/retry` | 从失败处续跑（`run.failed` 且 `resume_from` 非空） | `202 {run_id: 新, resume_of: 旧}`，从检查点继续 | `409` 不可续跑 |
| GET | `/conversations/{cid}/messages` | 历史消息（含各段耗时） | `200` | 404 |
| GET / DELETE | `/memory`、`/memory/{id}` | 查看、删除长期记忆（§8） | `200` / `204` | 404 |
| GET | `/health` | 依赖健康与熔断状态 | `200` | — |

事件契约（`agent/contracts/run_event.json` 由此生成）：

```python
from datetime import datetime
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field

class Contract(BaseModel):
    """所有底座契约的基类：多余字段报错，实例不可变。"""
    model_config = ConfigDict(extra="forbid", frozen=True)

EventName = Literal[
    "run.queued", "run.started",
    "stage.started", "stage.completed",
    "step.started", "step.completed", "step.failed",
    "message.delta",
    "approval.required", "approval.resolved",
    "run.completed", "run.failed", "run.cancelled",
    "ping",
]
TERMINAL_EVENTS = frozenset({"run.completed", "run.failed", "run.cancelled"})
Stage = Literal["guard", "intent", "retrieve", "plan", "tool", "approval", "verify", "respond", "memorize"]

class RunAccepted(Contract):                 # POST .../runs 的响应
    run_id: str
    status: Literal["queued", "running"]
    events_url: str

class RunEvent(Contract):                    # 每个 SSE 事件 data 里的信封
    schema_version: Literal["3"] = "3"
    id: int | None = Field(default=None, ge=1)   # 与 SSE id 字段相同；ping 为 None
    run_id: str
    conversation_id: str
    event: EventName
    ts: datetime                             # UTC
    elapsed_ms: int = Field(ge=0)            # 自运行创建起的墙钟时间
    data: dict[str, Any] = {}

class Queued(Contract):                      # run.queued
    position: int = Field(ge=1)
    eta_s: int = Field(ge=0)
    reason: Literal["capacity", "conversation_busy"]

class RunError(Contract):                    # run.failed 与 step.failed 的 error
    code: str                                # §14 错误码表
    message: str = Field(max_length=120)     # 是什么错误、对用户有什么影响
    next_action: Literal["retry", "wait", "resume", "relogin", "contact_admin", "none"]
    retryable: bool
    retry_after_s: int | None = None
    request_id: str
    partial: bool = False                    # 是否保留了已完成的步骤与文字
    resume_from: str | None = None           # 可从此步续跑

class StepInfo(Contract):                    # step.started / step.completed / step.failed
    step_id: str
    stage: Stage
    name: str                                # 工具名或子任务名
    label: str = Field(max_length=40)        # 服务端模板生成，例如“正在查你最近的订单”
    attempt: int = Field(default=1, ge=1)
    duration_ms: int | None = None           # completed / failed 必填
    summary: str | None = Field(default=None, max_length=80)
    error: RunError | None = None            # failed 必填

class Timings(Contract):
    queue_ms: int
    approval_wait_ms: int
    exec_ms: int                             # 不含排队与审批等待
    total_ms: int

class Completed(Contract):                   # run.completed
    answer: "FinalAnswer"                    # §10
    timings: Timings
    usage: dict[str, int]                    # input / output / cache_read tokens、tool_calls
```

线上格式（每个事件以空行结束；`data` 是 `RunEvent` 的 JSON）：

```text
retry: 3000

id: 1
event: run.queued
data: {"schema_version":"3","id":1,"run_id":"run_7f3a","conversation_id":"c_19","event":"run.queued","ts":"2026-10-29T08:00:00Z","elapsed_ms":40,"data":{"position":2,"eta_s":6,"reason":"capacity"}}

event: ping
data: {"ts":"2026-10-29T08:00:15Z"}

id: 2
event: run.started
data: {…"data":{"trace_id":"t_51c0","degraded":[]}}
```

LangGraph 输出与事件的对应：节点完成（`updates` 模式）→ `stage.completed`；工具节点内用 `get_stream_writer()` 发 `step.*`（`custom` 模式）；只流 `respond` 节点的模型文本（`messages` 模式，按 `langgraph_node` 过滤）→ `message.delta`；中断 → `approval.required`。分类、核验等内部模型调用打 `nostream` 标签。

v2 → v3 对照（`agent/shared/protocol.md` v2 的写法一律按此改）：

| v2 | v3 |
|---|---|
| `meta` | `run.started`（`trace_id`、`degraded`） |
| `intent` | `stage.completed`（`stage: "intent"`，带意图与置信度） |
| `tool_start` / `tool_end` | `step.started` / `step.completed` 或 `step.failed` |
| `delta` | `message.delta` |
| `card`、`suggestions` | 并入 `run.completed.answer` 的 `cards`、`quick_replies` |
| `confirm_request` | `approval.required`；审批结果 `approval.resolved` |
| `notice`（降级提示） | `run.started.degraded` 或带说明的 `step.failed` |
| `done` / `error` | `run.completed` / `run.failed` |
| `seq`、`turn_id` | `id`、`run_id` |
| `409 AGENT_BUSY` | 会话内排队（`reason: "conversation_busy"`）；会话队列满 → `429 QUEUE_FULL` |
| 小程序 WebSocket 主通道 | SSE 分块接收；WebSocket 只在确需双向实时时用 |

### 默认值

`SSE_RETRY_MS = 3000` · `SSE_PING_INTERVAL_S = 15` · `EVENT_LOG_TTL_S = 86400` · `DEDUP_TTL_S = 86400` · `run.queued` 更新最多每秒一次 · 首事件 ≤ 1 秒。并发、队列、限流上限见 §16。

### 依据

- 〔E01 WHATWG SSE〕事件流只按 UTF-8 解码；状态码不是 200 或类型不是 `text/event-stream` 即连接失败且不再重连；`204` 让客户端停止重连；重连时带 `Last-Event-ID`；`retry` 字段设置重连间隔；“每 15 秒左右”发一行注释防止代理断开；分块传输可能影响可靠性。
- 〔E02 MDN 使用服务器发送事件〕冒号开头的行是注释，可用作保活；连接关闭后浏览器默认重连，要用 `close()` 结束；HTTP/1.1 下每个浏览器对同一域名只有 6 个连接。
- 〔E03 Claude Streaming〕用命名事件和 `ping`；流中可能出现 `overloaded_error`（非流式下对应 529）；“可能新增事件类型，代码要能容忍未知事件”。
- 〔E04 Claude API errors〕错误可能在返回 200 之后发生；每个响应带请求编号。
- 〔E06 Azure 队列削峰〕队列作为缓冲，按平均负载配置消费能力；监控队列深度，生产速度大于消费速度时扩容要有界或在入口削减；无界扩容只会把过载推给下游；消费者要幂等。
- 〔E09 Stripe 幂等请求〕重复请求返回首次保存的结果；键用 V4 UUID、≤ 255 字符；保存约 24 小时；参数不同则报错；校验失败的请求不保存结果。
- 〔E10 MDN Retry-After〕`Retry-After` 给出客户端需要等待的秒数或时间点。
- 〔E11 Nginx 代理模块〕默认 `proxy_buffering on`；关闭后响应立即转发；可用 `X-Accel-Buffering: no` 按响应关闭；`proxy_read_timeout` 默认 60 秒，两次读之间没有数据就断开。
- 〔E12 NN/g 响应时间三个界限〕1 秒是思路不被打断的界限，超过 1 秒要提示正在处理，超过 10 秒要有进度和可中断的出口。〔E13 NN/g 进度指示〕立即给反馈，否则用户会以为没收到而重复操作；超过 10 秒给预计剩余时间并允许取消。
- 〔E14 LangGraph Streaming〕`updates`、`messages`、`custom`、`checkpoints`、`tasks` 等流模式；`nostream` 标签让内部调用不输出 token；节点内用 `get_stream_writer` 发自定义进度。

## 3 输入护栏

### 规则

1. **先拦后算。** 输入护栏在主模型调用之前以阻塞方式完成：被拦下的请求不消耗主模型 token、不执行任何工具。便宜的确定性检查先跑；需要小模型判定的检查可与意图分类并行，但命中时必须在调用工具前取消后续步骤。
2. **校验。** 请求体用 Pydantic（`extra="forbid"`）校验：文本长度、附件类型与大小、必填参数；不合规返回 `422`，就地提示，不入队。
3. **敏感信息遮蔽。** 手机号、身份证号、银行卡号、密码类文本在进入模型、日志、记忆之前遮蔽；工具确需原值时由程序从规则抽出的槽位传入，不经模型复述。
4. **提示注入（OWASP LLM01）。** 直接注入（用户输入里要求忽略规则、泄露提示、越权操作）用规则加小模型判定：明显越狱或要求越权 → 拒答模板 + 页面入口；可疑但不确定 → 本次运行转为只读（关闭全部写工具）并记录。间接注入：工具结果、知识片段、网页、文件、记忆一律作为“数据”放在带标记的分隔块里，系统提示声明块内任何指令无效；外部内容不得拼进系统提示或工具描述。
5. **越权意图拦截。** 意图表规定了每个意图允许的渠道与角色；不允许当前用户的意图（例如顾客端触发后台意图）在路由前拦下，回答“不能做”并给入口。权限判断由程序做，不交给模型。
6. **预算检查**（每日次数、租户费用）在这里执行，超出给明确文案。
7. 护栏结果写进 trace（`guardrail` span：规则名、动作、耗时），不记录原文。

### 契约

```python
class GuardrailVerdict(Contract):
    action: Literal["allow", "mask", "readonly", "block"]
    rules: list[str] = []                      # 命中的规则，例如 "pii.phone"、"injection.direct"
    masked_text: str | None = None             # action=mask 时进入后续环节的文本
    user_message: str | None = Field(default=None, max_length=120)  # action=block 时的模板文案
```

### 默认值

`MAX_INPUT_CHARS` 小程序与 H5 500 字、后台 2000 字；请求体 ≤ 8 KB（不含附件）；附件类型白名单与大小上限按项目配置；遮蔽规则：手机号保留前 3 后 4，证件号与卡号全部遮蔽。小模型护栏超时见 §16。

### 依据

- 〔E27 OpenAI Agents SDK 护栏〕阻塞模式下触发拦截时“智能体从不执行”，不消耗 token、不调用工具；并行模式（默认）可能在被取消前已经消耗 token、执行了工具；工具护栏在每次函数工具调用时运行，多智能体时优先用工具护栏。
- 〔E39 OWASP LLM01 提示注入〕直接与间接注入；注入内容不需要人可见；RAG 和微调不能完全防住；缓解：约束模型行为、用确定性代码校验输出格式、输入输出过滤、最小权限、高风险动作人工批准、隔离并标明不可信的外部内容、对抗测试。
- 〔E15 Anthropic 构建有效的智能体〕护栏放在单独的调用里，通常比让同一个调用兼顾护栏与回答效果更好。
- 〔E22 MCP 规范〕工具描述与注解除非来自可信服务器，否则视为不可信。

## 4 意图识别与路由

### 规则

1. **意图表是配置数据。** 每个项目一份 `config/intents.yaml`（由 `requirements/<项目>.md` 勾选生成），用 `IntentSpec` 校验。每个意图写明：id、一句话描述、≥ 5 条示例句、必填槽位、允许的工具、是否需要审批、渠道、角色、是否用知识库、启用开关。分类提示词、语义路由、评测集都从这张表生成，提示词里不另写一份。
2. **三级识别，便宜的先跑。** ① 规则：正则与关键词（订单号、手机号、“转人工”），命中即预填槽位或直接判定；② 语义路由：用示例句的向量相似度，阈值在评测集上校准为概率后才允许直接判定，未校准前只提供候选；③ 小模型分类：结构化输出 `IntentResult`（温度 0；输入启用意图清单、最近 6 条消息、运行摘要）。
3. **置信度阈值。** `≥ 0.75` 直接进入；`0.5–0.75` 或缺必填槽位 → 追问（一次只问 1 个问题，附 ≤ 4 个快捷回复，文案由服务端模板生成）；`< 0.5` → 不猜：回答“我能帮你做这些”，列候选与入口，不调用任何写工具。同一意图连续追问 2 次仍缺 → 给页面入口结束本轮。
4. **多意图。** 先处理主意图，次意图最多 1 个，在回答末尾承接；不并行执行两个意图的写操作。
5. **路由决定工具范围。** 本轮只把当前意图的 `allowed_tools` 暴露给模型（通常 ≤ 5 个）。
6. **交接（handoff）** 只用于真正不同的专门智能体或转人工。交接本身是一次工具调用，交接参数（原因、优先级、摘要）用 Pydantic 模型校验；权限在服务端交接开始时检查，失败直接报错；转给对方的历史按需过滤，并把接收方当作这些数据的接收者对待。
7. 意图表任何改动（新增意图、改描述或示例）都要跑意图评测集（§11）。

### 契约

```python
Channel = Literal["miniprogram", "h5", "admin"]

class IntentSpec(Contract):                    # config/intents.yaml 每一项
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,40}$")
    description: str = Field(max_length=80)
    utterances: list[str] = Field(min_length=5)    # 语义路由与评测共用的示例句
    required_slots: list[str] = []
    optional_slots: list[str] = []
    allowed_tools: list[str] = []
    needs_approval: bool = False
    channels: list[Channel]
    roles: list[str] = []                          # 空 = 所有已登录用户
    knowledge: bool = False
    enabled: bool = True

class Slot(Contract):
    name: str
    value: str | int | float | None
    source: Literal["user", "rule", "context", "default"]

class IntentResult(Contract):
    schema_version: Literal["1"] = "1"
    intent: str                                    # 必须是已启用意图的 id（校验器对照意图表）
    confidence: float = Field(ge=0, le=1)
    router: Literal["rule", "semantic", "model"]
    slots: list[Slot] = []
    missing_required: list[str] = []
    needs_clarification: bool
    candidates: list[str] = Field(default=[], max_length=3)
    reason: str = Field(default="", max_length=120)    # 只进 trace，不给用户看
```

### 默认值

`INTENT_HIGH = 0.75` · `INTENT_LOW = 0.5` · `MAX_CLARIFY_ROUNDS = 2` · 快捷回复 ≤ 4 · 每个意图示例句 ≥ 5 · 分类输入最近 6 条消息 · 语义路由阈值按评测集校准（误判率 ≤ 2% 时才允许直接判定）。

### 依据

- 〔E15 Anthropic 构建有效的智能体〕路由工作流：先分类再交给专门的后续处理，否则为一类输入优化会伤害另一类；简单常见的问题交给小模型。
- 〔E37 semantic-router〕按示例句在向量空间比较语义，毫秒级决策；默认总是选最佳路由，要“都不是”需用标注数据 `fit` 校准，再设 `confidence_threshold`；可先用快路由，不确定时再落到慢路由。
- 〔E28 OpenAI Agents SDK 交接〕交接以工具呈现给模型；`input_type` 用 Pydantic 校验模型给出的交接参数，只放交接时由模型决定的元数据；授权在 `on_handoff` 开头检查，失败要抛错；转交的历史不会自动脱敏。
- 〔E41 阿里云百炼 Function Calling〕给模型的工具建议不超过 20 个，工具多时在前面加检索或模型路由。

## 5 编排：工作流优先

### 规则

1. **工作流优先。** 固定节点：`guard → route → (clarify | retrieve) → plan → (run_tools | approve) → verify → respond → memorize`，路由用条件边。自由循环只存在于 `plan ↔ run_tools` 之间，并受工具调用次数、图步数、同名同参重复调用上限约束（§16）。从最简单的方案开始，只有评测证明更好时才增加复杂度。
2. **状态模型。** `AgentState` 用 TypedDict（性能好）或 Pydantic；会话级字段跨轮保留，轮次级字段在 `guard` 统一重置；`messages` 必须用 `add_messages` reducer、只追加，确需清空用 `Overwrite`；数据库连接、缓存等不入检查点（`UntrackedValue`）。
3. **检查点。** 只在父图编译时挂 checkpointer；生产用 Postgres，内存实现只用于测试（进程重启全部丢失）；`thread_id = conversation_id`（< 255 字符）；每个超步后落检查点；按保留期定期清理旧检查点。
4. **节点会重跑。** 恢复时节点从头执行，所以节点内的写操作必须幂等（幂等键、upsert、先查后写）；写操作放在审批 `interrupt()` 之后，或放进单独节点。
5. 同一节点不混用普通边和动态路由；并行分支（知识库两路召回、互不依赖的只读工具）用多条出边或 `Send`，汇合的字段要有 reducer。
6. **多智能体有条件。** 只在子任务可并行、彼此独立、单个上下文装不下时使用（多智能体的 token 用量约为普通对话的 15 倍）；每个子智能体要有目标、输出格式、可用工具和边界，结果以精简摘要回传。门店业务助手默认单智能体工作流。
7. 显式设置 `recursion_limit = MAX_GRAPH_STEPS`（LangGraph 1.0.6 起默认 1000 步，不能依赖）。
8. **发布与图变更。** 有中断中的线程时，只允许新增节点，不改名、不删节点、不改状态键名；新旧版本并行运行、逐步切流。
9. **推理与行动交替（ReAct）由程序驱动。** `plan` 节点让模型输出一个决策；`route` 是普通函数，按状态与计数决定去 `run_tools`、`clarify`、`approve`、`respond` 还是结束；`run_tools` 先过权限（§6 规则 15）与参数校验（§6 规则 10），再执行，结果作为观察写回状态。每进入一次 `plan` 记一个决策轮次（`rounds`），超过 `MAX_ROUNDS`（§16）立即发 `run.failed`（`STEP_LIMIT`，`partial: true`），不再调用模型收尾，也不自动加轮次；工具次数、同名同参重复与 `recursion_limit` 只是兜底。循环只有五个出口：最终回答、追问、等待审批、报错、达到上限。用模型原生的结构化工具调用，不用依赖停止词解析文字的 ReAct 模板。
10. **多轮对话。** 每条用户消息是一次新运行：`rounds`、`tool_calls_made`、`tool_call_counts`、`call_signatures`、`used_single` 等轮次级字段在 `guard` 重置；跨消息只保留 `messages`（较早部分压成 `summary`，旧的工具结果只留一句话，§8）、已确认的意图参数与长期记忆。追问是一次运行的正常结束（§4），连续追问上限见 §16。

### 契约

```python
from typing import Annotated, TypedDict
from langgraph.graph.message import add_messages

class AgentState(TypedDict, total=False):
    # 会话级（跨轮保留）
    messages: Annotated[list, add_messages]    # 只追加
    summary: str                               # 运行摘要（§8）
    tenant_id: str
    user_id: str                               # 只来自令牌
    channel: Channel
    # 轮次级（guard 每轮重置）
    run_id: str
    trace_id: str
    verdict: GuardrailVerdict | None
    intent: IntentResult | None
    context: RetrievedContext | None
    rounds: int                                # 决策轮次：每进入一次 plan 加 1，超过 MAX_ROUNDS 即 STEP_LIMIT
    tool_calls_made: int
    tool_call_counts: dict[str, int]           # 每个工具本次运行的调用次数（multi 对照 max_calls_per_run）
    used_single: list[str]                     # 已成功的 single 工具：移出 tools，再调即拒绝
    call_signatures: list[str]                 # 同名同参重复检测
    pending_approval: ApprovalRequest | None
    verify_feedback: str | None
    final: FinalAnswer | None
    degraded: list[str]                        # 本轮触发的降级，例如 "kb_skipped"、"llm_fallback"
```

节点职责与失败处理沿用 `agent/backend/SPEC.md` §2.1 的节点表（`classify` 即本文 `route`，`confirm` 即本文 `approve`）。

### 默认值

会话保留 30 天，检查点与事件日志随会话清理；工具调用次数、图步数、重复调用上限见 §16。

### 依据

- 〔E15 Anthropic 构建有效的智能体〕工作流是预定义代码路径，智能体由模型自行决定流程；先找最简单的方案，只在明显改善结果时增加复杂度；智能体要有最大迭代次数等停止条件。
- 〔E19 Anthropic 多智能体研究系统〕多智能体约用普通对话 15 倍 token；适合可大量并行、信息超过单个上下文的任务；需要共享上下文或依赖多的任务不适合；每个子智能体要有目标、输出格式、工具指引和边界。
- 〔E33 LangGraph Graph API〕未指定 reducer 的键每次被覆盖；合并型 reducer 返回空值不能清空字段，要用 `Overwrite`；检查点在超步边界保存，节点恢复时重跑，需要幂等键或 upsert；不要在同一节点混用普通边和动态路由；默认递归上限 1000；有中断线程时不要改名或删除节点。
- 〔E31 LangGraph Persistence〕checkpointer 按 `thread_id` 保存线程状态，Store 保存跨线程数据；内存实现重启即丢；`thread_id` 小于 255 字符；检查点会无限增长，要定期清理。
- 〔E17 Anthropic 上下文工程〕上下文是有限资源，用最少的高信号 token 达到目标。
- 〔E48 ReAct 论文〕让模型交替生成推理与动作：推理用来形成、跟踪和调整计划并处理异常，动作调用外部接口取得信息；比只做思维链少幻觉与错误传递。
- 〔E51 OpenAI Agents SDK 运行智能体〕循环：有工具调用就执行、追加结果、再循环；只有输出最终结果且没有工具调用才结束；超过 `max_turns` 抛出 `MaxTurnsExceeded`。
- 〔E53 LangGraph GRAPH_RECURSION_LIMIT〕图在达到停止条件前走到最大步数时报错，通常是环路造成的死循环；可在调用配置里设 `recursion_limit`。

## 6 工具层

### 规则

1. **一个工具一个模块**：契约（参数模型、返回模型、元数据）+ 实现 + 测试，放在 `06-tools/<工具名>.py`。注册表启动时校验契约，不合格的工具拒绝注册，服务不启动。
2. **命名**：`动词_对象`（`get_order`、`search_products`）；接多个系统时加系统前缀（`crm_get_customer`）。名称稳定，语义变了就改名。
3. **描述写给新同事**：做什么、什么时候用、什么时候不用（指向替代工具）、一个输入例子、边界情况；参数名明确（`order_id` 而不是 `id`）。
4. **参数模型**：Pydantic，`extra="forbid"`；每个字段有类型、范围、枚举（`Literal`）、长度、`description` 和例子；身份字段（`user_id`、`tenant_id`）永远不是参数，由程序注入；JSON Schema 由 `model_json_schema()` 生成，不手写。参数设计成不容易填错（固定格式、枚举而不是自由文本）。
5. **返回模型**精简、对模型友好：用可读名称和后续调用需要的 id，不返回内部技术字段；列表带分页（`limit`、`next_cursor`、`has_more`），超长截断并说明“还有 N 项，可用 cursor 继续”；需要时提供 `detail: "concise" | "detailed"`。结果进入模型前脱敏、截断（§16）。动作类工具返回明确的状态说明（“已加入购物车”“操作失败：库存不足”）。
6. **副作用级别**：`read` 直接执行；`write`（低风险写）执行后回显并可撤销；`write_high` 必须审批（§10），且必须声明对应的只读预览工具；支付、退款执行、改价、改库存、改权限、删除账号一律不注册为工具，只给页面入口。默认只读。
7. **超时与重试**：每个工具声明超时；只有 `read` 和带幂等键的写工具可自动重试；重试与熔断按 §14。重试、降级还是终止由程序按契约决定；程序决定继续运行时，才把 `TIMEOUT` 型结果交给模型（`for_model` 写明已重试几次、不要用同样参数再调、可改用什么）；关键工具超时且无法降级，直接以 `TOOL_TIMEOUT` 失败。
8. **幂等键**：写工具一律带 `Idempotency-Key = uuid5(命名空间, tenant:conversation:run:tool:参数摘要)`；同一审批重放得到同一个键，后端据此去重。
9. **错误分类**：`retryable`（超时、连接中断、429、5xx）、`fatal`（参数错误、不存在、冲突、业务拒绝）、`needs_user`（登录过期、无权限、缺信息、需确认）。给模型的错误说明具体、可执行（哪个字段、为什么、正确示例），不给错误码堆栈；给用户的文案由模板生成。
10. **参数校验失败不调用后端**，也不只回一句“参数错误”：回给模型一条 `INVALID_ARGS` 错误型结果，带原样的当前参数 `received_args`、逐字段的 `problems`（字段、收到的值、原因、正确写法、例子）、一句话改法 `how_to_fix` 和剩余次数 `attempts_left`；这些由 Pydantic `ValidationError` 的 `loc`、`input`、`msg` 与字段的 `description`、`examples` 生成。JSON 解析失败同样处理（`received_args` 放原始字符串）。业务拒绝也写原因与下一步。同一工具每次运行最多纠正 2 次，计入工具调用与轮次上限，用完按 `fatal` 结束该步骤。
11. **少而精**：一个智能体的工具总数 ≤ 20，每轮只暴露当前意图允许的工具；功能重叠的工具合并；不把后端接口一对一包成工具，能用一个“取上下文”工具完成的不拆成多个底层调用。工具描述的任何改动按提示词改动对待，跑 §11 评测。
12. **外部系统优先 MCP**：通过 MCP 客户端接入；MCP 服务器给出的工具描述和注解视为不可信，只启用经审核、在白名单里的工具，并同样套上本节契约（副作用、超时、幂等、错误分类、熔断分组）；调用前满足用户同意与授权。白名单里每个 MCP 工具由我方登记 `side_effect` 与 `result_kind`，注解只作参考；服务器发出工具清单变化通知后，新增或改动的工具停用到审核通过。按最新规范，参数校验错误、接口失败与业务错误以 `isError: true` 的结果返回，转成同一 `ToolError` 交给模型自我纠正；未知工具、请求格式错误、服务器错误是协议错误，不交给模型，按 `fatal` 或依赖失败处理〔E49〕。授权按 OAuth 2.1：令牌只发给签发给该服务器的那一方，不转发用户令牌或别处的令牌〔E50〕。
13. **模型格式适配统一在 `10-llm`**：工具只定义一次（Pydantic），适配层转换为各家格式，并把模型的调用转回统一的 `ToolCall`：
    - OpenAI 兼容格式（通义千问百炼、vLLM 部署的 Qwen 等）：`tools=[{"type": "function", "function": {name, description, parameters}}]`；返回的参数是 JSON 字符串，必须自行解析并用 Pydantic 校验，解析失败按参数错误回给模型；结果以 `{"role": "tool", "tool_call_id": …}` 回传；`parallel_tool_calls` 默认开启，一次可能返回多个调用（设为关闭也可能多个），必须遍历全部并按顺序回传；流式时函数名在第一块，参数按 `index` 拼接完整后再解析；通义千问不支持 `tool_choice: "required"`；让模型总结工具结果时去掉 `tool_choice`。
    - Claude：`{name, description, input_schema}`，可加 `strict: true`；结果用 `tool_result`（`tool_use_id`），失败时标记为错误；`disable_parallel_tool_use` 控制每轮最多一个调用；流式时工具参数是部分 JSON，拼接完整后再解析。
    - 自部署 Qwen3：用 Hermes 风格工具模板（vLLM `--tool-call-parser hermes`），不用 ReAct 一类依赖停止词的模板。
    - DeepSeek：按 OpenAI 兼容格式接入；本次采集的 DeepSeek 文档页为 404，接入前补采并核对（差距表）。
14. **结果分级**：`result_kind` 二选一。`single`（单一结果）：一次调用给出本次运行所需的完整结果，同参再调结果不变（取一张订单、算一次报价、生成一份草稿）；成功一次后记入 `used_single`，之后本次运行发给模型的 `tools` 不再包含它，执行层同时拒绝再调用（`TOOL_NOT_AVAILABLE`，`for_model` 写明已在第几步执行、结果在哪）；参数错误、超时与重试失败不算用掉。`multi`（多种结果）：参数不同结果不同，可在一次运行内多次调用（按条件检索、分页、逐个对象查询）；描述写清覆盖范围、不做什么并指向替代工具，契约声明 `max_calls_per_run`（默认 3，≤ 5），同名同参不重复；功能重叠的 `multi` 工具合并。移除工具定义会让提示缓存从工具段起失效一次〔E21〕：工具按固定顺序传入，只在 `single` 成功后移除，一次运行最多几次，代价可控。
15. **权限由程序逐轮计算**：每次调用模型前，`tools` = 角色允许 ∩ 当前意图允许 ∩ 本次运行仍可用（去掉 `used_single`、已达 `max_calls_per_run` 的、已在待审批的 `write_high`）∩ MCP 白名单；执行前按同一规则再校验一次，不在清单里、超次数或越权的调用不执行，返回 `TOOL_NOT_AVAILABLE` 或 `FORBIDDEN` 型结果并计入轮次。身份字段由程序注入（规则 4），后端接口按用户令牌鉴权，智能体权限不超过用户本人；不让模型判断某个操作有没有权限〔E54〕。

### 契约

```python
from pydantic import model_validator

SideEffect = Literal["read", "write", "write_high"]
ResultKind = Literal["single", "multi"]       # 规则 14

class ToolSpec(Contract):
    name: str = Field(pattern=r"^[a-z]+(_[a-z0-9]+)+$")     # 动词_对象
    description: str = Field(min_length=20, max_length=600)  # 做什么 / 何时用 / 何时不用 / 例子
    params: type[BaseModel]
    returns: type[BaseModel]
    side_effect: SideEffect
    result_kind: ResultKind                  # single：成功一次后本次运行不再提供；multi：可多次调用
    max_calls_per_run: int = Field(default=1, ge=1, le=5)   # single 固定 1；multi 写明 2–5（默认 3）
    preview_tool: str | None = None          # write_high 必填：执行前取服务端事实的只读工具
    timeout_s: float = Field(gt=0, le=30)
    max_retries: int = Field(ge=0, le=3)     # 只对 retryable 错误
    idempotent: bool
    breaker_key: str                         # 熔断分组：后端服务名或 MCP 服务器名
    channels: frozenset[Channel]
    roles: frozenset[str] = frozenset()      # 后台权限码；空 = 所有登录用户
    max_result_chars: int = 2000
    source: Literal["local", "mcp"] = "local"

    @model_validator(mode="after")
    def _guard(self) -> "ToolSpec":
        if self.side_effect == "write_high" and not self.preview_tool:
            raise ValueError("write_high 工具必须声明只读预览工具")
        if self.side_effect != "read" and not self.idempotent:
            raise ValueError("写工具必须声明幂等键规则")
        if self.result_kind == "single" and self.max_calls_per_run != 1:
            raise ValueError("single 工具每次运行只成功执行一次")
        if self.result_kind == "multi" and self.max_calls_per_run < 2:
            raise ValueError("multi 工具写明每次运行最多调用几次（2–5）")
        return self

class GetOrderParams(Contract):
    """查当前用户的一张订单（状态、金额、配送进度）。查列表用 list_orders。"""
    order_id: str = Field(pattern=r"^\d{14,20}$", description="订单号，14–20 位数字", examples=["20261023115200"])
    detail: Literal["concise", "detailed"] = "concise"

class OrderView(Contract):
    order_id: str
    status: Literal["pending_pay", "paid", "delivering", "done", "cancelled"]
    status_text: str
    amount_cents: int = Field(ge=0)
    eta_text: str | None = None
    route: str                               # 去页面的入口

class ArgProblem(Contract):
    field: str                               # "date_from"；整体问题写 "(args)"
    received: Any = None                     # 收到的值；缺失为 None
    reason: str                              # "需要具体日期，收到相对时间"
    expected: str                            # "日期，YYYY-MM-DD，不早于 2025-01-01"
    example: Any = None                      # "2026-10-20"

class ToolError(Contract):
    kind: Literal["retryable", "fatal", "needs_user"]
    code: str                                # TIMEOUT / UPSTREAM_5XX / RATE_LIMITED / INVALID_ARGS / TOOL_NOT_AVAILABLE / NOT_FOUND / CONFLICT / FORBIDDEN / LOGIN_EXPIRED
    for_model: str = Field(max_length=300)   # “order_id 必须是 14–20 位数字，例如 20261023115200”
    for_user: str = Field(max_length=80)     # 模板文案
    retry_after_s: int | None = None
    received_args: dict[str, Any] | str | None = None   # INVALID_ARGS：原样的当前参数（JSON 解析失败时为原始字符串）
    problems: list[ArgProblem] = []                     # INVALID_ARGS：逐字段的问题
    how_to_fix: str | None = None                       # INVALID_ARGS：一句话改法
    attempts_left: int | None = None                    # 本工具本次运行还可纠正几次

    @model_validator(mode="after")
    def _invalid_args_complete(self) -> "ToolError":
        if self.code == "INVALID_ARGS" and (self.received_args is None or not self.problems
                                            or not self.how_to_fix or self.attempts_left is None):
            raise ValueError("INVALID_ARGS 必须带 received_args、problems、how_to_fix 与 attempts_left")
        return self

class ToolCall(Contract):
    call_id: str
    name: str
    args: dict[str, Any]                     # 校验前的原始参数

class ToolResult(Contract):
    call_id: str
    tool: str
    ok: bool
    data: dict[str, Any] | list[Any] | None = None   # 已按 returns 校验、已脱敏、已截断
    truncated: bool = False
    has_more: bool = False
    next_cursor: str | None = None
    error: ToolError | None = None
    attempts: int = 1
    duration_ms: int
```

模型看到的参数错误（`tool_result` 或 `role: "tool"` 的内容）：

```json
{"ok": false, "error": {"kind": "fatal", "code": "INVALID_ARGS",
  "received_args": {"date_from": "上周", "status": "全部"},
  "problems": [{"field": "date_from", "received": "上周", "reason": "需要具体日期，收到相对时间",
                "expected": "日期，YYYY-MM-DD", "example": "2026-10-20"}],
  "how_to_fix": "把“上周”换算成具体日期后重试", "attempts_left": 1,
  "for_model": "date_from 需要具体日期，例如 2026-10-20"}}
```

### 默认值

智能体工具总数 ≤ 20；每轮暴露 ≤ 5 个；参数自我纠正每工具每运行 ≤ 2 次；`single` 工具每次运行成功 1 次，`multi` 工具每次运行默认 ≤ 3 次；工具结果 ≤ 2000 字符、列表 ≤ 10 项。各类工具的超时与重试次数见 §16。

### 依据

- 〔E16 Anthropic 为智能体写好工具〕工具多不一定好，重叠的工具会分散模型注意力，每个工具要有清晰独立的用途；不要只是包一层现有接口；按服务和资源加命名空间前缀；返回有意义的上下文，少用 uuid 这类底层标识；用分页、过滤、截断并给出继续的指引；`response_format` 简洁版约为详细版三分之一 token；错误信息给出具体可改进的建议而不是不透明的错误码；描述写得像给新同事讲解，参数用 `user_id` 而不是 `user`；用严格的数据模型约束输入输出。
- 〔E15 Anthropic 构建有效的智能体〕好的工具定义包含使用示例、边界情况、输入格式要求和与其他工具的界限；改参数让模型更难犯错（防呆）。
- 〔E17 Anthropic 上下文工程〕工程师都说不清该用哪个工具时，模型也做不到；工具要自成一体、容错、用途清楚。
- 〔E20 Claude 工具调用〕`input_schema`、`tool_use` / `tool_result` 往返；`strict: true` 保证调用符合模式；`disable_parallel_tool_use`；工具定义计入输入 token。
- 〔E24 Pydantic AI 函数工具〕参数模式由函数签名生成，含 `additionalProperties: false`；可要求每个参数都有描述。
- 〔E41 阿里云百炼 Function Calling〕参数为 JSON 字符串需解析；`parallel_tool_calls` 默认 true，部分模型设 false 仍可能返回多个调用；流式参数按 `index` 拼接；不支持 `required`；工具默认只读，不可逆操作需用户确认；工具描述按输入 token 计费；建议设置重试上限（例如 3 次）与每步超时。
- 〔E42 Qwen 文档 Function Calling〕推荐 Hermes 风格；不推荐基于停止词的模板；生成的调用可能格式错误无法解析，生产代码要自行解析并准备补救。
- 〔E22 MCP 规范〕JSON-RPC 2.0；主机、客户端、服务器三种角色；工具代表任意代码执行，调用前要得到用户明确同意；工具描述和注解视为不可信。
- 〔E43 DeepSeek Function Calling〕本次采集为 404 页面，无可用内容。
- 〔E49 MCP 规范“工具”最新版〕协议错误（未知工具、请求格式错误、服务器错误）模型难以修正；接口失败、参数校验错误、业务错误放在结果里并标 `isError: true`，客户端应交给模型自我纠正；注解除非来自可信服务器否则不可信；应始终有人能拒绝工具调用；`listChanged` 时服务器会通知工具清单变化。
- 〔E50 MCP 规范“授权”最新版〕基于 OAuth 2.1；服务器只接受签发给自己的令牌，客户端不发送其他授权服务器签发的令牌。
- 〔E52 Claude 文档“定义工具”〕描述要极其详细（做什么、何时用与不用、每个参数的含义、限制），这是影响工具调用效果最大的因素。
- 〔E54 OWASP LLM06:2025 过度代理〕只给必要的工具、功能和最小权限；以用户本人的身份与权限执行；高影响动作要人批准；在下游系统里做完全仲裁，不依赖模型判断能否执行。
- 〔E21 Claude 提示缓存〕缓存按 工具 → 系统 → 消息 分层，某一层改动会让该层及之后全部失效。

## 7 结构化输出与模型适配

### 规则

1. **程序消费的模型输出全部用 Pydantic 模型定义并校验**：意图、工具调用决策、核验判定、记忆候选、最终回答的结构部分。用 `model_validate_json` 校验原始 JSON；`extra="forbid"`。Pydantic 默认会做类型转换（例如 `"2.72"` 转成浮点数），金额、id、状态等字段用严格类型或 `strict=True`。
2. **输出方式按可靠性选**：优先“工具输出”方式（把输出模型当作一个工具让模型调用）或供应商原生结构化输出；只用提示词约束 JSON 最不可靠，只作兜底。
3. **校验失败**：把校验错误（字段路径与原因）回给模型重试 1 次；仍失败就降级（规则结果或模板回答），记 `degraded: ["structured_output"]`。
4. **流式中的部分输出不触发副作用**：只有最终完整输出才能驱动写操作。
5. **契约导出**：每个契约模型用 `model_json_schema()` 导出到 `agent/contracts/<名称>.json`（含 `schema_version`），前端与评测都用这份。
6. **适配层统一接口**：`decide(messages, tools) -> Decision` 与 `structured(prompt, Model) -> Model`。各家差异（工具格式、并行调用、流式参数拼接、思考模式开关、错误码映射、提示缓存标记）只在适配层处理；适配层不得改变工具的副作用级别与权限逻辑。
7. **错误码映射**：429（有无 `retry-after`）、529 / 过载、500 / 502 / 503 / 504、超时、连接中断 → 瞬时错误（可重试）；400 / 401 / 403 / 404 / 413 → 不重试；花费上限类 429 不带 `retry-after`，按不可恢复处理（熔断该供应商并告警）。用 SDK 的类型化异常判断，不靠匹配错误文字。
8. **模型分层**：路由、分类、护栏、核验、摘要用小模型；规划与最终回答用主模型；备用模型来自不同供应商或更小的型号（§14 回退链）。

### 契约

```python
from typing import Annotated, Union

class AnswerDecision(Contract):
    type: Literal["answer"]
    text: str = Field(max_length=1500)

class ToolDecision(Contract):
    type: Literal["tool"]
    calls: list[ToolCall] = Field(min_length=1, max_length=3)   # 同一步内互不依赖的只读调用可并行

Decision = Annotated[Union[AnswerDecision, ToolDecision], Field(discriminator="type")]
```

### 默认值

`STRUCTURED_RETRIES = 1`；分类与结构化调用温度 0；模型超时与重试见 §16。

### 依据

- 〔E26 Pydantic 模型〕Pydantic 保证输出类型而不是输入；默认会转换数据，严格模式不转换；默认忽略多余字段，`forbid` 报错；`model_validate_json` 直接校验 JSON 更快；一次校验的所有错误放在一个异常里，每条带位置与类型；`model_construct()` 不做校验，只用于可信数据。
- 〔E25 Pydantic AI 输出〕工具输出是默认方式，几乎所有模型都支持且效果好；原生输出不是所有模型都支持；提示词输出最不可靠；输出校验器抛 `ModelRetry` 让模型重试，重试预算默认 1；流式时校验器对部分输出也会调用，执行副作用前要判断是否是最终输出。
- 〔E23 Pydantic AI〕输出模型带描述与范围约束，每次运行都校验；测试可用离线测试模型。
- 〔E04 Claude API errors〕各错误类型与状态码；SDK 对瞬时错误默认重试两次并遵守 `retry-after`；花费上限类 429 没有 `retry-after`；用类型化异常，不匹配文字。
- 〔E05 Claude Rate limits〕令牌桶限流；超限返回 429，带 `retry-after` 时按它等待，不是每个 429 都带。
- 〔E41 阿里云百炼〕〔E42 Qwen 文档〕各家工具调用格式与思考模式开关不同（见 §6 第 13 条）。

## 8 记忆

### 规则

短期（会话内）：

1. 会话状态与消息由 checkpointer 按 `thread_id = conversation_id` 持久化。进模型前裁剪（最近 12 条，从用户消息开始、到用户消息或工具结果结束）；累计超过阈值时把更早的消息压成运行摘要，工具调用与对应结果成对删除；不改检查点里的原始记录。
2. 工具结果瘦身：之前轮次的工具结果在进入模型时替换为一句话摘要；上下文只留对下一步有用的信息。

长期（跨会话）：

3. 存在 Store，命名空间 `(tenant_id, user_id, kind)`，`kind ∈ profile | preferences | facts`；生产用数据库存储，内存实现只用于测试。命名空间强制带用户，跨用户读取不可能发生。
4. 只写用户明确陈述的稳定信息（忌口、过敏、称呼、常用偏好），置信度 ≥ 0.8；不写模型推测、一次性事实（“今天不吃辣”）。禁止写入密码、证件号、银行卡号、完整手机号与完整地址、支付信息、凭据——写入前用规则检查，命中即丢弃。
5. 每条记忆带来源与时间：`source = {conversation_id, run_id}`、`created_at`、`updated_at`、`expires_at`。冲突时新内容覆盖旧内容，旧内容保留为上一版（`previous`）。
6. 写入在回答之后后台进行，不阻塞回答；读取时最多注入 5 条，回答里用到时说明来源（“按你之前说的不要香菜”）。
7. **用户能看、能删**：`GET /memory` 列出全部；`DELETE /memory/{id}` 删一条；`DELETE /memory` 全部删除。删除立即生效（存储与向量索引同时删），之后不得再被检索到。删除会话时同时删除它的检查点与事件日志。

### 契约

```python
class MemoryItem(Contract):
    id: str
    kind: Literal["profile", "preferences", "facts"]
    text: str = Field(max_length=200)
    confidence: float = Field(ge=0.8, le=1)
    source: dict[str, str]                 # {"conversation_id": …, "run_id": …}
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    previous: str | None = None            # 被覆盖前的上一版
```

### 默认值

裁剪 12 条 · 摘要触发 2000 token · 摘要 ≤ 200 字 · 写入置信度 ≥ 0.8 · 每次注入 ≤ 5 条 · 记忆有效期 180 天 · 会话保留 30 天。

### 依据

- 〔E30 LangGraph 记忆〕短期记忆按线程由 checkpointer 持久化，长期记忆跨线程按自定义命名空间存储；完整历史可能超出上下文导致无法恢复的错误，过时内容也会分散模型；记忆分语义、情景、程序三类；热路径写入增加时延，后台写入不影响主流程；内存 Store 只用于开发。
- 〔E31 LangGraph Persistence〕checkpointer 与 Store 两套持久化；内存实现重启即丢；检查点要定期清理。
- 〔E17 Anthropic 上下文工程〕上下文越长回忆越差，要当作有限资源；压缩、工具结果清理是最安全的轻量压缩方式。
- 〔E34 Mem0 概览〕写入时把对话提炼为事实并关联实体，召回时只返回最相关的记忆；按用户、智能体、应用、运行划分作用域；默认带审计日志。
- 〔E38 OWASP LLM02 敏感信息泄露〕个人信息、财务信息等不得经模型泄露。

## 9 知识库（RAG）

### 规则

入库：

1. **流水线**：解析 → 清洗（去页眉页脚、表格转文本）→ 分块（按标题与语义边界，300–500 token，重叠 50，标题路径作前缀）→ **上下文前缀**（用小模型结合全文为每块生成 50–100 token 的说明，拼在块前，同时用于向量与 BM25 索引；生成时对全文用提示缓存）→ 向量化 → 写入 pgvector（或 Qdrant）。
2. **元数据必填**：`tenant_id`、`store_ids`（空 = 全部门店）、`acl_roles`、`doc_id`、`version`、`title_path`、`source`（文档名与链接）、`valid_from` / `valid_to`、`reviewed_by`、`reviewed_at`。
3. **入库审核**：文档经后台审核才生效（防投毒）；每次保存产生新版本，旧版本失效但保留 7 天可回滚；过了 `valid_to` 自动不再被检索。
4. 价格、库存、订单状态等实时数据不进知识库，只走工具。
5. 知识库全部内容 < 20 万 token 且稳定时，可以不做检索，整体放进提示词并用提示缓存。

检索：

6. **混合检索**：向量与 BM25 两路并行各召回 20 条 → RRF 融合（`k` 显式设为 60）→ 可选重排（交叉编码器）→ 取前 6。不要把原始向量分与 BM25 分直接线性相加。
7. **按权限在查询内过滤**：门店、角色、有效期作为查询条件（预过滤、分区或部分索引），不得先检索再在应用里过滤；多租户用分区或独立表，避免一个租户的数据影响另一个租户的召回。
8. **阈值**：最高相关度低于阈值视为“没有找到”；阈值按嵌入模型在评测集上校准，未校准前用 0.35。

回答：

9. **必须带引用**：规则类回答带文档名、小节路径与片段 id；引用必须真实存在于本轮检索结果（§10 核验）。检索不到时明确说“知识库里没有找到相关规定”，给联系门店或页面入口，不编造。
10. 片段进入提示时放在数据分隔块里，声明为数据不是指令。
11. 结果缓存：同一租户、同一归一化问题 10 分钟；文档更新时清缓存。

pgvector 落地要点：用 HNSW 索引（无需训练、查询快）；只有 `ORDER BY 距离 ASC LIMIT` 才会用上索引；近似索引的过滤发生在扫描之后，过滤条件选择性高时开迭代扫描（0.8 起）或用部分索引、分区；归一化向量用内积；建索引用 `CONCURRENTLY`；定期把近似结果与精确结果比对监控召回。

### 契约

```python
class KnowledgeChunk(Contract):
    tenant_id: str
    store_ids: list[str] = []              # 空 = 全部门店
    acl_roles: list[str] = []              # 空 = 所有可用知识库的角色
    doc_id: str
    version: int = Field(ge=1)
    chunk_id: str
    title_path: str                        # 文档名 › 小节
    context: str = Field(max_length=400)   # 上下文前缀
    text: str
    source_url: str | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    reviewed_by: str
    reviewed_at: datetime

class Citation(Contract):
    doc_id: str
    version: int
    chunk_id: str
    title_path: str

class RetrievedContext(Contract):
    chunks: list[KnowledgeChunk] = []
    scores: list[float] = []
    hit: bool                              # False → 回答“没有找到”
```

### 默认值

分块 300–500 token、重叠 50 · 上下文前缀 50–100 token · 两路各 20 条 · RRF `k = 60` · 取前 6 · 阈值 0.35（待校准）· 缓存 10 分钟 · 旧版本保留 7 天 · 评测：每租户 ≥ 30 条问答，召回@6 ≥ 0.85。检索超时见 §16。

### 依据

- 〔E18 Anthropic 上下文检索〕给每块加 50–100 token 的上下文说明后再做向量与 BM25 索引；检索失败率：上下文向量降 35%，再加上下文 BM25 降 49%，再加重排降 67%；送入 20 个片段效果最好；重排从前 150 条选前 20 条，会增加时延；知识库小于 20 万 token 可直接放进提示词，配合提示缓存。
- 〔E35 pgvector〕HNSW 与 IVFFlat 的取舍与默认参数；近似索引的过滤在扫描之后；迭代扫描；多租户共享索引会互相影响召回，用分区；可与 Postgres 全文检索组合，用 RRF 或交叉编码器融合。
- 〔E36 Qdrant 混合查询〕预取 + 融合；RRF 只看排名，Qdrant 默认 `k=2`，要显式设置；预取数量至少为主查询的 limit + offset；不要线性混合未归一化的分数；没有评测集时权重保持默认。
- 〔E38 OWASP LLM04 数据与模型投毒〕〔E38 LLM08 向量与嵌入弱点〕入库数据可被投毒；RAG 系统要按权限隔离检索。
- 〔E39 OWASP LLM01〕RAG 不能消除注入，片段要作为不可信内容隔离。

## 10 输出护栏与人工审批

### 规则

输出护栏：

1. 最终回答先过 `FinalAnswer` 校验，再过确定性核验（代码实现）：数字落地（金额、订单号、时间、数量能在本轮工具结果或引用中逐字找到，金额按分比对）、实体落地、引用存在、失败状态一致（工具失败不得说成功）、禁止承诺词、长度、至少一个页面入口。语义类检查（越界话题、承诺）用小模型判定，模型不可用时只做规则检查并记降级。规则细则见 `agent/backend/SPEC.md` §9。
2. 不通过 → 带核验反馈重生成 1 次 → 仍不通过 → 模板回答（只含工具结果里的事实 + 页面入口）。
3. 回答与卡片只能含当前用户的数据；手机号、地址等按规则遮蔽。
4. **模型输出一律当作不可信输入**：前端渲染前转义，Markdown 渲染禁用原始 HTML 与脚本链接；模型文本不得直接拼进 SQL、命令、URL、文件路径，只能作为参数经校验后使用。
5. 被拦下的输出用固定文案替换，文案里不含敏感信息。

人工审批：

6. `write_high` 工具一律先审批：先调只读预览接口拿到服务端事实（金额、数量、地址），组装 `ApprovalRequest`，再 `interrupt()`；界面显示的数字只来自预览，不来自模型文本。
7. `interrupt()` 之前不得有写操作（恢复时节点从头重跑）；不要用 `try/except` 包住 `interrupt()`；一个节点只调用一次 `interrupt()`，不放进循环；恢复用 `Command(resume=…)`，恢复值用 Pydantic 校验。
8. 审批有效期 10 分钟；过期、拒绝、预览与执行不一致（价格或库存变了）都不执行；过期自动取消并通知（`approval.resolved`，`result: "expired"`）；一个会话同时最多一个待审批，新输入到达时旧审批作废并记审计。
9. 审批等待单独计时，不计入执行耗时与整轮时长上限。
10. 审批通过后带幂等键执行，重复审批不重复执行；每次 `write_high` 执行写审计（租户、用户、会话、运行、工具、幂等键、审批 id、时间、结果）。

### 契约

```python
class QuickReply(Contract):
    label: str = Field(max_length=12)
    send_text: str | None = None
    route: str | None = None

class FinalAnswer(Contract):
    text: str = Field(max_length=1500)
    cards: list[dict[str, Any]] = []       # 卡片类型与字段见 agent/shared/protocol.md §4
    quick_replies: list[QuickReply] = Field(default=[], max_length=4)
    citations: list[Citation] = []
    degraded: list[str] = []
    partial: bool = False

class ApprovalRequest(Contract):            # approval.required
    approval_id: str
    action: str                             # 工具名，例如 create_order
    summary: str = Field(max_length=200)    # 服务端模板，数字来自预览接口
    facts: dict[str, Any]                   # 结构化展示；金额以分为单位
    amount_cents: int | None = None
    danger: bool = False                    # 取消、删除类
    expires_at: datetime
    idempotency_key: str

class ApprovalDecision(Contract):           # POST /runs/{run_id}/approvals/{approval_id}
    approved: bool
    client_message_id: str

class ApprovalResolved(Contract):           # approval.resolved
    approval_id: str
    result: Literal["approved", "rejected", "expired", "superseded"]
    wait_ms: int

class VerifyResult(Contract):
    passed: bool
    violations: list[dict[str, str]] = []   # {rule, detail}
```

### 默认值

`APPROVAL_TTL_S = 600` · 核验不通过重生成 1 次 · 回答长度：小程序与 H5 ≤ 600 字，后台 ≤ 1500 字 · 每个回答至少 1 个页面入口。

### 依据

- 〔E27 OpenAI Agents SDK 护栏〕输出护栏在智能体完成后运行；被拦下的输出替换为固定文案，文案不得含敏感数据；工具输入护栏可在审批前后各跑一次。
- 〔E32 LangGraph 中断〕中断时保存状态并无限等待；恢复必须用同一个 `thread_id`；`Command(resume=…)` 的值成为 `interrupt` 的返回值；恢复时节点从头执行，中断前的代码会再跑一遍，副作用应幂等或放在中断之后；不要用 `try/except` 包住中断；多个中断按顺序匹配，不要在循环里中断；可用 Pydantic 校验恢复值。
- 〔E39 OWASP LLM01〕高风险动作人工批准；用确定性代码校验输出格式。
- 〔E38 OWASP LLM05 不当输出处理、LLM06 过度授权、LLM09 错误信息〕模型输出要校验、清洗后再使用；工具权限最小化；回答要有依据。
- 〔E19 Anthropic 多智能体研究系统〕用单独的引用环节核对每条结论的出处。

## 11 可观测与评测

### 规则

可观测：

1. 每次运行一条 trace（`trace_id`），`group_id = conversation_id`；span 类型固定：`run`、`queue`、`guardrail`、`intent`、`retrieve`、`llm`（模型、输入 / 输出 / 缓存读取 token、首 token 时延、耗时、是否回退）、`tool`（名称、参数摘要、状态、重试次数、耗时）、`approval`、`verify`、`memorize`。属性名采用 OpenTelemetry 生成式 AI 语义约定（本次只采到“已迁移”提示页，落地时到其新仓库核对属性名）。
2. 默认不记录模型输入输出与工具参数原文，只记长度、摘要、错误码；排障时按租户短时打开并自动过期。脱敏和导出放在同一个导出器里，脱敏失败就丢弃这一批，只记一条不含内容的日志。
3. 日志每行带 `request_id`、`run_id`、`trace_id`；不记录密钥、令牌、完整手机号。
4. 线上指标：运行成功率、按错误码的错误率、超时率、取消率、首事件时延、首 token 时延、整轮时长（平均与 P95）、队列深度与等待时间、熔断状态变化、回退率、追问率、审批通过率、点踩率、每次运行 token 与费用、提示缓存命中率。
5. 告警：错误率 > 5%（5 分钟）、P95 超过目标 2 倍、熔断打开、回退率 > 10%、排队等待 P95 > 30 秒、费用达到预算 80%。

评测：

6. 四类评测集，每类有通过线：**意图识别**（每个启用意图 ≥ 20 条，含口语、错字、指代、多意图）、**工具选择与参数**（含“不该调用工具”的样例）、**引用正确率**（引用存在且支持结论）、**拒答正确率**（该拒答、该说“没有找到”的问题）；另有**对抗集**（直接与间接注入、越权、编造诱导、循环诱导）。
7. **回归门槛**：改提示词、换模型或模型版本、改工具（含描述）、改意图表、改契约、改检索参数，都必须在合并前跑离线评测；任一指标低于通过线，或比上一版本下降超过 2 个百分点，不得发布。
8. **怎么判**：能确定的用代码判（意图、工具名、参数、引用 id、是否拒答）；开放回答用单次调用的模型评审打 0–1 分并给通过 / 不通过（标准：事实、引用、完整、工具效率）；定期人工抽检；会改变状态的流程按最终状态判，不逐步比对。
9. 线上失败（点踩、核验失败、转人工）定期回流成评测样例；评测从约 20 条代表性样例就开始迭代，不等大数据集；发布用新旧版本并行、逐步切流。

### 默认值

| 指标 | 通过线 |
|---|---|
| 意图准确率 | ≥ 0.90 |
| 工具选择与参数准确率 | ≥ 0.90 |
| 引用正确率 | ≥ 0.95 |
| 拒答正确率 | ≥ 0.95 |
| 知识库召回@6 | ≥ 0.85 |
| 注入成功、未审批即执行高风险操作、数字落地违规 | 0 |
| 相对上一版本的下降 | ≤ 2 个百分点 |

### 依据

- 〔E29 OpenAI Agents SDK 追踪〕trace 带 `trace_id` 与 `group_id`（关联同一会话），span 分模型生成、函数调用、护栏、交接等类型；默认会记录模型与工具的输入输出，可关闭；脱敏处理器失败时不能阻止其他导出器拿到数据，所以脱敏与导出放在同一个导出器里，失败就丢弃这一批。
- 〔E44 LangSmith 评测〕离线评测在开发中比较版本、发现回归，在线评测看真实流量；把真实失败加进数据集；评审方式有人工、代码规则、模型评审、成对比较。
- 〔E19 Anthropic 多智能体研究系统〕约 20 条查询就足以开始；单次调用的模型评审输出 0–1 分加通过 / 不通过最稳定；会改状态的智能体看最终状态；生产全链路追踪但不监看对话内容；新旧版本并行逐步切流。
- 〔E16 Anthropic 为智能体写好工具〕评测任务应接近真实、可能需要多次工具调用；记录工具调用耗时、次数、token 与工具错误；用留出集防止过拟合。
- 〔E45 / E46 OpenTelemetry 生成式 AI 语义约定〕两页都是“已迁移”提示页，没有采到属性名。

## 12 性能

### 规则

1. **时延目标**：首个事件 ≤ 1 秒（P95）；首个文字增量 P95 ≤ 3 秒。超过 1 秒界面显示等待，超过 10 秒显示阶段进度、已用时间和预计剩余（界面由 Luz-System 定义，服务端提供 `stage` / `step` 事件与 `eta_s`）。
2. **提示缓存**：提示按“工具定义 → 系统提示 → 静态知识与示例 → 会话历史 → 本轮输入”的固定顺序拼装；缓存断点放在最后一个每次都不变的块上；时间戳、用户特有信息、本轮输入放在断点之后；工具定义与顺序保持稳定（改工具定义会让整段缓存失效），工具参数的键顺序稳定；短于模型最低缓存长度的前缀不会被缓存，也不会报错；监控缓存命中。
3. **并行**：记忆读取与意图分类并行；知识库两路召回并行；同一步内互不依赖的只读工具并发执行，结果按调用顺序回传。
4. **模型分层**：小模型做路由、分类、护栏、核验、摘要；主模型只做规划与回答。
5. **上下文预算**：每次运行 token 上限（§16）；只放高信号内容（当前意图允许的工具、必要的记忆与片段、裁剪后的历史）；工具结果截断。
6. **结果缓存**：知识检索 10 分钟；同一运行内同名同参的只读工具调用复用结果。
7. 只流 `respond` 节点的文字；内部模型调用不流出。

### 默认值

首事件 ≤ 1 秒 · 首个文字增量 ≤ 3 秒（P95 目标）· 提示缓存默认 5 分钟有效（Claude；可选 1 小时）· 并行工具调用每步 ≤ 3（§16）。

### 依据

- 〔E12 / E13 NN/g〕0.1 秒、1 秒、10 秒三个界限；超过 1 秒给反馈，10 秒以上给预计完成时间。
- 〔E21 Claude 提示缓存〕缓存按工具、系统、消息的顺序覆盖整个前缀，改一层会让它及之后全部失效，改工具定义使整个缓存失效；默认 5 分钟、可选 1 小时；最多 4 个断点；断点放在最后一个保持不变的块上，时间戳等放在断点后；低于最低长度的提示静默不缓存；工具调用的键顺序要稳定；缓存读取价格约为基础输入的十分之一。
- 〔E05 Claude Rate limits〕多数模型的缓存读取不计入每分钟输入 token 限额。
- 〔E15 Anthropic 构建有效的智能体〕路由可把简单问题交给小模型以降低成本和时延。
- 〔E19 Anthropic 多智能体研究系统〕并行子任务与并行工具调用把复杂任务的耗时缩短最多 90%。
- 〔E14 LangGraph Streaming〕`nostream` 标签让内部调用照常执行但不输出 token。

## 13 安全：OWASP 大模型应用十大风险逐项落实

| 风险（2025） | 底座措施 | 所在环节 | 怎么验证 |
|---|---|---|---|
| LLM01 提示注入 | 直接注入检测；外部内容（工具结果、片段、记忆、文件）一律放数据块并声明不是指令；可疑时只读模式；高风险动作审批；对抗评测 | §3、§6、§9、§10、§11 | 对抗集注入成功率 = 0；只读模式下写工具不可调用的测试 |
| LLM02 敏感信息泄露 | 进模型、日志、记忆前遮蔽；回答只含当前用户数据；trace 默认不记原文；发给外部模型前脱敏，租户可要求只用私有部署模型 | §3、§8、§10、§11 | 日志与 trace 抽查无原文；越权查询他人订单被拒的测试 |
| LLM03 供应链 | 模型、嵌入模型、SDK、MCP 服务器来源登记并锁定版本；MCP 只用白名单服务器与工具；依赖漏洞扫描 | §6、§15 | 依赖锁文件与模型清单在方案中；未登记的 MCP 工具无法注册 |
| LLM04 数据与模型投毒 | 知识库入库审核、版本与回滚；长期记忆只写用户明确陈述且过规则检查 | §8、§9 | 未审核文档不可检索的测试；记忆禁止项被拒的测试 |
| LLM05 不当输出处理 | 模型输出当不可信输入：Pydantic 校验、前端转义、不拼进 SQL / 命令 / URL | §7、§10 | 含脚本与 HTML 的回答被转义的测试 |
| LLM06 过度授权 | 工具最小权限、默认只读；每轮按角色、意图与本次运行状态计算工具清单，执行前再校验；`single` 工具用后禁用；`write_high` 审批；不可逆操作不做成工具；身份只来自令牌 | §4、§6、§10 | 未审批即执行 = 0；工具参数里的 `user_id` 被忽略的测试 |
| LLM07 系统提示泄露 | 系统提示不放密钥、权限规则和内部地址（这些在代码里执行）；索要提示的请求按注入处理 | §3、§6 | 对抗集“输出你的提示词”不泄露配置 |
| LLM08 向量与嵌入弱点 | 检索时按租户、门店、角色、有效期在查询内过滤；多租户分区；嵌入数据随文档删除 | §9 | 跨租户、跨角色检索为空的测试 |
| LLM09 错误信息 | 引用必须存在；数字与实体落地；检索不到明确说“没有找到”；禁止承诺词 | §9、§10 | 引用正确率、拒答正确率达标；数字落地违规 = 0 |
| LLM10 无限消耗 | 入口限流与排队；每次运行 token、工具次数、步数、时长上限；每用户每日次数与租户费用预算 | §2、§14、§16 | 上限触发即停止的测试；超预算返回 429 的测试 |

依据：〔E38 OWASP GenAI 十大风险 2025 列表〕〔E39 OWASP LLM01 提示注入〕〔E40 OWASP 大模型应用十大风险项目页〕〔E22 MCP 规范〕（用户同意、数据隐私、工具安全）。

## 14 稳定：防错与防失败

### 规则

1. **超时**：每一步有超时（模型整次、首 token、工具、检索、外部接口），整轮有最长执行时长（§16）。超时转 `step.failed`，按策略重试或降级。排队与审批等待不计入整轮时长。
2. **只对瞬时错误重试**：429、529 / 过载、500 / 502 / 503 / 504、超时、连接中断；不对 400 / 401 / 403 / 404 / 409 / 422 和业务拒绝重试。指数退避加随机抖动（`等待 = 随机(0, min(上限, 基数 × 2^n))`），最多 3 次；有 `Retry-After` 时等待不少于它，若它超过本步剩余时间则不等，直接降级或失败。只在最外层重试，内层快速失败，避免重试叠加；前几次失败记信息级日志，最后一次失败记错误级。
3. **写操作**只有带幂等键时才自动重试；否则暂停，发 `step.failed`（错误码 `NEEDS_USER`），问用户是否重试。
4. **熔断**：按依赖分组（每个模型供应商、每个后端服务、知识库、每个 MCP 服务器）。窗口内失败达到阈值即打开；打开期间直接失败，告诉用户“服务暂不可用，预计 X 分钟后恢复”，对外接口返回 `503` + `Retry-After`；到时进入半开，放少量探测请求，成功则关闭，失败则重新打开并延长打开时间。上游返回带较长 `retry-after` 的 429 时可立即打开。状态变化发事件与告警；提供手动强制打开 / 关闭。
5. **降级**（梯度 L0–L5，用户文案见 `agent/backend/SPEC.md` §8.2）：知识库不可用 → 跳过检索并说明；主模型过载或不可用 → 换备用模型，或排队稍后自动续跑；全部模型不可用 → 规则意图 + 模板回答 + 只读工具按钮；写入关闭 → 给页面入口；停用 → 维护卡片。工具不可用时说明影响并给替代做法。降级结果不得伪装成成功。
6. **断点续跑**：每个节点后落检查点；断线、刷新、服务重启后从最近检查点继续，已完成的步骤与已执行的写操作不重做（幂等键保证）。服务启动时扫描“运行中”的运行，能恢复的恢复，不能恢复的标记为失败且可重试。
7. **保留部分结果**：失败时保留已完成步骤和已生成文字，`run.failed` 带 `partial: true` 与 `resume_from`；界面标“未完成”，提供“从失败处重试”（`POST /runs/{run_id}/retry`，从检查点继续，不重发消息）。
8. **取消**：用户随时可停止（`POST /runs/{run_id}/cancel`）。服务端停止后续步骤，取消进行中的模型与工具调用（不能取消的等它结束后丢弃结果）；未提交的写操作回滚（单个写操作在一个后端事务内完成，跨服务的多步写有补偿动作）；发 `run.cancelled`（含已完成部分）。
9. **上限**：每次运行 token、工具调用次数、图步数、同名同参重复调用、整轮时长、每用户每日运行数、租户每日费用（§16）。触发即停止并说明，不静默截断。
10. **审批超时**自动取消并通知（§10）。
11. **错误呈现**：说清是什么错误、对用户有什么影响、可以做什么（重试、等待、联系管理员），附可复制的 `request_id`；不暴露堆栈、内部地址和供应商原始报错。

### 契约

错误码（v3，`RunError.code` 与 HTTP 错误体共用；替代 `agent/backend/SPEC.md` §8.3）：

| code | HTTP / 事件 | 可重试 | 含义 | 用户可以做什么 |
|---|---|---|---|---|
| `VALIDATION_FAILED` | 422 | 否 | 输入不合规 | 按提示修改后再发 |
| `IDEMPOTENCY_KEY_REUSED` | 409 | 否 | 同一 `client_message_id` 内容不同 | 作为新消息发送 |
| `QUEUE_FULL` | 429 + `Retry-After` | 是 | 队列满或预计等待超上限 | 稍后自动重试或取消 |
| `RATE_LIMITED` | 429 + `Retry-After` | 是 | 用户或租户限流 | 等待后重试 |
| `BUDGET_EXCEEDED` | 429 | 否 | 每日次数或费用用完 | 次日再试或联系管理员 |
| `QUEUE_TIMEOUT` | `run.failed` | 是 | 排队超时 | 重试 |
| `MODEL_UNAVAILABLE` | `run.failed` / 503 | 是 | 模型回退链全部失败或熔断中 | 等待预计时间后重试 |
| `DEPENDENCY_UNAVAILABLE` | `step.failed` / 503 + `Retry-After` | 是 | 后端、知识库或 MCP 熔断中 | 等待，或用页面入口 |
| `TOOL_TIMEOUT` | `step.failed` | 是 | 关键工具超时且无法降级 | 重试或去页面查看 |
| `NEEDS_USER` | `step.failed` | — | 写操作失败且没有幂等键 | 确认后重试 |
| `APPROVAL_EXPIRED` | 410 / `approval.resolved` | 否 | 审批过期 | 重新发起 |
| `APPROVAL_MISMATCH` | 409 | 否 | 审批 id 不是当前待审批 | 刷新会话 |
| `STEP_LIMIT` | `run.failed` | 否 | 决策轮次、工具次数、步数或重复调用达到上限 | 换个说法或分步说明 |
| `TOKEN_LIMIT` | `run.failed` | 否 | 本次运行 token 用完 | 缩短问题后重试 |
| `RUN_TIMEOUT` | `run.failed` | 是 | 整轮执行超时 | 从失败处重试 |
| `UNAUTHORIZED` | 401 | 否 | 令牌无效或过期 | 重新登录 |
| `FORBIDDEN` | 403 | 否 | 无权限 | 联系管理员 |
| `DISABLED` | 503 | 否 | 助手停用 | 稍后再来 |
| `INTERNAL` | 500 / `run.failed` | 是 | 未分类错误 | 重试；复制请求编号联系管理员 |

### 默认值

全部超时、重试、熔断与上限见 §16。

### 依据

- 〔E07 Azure 重试模式〕非瞬时故障直接取消并报告；退避逐步或指数增加，并分散多个实例的重试时间；交互式应用少次数、短延迟，失败后提示稍后再试；激进重试会进一步拖垮繁忙服务；非幂等操作重试可能执行两次（如重复扣款）；嵌套重试会叠加延迟，内层应快速失败；与熔断配合。
- 〔E08 Azure 熔断模式〕关闭、打开、半开三态；失败计数按时间窗口；打开时立即失败，可返回有意义的默认值；半开放少量请求，失败则重新打开；打开时间可逐步加长；429 / 503 可带预计恢复时间；按独立依赖分别设熔断器；状态变化发事件，提供手动覆盖。
- 〔E04 Claude API errors〕529 过载、500 用指数退避重试；SDK 默认重试两次并遵守 `retry-after`；200 之后仍可能出错。〔E03 Claude Streaming〕流中断后可从最近的文本块续写，工具调用块无法部分恢复。
- 〔E05 Claude Rate limits〕429 带 `retry-after` 时按它等待。〔E10 MDN Retry-After〕503 时表示预计停机时长。
- 〔E09 Stripe 幂等请求〕带幂等键的写请求可以安全重复。
- 〔E33 LangGraph Graph API〕〔E31 LangGraph Persistence〕〔E32 LangGraph 中断〕检查点、节点重跑与幂等、恢复。
- 〔E19 Anthropic 多智能体研究系统〕不能出错就从头重来，要从出错处恢复；用重试与定期检查点等确定性保障；告诉模型工具在失败，让它调整，效果很好。
- 〔E15 Anthropic 构建有效的智能体〕智能体要有最大迭代次数等停止条件。〔E38 OWASP LLM10 无限消耗〕。

## 15 代码结构（对应现有 agent/ 编号目录）

技能内的参考实现在 `agent/` 下按编号分模块，每个模块 `NN-名称/code/<包名>/`，测试放 `NN-名称/tests/`；项目落地时整体复制到 `services/agent/`，目录不变。

| 目录 | 职责 | 现状 | 待补（详见 `agent/README.md` 差距表） |
|---|---|---|---|
| `00-core` | 契约（Pydantic 模型）、配置、错误码 | 已有：模型、配置、v2 错误码 | v3 事件与错误码、`ToolSpec`、`IntentSpec`、契约基类改 `extra="forbid"`、契约导出 |
| `01-app` | 接入层：运行接口、SSE、事件日志与续传、心跳、排队与并发上限、幂等、取消、续跑 | 缺 | 全部 |
| `02-guardrails` | 输入护栏、输出遮蔽与转义、注入检测 | 缺（零散规则在 `03-intent`、`04-memory`、`11-observability`） | 全部 |
| `03-intent` | 意图表、三级识别、阈值、追问 | 已有：规则预筛、模型分类、阈值、追问模板 | 意图表配置化、语义路由层、`needs_clarification` 与候选、`< 0.5` 不猜 |
| `04-memory` | 裁剪、摘要、长期记忆、查看与删除 | 已有：裁剪、摘要、Store 写入合并、列出、删除 | 上一版保留、来源记录、删除同步向量索引、接口接线 |
| `05-knowledge` | 入库、混合检索、引用 | 已有：分块、内存 BM25、RRF、阈值、引用、缓存 | 上下文前缀、重排、权限预过滤、两路并行、pgvector 实现、入库审核 |
| `06-tools` | 工具注册表、工具契约、每个工具一个模块、MCP 客户端、幂等键 | 缺（v1 注册表在 `backend/code/agent_graph/tools.py`） | 全部 |
| `07-graph` | 编排图、状态、节点、检查点、审批中断 | 缺（v1 四节点图在 `backend/code/agent_graph/graph.py`） | 全部，接入 00–12 各模块 |
| `08-resilience` | 超时、重试、熔断、回退链、预算、降级梯度 | 已有 | 遵守 `Retry-After`、全抖动、529 识别、打开时间递增、整轮时长与步数上限 |
| `09-verify` | 输出核验（数字、实体、引用、承诺、入口）与模板回答 | 已有 | 引用存在性对照 v3 `Citation`；与 `02-guardrails` 的遮蔽、转义衔接 |
| `10-llm` | 模型适配：各家工具格式、结构化输出、流式、提示缓存、模型分层 | 部分：OpenAI 兼容非流式、结构化输出 | Claude 格式、并行调用、流式参数拼接、缓存断点、重试次数改为 1 |
| `11-observability` | trace / span、指标、脱敏日志 | 部分：进程内指标、节点计时、脱敏事件日志 | OpenTelemetry 导出、费用与 P95、首事件时延、队列指标 |
| `12-evals` | 评测集与回归脚本、CI 门槛 | 缺 | 全部 |
| `backend/` | 门店业务助手的应用规范（`SPEC.md` v2）与 v1 参考图 | 已有 | v1 代码迁入 `06-tools`、`07-graph` 后只保留规范 |
| `shared/protocol.md` | 前后端协议 v3 | 已有 | — |
| `requirements/_template.md` | 项目定制清单（填好即项目的 AGENT.md） | 已有 | — |
| `config/`（项目内） | `intents.yaml`、`tools.yaml`、上限覆盖 | 项目生成 | 由 `requirements/<项目>.md` 生成并用契约校验 |
| `contracts/`（生成） | 契约 JSON Schema，供前端与评测使用 | 缺 | 由 `00-core` 导出 |

依赖方向：`00-core` 不依赖任何模块；`01-app` 只依赖 `07-graph` 与 `00-core`；其余模块只依赖 `00-core`、`08-resilience`、`10-llm`、`11-observability`；业务模块之间不互相导入。

## 16 上限与超时（总表）

数值是 Luz 默认值；依据说明原则来源，具体数值按项目的模型配额与评测结果调整，并写进技术方案。表中的配置键都写在运行配置文件里（模块详设 config、OPS-005、`references/config-logging.md`），不在后台展示；改文件经评审合并后服务端热加载：超时、重试与上限即时生效，并发数与队列长度这类需要重建连接池的标为重启生效。

| 类别 | 项 | 默认 | 配置键 | 触发后 | 依据 |
|---|---|---|---|---|---|
| 接入 | 首个事件 | ≤ 1 秒（P95） | — | 界面先显示“已收到” | E12 |
| 接入 | 代理读超时 | ≥ 60 秒（大于心跳） | 网关配置 | 连接被断 | E11 |
| 排队 | 全局并发运行 | 每实例 32（按模型配额调） | `AGENT_MAX_RUNNING` | 入队 | E06 |
| 排队 | 每用户并发运行 | 2 | `USER_MAX_RUNNING` | 入队 | E06 |
| 排队 | 每会话并发运行 | 1（固定） | — | 会话内排队 | — |
| 排队 | 每会话待发送 | 3 | `CONVERSATION_PENDING_MAX` | 429 `QUEUE_FULL` | E06 |
| 排队 | 队列长度 | 200 | `QUEUE_MAX` | 429 `QUEUE_FULL` + `Retry-After` | E06、E10 |
| 排队 | 预计等待上限 | 60 秒 | `QUEUE_MAX_WAIT_S` | 429 `QUEUE_FULL` + `Retry-After` | E06、E12 |
| 排队 | 最长排队 | 120 秒 | `QUEUE_TIMEOUT_S` | `run.failed` `QUEUE_TIMEOUT` | E06 |
| 限流 | 每用户每分钟运行 | 20 | `USER_RUNS_PER_MIN` | 429 `RATE_LIMITED` | E05 |
| 预算 | 每用户每日运行 | 200 | `USER_DAILY_TURNS` | 429 `BUDGET_EXCEEDED` | E38 LLM10 |
| 预算 | 租户每日费用 | 项目指定 | `TENANT_DAILY_BUDGET_CENTS` | 80% 告警，100% 429 | E38 LLM10 |
| 超时 | 模型整次调用 | 20 秒 | `LLM_TIMEOUT_S` | 重试或回退 | E04 |
| 超时 | 模型首 token | 8 秒 | `LLM_FIRST_TOKEN_TIMEOUT_S` | 切备用模型 | E04 |
| 超时 | 小模型（路由、护栏、核验） | 3 秒 | `SMALL_LLM_TIMEOUT_S` | 规则兜底 | E15 |
| 超时 | 只读工具 / 写工具 / 批量工具 | 3 / 5 / 8 秒 | 工具契约 `timeout_s` | 重试或 `TOOL_TIMEOUT` | E41 |
| 超时 | 知识检索 | 1.5 秒 | `KB_TIMEOUT_S` | 跳过检索（L1） | E08 |
| 超时 | MCP 调用 | 5 秒 | 工具契约 `timeout_s` | 重试或降级 | E22 |
| 超时 | 整轮执行（不含排队与审批） | 120 秒 | `RUN_TIMEOUT_S` | `run.failed` `RUN_TIMEOUT`，保留部分结果 | E15 |
| 超时 | 审批有效期 | 600 秒 | `CONFIRM_TTL_S` | 自动取消并通知 | E32 |
| 重试 | 自动重试上限 | 3 次 | `MAX_RETRIES` | 降级或失败 | E07、E41 |
| 重试 | 退避 | 基数 0.5 秒、×2、上限 8 秒、全抖动 | `RETRY_BASE_S`、`RETRY_CAP_S` | — | E07 |
| 重试 | 模型 | 2 次后回退备用模型 | `LLM_MAX_RETRIES` | 回退链 | E04 |
| 重试 | 写工具 | 有幂等键 3 次；无幂等键 0 次 | 工具契约 | `NEEDS_USER` | E07、E09 |
| 熔断 | 打开条件 | 60 秒内 ≥ 10 次且失败率 ≥ 50% | `BREAKER_WINDOW_S`、`BREAKER_MIN_FAILS`、`BREAKER_FAIL_RATE` | 打开 | E08 |
| 熔断 | 打开时长 | 30 秒，再次失败翻倍，最长 300 秒 | `BREAKER_OPEN_S`、`BREAKER_OPEN_MAX_S` | 半开 | E08 |
| 熔断 | 半开探测 | 3 个请求 | `BREAKER_HALF_OPEN_PROBES` | 关闭或重新打开 | E08 |
| 上限 | 每次运行决策轮次 | 6 | `MAX_ROUNDS` | `run.failed` `STEP_LIMIT`，保留部分结果，不让模型收尾 | E15、E51 |
| 上限 | 每次运行工具调用 | 5 | `MAX_TOOL_CALLS` | `STEP_LIMIT` | E15 |
| 上限 | 单一结果工具 | 每次运行成功 1 次 | 工具契约 `result_kind` | 移出工具清单，再调返回 `TOOL_NOT_AVAILABLE` | E54 |
| 上限 | 多种结果工具调用 | 每次运行 3 次（≤ 5） | 工具契约 `max_calls_per_run` | 返回 `TOOL_NOT_AVAILABLE` | E16 |
| 上限 | 每次运行图步数 | 25（即 `recursion_limit`） | `MAX_GRAPH_STEPS` | `STEP_LIMIT` | E33 |
| 上限 | 同名同参重复调用 | 3 次即停止 | `MAX_SAME_CALL` | `STEP_LIMIT` | E15 |
| 上限 | 每步并行工具调用 | 3 | `MAX_PARALLEL_TOOLS` | 超出部分顺序执行 | E41 |
| 上限 | 每次运行 token（不含缓存读取） | 8000 | `MAX_TURN_TOKENS` | `TOKEN_LIMIT` | E38 LLM10 |
| 上限 | 工具结果 | 2000 字符、列表 10 项 | `TOOL_RESULT_MAX_CHARS` | 截断并说明 | E16 |
| 上限 | 参数自我纠正 | 每工具每运行 2 次 | — | 计入工具调用 | E16 |
| 上限 | 结构化输出重试 | 1 次 | `STRUCTURED_RETRIES` | 降级 | E25 |
| 上限 | 核验重生成 | 1 次 | — | 模板回答 | — |
| 上限 | 追问轮数 | 2 | `MAX_CLARIFY_ROUNDS` | 给页面入口 | — |
| 上限 | 工具数量 | 每智能体 ≤ 20，每轮暴露 ≤ 5 | — | 注册失败 | E41、E16 |
| 保留 | 事件日志 / 幂等记录 | 24 小时 | `EVENT_LOG_TTL_S`、`DEDUP_TTL_S` | 过期后按历史接口取结果 | E09 |
| 保留 | 会话、检查点 | 30 天 | `CONVERSATION_TTL_S` | 清理 | E31 |

## 17 底座检查清单

`agent` 类型的技术方案自动带入 `references/standards.json` 的 `AGENT-*` 规则作为核对表（`tech_plan.py check NN`）；下表是逐项核对方式。只有附了测试名或实测记录的条目才能标为完成，未做的在交付时列为缺口。

| # | 检查项 | 怎么核对 | 规则 |
|---|---|---|---|
| F01 | 智能体回复、进度、排队、审批、结果与错误全部经 SSE 推送，无轮询主通道 | 抓包看 `text/event-stream`；代码检索无定时轮询 | AGENT-002 |
| F02 | 事件只有 14 种，信封含 `id / run_id / ts / elapsed_ms`，`id` 单调递增 | 契约测试；事件序列测试 | AGENT-002 |
| F03 | 带 `Last-Event-ID` 重连只补发之后的事件；终止事件已送达时返回 204 | 断线重连集成测试 | AGENT-003 |
| F04 | 每 15 秒心跳；`X-Accel-Buffering: no`；Nginx 事件路径关闭缓冲 | 空闲 60 秒不断线的实测；网关配置文件 | AGENT-003 |
| F05 | 只把 `run.completed / failed / cancelled` 当结束；200 后的 `run.failed` 被正确展示 | 注入流中错误的测试 | AGENT-002 |
| F06 | 首个事件 P95 ≤ 1 秒 | 压测报告 | AGENT-019 |
| F07 | 全局、每用户、每会话并发上限生效；超出入队并推送位置与预计时间 | 并发测试 | AGENT-004 |
| F08 | 队列满返回 429 + `Retry-After`，界面显示预计等待 | 队列满测试 | AGENT-004 |
| F09 | 同一会话不会同时跑两个运行 | 并发发送测试 | AGENT-004 |
| F10 | 重复 `client_message_id` 返回同一 `run_id`；内容不同返回 409 | 幂等测试 | AGENT-005 |
| F11 | 身份字段只来自令牌，模型输出与工具参数里的被忽略 | 越权测试 | AGENT-001 |
| F12 | 输入护栏先于主模型执行；被拦请求不调用主模型、不执行工具 | 护栏测试（计数主模型调用为 0） | AGENT-013 |
| F13 | 外部内容放在数据块中；注入对抗集成功率为 0 | 对抗评测报告 | AGENT-013 |
| F14 | 意图表是配置数据，经 `IntentSpec` 校验；提示词与评测集由它生成 | 配置校验测试 | AGENT-010 |
| F15 | 置信度低于阈值时追问或列候选，不调用写工具 | 意图路由测试 | AGENT-010 |
| F16 | 每个工具有 Pydantic 参数与返回模型、副作用级别、超时、幂等声明、熔断分组 | 注册表启动校验测试 | AGENT-008 |
| F17 | 参数校验失败不调用后端，错误回给模型；纠正次数受限 | 工具测试 | AGENT-008、AGENT-023 |
| F18 | 每轮只暴露当前意图允许的工具；工具总数 ≤ 20 | 注册表测试 | AGENT-008 |
| F19 | 各家模型的工具调用经适配层转为统一 `ToolCall`；并行调用全部处理；流式参数拼接后再解析 | 适配层测试（各家响应样例） | AGENT-008 |
| F20 | 程序消费的模型输出全部经 Pydantic 校验，失败重试 1 次后降级 | 结构化输出测试 | AGENT-009 |
| F21 | 记忆带来源与时间；冲突保留上一版；禁止项不写入 | 记忆测试 | AGENT-011 |
| F22 | 用户能查看、删除记忆；删除后检索不到 | 接口测试 | AGENT-011 |
| F23 | 知识检索在查询内按租户、门店、角色、有效期过滤 | 跨租户与跨角色检索测试 | AGENT-012 |
| F24 | 规则类回答带真实存在的引用；检索不到明确说“没有找到” | 引用评测；无命中测试 | AGENT-012 |
| F25 | 回答数字、实体落地；失败不说成功；至少一个入口 | 核验测试 | AGENT-014 |
| F26 | 模型输出渲染前转义，不拼进 SQL、命令、URL | 转义测试；代码检索 | AGENT-014 |
| F27 | 高风险写先审批；未审批、过期、拒绝、不一致都不执行；重复审批不重复执行 | 审批测试（各一条） | AGENT-001 |
| F28 | 只对瞬时错误重试，指数退避加抖动，最多 3 次，遵守 `Retry-After` | 重试测试（注入 429、529、503、400） | AGENT-006 |
| F29 | 无幂等键的写操作不自动重试 | 写工具失败测试 | AGENT-006 |
| F30 | 依赖持续失败时熔断打开并给出预计恢复时间，半开探测后恢复 | 熔断测试 | AGENT-007 |
| F31 | 降级 L1–L5 各有说明文案，降级结果不伪装成功 | 降级测试 | AGENT-007 |
| F32 | 服务重启或断线后从检查点续跑，已完成步骤与写操作不重做 | 杀进程续跑测试 | AGENT-017 |
| F33 | 失败保留部分结果，可从失败处重试 | 失败续跑测试 | AGENT-017 |
| F34 | 取消后停止后续步骤，未提交的写操作回滚，推送 `run.cancelled` | 取消测试 | AGENT-017 |
| F35 | token、工具次数、步数、重复调用、整轮时长、每日次数、费用上限都会触发并说明 | 上限测试（每项一条） | AGENT-016 |
| F36 | 每次运行有 trace，span 齐全；日志带 `request_id`；不记录原文与密钥 | trace 抽查；日志检索 | AGENT-018 |
| F37 | 错误率、超时率、P95、队列、熔断、费用指标与告警已配置 | 监控面板与告警规则截图 | AGENT-018 |
| F38 | 意图、工具、引用、拒答、对抗五类评测集达到通过线 | 评测报告 | AGENT-015 |
| F39 | 改提示词、模型、工具、意图表、契约、检索参数会触发评测回归，下降超过 2 个百分点阻止发布 | CI 配置与一次阻止记录 | AGENT-015 |
| F40 | 提示按固定顺序拼装并设缓存断点，缓存命中率有监控 | 用量记录中的缓存读取 token | AGENT-019 |
| F41 | 默认单智能体工作流；用多智能体时方案写明并行理由与成本 | 方案审查 | AGENT-020 |
| F42 | 模型只输出决策，执行、重试、审批、终止都在程序里；决策轮次超过 `MAX_ROUNDS` 以 `STEP_LIMIT` 失败，不再调用模型收尾 | 轮次上限测试（模型一直请求工具） | AGENT-021 |
| F43 | `single` 工具成功后，下一次模型请求的 `tools` 不含它，强行调用被拒绝；`multi` 工具超过 `max_calls_per_run` 被拒绝 | 工具分级测试 | AGENT-022 |
| F44 | 每轮工具清单 = 角色 ∩ 意图 ∩ 本次运行可用 ∩ MCP 白名单；越权调用不执行，后端没有收到请求 | 权限测试 | AGENT-022 |
| F45 | 参数错误结果含 `received_args`、`problems`、`how_to_fix`、`attempts_left`，后端调用为 0；MCP `isError` 结果转成同一格式 | 参数错误测试 | AGENT-023 |

## 18 依据表

全部来源由 Luz-Crawl 采集，原文在 `D:\Codes\skill输出\Luz-Crawl\raw\017_…`（SSE、排队、防失败）、`018_…`（底座技术栈）与 `023_…`（ReAct、MCP 工具与授权、轮次上限、过度代理）的 `raw/<编号>/raw/content.json`。

| 编号 | 来源 | 链接 | 用于 | 采集 |
|---|---|---|---|---|
| E01 | WHATWG HTML Standard · 9.2 Server-sent events | https://html.spec.whatwg.org/multipage/server-sent-events.html | §2 | 017/1252 |
| E02 | MDN · 使用服务器发送事件 | https://developer.mozilla.org/zh-CN/docs/Web/API/Server-sent_events/Using_server-sent_events | §2 | 017/1250（代码块未采到） |
| E03 | Claude Platform Docs · Streaming messages | https://docs.anthropic.com/en/docs/build-with-claude/streaming | §2、§14 | 017/1251 |
| E04 | Claude Platform Docs · API errors | https://docs.anthropic.com/en/api/errors | §2、§7、§14、§16 | 017/1255 |
| E05 | Claude Platform Docs · Rate limits | https://docs.anthropic.com/en/api/rate-limits | §7、§12、§14 | 017/1259 |
| E06 | Azure Architecture Center · Queue-Based Load Leveling pattern | https://learn.microsoft.com/en-us/azure/architecture/patterns/queue-based-load-leveling | §2、§16 | 017/1249 |
| E07 | Azure Architecture Center · Retry pattern | https://learn.microsoft.com/en-us/azure/architecture/patterns/retry | §14、§16 | 017/1258（页面未出现 jitter 一词） |
| E08 | Azure Architecture Center · Circuit Breaker pattern | https://learn.microsoft.com/en-us/azure/architecture/patterns/circuit-breaker | §14、§16 | 017/1254 |
| E09 | Stripe API Reference · Idempotent requests | https://docs.stripe.com/api/idempotent_requests | §2、§6、§14 | 017/1256（整页 API 参考，标题为 Enums） |
| E10 | MDN · Retry-After | https://developer.mozilla.org/zh-CN/docs/Web/HTTP/Reference/Headers/Retry-After | §2、§14 | 017/1253 |
| E11 | Nginx · ngx_http_proxy_module | https://nginx.org/en/docs/http/ngx_http_proxy_module.html | §2、§16 | 017/1261 |
| E12 | NN/g · Response Time Limits | https://www.nngroup.com/articles/response-times-3-important-limits/ | §2、§12 | 017/1262 |
| E13 | NN/g · Progress Indicators Make a Slow System Less Insufferable | https://www.nngroup.com/articles/progress-indicators/ | §2、§12 | 017/1257 |
| E14 | LangGraph · Streaming | https://langchain-ai.github.io/langgraph/concepts/streaming/ | §2、§12 | 017/1260 |
| E15 | Anthropic · Building effective agents | https://www.anthropic.com/engineering/building-effective-agents | §3–§6、§12、§14 | 018/1267 |
| E16 | Anthropic · Writing effective tools for agents | https://www.anthropic.com/engineering/writing-tools-for-agents | §6、§11、§16 | 018/1274 |
| E17 | Anthropic · Effective context engineering for AI agents | https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents | §5、§6、§8 | 018/1282 |
| E18 | Anthropic · Contextual retrieval | https://www.anthropic.com/news/contextual-retrieval | §9 | 018/1286 |
| E19 | Anthropic · How we built our multi-agent research system | https://www.anthropic.com/engineering/multi-agent-research-system | §5、§10–§12、§14 | 018/1294 |
| E20 | Claude Platform Docs · Tool use overview | https://docs.anthropic.com/en/docs/agents-and-tools/tool-use/overview | §6 | 018/1268（只有概览页） |
| E21 | Claude Platform Docs · Prompt caching | https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching | §12 | 018/1272 |
| E22 | Model Context Protocol · Specification 2025-06-18 | https://modelcontextprotocol.io/specification/2025-06-18 | §3、§6、§13 | 018/1264（只有概览页，已有 2026-07-28 新版） |
| E23 | Pydantic AI | https://ai.pydantic.dev/ | §7 | 018/1265（首页） |
| E24 | Pydantic AI · Function Tools | https://ai.pydantic.dev/tools/ | §6 | 018/1273 |
| E25 | Pydantic AI · Output | https://ai.pydantic.dev/output/ | §7、§16 | 018/1276 |
| E26 | Pydantic · Models | https://docs.pydantic.dev/latest/concepts/models/ | §7 | 018/1263 |
| E27 | OpenAI Agents SDK · Guardrails | https://openai.github.io/openai-agents-python/guardrails/ | §3、§10 | 018/1266 |
| E28 | OpenAI Agents SDK · Handoffs | https://openai.github.io/openai-agents-python/handoffs/ | §4 | 018/1269 |
| E29 | OpenAI Agents SDK · Tracing | https://openai.github.io/openai-agents-python/tracing/ | §11 | 018/1275 |
| E30 | LangGraph · Memory overview | https://langchain-ai.github.io/langgraph/concepts/memory/ | §8 | 018/1270 |
| E31 | LangGraph · Persistence | https://langchain-ai.github.io/langgraph/concepts/persistence/ | §5、§8、§14 | 018/1281（概览页，无超步与重放细节） |
| E32 | LangGraph · Interrupts（human-in-the-loop） | https://langchain-ai.github.io/langgraph/concepts/human_in_the_loop/ | §10、§14 | 018/1287 |
| E33 | LangGraph · Graph API overview | https://langchain-ai.github.io/langgraph/concepts/multi_agent/ | §5、§14、§16 | 018/1293（链接跳到 Graph API 概览） |
| E34 | Mem0 · Platform overview | https://docs.mem0.ai/overview | §8 | 018/1271（简短概览） |
| E35 | pgvector README | https://github.com/pgvector/pgvector | §9 | 018/1277 |
| E36 | Qdrant · Hybrid queries | https://qdrant.tech/documentation/concepts/hybrid-queries/ | §9 | 018/1278 |
| E37 | aurelio-labs/semantic-router | https://github.com/aurelio-labs/semantic-router | §4 | 018/1288（1.x 版说明） |
| E38 | OWASP GenAI · LLM Top 10（2025） | https://genai.owasp.org/llm-top-10/ | §8–§10、§13、§16 | 018/1279（每项描述被截断） |
| E39 | OWASP GenAI · LLM01:2025 Prompt Injection | https://genai.owasp.org/llmrisk/llm01-prompt-injection/ | §3、§9、§10、§13 | 018/1290 |
| E40 | OWASP · Top 10 for Large Language Model Applications（项目页） | https://owasp.org/www-project-top-10-for-large-language-model-applications/ | §13 | 018/1291（项目页，无清单） |
| E41 | 阿里云百炼 · 通过 Function Calling 实现工具调用（通义千问） | https://help.aliyun.com/zh/model-studio/qwen-function-calling | §4、§6、§7、§16 | 018/1284 |
| E42 | Qwen 文档 · Function Calling | https://qwen.readthedocs.io/en/latest/framework/function_call.html | §6、§7 | 018/1292 |
| E43 | DeepSeek API 文档 · Function Calling | https://api-docs.deepseek.com/zh-cn/guides/function_calling | §6 | 018/1283（404，无内容，待补采） |
| E44 | LangSmith · Evaluation | https://docs.smith.langchain.com/evaluation/concepts | §11 | 018/1285（入口页） |
| E45 | OpenTelemetry · Generative AI semantic conventions | https://opentelemetry.io/docs/specs/semconv/gen-ai/ | §11 | 018/1280（“已迁移”提示页） |
| E46 | OpenTelemetry · Generative AI spans | https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-spans/ | §11 | 018/1289（“已迁移”提示页） |
| E47 | 微信开放文档 · wx.request（`enableChunked`：开启 transfer-encoding chunked，基础库 2.20.2 起；声明后走 HTTP/1） | https://developers.weixin.qq.com/miniprogram/dev/api/network/request/wx.request.html | §2 | 018 |
| E48 | Yao et al. · ReAct: Synergizing Reasoning and Acting in Language Models（arXiv 2210.03629） | https://arxiv.org/abs/2210.03629 | §5 | 023/1337（摘要页） |
| E49 | Model Context Protocol · Specification（latest）· Server · Tools | https://modelcontextprotocol.io/specification/latest/server/tools | §6、§13 | 023/1339 |
| E50 | Model Context Protocol · Specification（latest）· Basic · Authorization | https://modelcontextprotocol.io/specification/latest/basic/authorization | §6、§13 | 023/1343 |
| E51 | OpenAI Agents SDK · Running agents | https://openai.github.io/openai-agents-python/running_agents/ | §5、§16 | 023/1340 |
| E52 | Claude Platform Docs · Define tools | https://docs.anthropic.com/en/docs/agents-and-tools/tool-use/implement-tool-use | §6 | 023/1338（链接跳到 Define tools） |
| E53 | LangGraph · GRAPH_RECURSION_LIMIT | https://langchain-ai.github.io/langgraph/troubleshooting/errors/GRAPH_RECURSION_LIMIT/ | §5、§16 | 023/1341 |
| E54 | OWASP GenAI · LLM06:2025 Excessive Agency | https://genai.owasp.org/llmrisk/llm062025-excessive-agency/ | §1、§6、§13 | 023/1342 |

本次调研没有覆盖、落地前要补的：DeepSeek 工具调用文档（E43 为 404）、OpenTelemetry 生成式 AI 属性名（E45、E46 为迁移页）、LangGraph 检查点的超步与待写入细节、Pydantic AI 的工具重试与延迟工具（审批）页面。
