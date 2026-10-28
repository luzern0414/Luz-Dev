"""identity 配置：环境变量 → 冻结数据类；默认值按 R3（OWASP / ASVS / NIST / RFC 9700）与 R2（小程序官方）取值。

令牌签名密钥、签发方沿用框架 Settings（LUZ_JWT_SECRET / LUZ_JWT_ISSUER）；字段加密密钥沿用 LUZ_FIELD_KEY。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, "true" if default else "false").lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class IdentityConfig:
    # ---- 令牌 ----
    jwt_audience: str = field(default_factory=lambda: os.getenv("IDENTITY_JWT_AUDIENCE", "luz-app"))
    access_ttl_seconds: int = field(default_factory=lambda: _int("IDENTITY_ACCESS_TTL", 7200))
    jwt_leeway_seconds: int = field(default_factory=lambda: _int("IDENTITY_JWT_LEEWAY", 30))
    refresh_idle_days: int = field(default_factory=lambda: _int("IDENTITY_REFRESH_IDLE_DAYS", 14))
    refresh_absolute_days: int = field(default_factory=lambda: _int("IDENTITY_REFRESH_ABSOLUTE_DAYS", 30))
    refresh_reuse_grace_seconds: int = field(default_factory=lambda: _int("IDENTITY_REFRESH_REUSE_GRACE", 10))
    session_check: bool = field(default_factory=lambda: _bool("IDENTITY_SESSION_CHECK", True))
    # ---- 短信验证码 ----
    sms_enabled: bool = field(default_factory=lambda: _bool("IDENTITY_SMS_ENABLED", True))
    sms_ttl_seconds: int = field(default_factory=lambda: _int("IDENTITY_SMS_TTL", 300))
    sms_max_attempts: int = field(default_factory=lambda: _int("IDENTITY_SMS_MAX_ATTEMPTS", 5))
    sms_resend_seconds: int = field(default_factory=lambda: _int("IDENTITY_SMS_RESEND_SECONDS", 60))
    sms_daily_per_phone: int = field(default_factory=lambda: _int("IDENTITY_SMS_DAILY_PER_PHONE", _int("SMS_DAILY_LIMIT", 10)))
    sms_hourly_per_ip: int = field(default_factory=lambda: _int("IDENTITY_SMS_HOURLY_PER_IP", 20))
    sms_daily_per_ip: int = field(default_factory=lambda: _int("IDENTITY_SMS_DAILY_PER_IP", 50))
    # ---- 资料内容安全 ----
    content_check: bool = field(default_factory=lambda: _bool("IDENTITY_CONTENT_CHECK", True))
    content_check_fail_open: bool = field(default_factory=lambda: _bool("IDENTITY_CONTENT_CHECK_FAIL_OPEN", False))
    media_check_timeout_minutes: int = field(default_factory=lambda: _int("IDENTITY_MEDIA_CHECK_TIMEOUT_MINUTES", 60))
    # ---- 注销 ----
    deletion_cooling_days: int = field(default_factory=lambda: _int("IDENTITY_DELETION_COOLING_DAYS", 15))
    # ---- 数据保留 ----
    sms_retention_days: int = field(default_factory=lambda: _int("IDENTITY_SMS_RETENTION_DAYS", 7))
    session_retention_days: int = field(default_factory=lambda: _int("IDENTITY_SESSION_RETENTION_DAYS", 180))

    def validate(self) -> None:
        problems = []
        if not 60 <= self.access_ttl_seconds <= 86400:
            problems.append("IDENTITY_ACCESS_TTL must be 60..86400")
        if not 0 <= self.jwt_leeway_seconds <= 300:
            problems.append("IDENTITY_JWT_LEEWAY must be 0..300 (RFC 7519: a few minutes at most)")
        if not 1 <= self.refresh_idle_days <= self.refresh_absolute_days:
            problems.append("IDENTITY_REFRESH_IDLE_DAYS must be 1..IDENTITY_REFRESH_ABSOLUTE_DAYS")
        if self.refresh_absolute_days > 30:
            problems.append("IDENTITY_REFRESH_ABSOLUTE_DAYS must be <= 30 (NIST AAL1)")
        if not 60 <= self.sms_ttl_seconds <= 600:
            problems.append("IDENTITY_SMS_TTL must be 60..600 (NIST/ASVS: <= 10 minutes)")
        if not 1 <= self.sms_max_attempts <= 10:
            problems.append("IDENTITY_SMS_MAX_ATTEMPTS must be 1..10")
        if not 0 <= self.deletion_cooling_days <= 15:
            problems.append("IDENTITY_DELETION_COOLING_DAYS must be 0..15 (deletion must finish within 15 working days)")
        if problems:
            raise ValueError("; ".join(problems))


@lru_cache(maxsize=1)
def get_config() -> IdentityConfig:
    config = IdentityConfig()
    config.validate()
    return config


def reload_config() -> IdentityConfig:
    get_config.cache_clear()
    return get_config()


def production_problems(settings) -> list[str]:
    """生产配置检查（identity/__init__ 登记到 luzback.register_production_check("identity", ...)）。

    - LUZ_FIELD_KEY 必须能解出 32 字节（手机号 AES-GCM 加密与 phone_hash 查询都用它；换钥会使已有 phone_hash 失效）；
    - 需要安装 cryptography（否则密文退化为可逆编码）；
    - 配置项自身的取值范围（IdentityConfig.validate）。
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
    try:
        IdentityConfig().validate()
    except ValueError as exc:
        problems.append(str(exc))
    return problems
