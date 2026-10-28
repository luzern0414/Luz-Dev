"""意图分类（SPEC §3.2 第 2–5 步）。

`IntentClassifier(structured_fn)`：structured_fn(prompt, IntentResult) 由模型适配层提供（测试用 ScriptedStructured）；
为 None 或调用失败时退化为"规则意图或 general"。置信度阈值：≥0.75 直接进入；0.5–0.75 追问；<0.5 → general。
"""
from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from typing import Any, Literal

from agent_core import IntentResult, Slot
from agent_llm import structured_output

from .prefilter import PrefilterResult, prefilter
from .taxonomy import DEFAULT_ENABLED, INTENTS, enabled_intents_prompt

Route = Literal["proceed", "clarify", "general", "stop_clarify"]


def missing_required_for(intent: str, slots: Iterable[Slot]) -> list[str]:
    spec = INTENTS.get(intent)
    if spec is None:
        return []
    present = {s.name for s in slots if s.value not in (None, "")}
    missing: list[str] = []
    for group in spec.required_any:
        if not any(name in present for name in group):
            missing.append(group[0])
    return missing


def _resolve_references(pre: PrefilterResult, slots: list[Slot], last_entities: list[dict]) -> None:
    """指代消解：'这个''刚才那单' → 上一轮工具结果中的唯一订单 / 商品。多个候选时不填（由 clarify 列出）。"""
    if not pre.has_reference or not last_entities:
        return
    orders = [e for e in last_entities if e.get("order_id") is not None]
    products = [e for e in last_entities if e.get("product_id") is not None]
    names = {s.name for s in slots}
    if "order_ref" not in names and len(orders) == 1:
        slots.append(Slot(name="order_ref", value=str(orders[0]["order_id"]), source="context"))
    if "product_ref" not in names and len(products) == 1:
        slots.append(Slot(name="product_ref", value=str(products[0]["product_id"]), source="context"))


class IntentClassifier:
    def __init__(self, structured_fn: Callable[[str, type[IntentResult]], Any] | None = None,
                 enabled: Iterable[str] | None = None, high: float = 0.75, low: float = 0.5):
        self.structured_fn = structured_fn
        self.enabled = frozenset(enabled) if enabled is not None else DEFAULT_ENABLED
        self.high = high
        self.low = low
        self.last_prompt: str | None = None
        self.model_failures = 0

    def build_prompt(self, text: str, recent: list[dict], summary: str) -> str:
        history = "\n".join(f"{m.get('role')}: {str(m.get('content'))[:200]}" for m in recent[-6:])
        return (f"把用户最新一句归入下列意图之一，输出 IntentResult JSON（intent, confidence 0–1, slots, "
                f"missing_required, secondary, reason≤120）。温度 0。\n启用意图：\n{enabled_intents_prompt(self.enabled)}\n"
                f"运行摘要：{summary or '无'}\n最近对话：\n{history}\n用户最新一句：{text}")

    def classify(self, text: str, recent: list[dict] | None = None, summary: str = "",
                 last_entities: list[dict] | None = None) -> IntentResult:
        pre = prefilter(text)
        result = self._model_classify(text, recent or [], summary) if self.structured_fn else None
        if result is None:
            intent = pre.rule_intent or "general"
            result = IntentResult(intent=intent, confidence=1.0 if pre.rule_intent else 0.0,
                                  reason="rules_only" if pre.rule_intent else "model_unavailable")
        # 规则先验：关键词命中时提升对应意图
        if pre.rule_intent and result.intent != pre.rule_intent:
            boosted = min(1.0, result.confidence * 0.5 + pre.priors.get(pre.rule_intent, 0))
            if result.intent in ("general", "out_of_scope") or boosted > result.confidence:
                result = result.model_copy(update={"intent": pre.rule_intent, "confidence": max(boosted, self.high),
                                                   "secondary": result.intent if result.intent not in ("general", "out_of_scope") else None})
        # 未启用 → out_of_scope
        if result.intent not in self.enabled and result.intent != "general":
            result = result.model_copy(update={"intent": "out_of_scope", "confidence": 1.0, "missing_required": [],
                                               "reason": f"disabled:{result.intent}"[:120]})
        # 合并规则槽位（模型给的优先，规则补缺）
        slots = list(result.slots)
        names = {s.name for s in slots}
        for s in pre.slots:
            if s.name not in names:
                slots.append(s)
                names.add(s.name)
        _resolve_references(pre, slots, last_entities or [])
        missing = missing_required_for(result.intent, slots)
        return result.model_copy(update={"slots": slots, "missing_required": missing})

    def _model_classify(self, text: str, recent: list[dict], summary: str) -> IntentResult | None:
        prompt = self.build_prompt(text, recent, summary)
        self.last_prompt = prompt
        try:
            return structured_output(self.structured_fn, IntentResult, prompt, max_retries=2)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001 - 模型不可用 / 校验失败 → 规则兜底
            self.model_failures += 1
            return None

    def route(self, result: IntentResult, clarify_count: int = 0) -> Route:
        return decide_route(result, clarify_count, high=self.high, low=self.low)


MAX_CLARIFY = 2


def decide_route(result: IntentResult, clarify_count: int = 0, *, high: float = 0.75, low: float = 0.5,
                 max_clarify: int = MAX_CLARIFY) -> Route:
    if result.intent in ("general", "out_of_scope", "handoff", "greet_chitchat"):
        return "proceed"
    if result.confidence < low:
        return "general"
    if result.missing_required or result.confidence < high:
        return "clarify" if clarify_count < max_clarify else "stop_clarify"
    return "proceed"


def intent_to_json(result: IntentResult) -> str:
    return json.dumps(result.model_dump(), ensure_ascii=False)
