"""notification 配置：模块级配置数据类（环境变量读取，带安全默认值）+ 可替换时钟。

项目定制只改这里的配置键（环境变量）或调用 set_config / set_clock，不改业务代码。默认值依据见 MODULE.md §0、§9。
"""
from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

from luzback import utcnow


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, "true" if default else "false").lower() in {"1", "true", "yes", "on"}


def _ints(name: str, default: str) -> tuple[int, ...]:
    return tuple(int(x) for x in os.getenv(name, default).split(",") if x.strip())


@dataclass(frozen=True)
class NotificationConfig:
    # ---- 时区与静默时段（非紧急场景在静默时段内延后到结束时刻；按门店时区）----
    timezone: str = field(default_factory=lambda: _env("NOTIFY_TIMEZONE", os.getenv("LUZ_TIMEZONE", "Asia/Shanghai")))
    quiet_start: str = field(default_factory=lambda: _env("NOTIFY_QUIET_START", "22:00"))
    quiet_end: str = field(default_factory=lambda: _env("NOTIFY_QUIET_END", "08:00"))
    quiet_enabled: bool = field(default_factory=lambda: _bool("NOTIFY_QUIET_ENABLED", True))
    # ---- 投递重试（SPEC §9：订阅消息 / 短信任务重试 1m / 5m / 30m）----
    retry_backoff_seconds: tuple[int, ...] = field(default_factory=lambda: _ints("NOTIFY_RETRY_BACKOFF", "60,300,1800,7200"))
    max_attempts: int = field(default_factory=lambda: _int("NOTIFY_MAX_ATTEMPTS", 5))
    send_timeout_seconds: float = field(default_factory=lambda: float(_env("NOTIFY_SEND_TIMEOUT", "3")))
    sending_stuck_seconds: int = field(default_factory=lambda: _int("NOTIFY_SENDING_STUCK_SECONDS", 300))
    batch_size: int = field(default_factory=lambda: _int("NOTIFY_BATCH_SIZE", 100))
    # ---- 订阅消息 ----
    miniprogram_state: str = field(default_factory=lambda: _env("NOTIFY_MINIPROGRAM_STATE", "formal"))  # developer|trial|formal
    lang: str = field(default_factory=lambda: _env("NOTIFY_LANG", "zh_CN"))
    # 额度来源：frontend = 以前端上报的 wx.requestSubscribeMessage 结果计数；push = 以 subscribe_msg_popup_event 计数。
    # 同一次授权两条路径都会到达，同时计数会把额度翻倍，所以只能选一个（拒收 / 退订事件两条路径都生效）。
    quota_source: str = field(default_factory=lambda: _env("NOTIFY_QUOTA_SOURCE", "frontend"))
    quota_cap_per_template: int = field(default_factory=lambda: _int("NOTIFY_QUOTA_CAP", 50))
    # 43101 时本地额度如何处理：reset = 退回本次占用且把本地可用次数清零（以微信为准，避免反复 43101）；keep = 只退回占用
    on_43101: str = field(default_factory=lambda: _env("NOTIFY_ON_43101", "reset"))
    # 下发总量（官方：开通支付 3000 万 / 日，未开通 1000 万 / 日）；到达后延后到次日
    subscribe_daily_cap: int = field(default_factory=lambda: _int("NOTIFY_SUBSCRIBE_DAILY_CAP", 10_000_000))
    # ---- 短信频控（不高于云厂商默认值：阿里云 1 条 / 分钟、5 条 / 小时、10 条 / 天；腾讯云 30 秒同内容 1 条）----
    sms_per_minute: int = field(default_factory=lambda: _int("NOTIFY_SMS_PER_MINUTE", 1))
    sms_per_hour: int = field(default_factory=lambda: _int("NOTIFY_SMS_PER_HOUR", 5))
    sms_per_day: int = field(default_factory=lambda: _int("NOTIFY_SMS_PER_DAY", 10))
    sms_daily_cap: int = field(default_factory=lambda: _int("NOTIFY_SMS_DAILY_CAP", 100_000))  # 全店每日短信预算
    # ---- 每会员频控（非紧急场景；订阅消息 + 短信合计）----
    member_nonurgent_per_day: int = field(default_factory=lambda: _int("NOTIFY_MEMBER_NONURGENT_PER_DAY", 5))
    # ---- 站内信 ----
    inbox_retention_days: int = field(default_factory=lambda: _int("NOTIFY_INBOX_RETENTION_DAYS", 180))
    delivery_retention_days: int = field(default_factory=lambda: _int("NOTIFY_DELIVERY_RETENTION_DAYS", 180))
    cursor_secret: str = field(default_factory=lambda: _env("NOTIFY_CURSOR_SECRET", os.getenv("LUZ_JWT_SECRET", "dev-notify-cursor")))


_config: NotificationConfig | None = None


def get_config() -> NotificationConfig:
    global _config
    if _config is None:
        _config = NotificationConfig()
    return _config


def set_config(config: NotificationConfig | None = None, **overrides) -> NotificationConfig:
    """替换配置（项目启动或测试）。set_config(sms_per_day=3) 在当前配置上覆盖单项。"""
    global _config
    base = config or get_config()
    _config = replace(base, **overrides) if overrides else base
    return _config


def reset_config() -> None:
    global _config
    _config = None


# ---------- 时钟（测试用 luzback.testing.FakeClock 或返回 datetime 的函数替换）----------

_clock: Callable[[], datetime | float] = utcnow


def now() -> datetime:
    value = _clock()
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def set_clock(fn: Callable[[], datetime | float] | None) -> None:
    global _clock
    _clock = fn or utcnow
