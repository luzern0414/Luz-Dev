"""智能体编排图（LangGraph）。

节点：
- plan：调用模型，决定回答或调用工具
- run_tool：执行只读工具（带重试），结果回填给模型
- confirm：高风险工具先 interrupt，等待用户确认（前端弹出确认卡）；确认后执行，取消则结束

流程：
    START → plan ──(回答)──────────────→ END
             │
             ├─(只读工具)→ run_tool → plan
             └─(高风险)──→ confirm ──(确认)→ 执行 → plan
                                   └─(取消)→ END

安全与稳定性约束：
- user_id 只来自调用方传入的状态，工具参数里的 user_id 会被丢弃。
- 单轮最多 MAX_TOOL_CALLS 次工具调用，防止死循环。
- 高风险工具的幂等键由调用方给出（会话 id + 轮次号），重复恢复执行不会重复下单。
"""
from __future__ import annotations

from typing import Any, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from .llm import Decider
from .retry import call_with_retry
from .tools import ToolError, ToolRegistry, TransientToolError

MAX_TOOL_CALLS = 5


class AgentState(TypedDict, total=False):
    user_id: int
    idem_base: str             # 幂等键前缀，例如 "conv-1:turn-3"
    messages: list[dict]       # {"role": "user"|"assistant"|"tool", "content": str, ...}
    tool_calls_made: int
    next_call: dict | None     # 模型决定的下一次工具调用 {"name", "args"}
    final_text: str | None


def build_graph(decider: Decider, registry: ToolRegistry, checkpointer=None,
                sleep=None, retry_base_delay: float = 0.5):
    """组装并编译智能体图。返回可调用对象：app.invoke(state, config)。"""
    retry_kwargs: dict[str, Any] = {"base_delay": retry_base_delay}
    if sleep is not None:
        retry_kwargs["sleep"] = sleep

    def plan(state: AgentState) -> dict:
        if state.get("final_text"):
            return {}
        if state.get("tool_calls_made", 0) >= MAX_TOOL_CALLS:
            return {"final_text": "这次操作步骤较多，我先停在这里。请换个说法或分步告诉我。"}
        decision = decider.decide(state["messages"], registry.specs())
        if decision["type"] == "answer":
            return {"final_text": decision["text"], "next_call": None,
                    "messages": state["messages"] + [{"role": "assistant", "content": decision["text"]}]}
        return {"next_call": {"name": decision["name"], "args": decision.get("args", {})},
                "messages": state["messages"] + [{"role": "assistant", "content": "", "tool_call": decision}]}

    def route(state: AgentState) -> str:
        if state.get("final_text"):
            return END
        call = state["next_call"]
        tool = registry.get(call["name"])
        if tool is None:
            return "unknown_tool"
        return "confirm" if tool.risk == "high" else "run_tool"

    def unknown_tool(state: AgentState) -> dict:
        name = state["next_call"]["name"]
        note = f"工具 {name} 不存在，请只使用可用工具。"
        return {"messages": state["messages"] + [{"role": "tool", "name": name, "content": note, "ok": False}],
                "next_call": None}

    def run_tool(state: AgentState) -> dict:
        call = state["next_call"]
        tool = registry.get(call["name"])
        messages = list(state["messages"])
        try:
            args = tool.clean_args(call["args"])
            result = call_with_retry(lambda: tool.handler(state["user_id"], args), **retry_kwargs)
            messages.append({"role": "tool", "name": tool.name, "content": result, "ok": True})
        except ToolError as exc:
            messages.append({"role": "tool", "name": tool.name, "content": str(exc), "ok": False})
        except TransientToolError:
            messages.append({"role": "tool", "name": tool.name,
                             "content": "服务暂时不可用，请稍后再试", "ok": False})
        return {"messages": messages, "next_call": None,
                "tool_calls_made": state.get("tool_calls_made", 0) + 1}

    def confirm(state: AgentState) -> dict:
        # interrupt 之前不得有副作用：恢复执行时本节点会从头运行
        call = state["next_call"]
        tool = registry.get(call["name"])
        try:
            args = tool.clean_args(call["args"])
        except ToolError as exc:
            return {"messages": state["messages"] + [{"role": "tool", "name": tool.name,
                                                      "content": str(exc), "ok": False}],
                    "next_call": None}
        decision = interrupt({
            "type": "confirm",
            "action": tool.name,
            "args": args,
            "summary": f"即将执行：{tool.description}",
        })
        messages = list(state["messages"])
        if not (isinstance(decision, dict) and decision.get("approved") is True):
            return {"final_text": "好的，已取消这次操作，没有做任何改动。",
                    "next_call": None,
                    "messages": messages + [{"role": "assistant", "content": "已取消"}]}
        # 确认后执行：幂等键由会话前缀与工具名组成，恢复执行不会重复下单
        args_with_key = {**args, "_idempotency_key": f"{state['idem_base']}:{tool.name}"}
        try:
            result = call_with_retry(lambda: tool.handler(state["user_id"], args_with_key),
                                     idempotent=True, **retry_kwargs)
            messages.append({"role": "tool", "name": tool.name, "content": result, "ok": True})
        except ToolError as exc:
            messages.append({"role": "tool", "name": tool.name, "content": str(exc), "ok": False})
        except TransientToolError:
            messages.append({"role": "tool", "name": tool.name,
                             "content": "服务暂时不可用，操作未完成，请稍后重试", "ok": False})
        return {"messages": messages, "next_call": None,
                "tool_calls_made": state.get("tool_calls_made", 0) + 1}

    graph = StateGraph(AgentState)
    graph.add_node("plan", plan)
    graph.add_node("run_tool", run_tool)
    graph.add_node("confirm", confirm)
    graph.add_node("unknown_tool", unknown_tool)
    graph.add_edge(START, "plan")
    graph.add_conditional_edges("plan", route, {"run_tool": "run_tool", "confirm": "confirm",
                                                "unknown_tool": "unknown_tool", END: END})
    graph.add_edge("run_tool", "plan")
    graph.add_edge("confirm", "plan")
    graph.add_edge("unknown_tool", "plan")
    return graph.compile(checkpointer=checkpointer or MemorySaver())
