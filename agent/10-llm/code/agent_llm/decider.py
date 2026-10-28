"""Decider 接口：图只依赖 `decide(messages, tools) -> 决策字典`。

- {"type": "answer", "text": "...", "quick_replies": [...]?}
- {"type": "tool", "name": "...", "args": {...}, "reason": "..."?}

真实模型在适配器中把 tools 转换为各自的 function calling 格式，并把返回的 tool_call 转回决策字典。
ScriptedDecider 按预设脚本依次返回决策，不调用任何外部服务。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from pydantic import BaseModel


class ModelDisabled(Exception):
    """LLM_ENABLED=false 或适配器未配置时抛出：调用方走回退链 / 模板回答。"""


class Decider(Protocol):
    def decide(self, messages: list[dict], tools: list[dict]) -> dict: ...


def messages_to_dicts(messages: list[Any]) -> list[dict]:
    """把 langchain 消息对象（或已是字典的消息）转成 Decider 使用的 {role, content, ...} 字典。"""
    out: list[dict] = []
    for m in messages:
        if isinstance(m, dict):
            out.append(m)
            continue
        mtype = getattr(m, "type", "")
        role = {"human": "user", "ai": "assistant", "tool": "tool", "system": "system"}.get(mtype, mtype or "user")
        item: dict[str, Any] = {"role": role, "content": m.content if isinstance(m.content, str) else str(m.content)}
        if mtype == "ai" and getattr(m, "tool_calls", None):
            tc = m.tool_calls[0]
            item["tool_call"] = {"name": tc["name"], "args": tc.get("args", {}), "id": tc.get("id")}
        if mtype == "tool":
            item["name"] = getattr(m, "name", None)
            item["ok"] = getattr(m, "status", "success") == "success"
            artifact = getattr(m, "artifact", None)
            if artifact is not None:
                item["artifact"] = artifact
        out.append(item)
    return out


class ScriptedDecider:
    """按预设脚本依次返回决策。脚本项可以是字典或 `callable(messages) -> dict`。脚本用完后返回兜底回答。"""

    def __init__(self, script: list[dict | Callable[[list[dict]], dict]] | None = None):
        self._script = list(script or [])
        self.calls: list[list[dict]] = []
        self.tools_seen: list[list[dict]] = []

    def decide(self, messages: list[dict], tools: list[dict]) -> dict:
        self.calls.append(list(messages))
        self.tools_seen.append(list(tools))
        if not self._script:
            return {"type": "answer", "text": "（脚本已结束）"}
        step = self._script.pop(0)
        return step(messages) if callable(step) else step


class ScriptedStructured:
    """脚本化的结构化输出函数：`__call__(prompt, model) -> dict | BaseModel | str`。用于意图分类、核验、记忆抽取测试。"""

    def __init__(self, outputs: list[Any] | None = None, default: Any = None):
        self._outputs = list(outputs or [])
        self.default = default
        self.prompts: list[str] = []

    def __call__(self, prompt: str, model: type[BaseModel]) -> Any:
        self.prompts.append(prompt)
        if self._outputs:
            out = self._outputs.pop(0)
            if isinstance(out, Exception):
                raise out
            return out
        if self.default is None:
            raise ModelDisabled("no scripted output left")
        return self.default
