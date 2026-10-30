"""notification 配置：部署参数与秘密（环境变量）+ 运行参数（config/runtime 的 `[notification]`，热加载）+ 可替换时钟。

部署参数：时区 NOTIFY_TIMEZONE（默认 LUZ_TIMEZONE）；秘密：游标签名密钥 NOTIFY_CURSOR_SECRET（默认用 LUZ_JWT_SECRET）。
运行参数（静默时段、重试、订阅额度、短信频控、保留期限）的类型、范围与默认值登记在 `luzback/runtime_config.py` 的
NotificationSettings，依据见 MODULE.md §0、§9。`set_config(...)` 只给项目启动与测试用（运行参数转成测试用运行配置，
生产环境不能用）；`set_clock` 替换时钟。
"""
from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import partial

from luzback import utcnow
from luzback.module_config import ModuleConfig, runtime_default

_rt = partial(runtime_default, "notification")


@dataclass(frozen=True)
class NotificationConfig:
    # ---- 部署参数与秘密（环境变量）----
    timezone: str = field(default_factory=lambda: os.getenv("NOTIFY_TIMEZONE", os.getenv("LUZ_TIMEZONE", "Asia/Shanghai")))
    cursor_secret: str = field(default_factory=lambda: os.getenv("NOTIFY_CURSOR_SECRET",
                                                                 os.getenv("LUZ_JWT_SECRET", "dev-notify-cursor")))
    # ---- 运行参数（[notification]）：静默时段（非紧急场景在静默时段内延后到结束时刻；按门店时区）----
    quiet_start: str = field(default_factory=_rt("quiet_start"))
    quiet_end: str = field(default_factory=_rt("quiet_end"))
    quiet_enabled: bool = field(default_factory=_rt("quiet_enabled"))
    # ---- 投递重试（SPEC §9：订阅消息 / 短信任务重试 1m / 5m / 30m）----
    retry_backoff_seconds: tuple[int, ...] = field(default_factory=_rt("retry_backoff_seconds"))
    max_attempts: int = field(default_factory=_rt("max_attempts"))
    send_timeout_seconds: float = field(default_factory=_rt("send_timeout_seconds"))
    sending_stuck_seconds: int = field(default_factory=_rt("sending_stuck_seconds"))
    batch_size: int = field(default_factory=_rt("batch_size"))
    # ---- 订阅消息 ----
    miniprogram_state: str = field(default_factory=_rt("miniprogram_state"))  # developer|trial|formal
    lang: str = field(default_factory=_rt("lang"))
    # 额度来源：frontend = 以前端上报的 wx.requestSubscribeMessage 结果计数；push = 以 subscribe_msg_popup_event 计数。
    # 同一次授权两条路径都会到达，同时计数会把额度翻倍，所以只能选一个（拒收 / 退订事件两条路径都生效）。
    quota_source: str = field(default_factory=_rt("quota_source"))
    quota_cap_per_template: int = field(default_factory=_rt("quota_cap_per_template"))
    # 43101 时本地额度如何处理：reset = 退回本次占用且把本地可用次数清零（以微信为准，避免反复 43101）；keep = 只退回占用
    on_43101: str = field(default_factory=_rt("on_43101"))
    # 下发总量（官方：开通支付 3000 万 / 日，未开通 1000 万 / 日）；到达后延后到次日
    subscribe_daily_cap: int = field(default_factory=_rt("subscribe_daily_cap"))
    # ---- 短信频控（不高于云厂商默认值：阿里云 1 条 / 分钟、5 条 / 小时、10 条 / 天；腾讯云 30 秒同内容 1 条）----
    sms_per_minute: int = field(default_factory=_rt("sms_per_minute"))
    sms_per_hour: int = field(default_factory=_rt("sms_per_hour"))
    sms_per_day: int = field(default_factory=_rt("sms_per_day"))
    sms_daily_cap: int = field(default_factory=_rt("sms_daily_cap"))  # 全店每日短信预算
    # ---- 每会员频控（非紧急场景；订阅消息 + 短信合计）----
    member_nonurgent_per_day: int = field(default_factory=_rt("member_nonurgent_per_day"))
    # ---- 站内信与投递记录保留 ----
    inbox_retention_days: int = field(default_factory=_rt("inbox_retention_days"))
    delivery_retention_days: int = field(default_factory=_rt("delivery_retention_days"))


_module = ModuleConfig("notification", NotificationConfig)


def get_config() -> NotificationConfig:
    return _module.get()


def set_config(config: NotificationConfig | None = None, **overrides) -> NotificationConfig:
    """替换配置（项目启动或测试）。set_config(sms_per_day=3) 在当前配置上覆盖单项（运行参数转成测试用运行配置）。"""
    return _module.set(config, **overrides)


def reset_config() -> None:
    _module.reset()


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
