"""工具注册表。

每个工具声明：名称、描述、参数 JSON Schema、风险等级、处理函数。
- risk = "read"：只读，模型可直接调用。
- risk = "high"：产生副作用（下单、取消、退款），必须经用户确认后才执行。

安全约束：
- 处理函数的 user_id 由图的状态注入，永远不取自模型输出。
- 模型传入的、未在 schema 中声明的参数会被丢弃（例如模型自作主张传 user_id）。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

Handler = Callable[[int, dict], dict]  # (user_id, args) -> result


class ToolError(Exception):
    """永久性错误（参数错误、业务拒绝）。不重试，直接告诉模型。"""


class TransientToolError(Exception):
    """临时性错误（超时、上游 5xx）。由重试策略处理。"""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    risk: str  # "read" | "high"
    handler: Handler

    def __post_init__(self):
        if self.risk not in ("read", "high"):
            raise ValueError(f"unknown risk level {self.risk!r} for tool {self.name}")

    def clean_args(self, args: dict) -> dict:
        """只保留 schema 中声明的参数，并检查必填项。"""
        allowed = self.parameters.get("properties", {})
        cleaned = {k: v for k, v in (args or {}).items() if k in allowed}
        missing = [k for k in self.parameters.get("required", []) if k not in cleaned]
        if missing:
            raise ToolError(f"missing required argument(s): {', '.join(missing)}")
        return cleaned


@dataclass
class ToolRegistry:
    _tools: dict[str, Tool] = field(default_factory=dict)

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def specs(self) -> list[dict]:
        """给模型看的工具列表（不含处理函数，不含 user_id）。"""
        return [{"name": t.name, "description": t.description, "parameters": t.parameters}
                for t in self._tools.values()]


def default_registry(client) -> ToolRegistry:
    """示例工具集。client 为后端 HTTP 客户端，需提供 get(path) 与 post(path, json, headers)。"""
    registry = ToolRegistry()

    def search_products(user_id: int, args: dict) -> dict:
        resp = client.get("/api/v1/app/products", params={"keyword": args["keyword"]})
        return {"items": resp.get("items", [])}

    def get_order_status(user_id: int, args: dict) -> dict:
        resp = client.get(f"/api/v1/app/orders/{int(args['order_id'])}", user_id=user_id)
        return {"status": resp["status"], "total_cents": resp["total_cents"]}

    def create_order(user_id: int, args: dict) -> dict:
        resp = client.post("/api/v1/app/orders", json={"items": args["items"]},
                           user_id=user_id, idempotency_key=args["_idempotency_key"])
        return {"order_id": resp["order_id"], "order_no": resp["order_no"], "total_cents": resp["total_cents"]}

    def cancel_order(user_id: int, args: dict) -> dict:
        resp = client.post(f"/api/v1/app/orders/{int(args['order_id'])}/cancel", user_id=user_id,
                           idempotency_key=args["_idempotency_key"])
        return {"order_id": resp["order_id"], "status": resp["status"]}

    registry.register(Tool(
        name="search_products", risk="read",
        description="按关键词搜索在售菜品或套餐，返回名称与价格。",
        parameters={"type": "object", "properties": {"keyword": {"type": "string", "maxLength": 40}},
                    "required": ["keyword"]},
        handler=search_products,
    ))
    registry.register(Tool(
        name="get_order_status", risk="read",
        description="查询本人某个订单的当前状态与金额。",
        parameters={"type": "object", "properties": {"order_id": {"type": "integer"}}, "required": ["order_id"]},
        handler=get_order_status,
    ))
    registry.register(Tool(
        name="create_order", risk="high",
        description="为用户创建订单。必须先由用户确认，不得在用户未明确同意时调用。",
        parameters={"type": "object", "properties": {
            "items": {"type": "array", "minItems": 1, "maxItems": 50,
                      "items": {"type": "object", "properties": {
                          "sku_id": {"type": "integer"}, "qty": {"type": "integer", "minimum": 1, "maximum": 99}},
                          "required": ["sku_id", "qty"]}}},
            "required": ["items"]},
        handler=create_order,
    ))
    registry.register(Tool(
        name="cancel_order", risk="high",
        description="取消本人的待支付订单。必须先由用户确认。",
        parameters={"type": "object", "properties": {"order_id": {"type": "integer"}}, "required": ["order_id"]},
        handler=cancel_order,
    ))
    return registry
