"""计价引擎：分摊精确、满减门槛、互斥组、券在活动后计算、应付不为负、部分退款累计等于实付。"""
import random

import pytest

from pricing import (
    COUPON_AMOUNT, COUPON_PERCENT, FULL_REDUCTION, PERCENT, Line, Promotion, allocate, quote, refund_amount,
)

LINES = [Line(1, 2, 3000, "课程"), Line(2, 1, 1000, "饮品"), Line(3, 3, 333, "饮品")]  # 小计 7999


def test_allocate_is_exact_and_proportional():
    assert allocate(100, [300, 300, 300]) == [34, 33, 33]
    assert allocate(0, [5, 5]) == [0, 0]
    rng = random.Random(7)
    for _ in range(500):
        weights = [rng.randint(0, 50000) for _ in range(rng.randint(1, 8))]
        amount = rng.randint(0, sum(weights))
        shares = allocate(amount, weights)
        assert sum(shares) == amount
        assert all(0 <= s <= w for s, w in zip(shares, weights))
    with pytest.raises(ValueError):
        allocate(10, [3, 4])


def test_full_reduction_threshold_and_allocation():
    q = quote(LINES, [Promotion("满79减10", FULL_REDUCTION, threshold_cents=7900, amount_cents=1000)])
    assert q.discount_cents == 1000 and q.payable_cents == 6999
    assert sum(q.line_discount_cents) == 1000
    miss = quote(LINES, [Promotion("满100减10", FULL_REDUCTION, threshold_cents=10000, amount_cents=1000)])
    assert miss.discount_cents == 0 and miss.rejected[0]["reason"] == "未达门槛"


def test_scope_limits_discount_to_matching_lines():
    q = quote(LINES, [Promotion("饮品8折", PERCENT, rate_percent=80, categories=frozenset({"饮品"}))])
    assert q.line_discount_cents[0] == 0
    assert q.discount_cents == 1999 - 1999 * 80 // 100  # 向下取整到分


def test_exclusive_group_keeps_best_only():
    promos = [Promotion("A满减", FULL_REDUCTION, threshold_cents=5000, amount_cents=500, exclusive_group="活动"),
              Promotion("B九折", PERCENT, rate_percent=90, exclusive_group="活动")]
    q = quote(LINES, promos)
    assert [d["promotion_id"] for d in q.discounts] == ["B九折"]  # 799 > 500
    assert q.rejected == [{"promotion_id": "A满减", "reason": "互斥组内优惠较小"}]


def test_coupon_threshold_uses_amount_after_activities():
    promos = [Promotion("满减", FULL_REDUCTION, threshold_cents=7000, amount_cents=1500),
              Promotion("满70减5券", COUPON_AMOUNT, threshold_cents=7000, amount_cents=500)]
    q = quote(LINES, promos)
    assert [d["promotion_id"] for d in q.discounts] == ["满减"]  # 活动后 6499 未达券门槛
    assert any(r["promotion_id"] == "满70减5券" for r in q.rejected)


def test_only_one_ungrouped_coupon_applies():
    promos = [Promotion("券1", COUPON_AMOUNT, amount_cents=300), Promotion("券2", COUPON_PERCENT, rate_percent=95)]
    q = quote(LINES, promos)
    assert len([d for d in q.discounts]) == 1 and q.discounts[0]["promotion_id"] == "券2"  # 399 > 300


def test_payable_never_negative_and_lines_never_negative():
    q = quote([Line(1, 1, 500)], [Promotion("大额券", COUPON_AMOUNT, amount_cents=99999)], shipping_cents=200)
    assert q.discount_cents == 500 and q.payable_cents == 200
    assert q.line_paid_cents([Line(1, 1, 500)]) == [0]


def test_refund_amount_sums_to_line_paid_regardless_of_split():
    rng = random.Random(11)
    for _ in range(300):
        qty = rng.randint(1, 9)
        paid = rng.randint(0, 100000)
        refunded, total = 0, 0
        while refunded < qty:
            step = rng.randint(1, qty - refunded)
            total += refund_amount(paid, qty, refunded, step)
            refunded += step
        assert total == paid
    with pytest.raises(ValueError):
        refund_amount(1000, 2, 1, 2)


def test_random_quotes_are_consistent():
    rng = random.Random(3)
    for _ in range(200):
        lines = [Line(i, rng.randint(1, 4), rng.randint(1, 9999), rng.choice(["a", "b"])) for i in range(rng.randint(1, 5))]
        promos = [Promotion("满减", FULL_REDUCTION, threshold_cents=rng.randint(0, 20000), amount_cents=rng.randint(0, 3000)),
                  Promotion("折", PERCENT, rate_percent=rng.randint(50, 100), categories=frozenset({"a"})),
                  Promotion("券", COUPON_AMOUNT, threshold_cents=rng.randint(0, 20000), amount_cents=rng.randint(0, 5000))]
        q = quote(lines, promos)
        assert sum(q.line_discount_cents) == q.discount_cents
        assert all(paid >= 0 for paid in q.line_paid_cents(lines))
        assert q.payable_cents == q.subtotal_cents - q.discount_cents
