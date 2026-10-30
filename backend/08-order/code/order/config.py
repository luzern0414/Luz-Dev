"""order 配置：部署参数（环境变量）+ 运行参数（config/runtime 的 `[order]`，热加载）。

部署参数：订单号前缀 ORDER_NO_PREFIX（定了不改，否则订单号格式变化）、时区 ORDER_TIMEZONE（默认 LUZ_TIMEZONE）。
运行参数（支付时限、报价有效期、件数上限、条款、任务批量、开关）的类型、范围与默认值登记在
`luzback/runtime_config.py` 的 OrderSettings；这里的数据类是读取时的合并结果，属性名与键同名。
`set_config(...)` 只给项目启动与测试用：部署参数直接替换，运行参数转成测试用运行配置（生产环境不能用）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from luzback.module_config import ModuleConfig, runtime_default

_rt = partial(runtime_default, "order")


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class OrderConfig:
    # ---- 部署参数（环境变量）----
    order_no_prefix: str = field(default_factory=lambda: _env("ORDER_NO_PREFIX", ""))
    timezone: str = field(default_factory=lambda: _env("ORDER_TIMEZONE", _env("LUZ_TIMEZONE", "Asia/Shanghai")))
    # ---- 运行参数（[order]）----
    pay_timeout_minutes: int = field(default_factory=_rt("pay_timeout_minutes"))
    quote_ttl_seconds: int = field(default_factory=_rt("quote_ttl_seconds"))
    auto_complete_days: int = field(default_factory=_rt("auto_complete_days"))
    max_lines: int = field(default_factory=_rt("max_lines"))
    max_qty_per_line: int = field(default_factory=_rt("max_qty_per_line"))
    terms_required: bool = field(default_factory=_rt("terms_required"))
    terms_version: str = field(default_factory=_rt("terms_version"))
    close_batch: int = field(default_factory=_rt("close_batch"))
    complete_batch: int = field(default_factory=_rt("complete_batch"))
    balance_payment_enabled: bool = field(default_factory=_rt("balance_payment_enabled"))
    shipping_upload_enabled: bool = field(default_factory=_rt("shipping_upload_enabled"))

    def __post_init__(self) -> None:
        if len(self.order_no_prefix) > 4 or not (self.order_no_prefix.isalnum() or self.order_no_prefix == ""):
            raise ValueError("ORDER_NO_PREFIX must be <= 4 alphanumerics")


_module = ModuleConfig("order", OrderConfig)


def get_config() -> OrderConfig:
    return _module.get()


def set_config(config: OrderConfig | None = None, **overrides: Any) -> OrderConfig:
    return _module.set(config, **overrides)


def reset_config() -> None:
    _module.reset()
