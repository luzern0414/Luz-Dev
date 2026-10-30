"""after_sales 配置：运行参数在 config/runtime 的 `[after_sales]`（热加载），本模块没有部署参数与秘密。

类型、范围与默认值（七日无理由不少于 7 天：网络购买商品七日无理由退货暂行办法第十条）登记在
`luzback/runtime_config.py` 的 AfterSalesSettings；这里的数据类是读取时的合并结果，属性名与键同名。
`set_config(...)` 只给项目启动与测试用（运行参数转成测试用运行配置，生产环境不能用）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import partial
from typing import Any

from luzback.module_config import ModuleConfig, runtime_default

_rt = partial(runtime_default, "after_sales")


@dataclass(frozen=True)
class AfterSaleConfig:
    # ---- 运行参数（[after_sales]）----
    no_reason_days: int = field(default_factory=_rt("no_reason_days"))
    window_days: int = field(default_factory=_rt("window_days"))
    auto_approve_days: int = field(default_factory=_rt("auto_approve_days"))   # 0 = 关闭
    return_ship_days: int = field(default_factory=_rt("return_ship_days"))
    max_evidence: int = field(default_factory=_rt("max_evidence"))
    refund_retry_seconds: int = field(default_factory=_rt("refund_retry_seconds"))
    job_batch: int = field(default_factory=_rt("job_batch"))


_module = ModuleConfig("after_sales", AfterSaleConfig)


def get_config() -> AfterSaleConfig:
    return _module.get()


def set_config(config: AfterSaleConfig | None = None, **overrides: Any) -> AfterSaleConfig:
    return _module.set(config, **overrides)


def reset_config() -> None:
    _module.reset()
