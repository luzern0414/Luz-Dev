"""意图体系（SPEC §3.1）。每个项目在 requirements/<项目>.md 勾选启用的意图；未启用的归入 out_of_scope。

`required_any`：每个元组是一组"任选其一"的必填槽位（如 queue_booking 需要 headcount 或 ticket_ref）。
"""
from __future__ import annotations

from dataclasses import dataclass

from agent_core import INTENT_IDS


@dataclass(frozen=True)
class IntentSpec:
    id: str
    description: str
    required_any: tuple[tuple[str, ...], ...] = ()
    optional: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    risk: str = "-"
    channels: tuple[str, ...] = ("miniprogram", "h5")
    knowledge: bool = False
    examples: tuple[str, ...] = ()


_ALL = ("miniprogram", "h5", "admin")

INTENTS: dict[str, IntentSpec] = {s.id: s for s in [
    IntentSpec("greet_chitchat", "问候、寒暄、感谢", channels=_ALL, examples=("你好", "谢谢")),
    IntentSpec("product_search", "找商品/菜品、推荐、询问口味规格价格",
               optional=("keyword", "category", "budget_cents", "taste", "spicy_level", "occasion", "headcount"),
               tools=("search_products", "get_product"), risk="read", examples=("有什么招牌菜", "推荐个花束")),
    IntentSpec("product_compare", "两个以上商品比较", required_any=(("product_refs",),), tools=("get_product",),
               risk="read", examples=("鱼头和牛腩哪个好",)),
    IntentSpec("cart_add", "加入购物车（含规格、数量）", required_any=(("product_ref",), ("qty",)),
               optional=("spec", "remark"), tools=("cart_add",), risk="low_write", examples=("来两份鱼头",)),
    IntentSpec("order_create", "下单（堂食/外卖/自提）", required_any=(("items",), ("fulfillment",)),
               optional=("address_ref", "time_slot", "coupon_ref", "remark"), tools=("price_preview", "create_order"),
               risk="high", examples=("帮我下单",)),
    IntentSpec("order_query", "查订单状态、配送进度、取餐码", required_any=(("order_ref",),),
               tools=("list_orders", "get_order", "get_delivery"), risk="read", examples=("我的订单到哪了",)),
    IntentSpec("order_modify", "取消、改地址、改备注、催单", required_any=(("order_ref",), ("action",)),
               optional=("reason", "new_address_ref"), tools=("cancel_order", "update_order_address", "urge_order"),
               risk="high/low_write", examples=("取消刚才那单",)),
    IntentSpec("after_sales", "退款、售后、投诉、错漏单", required_any=(("order_ref",),), optional=("reason", "evidence"),
               tools=("create_after_sales_draft",), risk="high/handoff", examples=("少送了一份",)),
    IntentSpec("member_query", "会员、积分、余额、优惠券", tools=("get_member", "list_coupons", "get_points"),
               risk="read", examples=("我有多少积分",)),
    IntentSpec("coupon_claim", "领券", required_any=(("coupon_template_ref",),), tools=("claim_coupon",),
               risk="low_write", examples=("领一张券",)),
    IntentSpec("recharge", "充值档位、充值", required_any=(("plan_ref",),), tools=("list_recharge_plans",),
               risk="route", examples=("充值有什么优惠",)),
    IntentSpec("store_info", "营业时间、地址、电话、停车、配送范围与费用", optional=("store_ref",),
               tools=("get_store", "delivery_check"), risk="read", knowledge=True, examples=("几点关门",)),
    IntentSpec("queue_booking", "排队取号、查排号、取消排队、预约", required_any=(("headcount", "ticket_ref"),),
               optional=("zone", "phone"), tools=("queue_create", "queue_current", "queue_cancel"), risk="high/read",
               examples=("帮我取个号，4 个人",)),
    IntentSpec("policy_faq", "退换规则、配送规则、发票、过敏原、花材养护等", required_any=(("question",),),
               risk="read", knowledge=True, examples=("花束能退吗",)),
    IntentSpec("usage_help", "怎么用小程序/后台", required_any=(("question",),), risk="read", knowledge=True,
               channels=_ALL, examples=("在哪里改地址",)),
    IntentSpec("admin_report", "今日/本周数据、异常订单、低库存", required_any=(("metric",), ("range",)),
               optional=("store_ref",), tools=("admin_dashboard", "admin_list_orders"), risk="read", channels=("admin",),
               examples=("今天卖了多少",)),
    IntentSpec("admin_draft", "起草优惠券/活动/文案", required_any=(("type",),), optional=("fields",),
               tools=("admin_create_coupon_draft",), risk="high", channels=("admin",), examples=("起草一张满减券",)),
    IntentSpec("handoff", "明确要人工、投诉升级", channels=_ALL, examples=("转人工",)),
    IntentSpec("out_of_scope", "不相关或未启用", channels=_ALL),
    IntentSpec("general", "低置信度兜底：交给 plan 自由回答或追问", channels=_ALL),
]}

assert set(INTENTS) == set(INTENT_IDS), "taxonomy must cover every IntentId"

KNOWLEDGE_INTENTS: frozenset[str] = frozenset(s.id for s in INTENTS.values() if s.knowledge)
DEFAULT_ENABLED: frozenset[str] = frozenset(s.id for s in INTENTS.values() if s.id not in ("admin_report", "admin_draft"))
ADMIN_ENABLED: frozenset[str] = frozenset(("admin_report", "admin_draft", "usage_help", "greet_chitchat", "handoff",
                                            "out_of_scope", "general"))


def enabled_intents_prompt(enabled: set[str] | frozenset[str]) -> str:
    """给分类模型看的启用意图清单（一句话描述 + 示例 + 必填槽位）。"""
    lines = []
    for iid in INTENT_IDS:
        if iid not in enabled:
            continue
        spec = INTENTS[iid]
        req = "、".join("/".join(g) for g in spec.required_any) or "无"
        ex = "；".join(spec.examples) or "-"
        lines.append(f"- {iid}：{spec.description}。必填：{req}。示例：{ex}")
    return "\n".join(lines)


def slot_label(name: str) -> str:
    return {
        "order_ref": "订单", "product_ref": "商品", "product_refs": "要比较的商品", "qty": "数量", "items": "商品与数量",
        "fulfillment": "就餐方式（堂食/外卖/自提）", "action": "要做的操作", "coupon_template_ref": "要领的券",
        "plan_ref": "充值档位", "headcount": "用餐人数", "ticket_ref": "排号", "question": "具体问题",
        "metric": "要看的指标", "range": "时间范围", "type": "要起草的类型", "address_ref": "收货地址",
    }.get(name, name)
