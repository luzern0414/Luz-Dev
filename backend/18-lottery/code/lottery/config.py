"""lottery 配置：模块级数据类，项目定制只改这里（或启动时 set_config）。

法定上限不可放宽：
- prize_value_cap_cents ≤ 5_000_000（反不正当竞争法 2025 第十一条第（四）项、规范促销行为暂行规定第十七条：最高奖 ≤ 5 万元）。
- retention_days ≥ 730（规范促销行为暂行规定第十九条：设奖规则、公示信息、兑奖结果、获奖人员保存 2 年）。
- publicity_threshold_cents ≤ 50_000（第十三条第二款：超过 500 元的奖项兑奖情况随时公示；可以更严，不能更松）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace

LEGAL_PRIZE_CAP_CENTS = 5_000_000      # 5 万元
LEGAL_RETENTION_DAYS = 730             # 2 年
LEGAL_PUBLICITY_THRESHOLD_CENTS = 50_000  # 500 元
ODDS_BASIS = 10_000                    # 概率以万分比（bp）表示，含谢谢参与合计 10000


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


@dataclass(frozen=True)
class LotteryConfig:
    prize_value_cap_cents: int = field(default_factory=lambda: _int("LUZ_LOTTERY_PRIZE_CAP_CENTS", LEGAL_PRIZE_CAP_CENTS))
    publicity_threshold_cents: int = field(default_factory=lambda: _int("LUZ_LOTTERY_PUBLICITY_CENTS", LEGAL_PUBLICITY_THRESHOLD_CENTS))
    retention_days: int = field(default_factory=lambda: _int("LUZ_LOTTERY_RETENTION_DAYS", LEGAL_RETENTION_DAYS))
    timezone: str = field(default_factory=lambda: os.environ.get("LUZ_TIMEZONE", "Asia/Shanghai"))
    member_draws_per_minute: int = field(default_factory=lambda: _int("LUZ_LOTTERY_MEMBER_PER_MIN", 10))  # 0 = 关闭
    ip_draws_per_minute: int = field(default_factory=lambda: _int("LUZ_LOTTERY_IP_PER_MIN", 60))          # 0 = 关闭
    delivery_ack_timeout_seconds: int = 600       # 发奖事件发出后多久没有回执就重发
    delivery_max_attempts: int = 5                # 超过后 failed，等待人工重试
    delivery_backoff_seconds: tuple[int, ...] = (600, 1800, 7200, 21600, 43200)
    default_claim_days: int = 7                   # 实物奖领奖（填写收货信息）期限
    max_prizes: int = 12
    max_admin_grant: int = 100                    # 后台单次发放次数上限
    max_simulate: int = 1_000_000
    earn_payment_business_types: tuple[str, ...] = ("order",)  # 哪些支付业务类型可以送抽奖次数
    checkin_topic: str = "member.checked_in"
    payment_succeeded_topic: str = "payment.succeeded"
    payment_refunded_topic: str = "payment.refunded"
    delivery_result_topic: str = "lottery.delivery_result"
    purge_batch: int = 500

    def validate(self) -> "LotteryConfig":
        if not 0 < self.prize_value_cap_cents <= LEGAL_PRIZE_CAP_CENTS:
            raise ValueError("prize_value_cap_cents must be within (0, 5_000_000] (最高奖不得超过 5 万元)")
        if self.retention_days < LEGAL_RETENTION_DAYS:
            raise ValueError("retention_days must be >= 730 (有奖销售资料保存 2 年)")
        if not 0 < self.publicity_threshold_cents <= LEGAL_PUBLICITY_THRESHOLD_CENTS:
            raise ValueError("publicity_threshold_cents must be within (0, 50_000] (超过 500 元的奖项须公示)")
        if self.delivery_max_attempts < 1 or not self.delivery_backoff_seconds:
            raise ValueError("delivery retry settings invalid")
        return self


_config = LotteryConfig().validate()


def get_config() -> LotteryConfig:
    return _config


def set_config(cfg: LotteryConfig | None = None, **overrides) -> LotteryConfig:
    """替换配置（启动时或测试中）。违反法定上限直接 ValueError。"""
    global _config
    base = cfg or _config
    _config = replace(base, **overrides).validate() if overrides else base.validate()
    return _config


def reset_config() -> LotteryConfig:
    global _config
    _config = LotteryConfig().validate()
    return _config
