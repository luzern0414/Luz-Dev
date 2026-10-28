"""after_sales 配置（环境变量 → 安全默认值；项目启动时可 `set_config(...)` 覆盖）。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from typing import Any


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class AfterSaleConfig:
    # 七日无理由：签收（履约完成）次日起 7 日内（七日办法第十条）；质量等其他原因的申请窗口
    no_reason_days: int = field(default_factory=lambda: _int("AFTER_SALE_NO_REASON_DAYS", 7))
    window_days: int = field(default_factory=lambda: _int("AFTER_SALE_WINDOW_DAYS", 15))
    auto_approve_days: int = field(default_factory=lambda: _int("AFTER_SALE_AUTO_APPROVE_DAYS", 0))   # 0 = 关闭
    return_ship_days: int = field(default_factory=lambda: _int("AFTER_SALE_RETURN_SHIP_DAYS", 7))
    max_evidence: int = field(default_factory=lambda: _int("AFTER_SALE_MAX_EVIDENCE", 9))
    refund_retry_seconds: int = field(default_factory=lambda: _int("AFTER_SALE_REFUND_RETRY_SECONDS", 60))
    job_batch: int = field(default_factory=lambda: _int("AFTER_SALE_JOB_BATCH", 200))

    def __post_init__(self) -> None:
        if self.no_reason_days < 7:
            raise ValueError("AFTER_SALE_NO_REASON_DAYS must be >= 7 (网络购买商品七日无理由退货暂行办法第十条)")
        if self.auto_approve_days < 0 or self.return_ship_days < 1:
            raise ValueError("invalid after-sale timeouts")


_config: AfterSaleConfig | None = None


def get_config() -> AfterSaleConfig:
    global _config
    if _config is None:
        _config = AfterSaleConfig()
    return _config


def set_config(config: AfterSaleConfig | None = None, **overrides: Any) -> AfterSaleConfig:
    global _config
    _config = replace(config or get_config(), **overrides)
    return _config


def reset_config() -> None:
    global _config
    _config = None
