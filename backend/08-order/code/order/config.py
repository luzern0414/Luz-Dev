"""order 配置（环境变量 → 安全默认值；项目启动时可 `set_config(...)` 覆盖，后台可改参数由 23-app-settings 注入）。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from typing import Any


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, "true" if default else "false").lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class OrderConfig:
    pay_timeout_minutes: int = field(default_factory=lambda: _int("ORDER_PAY_TIMEOUT_MINUTES", 15))
    quote_ttl_seconds: int = field(default_factory=lambda: _int("ORDER_QUOTE_TTL", 600))
    auto_complete_days: int = field(default_factory=lambda: _int("ORDER_AUTO_COMPLETE_DAYS", 7))
    max_lines: int = field(default_factory=lambda: _int("ORDER_MAX_LINES", 50))
    max_qty_per_line: int = field(default_factory=lambda: _int("ORDER_MAX_QTY_PER_LINE", 99))
    terms_required: bool = field(default_factory=lambda: _bool("ORDER_TERMS_REQUIRED", True))
    terms_version: str = field(default_factory=lambda: _env("ORDER_TERMS_VERSION", "purchase-v1"))
    order_no_prefix: str = field(default_factory=lambda: _env("ORDER_NO_PREFIX", ""))
    timezone: str = field(default_factory=lambda: _env("ORDER_TIMEZONE", _env("LUZ_TIMEZONE", "Asia/Shanghai")))
    close_batch: int = field(default_factory=lambda: _int("ORDER_CLOSE_BATCH", 100))
    complete_batch: int = field(default_factory=lambda: _int("ORDER_COMPLETE_BATCH", 200))
    balance_payment_enabled: bool = field(default_factory=lambda: _bool("ORDER_BALANCE_PAYMENT", True))
    shipping_upload_enabled: bool = field(default_factory=lambda: _bool("ORDER_SHIPPING_UPLOAD", True))

    def __post_init__(self) -> None:
        if not 1 <= self.pay_timeout_minutes <= 24 * 60:
            raise ValueError("ORDER_PAY_TIMEOUT_MINUTES must be 1..1440 (WeChat time_expire >= 1 minute)")
        if self.quote_ttl_seconds < 30:
            raise ValueError("ORDER_QUOTE_TTL must be >= 30 seconds")
        if len(self.order_no_prefix) > 4 or not (self.order_no_prefix.isalnum() or self.order_no_prefix == ""):
            raise ValueError("ORDER_NO_PREFIX must be <= 4 alphanumerics")


_config: OrderConfig | None = None


def get_config() -> OrderConfig:
    global _config
    if _config is None:
        _config = OrderConfig()
    return _config


def set_config(config: OrderConfig | None = None, **overrides: Any) -> OrderConfig:
    global _config
    _config = replace(config or get_config(), **overrides)
    return _config


def reset_config() -> None:
    global _config
    _config = None
