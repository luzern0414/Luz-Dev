# 智能体（agent）

Luz Dev 的智能体开发部分：为小程序、App 与管理后台实现业务助手（LangGraph）；对话界面本身由 Luz Design 定义。

```
agent/
  README.md               本文件
  shared/protocol.md      前后端事件协议 v2（SSE / WebSocket / 轮询，事件、确认、错误码）
  $LUZ_DESIGN_ROOT/references/system/agents.md  对话界面规范（入口、消息类型、流式、确认面板、状态机）
  backend/SPEC.md         服务规范 v2（图、意图、记忆、知识库、工具、确认、降级熔断、核验、Pydantic、流式、API、安全、观测、评测、实现顺序）
  backend/code/           参考实现（v1：plan / run_tool / confirm / unknown_tool 四节点，脚本化模型，10 项测试）
  requirements/_template.md   项目定制清单模板（填好后即项目的 AGENT.md）
```

## 阅读顺序

1. `backend/SPEC.md` §0–§2（边界与图）→ §3 意图 → §6 工具 → §7 确认 → §8 降级 → §9 核验
2. `shared/protocol.md`
3. `$LUZ_DESIGN_ROOT/references/system/agents.md`
4. 新项目：复制 `requirements/_template.md` 填写，再按 `backend/SPEC.md` §17 的顺序实现

## 职责边界

| 智能体做 | 智能体不做 |
|---|---|
| 理解意图、追问缺失信息、选工具 | 直接发起支付、退款、改价、改库存、改权限 |
| 只读查询直接执行；低风险写（加购、领券）直接执行并回显 | 绕过确认执行高风险操作 |
| 高风险写生成服务端摘要 → 用户确认 → 带幂等键执行 | 自行决定身份（`user_id` 只来自令牌） |
| 用知识库回答规则类问题并给引用 | 编造订单号、金额、时间（核验节点拦截） |
| 解释失败并给页面入口；必要时转人工 | 替代页面：每个回答都带"去页面"入口 |

## 与后端的关系

- 工具只调用 `backend/API.md` 标注 `agent: yes` 且已实现的接口；"设计 / 待建"接口对应的工具注册为 `disabled`。
- 高风险操作由**智能体服务**在确认后执行（`backend/SPEC.md` §16 已同步此口径）；幂等键 UUID v5，满足后端 36 位要求。
- 后端需提供 `POST /api/v1/app/orders/preview`（报价）与服务间签名校验。

## 参考实现状态（v1 代码 vs v2 规范）

| v2 要求 | 代码状态 |
|---|---|
| 图骨架、工具注册、重试、`interrupt` 确认、幂等键、步数上限 | 已有（需按 §2.2 修正状态重置与 reducer） |
| 意图分类、追问、知识库、长期记忆、核验、Pydantic 参数校验、模型适配与回退、熔断、SSE 网关、观测 | 待实现（按 §17 顺序） |

## 运行测试

```powershell
# 从 Luz-Dev 根目录运行
python -m pip install langgraph langchain-core pydantic
python -m pytest -q agent
```
