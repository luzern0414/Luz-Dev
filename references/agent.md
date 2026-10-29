# Intelligent agent implementation

唯一规范是 `agent-foundation.md`（智能体底座）：固定流水线、每个环节的规则 / 契约 / 默认值 / 依据、代码目录、上限与超时、底座检查清单。本页只做路由与边界摘要，不另立规则；与底座不一致时以底座为准。

## 读什么

| 要做的事 | 读 |
|---|---|
| 任何智能体服务工作 | `python -X utf8 scripts/context.py agent-service`（规则卡）→ `agent-foundation.md` |
| 门店业务助手的意图表、工具清单、审批与降级文案、核验细则 | `../agent/backend/SPEC.md`（文首列出被底座取代的条目） |
| 前后端协议：请求、SSE 事件、卡片、审批、错误 | `../agent/shared/protocol.md` v3 |
| 代码现状与差距待办 | `../agent/README.md` |
| 项目定制清单 | `../agent/requirements/_template.md` |
| 对话界面（排队、逐步耗时、审批、记忆入口、引用、错误与降级的表达） | Luz System `references/agent-design.html` |
| 可核对规则（进 `agent` 类型方案的核对表） | `standards.json` → `AGENT-001`–`AGENT-020` |

## 边界

智能体可以理解意图、追问、检索已审核的知识、调用登记过的工具、发起审批、把用户引回页面。它不能绕过鉴权、权限、审批、预算、幂等和交易规则；不能直接执行支付、退款、改价、改库存、改权限；不能自行决定身份；不能编造订单号、金额和时间。

## 底座 v1 相对 v2 文档的变化

- 传输：所有端统一用 SSE 格式（小程序分块接收），WebSocket 只在确需双向实时时用，并写明理由。
- 事件：v3 的 14 个事件（`run.queued` … `run.cancelled`、`ping`）取代 `meta / delta / tool_start / … / done / error`。
- 同一会话运行中再次发送：进入会话内待发送队列，不再返回 `409 AGENT_BUSY`；队列满返回 `429` + `Retry-After`。
- 断线续传：事件日志 + `Last-Event-ID`，终止事件已送达时返回 204；可取消、可从失败处续跑。
- 结构化输出校验失败只重试 1 次（v2 为 2 次），之后降级。
- 新增：意图表配置化与语义路由、低置信度不猜；工具契约的副作用级别、幂等键、错误三分类；记忆保留上一版与来源；知识库上下文前缀、重排与权限预过滤；OWASP 十大风险逐项措施；评测回归门槛。
