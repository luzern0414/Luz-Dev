"""纯规则：选奖、分布（10 万次统计检验）、奖池校验、有利变更、脱敏、配置法定上限。"""
import math
from datetime import datetime, timedelta, timezone

import pytest

from lottery import rules
from lottery.config import LotteryConfig, reset_config, set_config
from lottery.hooks import FixedRandom, SecretsRandom, SeededRandom
from luzback import AppError, ErrorCode

ODDS = [
    {"prize_id": 11, "seq": 1, "name": "A", "kind": "coupon", "weight_bp": 1000, "value_cents": 1000},
    {"prize_id": 12, "seq": 2, "name": "B", "kind": "points", "weight_bp": 2000, "value_cents": 100},
    {"prize_id": 13, "seq": 3, "name": "C", "kind": "physical", "weight_bp": 100, "value_cents": 80000},
    {"prize_id": 14, "seq": 4, "name": "D", "kind": "member_card_days", "weight_bp": 900, "value_cents": 3000},
    {"prize_id": 15, "seq": 5, "name": "谢谢", "kind": "thanks", "weight_bp": 6000, "value_cents": 0},
]


def _pool(**over):
    base = [
        {"seq": 1, "name": "券", "kind": "coupon", "ref_id": 1, "quantity": 1, "value_cents": 1000, "weight_bp": 4000,
         "stock_total": 10, "description": "说明"},
        {"seq": 2, "name": "谢谢", "kind": "thanks", "weight_bp": 6000},
    ]
    base[0].update(over)
    return base


@pytest.mark.parametrize("r,expected", [(0, 11), (999, 11), (1000, 12), (2999, 12), (3000, 13), (3099, 13),
                                        (3100, 14), (3999, 14), (4000, 15), (9999, 15)])
def test_pick_boundaries(r, expected):
    prize, total = rules.pick(ODDS, r)
    assert prize["prize_id"] == expected and total == 10000


def test_pick_rejects_out_of_range():
    with pytest.raises(ValueError):
        rules.pick(ODDS, 10000)
    with pytest.raises(ValueError):
        rules.pick(ODDS, -1)


def test_distribution_100k_seeded_within_tolerance():
    """10 万次（固定种子）：每个奖项观测频率偏差 < 5σ，且卡方检验 p > 0.001。"""
    n = 100_000
    counts = rules.simulate(ODDS, n, SeededRandom(20261028))
    chi2 = 0.0
    for o in ODDS:
        p = o["weight_bp"] / 10000
        expected = n * p
        sigma = math.sqrt(n * p * (1 - p))
        observed = counts[o["prize_id"]]
        assert abs(observed - expected) < 5 * sigma, (o["name"], observed, expected)
        assert abs(observed / n - p) < 0.01
        chi2 += (observed - expected) ** 2 / expected
    assert chi2 < 18.467  # df=4, α=0.001


def test_distribution_with_secrets_source_is_sane():
    counts = rules.simulate(ODDS, 20_000, SecretsRandom())
    assert sum(counts.values()) == 20_000
    assert counts[15] > counts[11] > counts[13]


def test_restricted_pool_excludes_prize_and_renormalises():
    available = {11, 12, 15}
    assert rules.pool_range(ODDS, available) == 9000
    prize, total = rules.pick(ODDS, 3000, available)
    assert total == 9000 and prize["prize_id"] == 15


def test_replay_matches_recorded_pool():
    assert rules.replay(ODDS, 3050, 10000) == 13
    with pytest.raises(ValueError):
        rules.replay(ODDS, 3050, 9000)


def test_fixed_random_modulo():
    assert FixedRandom(10003).randbelow(10000) == 3


def test_validate_pool_happy():
    rules.validate_pool(_pool(), 5_000_000, 10_000, 12)


@pytest.mark.parametrize("pool,code", [
    (_pool(weight_bp=3000), ErrorCode.BUSINESS_RULE),                       # 合计 ≠ 10000
    (_pool(value_cents=5_000_001), ErrorCode.BUSINESS_RULE),                # 超 5 万
    (_pool(weight_bp=0), ErrorCode.BAD_REQUEST),                            # 不可能中出的奖项
    (_pool(stock_total=100), ErrorCode.BUSINESS_RULE),                      # 超预算 1000×100 > 10000
    (_pool(description=""), ErrorCode.BAD_REQUEST),                         # 券须写兑换规则
    (_pool(ref_id=None), ErrorCode.BAD_REQUEST),                            # 券须关联模板
])
def test_validate_pool_rejects(pool, code):
    with pytest.raises(AppError) as exc:
        rules.validate_pool(pool, 5_000_000, 10_000, 12)
    assert exc.value.code == code


def test_validate_pool_requires_thanks_and_real_prize():
    no_thanks = [_pool()[0] | {"weight_bp": 10000}, ]
    no_thanks.append(dict(no_thanks[0], seq=2))
    no_thanks[0]["weight_bp"] = no_thanks[1]["weight_bp"] = 5000
    with pytest.raises(AppError) as exc:
        rules.validate_pool(no_thanks, 5_000_000, 100_000, 12)
    assert "thanks" in exc.value.message
    only_thanks = [{"seq": 1, "name": "t1", "kind": "thanks", "weight_bp": 5000},
                   {"seq": 2, "name": "t2", "kind": "thanks", "weight_bp": 5000}]
    with pytest.raises(AppError):
        rules.validate_pool(only_thanks, 5_000_000, 100_000, 12)


def test_cap_exactly_50000_yuan_is_allowed():
    rules.validate_prize(_pool(value_cents=5_000_000)[0], 5_000_000)


def test_unlimited_prize_needs_budget_headroom():
    pool = _pool(stock_total=None)
    rules.validate_pool(pool, 5_000_000, 10_000, 12)
    pool2 = _pool(stock_total=10)
    pool2.append({"seq": 3, "name": "积分", "kind": "points", "quantity": 1, "value_cents": 1, "weight_bp": 0,
                  "stock_total": None, "description": "x"})
    pool2[1]["weight_bp"] = 5999
    pool2[2]["weight_bp"] = 1
    with pytest.raises(AppError):
        rules.validate_pool(pool2, 5_000_000, 10_000, 12)  # 有限奖品已占满预算


T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize("field,old,new,ok", [
    ("end_at", T0, T0 + timedelta(days=1), True),
    ("end_at", T0, T0 - timedelta(days=1), False),
    ("free_daily_chances", 1, 2, True),
    ("free_daily_chances", 2, 1, False),
    ("limit_daily", 3, 5, True),
    ("limit_daily", 3, 0, True),
    ("limit_daily", 0, 3, False),
    ("limit_daily", 5, 3, False),
    ("budget_cents", 100, 200, True),
    ("budget_cents", 200, 100, False),
    ("earn_payment_min_cents", 0, 5000, True),
    ("earn_payment_min_cents", 5000, 3000, True),
    ("earn_payment_min_cents", 3000, 5000, False),
    ("name", "a", "b", False),
    ("rules_text", "a", "b", False),
])
def test_is_favourable(field, old, new, ok):
    assert rules.is_favourable(field, old, new) is ok


def test_mask_member_and_periods():
    assert rules.mask_member(123456) == "用户****3456"
    assert rules.mask_member(1, "张三丰") == "张**"
    late = datetime(2026, 10, 27, 16, 30, tzinfo=timezone.utc)  # 上海 10-28 00:30
    assert rules.day_key(late, "Asia/Shanghai") == "2026-10-28"
    assert rules.month_key(datetime(2026, 10, 31, 16, 0, tzinfo=timezone.utc), "Asia/Shanghai") == "2026-11"
    assert rules.percent(125) == "1.25%"


def test_config_cannot_relax_legal_limits():
    with pytest.raises(ValueError):
        LotteryConfig(prize_value_cap_cents=5_000_001).validate()
    with pytest.raises(ValueError):
        LotteryConfig(retention_days=729).validate()
    with pytest.raises(ValueError):
        LotteryConfig(publicity_threshold_cents=60_000).validate()
    with pytest.raises(ValueError):
        set_config(retention_days=100)
    assert set_config(prize_value_cap_cents=1_000_000).prize_value_cap_cents == 1_000_000
    reset_config()
