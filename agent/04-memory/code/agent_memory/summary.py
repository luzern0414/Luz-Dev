"""运行摘要（SPEC §4.1）：累计 token（按字符近似）超过阈值时，把更早的消息压成 ≤200 字摘要并用 RemoveMessage 删除。

删除边界保证 tool_call 与对应 ToolMessage 成对删除，且保留部分从 human 开始。
"""
from __future__ import annotations

from collections.abc import Callable

from langchain_core.messages import AIMessage, BaseMessage, RemoveMessage, ToolMessage

SUMMARY_MAX_CHARS = 200


def _content_len(m: BaseMessage) -> int:
    c = m.content
    n = len(c) if isinstance(c, str) else len(str(c))
    if isinstance(m, AIMessage) and m.tool_calls:
        n += sum(len(str(tc.get("args", {}))) for tc in m.tool_calls)
    return n


def estimate_tokens(messages: list[BaseMessage]) -> int:
    """token 近似：按字符计（中文约 1 字 ≈ 1 token，英文偏保守）。"""
    return sum(_content_len(m) for m in messages)


def needs_summary(messages: list[BaseMessage], threshold_tokens: int = 2000, keep_last: int = 12) -> bool:
    return len(messages) > keep_last and estimate_tokens(messages) > threshold_tokens


def _cut_index(messages: list[BaseMessage], keep_last: int) -> int:
    cut = max(0, len(messages) - keep_last)
    # 不能从 ToolMessage 开始保留（其 tool_call 在被删除的 AI 消息里）；也不能从 AI 开始
    while cut < len(messages) and (isinstance(messages[cut], ToolMessage) or messages[cut].type != "human"):
        cut += 1
    return cut


def summarize_messages(messages: list[BaseMessage], summarizer: Callable[[list[BaseMessage], str], str],
                       existing_summary: str = "", keep_last: int = 12) -> dict:
    """返回状态更新 {"summary": str, "messages": [RemoveMessage, ...]}；没有可摘要的消息时返回 {}。"""
    cut = _cut_index(messages, keep_last)
    old = messages[:cut]
    if not old:
        return {}
    ids = [m.id for m in old if m.id]
    if len(ids) != len(old):
        raise ValueError("all messages must have ids to be summarized/removed")
    text = (summarizer(old, existing_summary) or "").strip()
    text = text[:SUMMARY_MAX_CHARS]
    return {"summary": text, "messages": [RemoveMessage(id=i) for i in ids]}


def simple_summarizer(old: list[BaseMessage], existing: str) -> str:
    """无模型时的模板摘要：保留每条 human 的前 30 字。"""
    parts = [existing] if existing else []
    for m in old:
        if m.type == "human":
            parts.append(str(m.content)[:30])
    return "；".join(p for p in parts if p)[:SUMMARY_MAX_CHARS]
