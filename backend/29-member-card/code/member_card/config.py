"""会员卡模块配置：运行参数在 config/runtime 的 `[member_card]` 与共用的 `[prepaid]`（热加载），本模块没有部署参数与秘密。

`[prepaid]`（一年期 LPR lpr_1y_bp、停止收取预付款 stop_prepaid）与储值共用，只存一份。
类型、范围与默认值登记在 `luzback/runtime_config.py` 的 MemberCardSettings / PrepaidSettings；这里的数据类是读取时的合并结果。
`set_config(...)` 只给项目启动与测试用（运行参数转成测试用运行配置，生产环境不能用）；`reset_config()` 撤销。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import partial
from typing import Any

from luzback.business import BusinessSetting, declare_business_settings
from luzback.module_config import ModuleConfig, runtime_default

_rt = partial(runtime_default, "member_card")
_prepaid = partial(runtime_default, "prepaid")


@dataclass(frozen=True)
class CardConfig:
    # ---- 运行参数（[member_card]）----
    pay_timeout_minutes: int = field(default_factory=_rt("pay_timeout_minutes"))
    app_freeze_enabled: bool = field(default_factory=_rt("app_freeze_enabled"))
    app_transfer_enabled: bool = field(default_factory=_rt("app_transfer_enabled"))
    refund_requires_approval: bool = field(default_factory=_rt("refund_requires_approval"))
    reminder_days: tuple[int, ...] = field(default_factory=_rt("reminder_days"))
    original_refund_max_days: int = field(default_factory=_rt("original_refund_max_days"))
    adjust_max_sessions: int = field(default_factory=_rt("adjust_max_sessions"))
    adjust_max_days: int = field(default_factory=_rt("adjust_max_days"))
    # ---- 共用（[prepaid]）----
    lpr_1y_bp: int = field(default_factory=_prepaid("lpr_1y_bp"))
    stop_prepaid: bool = field(default_factory=_prepaid("stop_prepaid"))


_module = ModuleConfig("member_card", CardConfig,
                       shared={"lpr_1y_bp": "prepaid.lpr_1y_bp", "stop_prepaid": "prepaid.stop_prepaid"})


def get_config() -> CardConfig:
    return _module.get()


def set_config(config: CardConfig | None = None, **overrides: Any) -> CardConfig:
    return _module.set(config, **overrides)


def reset_config() -> None:
    _module.reset()


declare_business_settings(
    BusinessSetting("member_card.replace_fee_cents", "补卡工本费", "member_card", "int", 0, unit="分",
                    description="挂失后补办新卡收取的工本费；0 表示免费。可按开卡门店分别设置",
                    minimum=0, maximum=100_000, store_scope=True, risk="high", sort_order=10),
)
