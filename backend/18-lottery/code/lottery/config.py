"""lottery 配置：运行参数在 config/runtime 的 `[lottery]`（热加载）；事件主题等接线在这里（代码，启动时 set_config）。

法定上限不可放宽（字段说明在加载时校验，`luzback/runtime_config.py` 的 LotterySettings）：
- prize_value_cap_cents ≤ 5_000_000（反不正当竞争法 2025 第十一条第（四）项、规范促销行为暂行规定第十七条：最高奖 ≤ 5 万元）。
- retention_days ≥ 730（规范促销行为暂行规定第十九条：设奖规则、公示信息、兑奖结果、获奖人员保存 2 年）。
- publicity_threshold_cents ≤ 50_000（第十三条第二款：超过 500 元的奖项兑奖情况随时公示；可以更严，不能更松）。
部署参数只有时区（LUZ_TIMEZONE）。`set_config(...)` 只给项目启动与测试用（运行参数转成测试用运行配置，生产环境不能用）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import partial

from luzback.module_config import ModuleConfig, runtime_default

LEGAL_PRIZE_CAP_CENTS = 5_000_000      # 5 万元
LEGAL_RETENTION_DAYS = 730             # 2 年
LEGAL_PUBLICITY_THRESHOLD_CENTS = 50_000  # 500 元
ODDS_BASIS = 10_000                    # 概率以万分比（bp）表示，含谢谢参与合计 10000

_rt = partial(runtime_default, "lottery")


@dataclass(frozen=True)
class LotteryConfig:
    # ---- 部署参数（环境变量）----
    timezone: str = field(default_factory=lambda: os.environ.get("LUZ_TIMEZONE", "Asia/Shanghai"))
    # ---- 运行参数（[lottery]）----
    prize_value_cap_cents: int = field(default_factory=_rt("prize_value_cap_cents"))
    publicity_threshold_cents: int = field(default_factory=_rt("publicity_threshold_cents"))
    retention_days: int = field(default_factory=_rt("retention_days"))
    member_draws_per_minute: int = field(default_factory=_rt("member_draws_per_minute"))  # 0 = 关闭
    ip_draws_per_minute: int = field(default_factory=_rt("ip_draws_per_minute"))          # 0 = 关闭
    delivery_ack_timeout_seconds: int = field(default_factory=_rt("delivery_ack_timeout_seconds"))
    delivery_max_attempts: int = field(default_factory=_rt("delivery_max_attempts"))
    delivery_backoff_seconds: tuple[int, ...] = field(default_factory=_rt("delivery_backoff_seconds"))
    default_claim_days: int = field(default_factory=_rt("default_claim_days"))
    max_prizes: int = field(default_factory=_rt("max_prizes"))
    max_admin_grant: int = field(default_factory=_rt("max_admin_grant"))
    max_simulate: int = field(default_factory=_rt("max_simulate"))
    purge_batch: int = field(default_factory=_rt("purge_batch"))
    # ---- 接线（代码；项目启动时 set_config）----
    earn_payment_business_types: tuple[str, ...] = ("order",)  # 哪些支付业务类型可以送抽奖次数
    checkin_topic: str = ""  # 签到送次数的事件主题（如 member.checked_in）；空 = 不订阅（基础包没有签到模块）
    payment_succeeded_topic: str = "payment.succeeded"
    refund_succeeded_topic: str = "refund.succeeded"  # 09-wechat-pay：payload 含 payment_id、business_type、amount_cents
    delivery_result_topic: str = "lottery.delivery_result"

    def validate(self) -> "LotteryConfig":
        """法定上限（运行配置加载时已按同样规则校验；这里给直接构造的配置对象用）。"""
        if not 0 < self.prize_value_cap_cents <= LEGAL_PRIZE_CAP_CENTS:
            raise ValueError("prize_value_cap_cents must be within (0, 5_000_000] (最高奖不得超过 5 万元)")
        if self.retention_days < LEGAL_RETENTION_DAYS:
            raise ValueError("retention_days must be >= 730 (有奖销售资料保存 2 年)")
        if not 0 < self.publicity_threshold_cents <= LEGAL_PUBLICITY_THRESHOLD_CENTS:
            raise ValueError("publicity_threshold_cents must be within (0, 50_000] (超过 500 元的奖项须公示)")
        if self.delivery_max_attempts < 1 or not self.delivery_backoff_seconds:
            raise ValueError("delivery retry settings invalid")
        return self


_module = ModuleConfig("lottery", LotteryConfig)


def get_config() -> LotteryConfig:
    return _module.get()


def set_config(cfg: LotteryConfig | None = None, **overrides) -> LotteryConfig:
    """替换配置（启动时或测试中）。违反法定上限直接 ValueError（运行配置校验拒绝）。"""
    if cfg is not None:
        cfg.validate()
    return _module.set(cfg, **overrides)


def reset_config() -> LotteryConfig:
    _module.reset()
    return get_config()
