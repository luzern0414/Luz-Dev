"""进模型前裁剪（SPEC §4.1）：取最近 12 条消息，从 human 开始、以 human/tool 结束；不改 checkpoint 里的原始消息。"""
from __future__ import annotations

from langchain_core.messages import BaseMessage, SystemMessage, trim_messages


def trim_for_model(messages: list[BaseMessage], max_messages: int = 12) -> list[BaseMessage]:
    system = [m for m in messages if isinstance(m, SystemMessage)]
    rest = [m for m in messages if not isinstance(m, SystemMessage)]
    trimmed = trim_messages(rest, strategy="last", token_counter=len, max_tokens=max_messages,
                            start_on="human", end_on=("human", "tool"))
    if not trimmed and rest:
        # 最近 max_messages 条里没有 human（极端情况）：退回到最后一条 human 起的片段
        idx = max((i for i, m in enumerate(rest) if m.type == "human"), default=0)
        trimmed = rest[idx:]
    return system + list(trimmed)
