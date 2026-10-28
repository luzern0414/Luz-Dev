"""coupon 配置与扩展钩子。

配置键（环境变量，均有安全默认值）见 MODULE.md §9；项目可在启动时 `set_config(...)` 覆盖。
扩展钩子：
- 人群判断 `set_audience_checker(fn)`：fn(db, user_id, audience: dict, now) -> None（符合）或原因字符串。
  默认实现只认 `all`；其他人群类型需要先 `set_member_profile_provider(fn)`（会员注册时间、等级、标签）。
- 购物车行 `set_cart_lines_provider(fn)`：fn(db, user_id) -> list[CartLine]，供“购物车可用券”接口使用
  （价格由服务端的购物车 / 商品模块给出，不信任客户端）。
"""
from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

RETURN_NEVER = "never"                # 退款一律不退券
RETURN_FULL_REFUND = "full_refund"    # 默认：整单全额退款才退券（七日办法第十五条“按原方式退回”）
RETURN_ANY_REFUND = "any_refund"      # 任何退款都退券（商户让利）
RETURN_POLICIES = (RETURN_NEVER, RETURN_FULL_REFUND, RETURN_ANY_REFUND)


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class CouponConfig:
    timezone: str = field(default_factory=lambda: _env("COUPON_TIMEZONE", _env("LUZ_TIMEZONE", "Asia/Shanghai")))
    return_policy: str = field(default_factory=lambda: _env("COUPON_RETURN_POLICY", RETURN_FULL_REFUND))
    return_window_hours: int = field(default_factory=lambda: _int("COUPON_RETURN_WINDOW_HOURS", 0))  # 0 = 只看有效期
    code_length: int = field(default_factory=lambda: _int("COUPON_CODE_LENGTH", 12))
    code_max_failures: int = field(default_factory=lambda: _int("COUPON_CODE_MAX_FAILURES", 5))
    code_window_seconds: int = field(default_factory=lambda: _int("COUPON_CODE_WINDOW_SECONDS", 600))
    code_batch_max: int = field(default_factory=lambda: _int("COUPON_CODE_BATCH_MAX", 10000))
    issue_batch_max: int = field(default_factory=lambda: _int("COUPON_ISSUE_BATCH_MAX", 10000))
    expire_batch_size: int = field(default_factory=lambda: _int("COUPON_EXPIRE_BATCH", 500))
    remind_before_hours: int = field(default_factory=lambda: _int("COUPON_REMIND_BEFORE_HOURS", 48))
    issue_job_batch: int = field(default_factory=lambda: _int("COUPON_ISSUE_JOB_BATCH", 200))
    max_coupons_per_order: int = field(default_factory=lambda: _int("COUPON_MAX_PER_ORDER", 3))

    def __post_init__(self) -> None:
        if self.return_policy not in RETURN_POLICIES:
            raise ValueError(f"COUPON_RETURN_POLICY must be one of {RETURN_POLICIES}")
        if not 8 <= self.code_length <= 32:
            raise ValueError("COUPON_CODE_LENGTH must be between 8 and 32")


_config: CouponConfig | None = None


def get_config() -> CouponConfig:
    global _config
    if _config is None:
        _config = CouponConfig()
    return _config


def set_config(config: CouponConfig | None = None, **overrides: Any) -> CouponConfig:
    global _config
    _config = replace(config or get_config(), **overrides)
    return _config


def reset_config() -> None:
    global _config
    _config = None


# ---------------------------------------------------------------- 会员画像与人群


@dataclass(frozen=True)
class MemberProfile:
    registered_at: datetime | None = None
    level: int | None = None
    tags: frozenset[str] = frozenset()


ProfileProvider = Callable[[Any, int], "MemberProfile | None"]
AudienceChecker = Callable[[Any, int, dict, datetime], "str | None"]

_profile_provider: ProfileProvider | None = None
_audience_checker: AudienceChecker | None = None


def set_member_profile_provider(fn: ProfileProvider | None) -> None:
    global _profile_provider
    _profile_provider = fn


def _membership_provider() -> ProfileProvider | None:
    """默认：14-membership 的 member_profile(db, user_id)（返回带 registered_at / level / tags 的对象）；未装配返回 None。"""
    try:
        from membership import member_profile  # type: ignore[attr-defined]  # noqa: PLC0415
    except ImportError:
        return None

    def provider(db: Any, user_id: int) -> MemberProfile | None:
        raw = member_profile(db, user_id)
        if raw is None:
            return None
        if isinstance(raw, MemberProfile):
            return raw
        get = raw.get if isinstance(raw, dict) else (lambda k, d=None: getattr(raw, k, d))
        level = get("level")
        return MemberProfile(registered_at=get("registered_at"), level=None if level is None else int(level),
                             tags=frozenset(get("tags") or ()))

    return provider


def get_member_profile_provider() -> ProfileProvider | None:
    """项目注入的优先；未注入时用 14-membership（可导入时）；都没有 → None（非 all 人群一律不可领）。"""
    return _profile_provider if _profile_provider is not None else _membership_provider()


def set_audience_checker(fn: AudienceChecker | None) -> None:
    global _audience_checker
    _audience_checker = fn


def get_audience_checker() -> AudienceChecker | None:
    return _audience_checker


# ---------------------------------------------------------------- 购物车行

CartLinesProvider = Callable[[Any, int], list]
_cart_lines_provider: CartLinesProvider | None = None


def set_cart_lines_provider(fn: CartLinesProvider | None) -> None:
    global _cart_lines_provider
    _cart_lines_provider = fn


def get_cart_lines_provider() -> CartLinesProvider | None:
    return _cart_lines_provider
