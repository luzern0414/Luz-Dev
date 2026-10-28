"""追问模板（SPEC §3.3）：只追问必填槽位，一次 1 个问题，快捷回复最多 4 个，文案由服务端模板生成。
连续 2 次追问仍缺 → 调用方给页面入口结束本轮（`MAX_CLARIFY_ROUNDS`）。
"""
from __future__ import annotations

from agent_core import IntentResult, QuickReply

from .taxonomy import INTENTS, slot_label

MAX_CLARIFY_ROUNDS = 2
MAX_QUICK_REPLIES = 4

_ROUTES = {
    "order_ref": "/pages/order/list", "product_ref": "/pages/index/index", "product_refs": "/pages/index/index",
    "items": "/pages/cart/index", "fulfillment": "/pages/cart/index", "coupon_template_ref": "/pages/coupon/center",
    "plan_ref": "/pages/recharge/index", "headcount": "/pages/queue/index", "ticket_ref": "/pages/queue/index",
    "address_ref": "/pages/address/list", "metric": "/admin/dashboard", "range": "/admin/dashboard",
    "type": "/admin/marketing/coupons", "question": "/pages/help/index",
}
_DEFAULT_ROUTE = "/pages/index/index"


def _ensure_route(replies: list[QuickReply], route: str) -> list[QuickReply]:
    replies = replies[:MAX_QUICK_REPLIES]
    if not any(r.route for r in replies):
        if len(replies) < MAX_QUICK_REPLIES:
            replies.append(QuickReply(label="去页面看", route=route))
        else:
            replies[-1] = replies[-1].model_copy(update={"route": route})
    return replies


def _slot_question(intent: str, slot: str, entities: list[dict]) -> tuple[str, list[QuickReply]]:
    if slot == "order_ref":
        orders = [e for e in entities if e.get("order_id") is not None][:3]
        if orders:
            replies = [QuickReply(label=f"订单…{str(o.get('order_no', o['order_id']))[-4:]}",
                                  send_text=f"订单 {o.get('order_no', o['order_id'])}") for o in orders]
            return f"你有 {len(orders)} 个相关订单，要处理哪一个？", replies
        return "要查哪一单？可以告诉我订单号，或者直接选最近一单。", [QuickReply(label="最近一单", send_text="最近一单")]
    if slot == "qty":
        return "要几份？", [QuickReply(label=f"{n} 份", send_text=f"{n} 份") for n in (1, 2, 3)]
    if slot == "fulfillment":
        return "堂食、外卖还是自提？", [QuickReply(label="堂食", send_text="堂食"), QuickReply(label="外卖", send_text="外卖"),
                                    QuickReply(label="自提", send_text="自提")]
    if slot == "headcount":
        return "几位用餐？", [QuickReply(label=f"{n} 位", send_text=f"{n} 位") for n in (2, 4, 6)]
    if slot == "action":
        return "这单要做什么？", [QuickReply(label="取消订单", send_text="取消这单"), QuickReply(label="催单", send_text="催一下这单"),
                             QuickReply(label="改地址", send_text="改一下收货地址")]
    if slot == "product_ref":
        products = [e for e in entities if e.get("product_id") is not None][:3]
        if products:
            return "要加哪一个？", [QuickReply(label=str(p.get("name", p["product_id"]))[:12],
                                          send_text=f"加 {p.get('name', p['product_id'])}") for p in products]
        return "要加哪个商品？告诉我名字就行。", []
    if slot == "range":
        return "看哪个时间范围？", [QuickReply(label="今天", send_text="今天"), QuickReply(label="本周", send_text="本周"),
                              QuickReply(label="本月", send_text="本月")]
    return f"请告诉我{slot_label(slot)}。", []


def build_clarify(result: IntentResult, entities: list[dict] | None = None,
                  candidates: list[str] | None = None) -> tuple[str, list[QuickReply]]:
    """返回 (追问文案, 快捷回复)。缺必填槽位时按槽位模板；否则按意图候选生成二选一追问。"""
    entities = entities or []
    if result.missing_required:
        slot = result.missing_required[0]
        text, replies = _slot_question(result.intent, slot, entities)
        return text, _ensure_route(replies, _ROUTES.get(slot, _DEFAULT_ROUTE))
    options = [c for c in ([result.intent] + ([result.secondary] if result.secondary else []) + list(candidates or []))
               if c in INTENTS and c not in ("general", "out_of_scope")]
    seen: list[str] = []
    for o in options:
        if o not in seen:
            seen.append(o)
    seen = seen[:3]
    if len(seen) >= 2:
        text = f"你是想{INTENTS[seen[0]].description.split('、')[0]}，还是{INTENTS[seen[1]].description.split('、')[0]}？"
    elif seen:
        text = f"你是想{INTENTS[seen[0]].description.split('、')[0]}吗？"
    else:
        text = "能再说具体一点吗？"
    replies = [QuickReply(label=INTENTS[o].description.split("、")[0][:12], send_text=INTENTS[o].examples[0] if INTENTS[o].examples else INTENTS[o].description[:12])
               for o in seen]
    replies.append(QuickReply(label="都不是", send_text="都不是，我想问别的"))
    return text, _ensure_route(replies, _DEFAULT_ROUTE)
