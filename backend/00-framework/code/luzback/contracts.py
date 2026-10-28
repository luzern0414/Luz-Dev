"""跨模块共享的数据结构（只放纯数据，不放逻辑），避免模块之间互相导入对方的 ORM。

报价（Quote）是订单、优惠券、活动、会员、配送费共同操作的对象：
    catalog 产出 QuoteLine → campaign.apply 填 campaign_alloc → coupon.compute 填 coupon_alloc
    → membership 填 member_alloc → points 填 points_alloc → geo 填 delivery → order 汇总 total。
所有金额整数分；分摊余数给最后一行。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class QuoteLine:
    product_id: int
    sku_id: int | None
    name: str
    unit_price_cents: int
    qty: int
    category_id: int | None = None
    set_meal_id: int | None = None
    selections: dict[str, Any] | None = None
    spec_text: str = ""
    image_url: str = ""
    tags: list[str] = field(default_factory=list)
    member_price_cents: int | None = None
    campaign_priced: bool = False          # 已享活动改价（券 / 会员叠加规则用）
    campaign_alloc_cents: int = 0
    coupon_alloc_cents: int = 0
    member_alloc_cents: int = 0
    points_alloc_cents: int = 0

    @property
    def gross_cents(self) -> int:
        return self.unit_price_cents * self.qty

    @property
    def net_cents(self) -> int:
        return self.gross_cents - self.campaign_alloc_cents - self.coupon_alloc_cents - self.member_alloc_cents - self.points_alloc_cents


@dataclass
class Quote:
    store_id: int
    user_id: int
    channel: str                            # dine_in | delivery | pickup
    lines: list[QuoteLine]
    at: datetime
    address_id: int | None = None
    table_id: int | None = None
    coupon_user_id: int | None = None
    points_used: int = 0
    expected_at: datetime | None = None
    remark: str = ""
    # 由各模块填写
    distance_m: int | None = None
    delivery_cents: int = 0
    packaging_cents: int = 0
    campaign_discount_cents: int = 0
    coupon_cents: int = 0
    member_discount_cents: int = 0
    points_cents: int = 0
    participations: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)   # 可读说明（"优惠券已失效，已为你去掉"）

    @property
    def items_cents(self) -> int:
        return sum(line.gross_cents for line in self.lines)

    @property
    def discounted_items_cents(self) -> int:
        return self.items_cents - self.campaign_discount_cents - self.coupon_cents - self.member_discount_cents

    @property
    def total_cents(self) -> int:
        return max(0, self.discounted_items_cents - self.points_cents) + self.delivery_cents + self.packaging_cents

    def to_totals(self) -> dict[str, int]:
        return {
            "items_cents": self.items_cents,
            "campaign_discount_cents": self.campaign_discount_cents,
            "coupon_cents": self.coupon_cents,
            "member_discount_cents": self.member_discount_cents,
            "points_cents": self.points_cents,
            "delivery_cents": self.delivery_cents,
            "packaging_cents": self.packaging_cents,
            "total_cents": self.total_cents,
        }


def allocate(amount_cents: int, weights: list[int]) -> list[int]:
    """按权重比例分摊金额，余数给最后一个非零权重项；权重全 0 时返回全 0。"""
    total = sum(weights)
    if amount_cents <= 0 or total <= 0:
        return [0 for _ in weights]
    shares = [amount_cents * w // total for w in weights]
    remainder = amount_cents - sum(shares)
    for index in range(len(weights) - 1, -1, -1):
        if weights[index] > 0:
            shares[index] += remainder
            break
    return shares


@dataclass
class RangeResult:
    in_range: bool
    distance_m: int
    reason: str = ""          # out_of_range | route_unavailable | ok


@dataclass
class PaymentInfo:
    payment_id: int
    business_type: str
    business_id: int
    user_id: int
    amount_cents: int
    out_trade_no: str
    channel_txn_id: str | None = None
    paid_at: datetime | None = None


@dataclass
class RefundInfo:
    refund_id: int
    payment_id: int
    business_type: str
    business_id: int
    business_ref: str
    amount_cents: int
    status: str
    out_refund_no: str
