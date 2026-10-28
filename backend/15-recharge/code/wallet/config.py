"""储值模块配置（环境变量，默认值安全保守）。测试可直接替换 `wallet.config.CONFIG`。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    return default if raw in (None, "") else raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class WalletConfig:
    split_policy: str = field(default_factory=lambda: os.environ.get("WALLET_SPLIT_POLICY", "proportional"))
    pay_timeout_minutes: int = field(default_factory=lambda: _int("WALLET_PAY_TIMEOUT_MINUTES", 15))
    refund_requires_approval: bool = field(default_factory=lambda: _bool("WALLET_REFUND_REQUIRES_APPROVAL", True))
    customer_refund_fee_bp: int = field(default_factory=lambda: _int("WALLET_CUSTOMER_REFUND_FEE_BP", 0))
    lpr_1y_bp: int = field(default_factory=lambda: _int("LUZ_LPR_1Y_BP", 300))  # 合同成立时一年期 LPR，需按公布值更新
    adjust_max_cents: int = field(default_factory=lambda: _int("WALLET_ADJUST_MAX_CENTS", 50_000))
    custom_amount_enabled: bool = field(default_factory=lambda: _bool("WALLET_CUSTOM_AMOUNT", False))
    custom_amount_min_cents: int = field(default_factory=lambda: _int("WALLET_CUSTOM_AMOUNT_MIN_CENTS", 100))
    original_refund_max_days: int = field(default_factory=lambda: _int("WALLET_ORIGINAL_REFUND_MAX_DAYS", 365))
    stop_prepaid: bool = field(default_factory=lambda: _bool("LUZ_STOP_PREPAID", False))  # 经营风险时停止收取预付款


CONFIG = WalletConfig()
