"""会员卡模块配置（环境变量，默认值保守）。测试可直接替换 `member_card.config.CONFIG`。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    return default if raw in (None, "") else raw.strip().lower() in ("1", "true", "yes", "on")


def _days(name: str, default: str) -> tuple[int, ...]:
    raw = os.environ.get(name) or default
    return tuple(sorted({int(x) for x in raw.split(",") if x.strip()}, reverse=True))


@dataclass
class CardConfig:
    pay_timeout_minutes: int = field(default_factory=lambda: _int("MEMBER_CARD_PAY_TIMEOUT_MINUTES", 15))
    lpr_1y_bp: int = field(default_factory=lambda: _int("LUZ_LPR_1Y_BP", 300))
    app_freeze_enabled: bool = field(default_factory=lambda: _bool("MEMBER_CARD_APP_FREEZE", True))
    app_transfer_enabled: bool = field(default_factory=lambda: _bool("MEMBER_CARD_APP_TRANSFER", True))
    refund_requires_approval: bool = field(default_factory=lambda: _bool("MEMBER_CARD_REFUND_REQUIRES_APPROVAL", True))
    reminder_days: tuple[int, ...] = field(default_factory=lambda: _days("MEMBER_CARD_REMINDER_DAYS", "7,3,1"))
    original_refund_max_days: int = field(default_factory=lambda: _int("MEMBER_CARD_ORIGINAL_REFUND_MAX_DAYS", 365))
    adjust_max_sessions: int = field(default_factory=lambda: _int("MEMBER_CARD_ADJUST_MAX_SESSIONS", 50))
    adjust_max_days: int = field(default_factory=lambda: _int("MEMBER_CARD_ADJUST_MAX_DAYS", 366))
    replace_fee_cents: int = field(default_factory=lambda: _int("MEMBER_CARD_REPLACE_FEE_CENTS", 0))
    stop_prepaid: bool = field(default_factory=lambda: _bool("LUZ_STOP_PREPAID", False))


CONFIG = CardConfig()
