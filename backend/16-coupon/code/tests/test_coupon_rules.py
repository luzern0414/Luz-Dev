"""纯函数规则：模板校验、规则文案、抵扣计算（经 17-campaign pricing）、分摊、叠加、发布后编辑。"""
from datetime import timedelta

import pytest

import pricing
from coupon import rules as R
from coupon.rules import CartLine
from luzback import AppError, ErrorCode

from coupon_fixtures import NOW, template_data


def valid(**kw):
    return R.validate_template(template_data(**kw))


def rules_of(**kw):
    return R.snapshot(valid(**kw))


# ---------------------------------------------------------------- 模板校验


@pytest.mark.parametrize("overrides", [
    dict(type="percent_off", rate_bp=8550),                                  # 不是 100 的倍数
    dict(type="percent_off", rate_bp=10000),                                 # 不打折
    dict(type="exchange", scope_type="all"),                                 # 兑换券必须指定范围
    dict(type="amount_off", amount_cents=0),
    dict(valid_type="fixed"),                                                # 缺固定区间
    dict(valid_type="relative", valid_days=None),
    dict(scope_type="categories", scope_ids=[]),
    dict(scope_type="all", scope_ids=[1]),
    dict(per_member_limit=1, per_member_daily_limit=2),
    dict(audience={"type": "levels", "levels": []}),
    dict(channels=["claim_center"], trigger_topic="member.registered"),
    dict(channels=["nowhere"]),
    dict(claim_start_at=NOW, claim_end_at=NOW),
])
def test_invalid_templates_are_rejected(overrides):
    with pytest.raises(AppError) as error:
        valid(**overrides)
    assert error.value.code == ErrorCode.BAD_REQUEST


def test_fixed_validity_claim_end_cannot_pass_use_end():
    with pytest.raises(AppError):
        valid(valid_type="fixed", use_start_at=NOW, use_end_at=NOW + timedelta(days=5),
              claim_end_at=NOW + timedelta(days=6))
    ok = valid(valid_type="fixed", use_start_at=NOW, use_end_at=NOW + timedelta(days=5),
               claim_end_at=NOW + timedelta(days=5))
    assert ok["valid_days"] is None


def test_validity_window_relative_with_start_delay():
    f = valid(valid_days=7, start_delay_days=1)
    start, end = R.validity_window(f, NOW)
    assert start == NOW + timedelta(days=1) and end == NOW + timedelta(days=8)


def test_describe_discloses_threshold_scope_and_stacking():
    assert R.describe(rules_of()) == "满100元减20元；全场通用；不可与其他优惠券同时使用"
    text = R.describe(rules_of(type="percent_off", rate_bp=8500, max_discount_cents=3000, threshold_cents=0,
                               scope_type="categories", scope_ids=[3], stack_group="A", stack_with_campaign=False))
    assert text == "8.5折；最多优惠30元；限指定分类；可与其他组的优惠券叠加；不与活动优惠同享"
    assert "兑换适用商品2件" in R.describe(rules_of(type="exchange", exchange_qty=2, scope_type="products",
                                                   scope_ids=[9], threshold_cents=0))
    assert R.describe_validity(valid(valid_days=7, start_delay_days=2)) == "领取后第2天生效，有效7天"


# ---------------------------------------------------------------- 抵扣计算


LINES = [
    CartLine(sku_id=11, product_id=1, qty=2, unit_price_cents=3000, category_id=3),      # 6000
    CartLine(sku_id=12, product_id=2, qty=1, unit_price_cents=5000, category_id=4),      # 5000
    CartLine(sku_id=13, product_id=3, qty=1, unit_price_cents=20000, product_kind="member_card"),
]


def test_threshold_counts_only_scope_lines_and_reports_shortfall():
    r = rules_of(scope_type="categories", scope_ids=[3])  # 范围内 6000 < 10000
    ev = R.evaluate([("c", r)], LINES)
    assert ev.rejected["c"].reason == "below_threshold"
    assert ev.rejected["c"].data["shortfall_cents"] == 4000
    assert ev.rejected["c"].message == "还差40元可用"
    ev = R.evaluate([("c", rules_of(scope_type="categories", scope_ids=[3, 4]))], LINES)  # 11000 ≥ 10000
    assert ev.discounts["c"]["amount_cents"] == 2000
    assert sum(s["amount_cents"] for s in ev.discounts["c"]["lines"]) == 2000
    assert ev.line_coupon_cents[2] == 0  # 会员卡行不在范围内


def test_percent_coupon_with_cap_and_floor_rounding():
    r = rules_of(type="percent_off", rate_bp=9000, threshold_cents=0, max_discount_cents=1500)
    ev = R.evaluate([("c", r)], LINES)  # 31000 × 10% = 3100 → 封顶 1500
    assert ev.discounts["c"]["amount_cents"] == 1500
    small = [CartLine(sku_id=1, product_id=1, qty=1, unit_price_cents=999)]
    ev = R.evaluate([("c", rules_of(type="percent_off", rate_bp=9000, threshold_cents=0))], small)
    # pricing 按“应付 = 金额 × 折扣 // 100”向下取整，优惠额因此向上取整（999 × 九折：应付 899，优惠 100）
    assert ev.discounts["c"]["amount_cents"] == 100


def test_exchange_coupon_frees_cheapest_units_of_scope():
    r = rules_of(type="exchange", exchange_qty=1, threshold_cents=0, scope_type="products", scope_ids=[1, 2])
    ev = R.evaluate([("c", r)], LINES)
    assert ev.discounts["c"]["amount_cents"] == 3000
    r2 = rules_of(type="exchange", exchange_qty=3, threshold_cents=0, scope_type="products", scope_ids=[1, 2])
    assert R.evaluate([("c", r2)], LINES).discounts["c"]["amount_cents"] == 3000 + 3000 + 5000


def test_member_card_product_kind_scope_and_exclusions():
    r = rules_of(scope_type="product_kinds", scope_ids=["member_card"], threshold_cents=0, amount_cents=5000)
    ev = R.evaluate([("c", r)], LINES)
    assert ev.discounts["c"]["lines"] == [{"index": 2, "amount_cents": 5000}]
    excluded = rules_of(threshold_cents=0, exclude_product_ids=[1, 2, 3])
    assert R.evaluate([("c", excluded)], LINES).rejected["c"].reason == "no_scope_items"


def test_coupon_threshold_uses_amount_after_campaign_and_stacking_flag():
    campaign = [pricing.Promotion("满100减30", pricing.FULL_REDUCTION, threshold_cents=10000, amount_cents=3000,
                                  sku_ids=frozenset({11, 12}))]
    lines = LINES[:2]  # 11000 → 活动后 8000
    ev = R.evaluate([("c", rules_of(threshold_cents=10000))], lines, campaign)
    assert ev.rejected["c"].reason == "below_threshold" and ev.rejected["c"].data["shortfall_cents"] == 2000
    ev = R.evaluate([("c", rules_of(threshold_cents=5000))], lines, campaign)
    assert ev.discounts["c"]["amount_cents"] == 2000
    assert ev.quote.payable_cents == 11000 - 3000 - 2000
    no_stack = rules_of(threshold_cents=0, stack_with_campaign=False)
    assert R.evaluate([("c", no_stack)], lines, campaign).rejected["c"].reason == "campaign_conflict"
    priced = [CartLine(sku_id=1, product_id=1, qty=1, unit_price_cents=5000, campaign_priced=True),
              CartLine(sku_id=2, product_id=2, qty=1, unit_price_cents=5000)]
    ev = R.evaluate([("c", no_stack)], priced)
    assert ev.discounts["c"]["lines"] == [{"index": 1, "amount_cents": 2000}]


def test_allocation_sums_exactly_with_largest_remainder():
    lines = [CartLine(sku_id=i, product_id=i, qty=1, unit_price_cents=p) for i, p in enumerate([3333, 3333, 3334], 1)]
    ev = R.evaluate([("c", rules_of(threshold_cents=0, amount_cents=1000))], lines)
    shares = [s["amount_cents"] for s in ev.discounts["c"]["lines"]]
    assert sum(shares) == 1000 and max(shares) - min(shares) <= 1
    assert ev.quote.line_paid_cents([pricing.Line(i, 1, l.unit_price_cents) for i, l in enumerate(lines)]) == [
        l.amount - s for l, s in zip(lines, ev.line_coupon_cents)]


def test_two_coupons_from_different_groups_both_apply():
    a = rules_of(threshold_cents=0, amount_cents=1000, stack_group="shop")
    b = rules_of(type="percent_off", rate_bp=9000, threshold_cents=0, stack_group="platform")
    ev = R.evaluate([("a", a), ("b", b)], LINES)
    assert set(ev.discounts) == {"a", "b"}
    assert ev.coupon_cents == sum(ev.line_coupon_cents)


def test_stack_conflicts():
    assert not R.stack_conflicts([""])
    assert R.stack_conflicts(["", "A"])
    assert R.stack_conflicts(["A", "A"])
    assert not R.stack_conflicts(["A", "B"])


def test_invalid_lines_and_coupon_kinds_in_campaign():
    with pytest.raises(AppError):
        R.evaluate([], [])
    with pytest.raises(AppError):
        R.evaluate([], [CartLine(sku_id=1, product_id=1, qty=0, unit_price_cents=1)])
    with pytest.raises(ValueError):
        R.evaluate([], LINES, [pricing.Promotion("x", pricing.COUPON_AMOUNT, amount_cents=1)])


# ---------------------------------------------------------------- 发布后编辑


def test_unfavorable_changes_detected():
    old = rules_of(scope_type="categories", scope_ids=[3])
    assert R.unfavorable_changes(old, {**old, "amount_cents": 1500}) == ["amount_cents"]
    assert R.unfavorable_changes(old, {**old, "threshold_cents": 20000}) == ["threshold_cents"]
    assert R.unfavorable_changes(old, {**old, "scope_ids": [4]}) == ["scope"]
    assert R.unfavorable_changes(old, {**old, "amount_cents": 3000, "threshold_cents": 5000,
                                       "scope_type": "all", "scope_ids": []}) == []
    assert R.unfavorable_changes(old, {**old, "stack_with_campaign": False}) == ["stack_with_campaign"]


def test_check_published_edit():
    old = valid(total_stock=100, per_member_limit=2, claim_end_at=NOW + timedelta(days=10))
    R.check_published_edit(old, {**old, "total_stock": 200, "per_member_limit": 3,
                                 "claim_end_at": NOW + timedelta(days=20)}, issued_count=50)
    for change in ({"total_stock": 40}, {"per_member_limit": 1}, {"claim_end_at": NOW + timedelta(days=5)},
                   {"type": "percent_off"}, {"store_id": 9}, {"valid_type": "fixed"}):
        with pytest.raises(AppError) as error:
            R.check_published_edit(old, {**old, **change}, issued_count=50)
        assert error.value.code == ErrorCode.BUSINESS_RULE


def test_code_generation_and_normalization():
    code = R.generate_code(12)
    assert len(code) == 12 and set(code) <= set(R.CODE_ALPHABET)
    assert R.normalize_code(" abcd-efgh 2345 ") == "ABCDEFGH2345"
