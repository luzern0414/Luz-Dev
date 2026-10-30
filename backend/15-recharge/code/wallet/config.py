"""储值模块配置：运行参数在 config/runtime 的 `[wallet]` 与共用的 `[prepaid]`（热加载），本模块没有部署参数与秘密。

经营设置（后台“经营设置”页维护，保存即生效）：消费时本金与赠送金的扣减顺序 wallet.split_policy、
顾客原因退款手续费 wallet.customer_refund_fee_bp；读取用 luzback.business.business_value。

`[prepaid]`（监管口径预设 policy、一年期 LPR lpr_1y_bp、停止收取预付款 stop_prepaid）由储值与会员卡共用，只存一份。
类型、范围与默认值登记在 `luzback/runtime_config.py` 的 WalletSettings / PrepaidSettings；这里的数据类是读取时的合并结果。
`set_config(...)` 只给项目启动与测试用（运行参数转成测试用运行配置，生产环境不能用）；`reset_config()` 撤销。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import partial
from typing import Any

from luzback.business import BusinessSetting, declare_business_settings
from luzback.module_config import ModuleConfig, runtime_default

_rt = partial(runtime_default, "wallet")
_prepaid = partial(runtime_default, "prepaid")


@dataclass(frozen=True)
class WalletConfig:
    # ---- 运行参数（[wallet]）----
    pay_timeout_minutes: int = field(default_factory=_rt("pay_timeout_minutes"))
    refund_requires_approval: bool = field(default_factory=_rt("refund_requires_approval"))
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


declare_business_settings(
    BusinessSetting("wallet.split_policy", "储值消费的扣减顺序", "wallet", "choice", "proportional",
                    description="顾客用余额付款时，本金与赠送金按比例同时扣，或先扣完本金再扣赠送金；只影响之后的消费",
                    choices=(("proportional", "本金与赠送金按比例扣"), ("principal_first", "先扣本金，再扣赠送金")),
                    risk="high", sort_order=10),
    BusinessSetting("wallet.customer_refund_fee_bp", "顾客原因退款手续费", "wallet", "int", 0, unit="万分比",
                    description="顾客主动申请退储值时按未用本金收取的手续费；100 表示 1%。商户原因退款不收；须与储值协议一致",
                    minimum=0, maximum=10_000, store_scope=True, risk="high", sort_order=20),
)
