"""计价引擎：满减、折扣、优惠券的叠加与互斥，优惠按行分摊，以及按分摊结果计算部分退款。

纯函数，不访问数据库；金额一律为整数“分”（references/patterns/pricing.md）。

计价顺序（最常见的商户口径，方案中可调整）：
1. 商品小计 = Σ 单价 × 数量（单价取下单时的价格快照）。
2. 活动优惠（满减、折扣）按优先级在适用范围内计算；同一互斥组只取优惠最大的一个。
3. 优惠券在活动后的金额上计算（门槛按活动后的适用金额判断）；同一订单最多一张同组券。
4. 每一项优惠只在其适用的行之间按金额比例分摊，最大余数法保证分摊合计与优惠额完全相等。
5. 应付 = 小计 − 优惠 + 运费，不小于 0；优惠不会让任何一行变成负数。
"""
from __future__ import annotations

from dataclasses import dataclass, field

FULL_REDUCTION = "full_reduction"   # 满 threshold 减 amount
PERCENT = "percent"                 # 打 rate_percent 折（90 = 九折），可设 cap
COUPON_AMOUNT = "coupon_amount"     # 满 threshold 减 amount（券）
COUPON_PERCENT = "coupon_percent"   # 折扣券
COUPON_KINDS = {COUPON_AMOUNT, COUPON_PERCENT}
KINDS = {FULL_REDUCTION, PERCENT} | COUPON_KINDS


@dataclass(frozen=True)
class Line:
    sku_id: int
    qty: int
    unit_price_cents: int
    category: str = ""

    @property
    def amount(self) -> int:
        return self.qty * self.unit_price_cents


@dataclass(frozen=True)
class Promotion:
    id: str
    kind: str
    threshold_cents: int = 0
    amount_cents: int = 0
    rate_percent: int = 100
    rate_bp: int | None = None                 # 万分比折扣率（8850 = 八八五折），优先于 rate_percent
    cap_cents: int | None = None
    sku_ids: frozenset[int] = frozenset()      # 为空表示不限商品
    categories: frozenset[str] = frozenset()   # 为空表示不限分类
    line_indexes: frozenset[int] = frozenset() # 按行号限定（0 起），为空表示不按行号限定；用于券已算好适用行时
    exclusive_group: str = ""                  # 同组只取一个（取优惠最大者）
    priority: int = 100                        # 越小越先计算

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown promotion kind {self.kind}")
        if self.amount_cents < 0 or self.threshold_cents < 0 or not 0 < self.rate_percent <= 100:
            raise ValueError(f"invalid promotion values for {self.id}")
        if self.rate_bp is not None and not 0 < self.rate_bp <= 10000:
            raise ValueError(f"invalid rate_bp for {self.id}")

    @property
    def effective_rate_bp(self) -> int:
        return self.rate_bp if self.rate_bp is not None else self.rate_percent * 100

    def applies_to(self, line: Line, index: int | None = None) -> bool:
        if self.line_indexes and (index is None or index not in self.line_indexes):
            return False
        return (not self.sku_ids or line.sku_id in self.sku_ids) and (not self.categories or line.category in self.categories)


@dataclass
class Quote:
    subtotal_cents: int
    shipping_cents: int
    discounts: list[dict] = field(default_factory=list)          # [{"promotion_id", "amount_cents", "lines": [...]}]
    line_discount_cents: list[int] = field(default_factory=list)  # 每行累计分摊的优惠
    rejected: list[dict] = field(default_factory=list)            # [{"promotion_id", "reason"}]

    @property
    def discount_cents(self) -> int:
        return sum(item["amount_cents"] for item in self.discounts)

    @property
    def payable_cents(self) -> int:
        return max(0, self.subtotal_cents - self.discount_cents + self.shipping_cents)

    def line_paid_cents(self, lines: list[Line]) -> list[int]:
        return [line.amount - discount for line, discount in zip(lines, self.line_discount_cents)]


def allocate(amount: int, weights: list[int]) -> list[int]:
    """把 amount 按 weights 比例分成整数，合计严格等于 amount（最大余数法，余数相同按顺序）。"""
    if amount < 0 or any(weight < 0 for weight in weights):
        raise ValueError("amount and weights must be non-negative")
    total = sum(weights)
    if amount == 0 or total == 0:
        return [0] * len(weights)
    if amount > total:
        raise ValueError("cannot allocate more than the total weight")
    shares = [amount * weight // total for weight in weights]
    remainders = sorted(range(len(weights)), key=lambda i: (-(amount * weights[i] % total), i))
    for index in remainders[: amount - sum(shares)]:
        shares[index] += 1
    return shares


def _discount_for(promotion: Promotion, base: int) -> int:
    if base <= 0 or base < promotion.threshold_cents:
        return 0
    if promotion.kind in {FULL_REDUCTION, COUPON_AMOUNT}:
        value = promotion.amount_cents
    else:
        value = base * (10000 - promotion.effective_rate_bp) // 10000  # 优惠额向下取整到分：商户不多让利
    if promotion.cap_cents is not None:
        value = min(value, promotion.cap_cents)
    return min(value, base)


def quote(lines: list[Line], promotions: list[Promotion], shipping_cents: int = 0) -> Quote:
    if not lines:
        raise ValueError("empty order")
    if any(line.qty <= 0 or line.unit_price_cents < 0 for line in lines):
        raise ValueError("invalid line")
    result = Quote(subtotal_cents=sum(line.amount for line in lines), shipping_cents=shipping_cents,
                   line_discount_cents=[0] * len(lines))

    def remaining(index: int) -> int:
        return lines[index].amount - result.line_discount_cents[index]

    def apply(stage: list[Promotion]) -> None:
        # 同一互斥组：先试算，只保留优惠最大的一个
        chosen: dict[str, tuple[int, Promotion]] = {}
        free: list[Promotion] = []
        for promotion in sorted(stage, key=lambda p: (p.priority, p.id)):
            if not promotion.exclusive_group:
                free.append(promotion)
                continue
            scoped = [i for i, line in enumerate(lines) if promotion.applies_to(line, i)]
            value = _discount_for(promotion, sum(remaining(i) for i in scoped))
            best = chosen.get(promotion.exclusive_group)
            if best is None or value > best[0]:
                if best is not None:
                    result.rejected.append({"promotion_id": best[1].id, "reason": "互斥组内优惠较小"})
                chosen[promotion.exclusive_group] = (value, promotion)
            else:
                result.rejected.append({"promotion_id": promotion.id, "reason": "互斥组内优惠较小"})
        ordered = sorted(free + [item[1] for item in chosen.values()], key=lambda p: (p.priority, p.id))
        for promotion in ordered:
            scoped = [i for i, line in enumerate(lines) if promotion.applies_to(line, i)]
            base = sum(remaining(i) for i in scoped)
            value = _discount_for(promotion, base)
            if value == 0:
                reason = "无适用商品" if not scoped else ("未达门槛" if base < promotion.threshold_cents else "无可优惠金额")
                result.rejected.append({"promotion_id": promotion.id, "reason": reason})
                continue
            shares = allocate(value, [remaining(i) for i in scoped])
            for index, share in zip(scoped, shares):
                result.line_discount_cents[index] += share
            result.discounts.append({"promotion_id": promotion.id, "amount_cents": value,
                                     "lines": [{"index": i, "amount_cents": s} for i, s in zip(scoped, shares)]})

    apply([p for p in promotions if p.kind not in COUPON_KINDS])
    coupons = [p for p in promotions if p.kind in COUPON_KINDS]
    ungrouped = [p for p in coupons if not p.exclusive_group]
    if len(ungrouped) > 1:
        # 未声明分组的券默认互斥：一单一券
        coupons = [p for p in coupons if p.exclusive_group] + [
            Promotion(**{**p.__dict__, "exclusive_group": "__coupon__"}) for p in ungrouped
        ]
    apply(coupons)
    return result


def refund_amount(paid_line_cents: int, qty: int, refunded_qty: int, refund_qty: int) -> int:
    """按行实付金额计算本次退 refund_qty 件应退金额。

    使用累计公式 paid × (已退 + 本次) // 数量 − paid × 已退 // 数量，
    无论分几次退，全部退完时累计金额严格等于该行实付。
    """
    if qty <= 0 or refund_qty <= 0 or refunded_qty < 0 or refunded_qty + refund_qty > qty:
        raise ValueError("invalid refund quantity")
    return paid_line_cents * (refunded_qty + refund_qty) // qty - paid_line_cents * refunded_qty // qty
