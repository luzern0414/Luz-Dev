"""储值模块配置：运行参数在 config/runtime 的 `[wallet]` 与共用的 `[prepaid]`（热加载），本模块没有部署参数与秘密。

`[prepaid]`（监管口径预设 policy、一年期 LPR lpr_1y_bp、停止收取预付款 stop_prepaid）由储值与会员卡共用，只存一份。
类型、范围与默认值登记在 `luzback/runtime_config.py` 的 WalletSettings / PrepaidSettings；这里的数据类是读取时的合并结果。
`set_config(...)` 只给项目启动与测试用（运行参数转成测试用运行配置，生产环境不能用）；`reset_config()` 撤销。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import partial
from typing import Any

from luzback.module_config import ModuleConfig, runtime_default

_rt = partial(runtime_default, "wallet")
_prepaid = partial(runtime_default, "prepaid")


@dataclass(frozen=True)
class WalletConfig:
    # ---- 运行参数（[wallet]）----
    split_policy: str = field(default_factory=_rt("split_policy"))
    pay_timeout_minutes: int = field(default_factory=_rt("pay_timeout_minutes"))
    refund_requires_approval: bool = field(default_factory=_rt("refund_requires_approval"))
    customer_refund_fee_bp: int = field(default_factory=_rt("customer_refund_fee_bp"))
    adjust_max_cents: int = field(default_factory=_rt("adjust_max_cents"))
    custom_amount_enabled: bool = field(default_factory=_rt("custom_amount_enabled"))
    custom_amount_min_cents: int = field(default_factory=_rt("custom_amount_min_cents"))
    original_refund_max_days: int = field(default_factory=_rt("original_refund_max_days"))
    # ---- 共用（[prepaid]）----
    lpr_1y_bp: int = field(default_factory=_prepaid("lpr_1y_bp"))  # 合同成立时一年期 LPR，需按公布值更新
    stop_prepaid: bool = field(default_factory=_prepaid("stop_prepaid"))  # 经营风险时停止收取预付款


_module = ModuleConfig("wallet", WalletConfig,
                       shared={"lpr_1y_bp": "prepaid.lpr_1y_bp", "stop_prepaid": "prepaid.stop_prepaid"})


def get_config() -> WalletConfig:
    return _module.get()


def set_config(config: WalletConfig | None = None, **overrides: Any) -> WalletConfig:
    return _module.set(config, **overrides)


def reset_config() -> None:
    _module.reset()
