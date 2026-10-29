# 智能体前后端协议 v3

本协议定义客户端（小程序、H5、管理后台）与智能体服务之间的请求、事件流、审批与错误。**传输、事件名、事件信封、续传、排队、幂等与错误码的规则以 `../../references/agent-foundation.md` §2、§14 为准**，本文只补充请求体、事件数据的业务字段、卡片与审批面板数据。界面行为见 Luz System `references/agent-design.html`。

v2 → v3 的对照表见底座 §2“契约”。

## 1. 传输

- 所有端都用 SSE 格式（`text/event-stream`，UTF-8）。H5 与管理后台用 `fetch` 流式读取（可带 `Authorization` 头）；小程序分块接收后按同一规则解析，并手动带 `Last-Event-ID` 重连；不能分块接收时轮询 `GET /runs/{run_id}/events?after_id=`（同一份事件日志）。
- 只有确需双向实时（例如语音）才用 WebSocket，并在技术方案里写明理由。
- 所有 HTTP 请求带用户令牌（`Authorization: Bearer`）与 `X-Client: miniprogram|h5|admin`；响应带 `X-Request-Id`。

## 2. 请求

### 2.1 新建会话

`POST /api/v1/agent/conversations`

```json
{ "channel": "miniprogram", "entry_page": "order_detail", "entry_ref": "20261023115200" }
```

→ `201 { "conversation_id": "c_01HZ…", "expires_at": "…" }`。`entry_page / entry_ref` 让助手知道用户从哪来（例如从订单详情进入时默认指代该订单）。

### 2.2 发一条消息

`POST /api/v1/agent/conversations/{cid}/runs`

```json
{ "text": "帮我看看刚才那单到哪了", "client_message_id": "uuid-v4", "locale": "zh-CN" }
```

- `text` 1–500 字（后台 1–2000 字）；`client_message_id` 由客户端生成，重发同一条消息时复用。
- 返回 `202 { "run_id": "run_…", "status": "queued", "events_url": "/api/v1/agent/runs/run_…/events" }`；重复的 `client_message_id` 返回 `200` 和同一个 `run_id`；同一键内容不同 → `409 IDEMPOTENCY_KEY_REUSED`。
- 同一会话已有运行在进行：新运行进入会话内待发送队列（事件 `run.queued`，`reason: "conversation_busy"`）；会话队列或全局队列满 → `429 QUEUE_FULL` + `Retry-After`。

### 2.3 读取事件

`GET /api/v1/agent/runs/{run_id}/events`（首次连接与重连同一接口，重连带 `Last-Event-ID`）。终止事件已送达时返回 `204`，客户端停止重连。

### 2.4 审批

`POST /api/v1/agent/runs/{run_id}/approvals/{approval_id}`

```json
{ "approved": true, "client_message_id": "uuid-v4" }
```

- 服务端校验 `approval_id` 是当前待审批且未过期；否则 `409 APPROVAL_MISMATCH` / `410 APPROVAL_EXPIRED`。
- 通过后在同一 `run_id` 的事件流里继续（`id` 继续递增）。

### 2.5 其他

`POST /runs/{run_id}/cancel`（停止）、`POST /runs/{run_id}/retry`（从失败处续跑，返回新 `run_id` 与 `resume_of`）、`GET /conversations/{cid}/messages?before=&limit=`（历史，含各段耗时）、`POST /conversations/{cid}/feedback { run_id, rating: up|down, reason? }`、`GET / DELETE /memory`、`DELETE /memory/{id}`。

## 3. 事件

事件名、信封（`schema_version / id / run_id / conversation_id / event / ts / elapsed_ms / data`）与 Pydantic 契约见底座 §2。业务字段：

| event | data | 说明 |
|---|---|---|
| `run.queued` | `{ position, eta_s, reason }` | 排队位置与预计开始秒数；变化时更新，最多每秒一次 |
| `run.started` | `{ trace_id, degraded: [] }` | 本次运行开始；`degraded` 非空时说明降级影响 |
| `stage.started` / `stage.completed` | `{ stage, label, duration_ms? }`；意图阶段完成时带 `{ intent, confidence }` | 阶段与耗时；意图只用于埋点，不展示 |
| `step.started` / `step.completed` / `step.failed` | `StepInfo` | 工具或子任务：服务端一句话（“正在查你最近的订单”）、尝试次数、耗时、失败原因 |
| `message.delta` | `{ text }` | 回答文字增量；前端按 80 毫秒合并渲染 |
| `approval.required` | `ApprovalRequest`（§5） | 等待审批，单独计时 |
| `approval.resolved` | `{ approval_id, result, wait_ms }` | `approved / rejected / expired / superseded` |
| `run.completed` | `{ answer: FinalAnswer, timings, usage }` | 结束；`answer` 含完整 `text`（客户端以此校正增量）、`cards`、`quick_replies`（≤ 4）、`citations` |
| `run.failed` | `RunError` | 结束；说明错误、影响、可做的事，带 `request_id`；`partial` 为真时保留已完成部分 |
| `run.cancelled` | `{ completed_steps, partial_text }` | 结束；显示“已停止”和已完成部分 |
| `ping` | `{ ts }` | 每 15 秒心跳，不带 `id`，不入事件日志 |

顺序保证：同一运行内 `id` 严格递增；终止事件（`run.completed / run.failed / run.cancelled`）是最后一个事件；`approval.required` 之后、`approval.resolved` 之前只会有 `ping`。重连补发按 `id` 去重。

## 4. 卡片

```json
{ "type": "product", "route": "/pages/product/detail?id=123", "payload": { "id": 123, "name": "剁椒鱼头", "price_cents": 8800, "image": "…", "tags": ["招牌"] } }
```

| type | payload 要点 | 前端组件 |
|---|---|---|
| `product` | id, name, price_cents, image, tags, sold_out | 商品卡（最多 3 个一组） |
| `order` | order_no_tail, status, status_text, amount_cents, eta_text | 订单卡 |
| `store` | name, hours_text, address, distance_text, phone | 门店卡（带导航 / 拨号） |
| `approval` | 同 `ApprovalRequest`（历史回显用） | 审批面板（只读） |
| `handoff` | reason, cs_route, phone | 转人工卡 |
| `citation` | items[{title_path, doc_id, version}] | “来源”折叠 |
| `notice` | level, message | 系统提示条 |

`route` 为小程序页面路径或后台路由；所有卡片必须有 `route`（核验“入口”规则）。

## 5. 审批请求

```json
{
  "approval_id": "a_01HZ…",
  "action": "create_order",
  "summary": "外卖配送 · 剁椒鱼头 ×1、米饭 ×2 · 送至 长沙路 88 号（张先生）· 预计 12:30 · 实付 ¥143.20（已用满 99 减 20 券）",
  "facts": { "fulfillment": "delivery", "items": [ { "name": "剁椒鱼头", "qty": 1, "spec": "中份·微辣" } ], "address": "长沙路 88 号 3 栋 1201（张先生 138****0000）", "eta": "12:30", "amount_cents": 14320, "coupon": "满 99 减 20" },
  "amount_cents": 14320,
  "danger": false,
  "expires_at": "2026-10-23T12:10:00Z",
  "idempotency_key": "…"
}
```

- `summary` 与 `facts` 全部由服务端基于预览接口生成；前端不得根据模型文本重算。
- `danger: true`（取消、删除类）时确认按钮用 danger 样式。
- 过期后前端禁用按钮并显示“确认已过期，请重新发起”；服务端同时推 `approval.resolved`（`expired`）。

## 6. 错误

HTTP 错误体与 `run.failed` / `step.failed` 的 `error` 都是 `RunError`：

```json
{ "code": "MODEL_UNAVAILABLE", "message": "助手现在有点忙，预计 2 分钟后恢复", "next_action": "wait", "retryable": true, "retry_after_s": 120, "request_id": "req_…", "partial": false }
```

错误码表见底座 §14。`message` 为用户可读文案，前端直接展示；`request_id` 可复制；不展示堆栈与内部信息。

## 7. 不变量

1. 一个会话同时只有一个运行在执行；其余排队。
2. 高风险操作只在审批通过且未过期后执行；重复审批不重复执行。
3. 用户看到的所有文本由服务端生成；前端不拼接、不计算金额。
4. 事件可重放、可去重；断线重连不丢不重，不重新执行。
5. 只有终止事件代表结束；连接关闭不代表成功。
6. 任何回答至少带一个页面入口（卡片 `route` 或快捷回复 `route`）。
