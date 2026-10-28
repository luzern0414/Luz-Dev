"""长期记忆候选抽取（SPEC §4.2）：有模型时用结构化输出（MemoryCandidates），无模型时用规则（忌口、过敏、辣度、称呼、常点）。"""
from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from agent_core import MemoryCandidate
from agent_llm import structured_output

from .longterm import ONE_TIME_WORDS


class MemoryCandidates(BaseModel):
    items: list[MemoryCandidate] = []


_RULES: tuple[tuple[re.Pattern[str], str, str, float], ...] = (
    (re.compile(r"对([一-龥]{1,6})过敏"), "profile", "对{0}过敏", 0.95),
    (re.compile(r"(?:不吃|不要|别放|不能吃|忌)([一-龥]{1,6})"), "preferences", "不吃{0}", 0.9),
    (re.compile(r"(?:叫我|我叫|称呼我)([一-龥A-Za-z]{1,6})"), "profile", "称呼：{0}", 0.9),
    (re.compile(r"(微辣|中辣|特辣|不辣|不要辣)"), "preferences", "辣度：{0}", 0.85),
    (re.compile(r"(?:喜欢|爱吃|常点|常买)([一-龥]{1,8})"), "preferences", "喜欢{0}", 0.85),
)
_STOP = ("的", "了", "吗", "呢", "吧", "啊", "，", "。", "！", "？", "。")


def _clean(word: str) -> str:
    for s in _STOP:
        word = word.split(s)[0]
    return word.strip()


def extract_candidates(text: str, extractor: Callable[[str, type[MemoryCandidates]], Any] | None = None,
                       ) -> list[MemoryCandidate]:
    """返回候选列表（未过滤置信度；写入时由 MemoryStore 把关）。"""
    if extractor is not None:
        prompt = ("从用户这句话中抽取可长期记忆的稳定偏好/称呼（忌口、过敏、辣度、常点品类、花材偏好、送达偏好）。"
                  "不要抽取支付信息、完整手机号/地址、一次性事实（'今天不吃辣'）或你的推测。"
                  f"输出 {{items: [{{kind: profile|preferences|facts, text, confidence}}]}}。\n用户：{text}")
        try:
            return list(structured_output(extractor, MemoryCandidates, prompt).items)
        except Exception:  # noqa: BLE001 - 模型失败退化为规则
            pass
    one_time = any(w in text for w in ONE_TIME_WORDS)
    out: list[MemoryCandidate] = []
    seen: set[str] = set()
    for pattern, kind, template, conf in _RULES:
        for m in pattern.finditer(text):
            word = _clean(m.group(1))
            if not word:
                continue
            t = template.format(word)
            if t in seen:
                continue
            seen.add(t)
            out.append(MemoryCandidate(kind=kind, text=t, confidence=conf * (0.5 if one_time else 1.0)))  # type: ignore[arg-type]
    return out
