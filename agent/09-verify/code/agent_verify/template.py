"""模板回答（SPEC §9 末段）：核验两次不通过时，只包含工具结果中的事实 + 页面入口。"""
from __future__ import annotations

from typing import Any

from agent_core import FinalAnswer, QuickReply, ToolResult

_FACT_KEYS = (("order_no", "订单号"), ("status_text", "状态"), ("status", "状态"), ("pickup_code", "取餐码"),
              ("total_cents", "金额"), ("amount_cents", "金额"), ("payable_cents", "应付"), ("eta_text", "预计"),
              ("name", "名称"), ("hours_text", "营业时间"), ("address", "地址"))


def _fmt(key: str, value: Any) -> str:
    if key.endswith("_cents") and isinstance(value, int):
        return f"¥{value / 100:.2f}"
    return str(value)


def facts_from(result: ToolResult, limit: int = 4) -> list[str]:
    data = result.data if isinstance(result.data, dict) else {}
    facts: list[str] = []
    for key, label in _FACT_KEYS:
        if key in data and data[key] not in (None, "") and len(facts) < limit:
            facts.append(f"{label}：{_fmt(key, data[key])}")
    return facts


def template_answer(tool_results: list[ToolResult], route: str = "/pages/index/index", *, degraded: list[str] | None = None,
                    reason: str = "") -> FinalAnswer:
    lines: list[str] = []
    for r in tool_results:
        if r.ok:
            facts = facts_from(r)
            if facts:
                lines.append("；".join(facts))
        else:
            lines.append(f"{r.message or '查询没有成功'}")
    text = "我把查到的信息整理如下：" + "。".join(lines) + "。" if lines else "这个问题我暂时答不准确，你可以到页面里查看。"
    return FinalAnswer(text=text[:600], quick_replies=[QuickReply(label="去页面看", route=route)],
                       degraded=list(degraded or []) + (["verify_template"] if reason else []))
