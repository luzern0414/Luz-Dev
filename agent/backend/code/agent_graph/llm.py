"""模型适配层。

图只依赖 Decider 接口：decide(messages, tools) -> 决策字典。
- {"type": "answer", "text": "..."}
- {"type": "tool", "name": "...", "args": {...}}

真实模型（OpenAI / Anthropic / 其他）在 adapter 中把 tools 转换为各自的 function calling 格式，
并把返回的 tool_call 转换为上面的决策字典。适配层不得改变工具的风险等级与权限逻辑。
本文件提供 ScriptedDecider 用于测试与演练，不调用任何外部服务。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Protocol


class Decider(Protocol):
    def decide(self, messages: list[dict], tools: list[dict]) -> dict: ...


class ScriptedDecider:
    """按预设脚本依次返回决策。脚本用完后返回一条兜底回答，避免测试挂起。"""

    def __init__(self, script: list[dict | Callable[[list[dict]], dict]]):
        self._script = list(script)
        self.calls: list[list[dict]] = []

    def decide(self, messages: list[dict], tools: list[dict]) -> dict:
        self.calls.append(list(messages))
        if not self._script:
            return {"type": "answer", "text": "（脚本已结束）"}
        step = self._script.pop(0)
        return step(messages) if callable(step) else step
