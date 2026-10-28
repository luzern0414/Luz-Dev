"""规则预筛（SPEC §3.2 第 1 步，无模型）：订单号、取餐码、手机号正则预填槽位；关键词提升 handoff / after_sales 先验。

正则用数字前后断言而不是 \\b：中文与数字在 Python 的 \\w 里都算"词字符"，`\\b\\d{14,20}\\b` 在"订单20261023115200到哪了"里匹配不到。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from agent_core import Slot

ORDER_NO_RE = re.compile(r"(?<!\d)\d{14,20}(?!\d)")
PICKUP_CODE_RE = re.compile(r"(?<![A-Z0-9])[A-Z]\d{3}(?![0-9])")
PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
RECENT_RE = re.compile(r"最近(的|一|那)?[一单]?|刚才(的|那)?[一单]?|刚下的|上一单|最新的?一?单|这单|那单|这个订单")
REFERENCE_RE = re.compile(r"这个|那个|刚才|这单|那单|上面|它")

_KEYWORDS: dict[str, tuple[str, ...]] = {
    "handoff": ("转人工", "人工客服", "找客服", "真人", "接人工", "人工服务"),
    "after_sales": ("退款", "售后", "投诉", "错单", "漏单", "少送", "送错", "退货", "赔偿"),
}
_ACTION_WORDS: dict[str, str] = {
    "取消": "cancel", "退单": "cancel", "改地址": "change_address", "换地址": "change_address",
    "催单": "urge", "催一下": "urge", "改备注": "update_remark",
}
_FULFILLMENT: dict[str, str] = {"外卖": "delivery", "配送": "delivery", "送到": "delivery", "堂食": "dine_in",
                                "自提": "pickup", "自取": "pickup"}
_HEADCOUNT_RE = re.compile(r"(\d{1,2})\s*(个人|位|人)")
_QTY_RE = re.compile(r"(\d{1,2}|[一二两三四五六七八九十])\s*(份|个|杯|束|枝|件)")
_CN_NUM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


@dataclass
class PrefilterResult:
    slots: list[Slot] = field(default_factory=list)
    priors: dict[str, float] = field(default_factory=dict)   # 关键词命中 → 先验提升
    rule_intent: str | None = None                            # 规则可直接判定的意图
    has_reference: bool = False                               # 含指代词（"这个""刚才那单"）

    def slot(self, name: str) -> Slot | None:
        return next((s for s in self.slots if s.name == name), None)


def prefilter(text: str) -> PrefilterResult:
    res = PrefilterResult()
    t = text.strip()
    if m := ORDER_NO_RE.search(t):
        res.slots.append(Slot(name="order_ref", value=m.group(0), source="rule"))
    elif RECENT_RE.search(t) and any(k in t for k in ("单", "订单", "到哪")):
        res.slots.append(Slot(name="order_ref", value="latest", source="rule"))
    if m := PICKUP_CODE_RE.search(t):
        res.slots.append(Slot(name="pickup_code", value=m.group(0), source="rule"))
    if m := PHONE_RE.search(t):
        res.slots.append(Slot(name="phone", value=m.group(0), source="rule"))
    for word, action in _ACTION_WORDS.items():
        if word in t:
            res.slots.append(Slot(name="action", value=action, source="rule"))
            break
    for word, f in _FULFILLMENT.items():
        if word in t:
            res.slots.append(Slot(name="fulfillment", value=f, source="rule"))
            break
    if m := _HEADCOUNT_RE.search(t):
        res.slots.append(Slot(name="headcount", value=int(m.group(1)), source="rule"))
    if m := _QTY_RE.search(t):
        raw = m.group(1)
        res.slots.append(Slot(name="qty", value=int(raw) if raw.isdigit() else _CN_NUM.get(raw, 1), source="rule"))
    for intent, words in _KEYWORDS.items():
        if any(w in t for w in words):
            res.priors[intent] = res.priors.get(intent, 0) + 0.3
    if res.priors.get("handoff"):
        res.rule_intent = "handoff"
    elif res.priors.get("after_sales"):
        res.rule_intent = "after_sales"
    res.has_reference = bool(REFERENCE_RE.search(t))
    return res
