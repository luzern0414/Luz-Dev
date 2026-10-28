"""模型适配层：Decider 协议（脚本化实现用于测试；OpenAI 兼容 HTTP 适配器用于生产，开关控制、测试不调用），
结构化输出助手（校验失败回传重试，最多 2 次）。适配层不得改变工具的风险等级与权限逻辑。
"""
from .decider import Decider, ModelDisabled, ScriptedDecider, ScriptedStructured, messages_to_dicts
from .openai_compat import OpenAICompatibleDecider, build_chat_request, parse_chat_response
from .structured import StructuredOutputError, structured_output

__all__ = [
    "Decider", "ModelDisabled", "ScriptedDecider", "ScriptedStructured", "messages_to_dicts",
    "OpenAICompatibleDecider", "build_chat_request", "parse_chat_response",
    "StructuredOutputError", "structured_output",
]
