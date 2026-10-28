"""订单行种类注册表（扩展点，对应芋道 TradeOrderHandler / R7 §2.3 履约处理器）。

每种行（goods 实物、service 线下服务、member_card 会员卡 …）实现一个 `LineKind`：

| 钩子 | 时机（同一事务内） | 默认 |
|---|---|---|
| resolve(db, user_id, store_id, item, now) → ResolvedLine | 试算 / 下单：服务端取价、校验可售 | 必须实现 |
| reserve(db, order, items, now) | 下单：占库存等 | 无 |
| release(db, order, items, now) | 取消 / 超时关闭：释放占用 | 无 |
| confirm(db, order, items, now) | 支付成功：确认占用 | 无 |
| fulfil(db, order, item, now) → fulfil_ref or None | 支付成功：能立即交付的（发卡）返回引用；None 表示等待人工履约（发货 / 到店服务） | None |
| refund_cap(db, order, item, qty, now) → int or None | 售后试算：本行最多可退（会员卡按法释〔2025〕4号由 29-member-card 计算） | None（不另设上限） |
| on_refunded(db, order, item, qty, amount, restock, now) | 售后退款成功：回库存 / 终止权益 | 无 |

注册：`register_line_kind(MyKind())`；同名覆盖。钩子抛 AppError 会让整个用例回滚（支付钩子失败 → 支付模块自动退款）。
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from luzback import AppError, ErrorCode

from .rules import GOODS_OFFLINE_SERVICE, GOODS_PHYSICAL


@dataclass
class ResolvedLine:
    """服务端解析出的行（价格只来自这里，不信任客户端）。"""

    kind: str
    product_id: int
    name: str
    unit_price_cents: int
    qty: int
    goods_kind: str = GOODS_PHYSICAL
    sku_id: int | None = None
    spec: str = ""
    image: str = ""
    category_id: int | None = None
    limit_per_user: int | None = None      # 每人限购（跨订单累计，不含已取消 / 关闭）
    returnable: bool = True                # 是否适用七日无理由（法定除外商品由商品模块标记，购买时提示）
    stock_qty: int | None = None           # 当前可售（试算展示用；None 表示不限）
    snapshot: dict[str, Any] = field(default_factory=dict)

    @property
    def gross_cents(self) -> int:
        return self.unit_price_cents * self.qty

    @property
    def product_key(self) -> str:
        return f"{self.kind}:{self.product_id}"


class LineKind:
    name = ""

    def resolve(self, db: Session, user_id: int, store_id: int | None, item: dict[str, Any], now: datetime) -> ResolvedLine:
        raise NotImplementedError

    def reserve(self, db: Session, order: Any, items: list[Any], now: datetime) -> None:
        return None

    def release(self, db: Session, order: Any, items: list[Any], now: datetime) -> None:
        return None

    def confirm(self, db: Session, order: Any, items: list[Any], now: datetime) -> None:
        return None

    def fulfil(self, db: Session, order: Any, item: Any, now: datetime) -> str | None:
        return None

    def refund_cap(self, db: Session, order: Any, item: Any, qty: int, now: datetime) -> int | None:
        return None

    def on_refunded(self, db: Session, order: Any, item: Any, qty: int, amount_cents: int, restock: bool,
                    now: datetime) -> None:
        return None


_kinds: dict[str, LineKind] = {}


def register_line_kind(kind: LineKind) -> None:
    if not kind.name:
        raise ValueError("line kind needs a name")
    _kinds[kind.name] = kind


def get_line_kind(name: str) -> LineKind:
    kind = _kinds.get(name)
    if kind is None:
        raise AppError(ErrorCode.BAD_REQUEST, f"unsupported line kind: {name}")
    return kind


def line_kinds() -> list[str]:
    return sorted(_kinds)


# ---------------------------------------------------------------- 库存端口（07-inventory）


class StockPort:
    """库存预占：ref 为业务单号（order:{id}），items={sku_id: qty}。默认委托 07-inventory 的 reservation。"""

    def reserve(self, db: Session, ref: str, items: dict[int, int], ttl_seconds: int, now: datetime) -> None:
        from inventory import reservation
        reservation.reserve(db, ref, items, ttl_seconds, now=now)

    def confirm(self, db: Session, ref: str) -> None:
        from inventory import reservation
        reservation.confirm(db, ref)

    def release(self, db: Session, ref: str) -> None:
        from inventory import reservation
        reservation.release(db, ref)

    def restock(self, db: Session, sku_id: int, qty: int, ref: str) -> None:
        """退货入库（部分数量）：07-inventory 按 ref 去重（同一 ref 对同一 SKU 只回补一次，流水唯一约束兜底）。"""
        import inventory
        inventory.restore(db, sku_id, qty, ref=ref)

    def sku(self, db: Session, sku_id: int) -> dict[str, Any] | None:
        import inventory
        row = db.get(inventory.Sku, sku_id)
        if row is None:
            return None
        return {"id": row.id, "name": row.name, "price_cents": row.price_cents, "stock": row.stock,
                "on_sale": row.status == "on_sale", "product_id": getattr(row, "product_id", None) or row.id}


_stock = StockPort()


def set_stock_port(port: StockPort | None) -> None:
    global _stock
    _stock = port or StockPort()


def stock_port() -> StockPort:
    return _stock


# ---------------------------------------------------------------- 内置：goods（实物，07-inventory）


class GoodsKind(LineKind):
    """实物：item = {kind: "goods", sku_id, qty}。价格与名称取自库存 SKU（项目接入 03-catalog 时替换 price_source）。"""

    name = "goods"

    def __init__(self, price_source: Callable[[Session, int], dict[str, Any] | None] | None = None):
        self.price_source = price_source

    def resolve(self, db, user_id, store_id, item, now):
        sku_id = int(item.get("sku_id") or 0)
        sku = (self.price_source or stock_port().sku)(db, sku_id) if sku_id else None
        if sku is None or not sku.get("on_sale", True):
            raise AppError(ErrorCode.NOT_FOUND, "product not on sale", data={"sku_id": sku_id})
        return ResolvedLine(kind=self.name, product_id=int(sku.get("product_id") or sku_id), sku_id=sku_id,
                            name=sku["name"], unit_price_cents=int(sku["price_cents"]), qty=int(item["qty"]),
                            goods_kind=sku.get("goods_kind", GOODS_PHYSICAL), spec=sku.get("spec", ""),
                            image=sku.get("image", ""), category_id=sku.get("category_id"),
                            limit_per_user=sku.get("limit_per_user"), returnable=sku.get("returnable", True),
                            stock_qty=sku.get("stock"), snapshot={"sku": {k: v for k, v in sku.items() if k != "stock"}})

    @staticmethod
    def _items(items) -> dict[int, int]:
        merged: dict[int, int] = {}
        for it in items:
            if it.kind == "goods" and it.sku_id:
                merged[it.sku_id] = merged.get(it.sku_id, 0) + it.qty
        return merged

    def reserve(self, db, order, items, now):
        merged = self._items(items)
        if merged:
            from .config import get_config
            stock_port().reserve(db, f"order:{order.id}", merged, get_config().pay_timeout_minutes * 60, now)

    def confirm(self, db, order, items, now):
        if self._items(items):
            stock_port().confirm(db, f"order:{order.id}")

    def release(self, db, order, items, now):
        if self._items(items):
            stock_port().release(db, f"order:{order.id}")

    def on_refunded(self, db, order, item, qty, amount_cents, restock, now):
        if restock and qty > 0 and item.sku_id:
            # 引用带“退完后的累计件数”：同一行多次部分退货各回补一次，同一次结算重放只回补一次
            stock_port().restock(db, item.sku_id, qty, f"refund:order:{order.id}:item:{item.id}:upto:{item.refunded_qty}")


# ---------------------------------------------------------------- 内置：service（线下服务）

ServiceCatalog = Callable[[Session, int, int | None], dict[str, Any] | None]
_service_catalog: ServiceCatalog | None = None


def set_service_catalog(fn: ServiceCatalog | None) -> None:
    """线下服务价目：fn(db, product_id, store_id) -> {name, price_cents, spec?, image?, limit_per_user?, returnable?} | None。"""
    global _service_catalog
    _service_catalog = fn


class ServiceKind(LineKind):
    """到店服务（课程、护理、维修…）：item = {kind: "service", product_id, qty}。支付后等待门店核销 / 完成（人工履约）。"""

    name = "service"

    def resolve(self, db, user_id, store_id, item, now):
        product_id = int(item.get("product_id") or 0)
        data = _service_catalog(db, product_id, store_id) if (_service_catalog and product_id) else None
        if data is None or not data.get("on_sale", True):
            raise AppError(ErrorCode.NOT_FOUND, "service not on sale", data={"product_id": product_id})
        return ResolvedLine(kind=self.name, product_id=product_id, name=data["name"],
                            unit_price_cents=int(data["price_cents"]), qty=int(item["qty"]),
                            goods_kind=data.get("goods_kind", GOODS_OFFLINE_SERVICE), spec=data.get("spec", ""),
                            image=data.get("image", ""), category_id=data.get("category_id"),
                            limit_per_user=data.get("limit_per_user"), returnable=data.get("returnable", True),
                            snapshot={"service": data})


# ---------------------------------------------------------------- 内置：member_card（29-member-card）


class MemberCardPort:
    """会员卡适配：读取卡种、发卡、退卡试算。默认委托 29-member-card 的导出。"""

    def product(self, db: Session, product_id: int) -> Any | None:
        import member_card
        return db.get(member_card.CardProduct, product_id)

    def issue(self, db: Session, product: Any, user_id: int, store_id: int | None, ref: str, paid_cents: int,
              paid_at: datetime) -> int:
        import member_card
        card = member_card.issue_card(db, product, user_id, store_id=store_id, source="purchase", issue_ref=ref,
                                      paid_cents=paid_cents, paid_at=paid_at, at=paid_at)
        return int(card.id)

    def refund_quote(self, db: Session, card_id: int, now: datetime) -> int:
        import member_card
        card = member_card.get_card(db, card_id)
        return int(member_card.quote_refund(db, card, member_card.CAUSE_CUSTOMER, now)["refund_cents"])


_member_card: MemberCardPort = MemberCardPort()


def set_member_card_port(port: MemberCardPort | None) -> None:
    global _member_card
    _member_card = port or MemberCardPort()


class MemberCardKind(LineKind):
    """会员卡（次卡 / 期限卡 / 时段卡）：item = {kind: "member_card", product_id, qty: 1}。

    支付成功同事务发卡（issue_ref = order_item:{id}，幂等）；售后退款上限由会员卡模块按已使用量计算。
    """

    name = "member_card"

    def resolve(self, db, user_id, store_id, item, now):
        product_id = int(item.get("product_id") or 0)
        qty = int(item["qty"])
        if qty != 1:
            raise AppError(ErrorCode.BAD_REQUEST, "member card lines must have qty 1")
        product = _member_card.product(db, product_id) if product_id else None
        if product is None or getattr(product, "status", "on") != "on" or getattr(product, "deleted_at", None):
            raise AppError(ErrorCode.NOT_FOUND, "card product not on sale", data={"product_id": product_id})
        if int(product.price_cents) <= 0:
            raise AppError(ErrorCode.BUSINESS_RULE, "free cards are issued by stores", data={"reason": "free_product"})
        return ResolvedLine(kind=self.name, product_id=product_id, name=product.name,
                            unit_price_cents=int(product.price_cents), qty=1,
                            goods_kind=getattr(product, "goods_kind", GOODS_OFFLINE_SERVICE) or GOODS_OFFLINE_SERVICE,
                            limit_per_user=getattr(product, "per_member_limit", None), returnable=False,
                            snapshot={"card_product": {"id": product_id, "name": product.name,
                                                       "kind": getattr(product, "kind", ""),
                                                       "price_cents": int(product.price_cents)}})

    def fulfil(self, db, order, item, now):
        product = _member_card.product(db, item.product_id)
        if product is None:
            raise AppError(ErrorCode.CONFLICT, "card product removed", data={"product_id": item.product_id})
        card_id = _member_card.issue(db, product, order.user_id, order.store_id, f"order_item:{item.id}",
                                     item.paid_cents, now)
        return f"member_card:{card_id}"

    def refund_cap(self, db, order, item, qty, now):
        if not item.fulfil_ref or not item.fulfil_ref.startswith("member_card:"):
            return None
        return _member_card.refund_quote(db, int(item.fulfil_ref.split(":", 1)[1]), now)


register_line_kind(GoodsKind())
register_line_kind(ServiceKind())
register_line_kind(MemberCardKind())


def snapshot_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, default=str, sort_keys=True)
