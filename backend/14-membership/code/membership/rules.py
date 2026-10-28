"""membership 纯函数规则：积分换算、抵扣上限、追回比例、欠分抵销、等级判定、签到。

全部整数运算；便于单测，不依赖数据库。
"""
from __future__ import annotations

from collections.abc import Iterable

SIGNIN_BASE_POINTS = 5
SIGNIN_STREAK_BONUS_MAX = 7


def points_for_order(paid_cents: int, per_yuan: int, multiplier_bp: int = 10000) -> int:
    """规则 1：floor(paid / 100) × per_yuan × multiplier_bp / 10000。"""
    if paid_cents <= 0 or per_yuan <= 0:
        return 0
    return (paid_cents // 100) * per_yuan * multiplier_bp // 10000


def redeem_cents(points: int, rate: int) -> int:
    """rate 积分 = 1 元（100 分）。"""
    if points <= 0 or rate <= 0:
        return 0
    return points * 100 // rate


def floor_to_rate(points: int, rate: int) -> int:
    if points <= 0 or rate <= 0:
        return 0
    return points - points % rate


def max_redeemable_points(discounted_cents: int, cap_bp: int, rate: int, balance: int) -> int:
    """规则 2：抵扣 ≤ 折后金额 × cap_bp，且 ≤ 余额，且为 rate 的整数倍。"""
    if discounted_cents <= 0 or balance <= 0:
        return 0
    cap_cents = discounted_cents * cap_bp // 10000
    cap_points = cap_cents * rate // 100
    return floor_to_rate(min(cap_points, balance), rate)


def clawback_due(earned: int, refund_cents: int, paid_cents: int) -> int:
    """规则 3：按退款比例追回；全额退款追回全部；余数向上取整，避免少追。"""
    if earned <= 0 or refund_cents <= 0 or paid_cents <= 0:
        return 0
    if refund_cents >= paid_cents:
        return earned
    return -(-earned * refund_cents // paid_cents)


def split_debt(points: int, debt: int) -> tuple[int, int]:
    """入账先抵销欠分：返回 (抵销的欠分, 实际入余额的积分)。"""
    used = min(max(debt, 0), max(points, 0))
    return used, max(points, 0) - used


def free_level_for(levels: Iterable, growth_cents: int, balance: int) -> int:
    """满足阈值的最高免费等级（free / spend / points）；没有配置等级时为 1。"""
    best = 1
    for lv in levels:
        if lv.threshold_type == "free":
            ok = True
        elif lv.threshold_type == "spend":
            ok = growth_cents >= lv.threshold_value
        elif lv.threshold_type == "points":
            ok = balance >= lv.threshold_value
        else:
            ok = False
        if ok and lv.level > best:
            best = lv.level
    return best


def signin_points(streak_days: int) -> int:
    """签到：基础 5 分，连续签到每天 +1，加成最多 7。"""
    streak = max(streak_days, 1)
    return SIGNIN_BASE_POINTS + min(streak - 1, SIGNIN_STREAK_BONUS_MAX)
