# 智能体前后端协议 v2

本协议定义客户端（小程序、H5、管理后台）与智能体网关之间的请求、事件流、确认与错误。字段与 `../backend/SPEC.md` §10 的 Pydantic 模型一致；界面行为见 `$LUZ_DESIGN_ROOT/references/system/agents.md`。

## 1. 传输

| 端 | 主通道 | 备用 | 说明 |
|---|---|---|---|
| H5、管理后台 | SSE：`POST /api/v1/agent/conversations/{id}/turns` 直接返回 `text/event-stream` | 轮询 | 客户端用 `Last-Event-ID`（= 最后收到的 `seq`）重连 |
| 微信小程序 | WebSocket：`wss://<host>/api/v1/agent/ws?token=<短期票据>`；发轮次用 HTTP `POST .../turns` 得到 `202 {turn_id}`，事件从 WebSocket 推送 | 轮询 `GET .../events?after_seq=` 每 800 毫秒 | 心跳 25 秒；重连 1/2/4/8 秒，最多 4 次后转轮询 |

WebSocket 票据由 `POST /api/v1/agent/ws-ticket` 用用户令牌换取，有效 60 秒，一次性。

所有 HTTP 请求带用户令牌（`Authorization: Bearer`）与 `X-Client: miniprogram|h5|admin`。

## 2. 请求

### 2.1 新建会话

`POST /api/v1/agent/conversations`

```json
{ "channel": "miniprogram", "entry_page": "order_detail", "entry_ref": "20261023115200" }
```

→ `201 { "conversation_id": "c_01HZ…", "expires_at": "…" }`。`entry_page/entry_ref` 让助手知道用户从哪来（例如从订单详情进入时默认指代该订单）。

### 2.2 发一轮

`POST /api/v1/agent/conversations/{id}/turns`

```json
{ "text": "帮我看看刚才那单到哪了", "client_message_id": "uuid-v4", "locale": "zh-CN" }
```

- `text` 1–500 字；`client_message_id` 24 小时内去重（重复提交返回同一 `turn_id` 并重放事件）。
- 返回：SSE 流（H5/后台）或 `202 { "turn_id": 7 }`（小程序）。
- 同一会话已有一轮未结束 → `409 { "code": "AGENT_BUSY" }`。

### 2.3 确认

`POST /api/v1/agent/conversations/{id}/confirm`

```json
{ "request_id": "r_01HZ…", "approved": true, "client_message_id": "uuid-v4" }
```

- 服务端校验 `request_id` 等于当前待确认且未过期；否则 `409 CONFIRM_MISMATCH` / `410 CONFIRM_EXPIRED`。
- 通过后继续同一 `turn_id` 的事件流（`seq` 继续递增）。

### 2.4 其他

`GET .../events?after_seq=`、`GET .../messages?before=&limit=`、`POST .../feedback { turn_id, rating: up|down, reason? }`、`GET/DELETE /api/v1/agent/memory`。

## 3. 事件

所有事件统一信封：

```json
{ "seq": 12, "turn_id": 7, "event": "delta", "ts": "2026-10-23T12:00:00Z", "data": { } }
```

SSE 形式：`id: 12`、`event: delta`、`data: {…信封…}`。WebSocket 与轮询直接发信封 JSON（轮询返回 `{ "events": [...], "next_seq": 13, "done": false }`）。

| event | data | 说明 |
|---|---|---|
| `meta` | `{ trace_id, degraded: [] }` | 每轮第一个事件 |
| `intent` | `{ intent, confidence }` | 可选，供前端埋点，不展示 |
| `delta` | `{ text }` | 最终回答的文本增量；前端按 80 毫秒合并渲染 |
| `tool_start` | `{ tool, summary }` | 服务端一句话，如"正在查你最近的订单" |
| `tool_end` | `{ tool, ok, summary, latency_ms }` | 失败时 `summary` 为用户可读原因 |
| `card` | `Card` | 商品/订单/门店/转人工/引用/通知卡片，见 §4 |
| `confirm_request` | `ConfirmRequest` | 见 §5；收到后本轮进入等待确认，不会再有 `delta` |
| `notice` | `{ level: info|warning, message }` | 降级或系统提示，如"规则说明暂时查不到" |
| `suggestions` | `{ items: QuickReply[] }` | 最多 4 个快捷回复 |
| `done` | `FinalAnswer` | 本轮结束；含完整 `text`（客户端以此校正增量）、`cards`、`quick_replies`、`citations` |
| `error` | `AgentError` | 本轮结束；见 §6 |
| `ping` | `{}` | 保活，不计入 `seq`（`seq` 置 0） |

顺序保证：同一轮内 `seq` 严格递增；`done` 或 `error` 是最后一个事件；`confirm_request` 之后直到确认前不会有新事件。重复投递（重连回填）按 `seq` 去重。

## 4. 卡片

```json
{ "type": "product", "route": "/pages/product/detail?id=123", "payload": { "id": 123, "name": "剁椒鱼头", "price_cents": 8800, "image": "…", "tags": ["招牌"] } }
```

| type | payload 要点 | 前端组件 |
|---|---|---|
| `product` | id, name, price_cents, image, tags, sold_out | 商品卡（最多 3 个一组） |
| `order` | order_no_tail, status, status_text, amount_cents, eta_text | 订单卡 |
| `store` | name, hours_text, address, distance_text, phone | 门店卡（带导航/拨号） |
| `confirm` | 同 `ConfirmRequest`（历史回显用） | 确认面板（只读） |
| `handoff` | reason, cs_route, phone | 转人工卡 |
| `citation` | items[{title_path}] | "来源"折叠 |
| `notice` | level, message | 系统提示条 |

`route` 为小程序页面路径或后台路由；所有卡片必须有 `route`（§9 核验"入口"规则）。

## 5. 确认请求

```json
{
  "request_id": "r_01HZ…",
  "action": "create_order",
  "summary": "外卖配送 · 剁椒鱼头 ×1、米饭 ×2 · 送至 长沙路 88 号（张先生）· 预计 12:30 · 实付 ¥143.20（已用满 99 减 20 券）",
  "args_view": { "fulfillment": "delivery", "items": [ { "name": "剁椒鱼头", "qty": 1, "spec": "中份·微辣" } ], "address": "长沙路 88 号 3 栋 1201（张先生 138****0000）", "eta": "12:30", "amount_cents": 14320, "coupon": "满 99 减 20" },
  "amount_cents": 14320,
  "expires_at": "2026-10-23T12:10:00Z",
  "danger": false
}
```

- `summary` 与 `args_view` 全部由服务端基于预览接口生成；前端不得根据模型文本重算。
- `danger: true`（取消、删除类）时确认按钮用 danger 样式。
- 过期后前端禁用按钮并显示"确认已过期，请重新发起"。

## 6. 错误

```json
{ "code": "MODEL_UNAVAILABLE", "message": "助手现在有点忙，请稍后再试", "retryable": true, "retry_after": 5, "trace_id": "t_…" }
```

错误码表见 `../backend/SPEC.md` §8.3。`message` 为用户可读文案，前端直接展示；`trace_id` 可复制。

## 7. 不变量

1. 一个会话同时只有一轮在处理。
2. 高风险操作只在 `approved: true` 且未过期后执行；重复确认不重复执行。
3. 用户看到的所有文本由服务端生成；前端不拼接、不计算金额。
4. 事件可重放、可去重；客户端断线重连不丢不重。
5. 任何回答至少带一个页面入口（卡片 `route` 或快捷回复 `route`）。
