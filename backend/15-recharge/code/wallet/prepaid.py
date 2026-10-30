"""预付式消费限额策略（PrepaidPolicy）：储值（15）与会员卡（29）共用的扩展点。

默认 `CONSERVATIVE`：取各地已生效规定中较严的一组，项目按经营地与行业切换预设或自定义。

| 预设 | 依据 | 要点 |
|---|---|---|
| conservative（默认） | 单用途卡办法第十八条 + 上海健身试行办法第八条 | 储值单笔 / 余额 ≤ 5000 元；期限卡 ≤ 5000 元且 ≤ 24 个月；次卡 ≤ 20000 元且 ≤ 60 次；同一会员合计 ≤ 20000 元；赠送 ≤ 实付 25% |
| shanghai_fitness_2025 | 沪体规文〔2025〕1号 第八条（2025-03-01） | 会籍类 ≤ 5000 元 / 24 个月；课时类 ≤ 20000 元 / 60 节；储值类 ≤ 5000 元；合计 ≤ 20000 元；无赠送上限 |
| shanghai_fitness_draft_2026 | 上海修订征求意见稿（未生效） | 会籍类不限额、≤ 12 个月；合计 ≤ 20000 元；赠送 ≤ 预收 25% |
| beijing_2022 | 北京市单用途预付卡管理条例（2022-06-01） | 条例未设金额上限；7 日未兑付全额退、5 日内退还；其他退款无约定 15 日内退还 |
| national_single_use_card | 单用途商业预付卡管理办法第十八条 | 记名卡单张（含充值后余额）≤ 5000 元 |

None 表示不限。限额按“一次收取”与“续约后余额”双重校验（上海办法：续约后余额、期限、次数仍不能超过上限）。
同一会员合计 = 所有已注册的“未兑付余额提供者”之和（储值本金 + 会员卡未兑付价值），见 `register_outstanding_provider`。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace

from sqlalchemy.orm import Session

from luzback import AppError, ErrorCode, current_config

CATEGORY_PERIOD = "period"            # 会籍类（期限卡、时段卡）
CATEGORY_COUNT = "count"              # 课时类（次卡）
CATEGORY_STORED_VALUE = "stored_value"  # 储值类


@dataclass(frozen=True)
class PrepaidPolicy:
    name: str
    period_max_cents: int | None = 500_000
    period_max_days: int | None = 730            # 24 个月按 730 天计
    count_max_cents: int | None = 2_000_000
    count_max_sessions: int | None = 60
    stored_value_max_cents: int | None = 500_000          # 单笔充值上限
    stored_value_max_balance_cents: int | None = 500_000  # 充值后余额（本金 + 赠送）上限
    member_total_max_cents: int | None = 2_000_000        # 同一会员各类预付款合计
    gift_ratio_max_bp: int | None = 2500                  # 赠送价值 ≤ 实付 × bp / 10000
    cooling_off_days: int = 7                             # 法释〔2025〕4号第十四条：付款之日起七日
    cooling_off_refund_days: int = 5                      # 北京条例第十六条：5 日内全额退还
    refund_deadline_days: int = 15                        # 北京条例第十七条：无约定 15 日内退还

    # ------------------------------------------------------------ checks

    def check_gift(self, paid_value: int, gift_value: int) -> None:
        """paid_value / gift_value 为同一计量（分、次或天）。"""
        if self.gift_ratio_max_bp is None or gift_value <= 0:
            return
        if gift_value * 10000 > paid_value * self.gift_ratio_max_bp:
            raise AppError(ErrorCode.LIMIT_REACHED, "赠送超过实付的允许比例",
                           {"reason": "gift_ratio_exceeded", "max_bp": self.gift_ratio_max_bp})

    def check_card(self, category: str, price_cents: int, units: int) -> None:
        """会员卡单次收取：期限卡 units=天数，次卡 units=次数（含赠送）。"""
        if category == CATEGORY_PERIOD:
            cap_cents, cap_units, unit = self.period_max_cents, self.period_max_days, "days"
        elif category == CATEGORY_COUNT:
            cap_cents, cap_units, unit = self.count_max_cents, self.count_max_sessions, "sessions"
        else:
            raise ValueError(f"unknown category {category}")
        if cap_cents is not None and price_cents > cap_cents:
            raise AppError(ErrorCode.LIMIT_REACHED, "单次预收金额超过上限",
                           {"reason": "prepaid_amount_cap", "category": category, "max_cents": cap_cents})
        if cap_units is not None and units > cap_units:
            raise AppError(ErrorCode.LIMIT_REACHED, "预收期限或次数超过上限",
                           {"reason": "prepaid_units_cap", "category": category, f"max_{unit}": cap_units})

    def check_card_after_renewal(self, category: str, outstanding_units: int, new_units: int) -> None:
        """续约后剩余次数 / 期限仍不得超过上限（上海办法第八条）。"""
        cap = self.period_max_days if category == CATEGORY_PERIOD else self.count_max_sessions
        if cap is not None and outstanding_units + new_units > cap:
            raise AppError(ErrorCode.LIMIT_REACHED, "续约后剩余期限或次数超过上限",
                           {"reason": "prepaid_units_cap_after_renewal", "category": category, "max": cap})

    def check_recharge(self, amount_cents: int, bonus_cents: int, balance_cents: int) -> None:
        if self.stored_value_max_cents is not None and amount_cents > self.stored_value_max_cents:
            raise AppError(ErrorCode.LIMIT_REACHED, "单笔充值超过上限",
                           {"reason": "recharge_amount_cap", "max_cents": self.stored_value_max_cents})
        cap = self.stored_value_max_balance_cents
        if cap is not None and balance_cents + amount_cents + bonus_cents > cap:
            raise AppError(ErrorCode.LIMIT_REACHED, "充值后余额超过上限",
                           {"reason": "balance_cap", "max_cents": cap})
        self.check_gift(amount_cents, bonus_cents)

    def check_member_total(self, db: Session, user_id: int, new_cents: int) -> None:
        if self.member_total_max_cents is None:
            return
        outstanding = member_outstanding_cents(db, user_id)
        if outstanding + new_cents > self.member_total_max_cents:
            raise AppError(ErrorCode.LIMIT_REACHED, "该会员预付款合计超过上限",
                           {"reason": "member_total_cap", "max_cents": self.member_total_max_cents,
                            "outstanding_cents": outstanding})


CONSERVATIVE = PrepaidPolicy(name="conservative")
SHANGHAI_FITNESS_2025 = PrepaidPolicy(name="shanghai_fitness_2025", gift_ratio_max_bp=None)
SHANGHAI_FITNESS_DRAFT_2026 = PrepaidPolicy(name="shanghai_fitness_draft_2026", period_max_cents=None, period_max_days=365,
                                            gift_ratio_max_bp=2500)
BEIJING_2022 = PrepaidPolicy(name="beijing_2022", period_max_cents=None, period_max_days=None, count_max_cents=None,
                             count_max_sessions=None, stored_value_max_cents=None, stored_value_max_balance_cents=500_000,
                             member_total_max_cents=None, gift_ratio_max_bp=None)
NATIONAL_SINGLE_USE_CARD = PrepaidPolicy(name="national_single_use_card", period_max_cents=None, period_max_days=None,
                                         count_max_cents=None, count_max_sessions=None, member_total_max_cents=None,
                                         gift_ratio_max_bp=None)
UNLIMITED = PrepaidPolicy(name="unlimited", period_max_cents=None, period_max_days=None, count_max_cents=None,
                          count_max_sessions=None, stored_value_max_cents=None, stored_value_max_balance_cents=None,
                          member_total_max_cents=None, gift_ratio_max_bp=None)

PRESETS: dict[str, PrepaidPolicy] = {p.name: p for p in (
    CONSERVATIVE, SHANGHAI_FITNESS_2025, SHANGHAI_FITNESS_DRAFT_2026, BEIJING_2022, NATIONAL_SINGLE_USE_CARD, UNLIMITED)}

_policy_resolver: Callable[[int | None], PrepaidPolicy] | None = None


def set_policy_resolver(fn: Callable[[int | None], PrepaidPolicy] | None) -> None:
    """扩展点：按门店返回策略（连锁跨城市经营时各店不同）。None 恢复默认（运行配置 [prepaid] policy）。"""
    global _policy_resolver
    _policy_resolver = fn


def policy_for(store_id: int | None = None) -> PrepaidPolicy:
    if _policy_resolver is not None:
        return _policy_resolver(store_id)
    return PRESETS.get(current_config().prepaid.policy, CONSERVATIVE)


def custom_policy(base: str = "conservative", **overrides) -> PrepaidPolicy:
    """在预设上改个别值：custom_policy("shanghai_fitness_2025", member_total_max_cents=None)。"""
    return replace(PRESETS[base], name=overrides.pop("name", f"{base}+custom"), **overrides)


# ---------------------------------------------------------------- 未兑付余额提供者

_providers: dict[str, Callable[[Session, int], int]] = {}


def register_outstanding_provider(name: str, fn: Callable[[Session, int], int]) -> None:
    """各预付业务注册“该会员未兑付预付款（分）”的计算函数；同名覆盖。"""
    _providers[name] = fn


def member_outstanding_cents(db: Session, user_id: int) -> int:
    return sum(int(fn(db, user_id)) for fn in _providers.values())
