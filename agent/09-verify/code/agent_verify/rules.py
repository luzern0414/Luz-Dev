"""规则核验（SPEC §9）。

检查：结构（长度）、数字落地（金额按分比对、订单号、取餐码、排号、时间、数量）、实体落地（「」引用的名称）、
政策陈述需引用、禁止承诺词、失败状态一致、安全（他人 user_id）、入口。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from agent_core import FinalAnswer, RetrievedContext, ToolResult, VerifyResult, Violation

AMOUNT_RE = re.compile(r"(?:[¥￥]\s?(\d+(?:\.\d{1,2})?))|((?<![\d.])\d+(?:\.\d{1,2})?)\s?元")
ORDER_NO_RE = re.compile(r"(?<!\d)\d{14,20}(?!\d)")
PICKUP_CODE_RE = re.compile(r"(?<![A-Z0-9])[A-Z]\d{3}(?![0-9])")
TIME_RE = re.compile(r"(?<!\d)(?:[01]?\d|2[0-3]):[0-5]\d(?!\d)")
QTY_RE = re.compile(r"(?:[×xX]\s?(\d{1,3})(?!\d))|((?<!\d)\d{1,3})\s?(?:份|件|个|杯|束|枝)")
QUEUE_RE = re.compile(r"(?:排号|号码|取号|叫号)[：:\s]?([A-Z]?\d{1,4})")
QUOTED_RE = re.compile(r"「([^」]{1,30})」|《([^》]{1,30})》")

POLICY_KEYWORDS: tuple[str, ...] = ("退换规则", "退款规则", "售后时效", "发票", "过敏原", "配送规则", "养护", "规定", "政策", "条款",
                                   "会员权益", "积分规则")
NO_RULE_PHRASES: tuple[str, ...] = ("没有找到相关规则", "没找到相关规则", "暂时查不到")
FORBIDDEN_PROMISES: tuple[str, ...] = ("一定", "保证", "已经退款", "保证退款", "必定", "肯定能", "承诺", "包退")
SUCCESS_WORDS: tuple[str, ...] = ("已下单", "已取消", "已为你", "已帮你", "成功", "已完成", "已提交", "下单成功")
FAILURE_WORDS: tuple[str, ...] = ("失败", "未能", "没能", "无法", "没有成功", "未成功", "没办法", "不能")

MAX_TEXT = {"miniprogram": 600, "h5": 600, "admin": 1500}


@dataclass
class GroundTruth:
    ints: set[int] = field(default_factory=set)
    cents: set[int] = field(default_factory=set)
    strings: set[str] = field(default_factory=set)
    blob: str = ""
    user_ids: set[str] = field(default_factory=set)

    def has_text(self, s: str) -> bool:
        return s in self.strings or s in self.blob


def _to_cents(amount: str) -> int:
    return int(round(float(amount) * 100))


def _walk(node: Any, gt: GroundTruth, key: str | None = None) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "user_id" and v is not None:
                gt.user_ids.add(str(v))
            _walk(v, gt, k)
    elif isinstance(node, list):
        for v in node:
            _walk(v, gt, key)
    elif isinstance(node, bool):
        return
    elif isinstance(node, int):
        gt.ints.add(node)
        gt.strings.add(str(node))
        if key and (key.endswith("_cents") or key in ("amount", "price", "total", "fee")):
            gt.cents.add(node)
    elif isinstance(node, float):
        gt.strings.add(str(node))
        gt.cents.add(int(round(node * 100)))
    elif isinstance(node, str):
        gt.strings.add(node)
        gt.blob += "\n" + node
        for m in AMOUNT_RE.finditer(node):
            gt.cents.add(_to_cents(m.group(1) or m.group(2)))


def build_ground_truth(tool_results: list[ToolResult], context: RetrievedContext | None = None) -> GroundTruth:
    gt = GroundTruth()
    for r in tool_results:
        if r.data is not None:
            _walk(r.data, gt)
    if context and context.hit:
        for c in context.chunks:
            _walk(c.text, gt)
            gt.blob += "\n" + c.title_path
    return gt


def _check_numbers(text: str, gt: GroundTruth) -> list[Violation]:
    out: list[Violation] = []
    for m in AMOUNT_RE.finditer(text):
        cents = _to_cents(m.group(1) or m.group(2))
        if cents not in gt.cents and cents not in gt.ints:
            out.append(Violation(rule="number_grounding", detail=f"金额 {m.group(0).strip()} 不在工具结果/引用中"))
    for m in ORDER_NO_RE.finditer(text):
        if not gt.has_text(m.group(0)):
            out.append(Violation(rule="number_grounding", detail=f"订单号 {m.group(0)} 不在工具结果中"))
    for m in PICKUP_CODE_RE.finditer(text):
        if not gt.has_text(m.group(0)):
            out.append(Violation(rule="number_grounding", detail=f"取餐码 {m.group(0)} 不在工具结果中"))
    for m in QUEUE_RE.finditer(text):
        if not gt.has_text(m.group(1)):
            out.append(Violation(rule="number_grounding", detail=f"排号 {m.group(1)} 不在工具结果中"))
    for m in TIME_RE.finditer(text):
        if not gt.has_text(m.group(0)):
            out.append(Violation(rule="number_grounding", detail=f"时间 {m.group(0)} 不在工具结果中"))
    for m in QTY_RE.finditer(text):
        n = int(m.group(1) or m.group(2))
        if n not in gt.ints:
            out.append(Violation(rule="number_grounding", detail=f"数量 {n} 不在工具结果中"))
    return out


def verify_answer(answer: FinalAnswer, tool_results: list[ToolResult], context: RetrievedContext | None = None,
                  *, channel: str = "miniprogram", user_id: str | None = None,
                  semantic: Any = None) -> VerifyResult:
    """规则核验。semantic：可选的 (text) -> list[Violation] 语义判定函数（小模型），失败时忽略。"""
    violations: list[Violation] = []
    text = answer.text or ""
    limit = MAX_TEXT.get(channel, 600)
    if len(text) > limit:
        violations.append(Violation(rule="structure", detail=f"文本 {len(text)} 字超过 {limit}"))
    gt = build_ground_truth(tool_results, context)
    violations.extend(_check_numbers(text, gt))
    for m in QUOTED_RE.finditer(text):
        name = m.group(1) or m.group(2)
        if not gt.has_text(name):
            violations.append(Violation(rule="entity_grounding", detail=f"「{name}」不在工具结果/引用中"))
    if any(k in text for k in POLICY_KEYWORDS) and not answer.citations and not any(p in text for p in NO_RULE_PHRASES):
        violations.append(Violation(rule="citation", detail="政策/规则类陈述没有引用"))
    for w in FORBIDDEN_PROMISES:
        if w in text:
            violations.append(Violation(rule="promise", detail=f"出现承诺词“{w}”"))
            break
    failed = [r for r in tool_results if not r.ok]
    if failed and any(w in text for w in SUCCESS_WORDS) and not any(w in text for w in FAILURE_WORDS):
        violations.append(Violation(rule="failure_state", detail="工具失败但回答描述为成功"))
    if user_id is not None and any(u != str(user_id) for u in gt.user_ids):
        violations.append(Violation(rule="security", detail="工具结果含其他用户的数据"))
    if not answer.has_entry():
        violations.append(Violation(rule="entry", detail="没有页面入口（cards.route / quick_replies.route）"))
    if semantic is not None:
        try:
            violations.extend(semantic(text) or [])
        except Exception:  # noqa: BLE001 - 语义核验不可用只做规则检查
            pass
    return VerifyResult(passed=not violations, violations=violations)
