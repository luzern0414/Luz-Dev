"""意图识别（SPEC §3）：意图体系、规则预筛、结构化分类（模型函数可注入）、置信度路由、追问模板。"""
from .clarify import MAX_CLARIFY_ROUNDS, build_clarify
from .classifier import IntentClassifier, decide_route, missing_required_for
from .prefilter import (ORDER_NO_RE, PHONE_RE, PICKUP_CODE_RE, RECENT_RE, PrefilterResult,
                        prefilter)
from .taxonomy import INTENTS, KNOWLEDGE_INTENTS, IntentSpec, enabled_intents_prompt

__all__ = [
    "MAX_CLARIFY_ROUNDS", "build_clarify", "IntentClassifier", "decide_route", "missing_required_for",
    "ORDER_NO_RE", "PHONE_RE", "PICKUP_CODE_RE", "RECENT_RE", "PrefilterResult", "prefilter",
    "INTENTS", "KNOWLEDGE_INTENTS", "IntentSpec", "enabled_intents_prompt",
]
