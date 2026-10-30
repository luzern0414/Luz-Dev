"""identity 配置：部署参数（环境变量）+ 运行参数（config/runtime 的 `[identity]`，热加载）。

令牌签名密钥、签发方沿用框架 Settings（LUZ_JWT_SECRET / LUZ_JWT_ISSUER）；字段加密密钥沿用 LUZ_FIELD_KEY。
运行参数的类型、范围与默认值（按 R3 OWASP / ASVS / NIST / RFC 9700 与 R2 小程序官方取值）登记在
`luzback/runtime_config.py` 的 IdentitySettings；这里的数据类是读取时的合并结果，属性名与键同名。
登录、短信发送、刷新的网关频控在 `[rate_limit]`（login_per_min、sms_send_per_min、refresh_per_min）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import partial

from luzback.module_config import ModuleConfig, runtime_default

_rt = partial(runtime_default, "identity")


@dataclass(frozen=True)
class IdentityConfig:
    # ---- 部署参数（环境变量）----
    jwt_audience: str = field(default_factory=lambda: os.getenv("IDENTITY_JWT_AUDIENCE", "luz-app"))
    # ---- 运行参数（[identity]）：令牌 ----
    access_ttl_seconds: int = field(default_factory=_rt("access_ttl_seconds"))
    jwt_leeway_seconds: int = field(default_factory=_rt("jwt_leeway_seconds"))
    refresh_idle_days: int = field(default_factory=_rt("refresh_idle_days"))
    refresh_absolute_days: int = field(default_factory=_rt("refresh_absolute_days"))
    refresh_reuse_grace_seconds: int = field(default_factory=_rt("refresh_reuse_grace_seconds"))
    session_check: bool = field(default_factory=_rt("session_check"))
    # ---- 短信验证码 ----
    sms_enabled: bool = field(default_factory=_rt("sms_enabled"))
    sms_ttl_seconds: int = field(default_factory=_rt("sms_ttl_seconds"))
    sms_max_attempts: int = field(default_factory=_rt("sms_max_attempts"))
    sms_resend_seconds: int = field(default_factory=_rt("sms_resend_seconds"))
    sms_daily_per_phone: int = field(default_factory=_rt("sms_daily_per_phone"))
    sms_hourly_per_ip: int = field(default_factory=_rt("sms_hourly_per_ip"))
    sms_daily_per_ip: int = field(default_factory=_rt("sms_daily_per_ip"))
    # ---- 资料内容安全 ----
    content_check: bool = field(default_factory=_rt("content_check"))
    content_check_fail_open: bool = field(default_factory=_rt("content_check_fail_open"))
    media_check_timeout_minutes: int = field(default_factory=_rt("media_check_timeout_minutes"))
    # ---- 注销与数据保留 ----
    deletion_cooling_days: int = field(default_factory=_rt("deletion_cooling_days"))
    sms_retention_days: int = field(default_factory=_rt("sms_retention_days"))
    session_retention_days: int = field(default_factory=_rt("session_retention_days"))


_module = ModuleConfig("identity", IdentityConfig)


def get_config() -> IdentityConfig:
    """当前请求 / 任务的 identity 配置（运行参数取当前快照，范围已由字段说明校验）。"""
    return _module.get()


def reload_config() -> IdentityConfig:
    """重新读部署参数（环境变量）；运行参数随运行配置热加载，不在这里改。"""
    return _module.reload()


def production_problems(settings) -> list[str]:
    """生产配置检查（identity/__init__ 登记到 luzback.register_production_check("identity", ...)）。

    - LUZ_FIELD_KEY 必须能解出 32 字节（手机号 AES-GCM 加密与 phone_hash 查询都用它；换钥会使已有 phone_hash 失效）；
    - 需要安装 cryptography（否则密文退化为可逆编码）。
    运行参数的取值范围由字段说明在加载时校验，不在这里重复。
    """
    import base64
    import binascii

    problems: list[str] = []
    key = getattr(settings, "field_encryption_key", "") or ""
    try:
        if len(base64.b64decode(key, validate=True)) != 32:
            problems.append("LUZ_FIELD_KEY must be base64 of 32 bytes")
    except (binascii.Error, ValueError):
        problems.append("LUZ_FIELD_KEY must be base64 of 32 bytes")
    try:
        import cryptography  # noqa: F401
    except ImportError:
        problems.append("package cryptography is required for phone encryption")
    return problems
