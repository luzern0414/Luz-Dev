"""运行配置：全部来自环境变量，代码里没有真实密钥。

用法：
    settings = get_settings()          # 进程内缓存
    settings.assert_production_safe()  # 启动时调用，prod 下拒绝开发默认值

模块级参数（如订单超时）也集中在这里，业务代码通过 settings 读取，不写常量；
后台可改的业务参数由 app_settings 模块覆盖（优先级高于环境变量）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache

DEV_SECRET = "dev-only-change-me"


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, "true" if default else "false").lower() in {"1", "true", "yes", "on"}


def _list(name: str, default: str) -> tuple[str, ...]:
    raw = os.getenv(name, default)
    return tuple(item.strip() for item in raw.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    # ---- 运行环境 ----
    env: str = field(default_factory=lambda: _env("LUZ_ENV", "dev"))
    service_name: str = field(default_factory=lambda: _env("LUZ_SERVICE_NAME", "luz-backend"))
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "info"))
    timezone: str = field(default_factory=lambda: _env("LUZ_TIMEZONE", "Asia/Shanghai"))

    # ---- 网关与内网 ----
    # 只有来自这些地址的 X-Forwarded-For 才可信（nginx 所在网段）；其他请求一律取 TCP 对端地址
    trusted_proxies: tuple[str, ...] = field(default_factory=lambda: _list("LUZ_TRUSTED_PROXIES", "127.0.0.1/32,::1/128"))
    # /metrics 只对内网开放；也可用 LUZ_METRICS_TOKEN 以 Bearer 访问
    internal_networks: tuple[str, ...] = field(default_factory=lambda: _list(
        "LUZ_INTERNAL_NETWORKS", "127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"))
    metrics_token: str = field(default_factory=lambda: _env("LUZ_METRICS_TOKEN", ""))

    # ---- 存储 ----
    database_url: str = field(default_factory=lambda: _env("LUZ_DATABASE_URL", "sqlite:///./luz-dev.db"))
    db_pool_size: int = field(default_factory=lambda: _int("LUZ_DB_POOL_SIZE", 20))
    db_statement_timeout_ms: int = field(default_factory=lambda: _int("LUZ_DB_STATEMENT_TIMEOUT_MS", 5000))
    redis_url: str = field(default_factory=lambda: _env("LUZ_REDIS_URL", ""))

    # ---- 令牌与口令 ----
    jwt_secret: str = field(default_factory=lambda: _env("LUZ_JWT_SECRET", DEV_SECRET))
    jwt_issuer: str = field(default_factory=lambda: _env("LUZ_JWT_ISSUER", "luz"))
    access_ttl_seconds: int = field(default_factory=lambda: _int("LUZ_ACCESS_TTL", 7200))
    refresh_ttl_seconds: int = field(default_factory=lambda: _int("LUZ_REFRESH_TTL", 30 * 86400))
    admin_access_ttl_seconds: int = field(default_factory=lambda: _int("LUZ_ADMIN_ACCESS_TTL", 7200))
    admin_refresh_ttl_seconds: int = field(default_factory=lambda: _int("LUZ_ADMIN_REFRESH_TTL", 7 * 86400))
    fresh_window_seconds: int = field(default_factory=lambda: _int("LUZ_FRESH_WINDOW", 900))
    password_min_length: int = field(default_factory=lambda: _int("LUZ_PASSWORD_MIN_LENGTH", 10))
    login_max_failures: int = field(default_factory=lambda: _int("LUZ_LOGIN_MAX_FAILURES", 5))
    login_lock_seconds: int = field(default_factory=lambda: _int("LUZ_LOGIN_LOCK_SECONDS", 900))

    # ---- 字段加密 ----
    field_encryption_key: str = field(default_factory=lambda: _env("LUZ_FIELD_KEY", ""))  # 32 字节 base64

    # ---- 服务间签名 ----
    service_secrets: tuple[str, ...] = field(default_factory=lambda: _list("LUZ_SERVICE_SECRETS", "agent:dev-agent-secret"))
    service_signature_skew_seconds: int = field(default_factory=lambda: _int("LUZ_SERVICE_SKEW", 300))

    # ---- 微信 ----
    wechat_appid: str = field(default_factory=lambda: _env("WECHAT_APPID", "wx-dev-appid"))
    wechat_secret: str = field(default_factory=lambda: _env("WECHAT_SECRET", "wx-dev-secret"))
    wechat_mch_id: str = field(default_factory=lambda: _env("WECHAT_MCH_ID", "1900000000"))
    wechat_api_v3_key: str = field(default_factory=lambda: _env("WECHAT_API_V3_KEY", ""))  # 32 字节
    wechat_mch_serial_no: str = field(default_factory=lambda: _env("WECHAT_MCH_SERIAL_NO", ""))
    wechat_mch_private_key_pem: str = field(default_factory=lambda: _env("WECHAT_MCH_PRIVATE_KEY_PEM", ""))
    wechat_platform_cert_pem: str = field(default_factory=lambda: _env("WECHAT_PLATFORM_CERT_PEM", ""))
    wechat_notify_url: str = field(default_factory=lambda: _env("WECHAT_NOTIFY_URL", "https://example.com/api/v1/webhooks/wechat/pay"))
    pay_notify_hmac_secret: str = field(default_factory=lambda: _env("LUZ_PAY_NOTIFY_SECRET", "dev-notify-secret"))  # 仅开发联调

    # ---- 业务默认值（可被 app_settings 覆盖）----
    order_pay_timeout_minutes: int = field(default_factory=lambda: _int("ORDER_PAY_TIMEOUT_MINUTES", 15))
    order_auto_accept: bool = field(default_factory=lambda: _bool("ORDER_AUTO_ACCEPT", False))
    order_auto_complete_hours: int = field(default_factory=lambda: _int("ORDER_AUTO_COMPLETE_HOURS", 24))
    order_accept_overdue_minutes: int = field(default_factory=lambda: _int("ORDER_ACCEPT_OVERDUE_MINUTES", 3))
    quote_ttl_seconds: int = field(default_factory=lambda: _int("ORDER_QUOTE_TTL", 600))
    address_max_per_user: int = field(default_factory=lambda: _int("ADDRESS_MAX_PER_USER", 20))
    points_per_yuan: int = field(default_factory=lambda: _int("POINTS_PER_YUAN", 1))
    points_redeem_rate: int = field(default_factory=lambda: _int("POINTS_REDEEM_RATE", 100))  # 100 积分 = 1 元
    points_redeem_cap_bp: int = field(default_factory=lambda: _int("POINTS_REDEEM_CAP_BP", 3000))  # 最多抵 30%
    queue_miss_seconds: int = field(default_factory=lambda: _int("QUEUE_MISS_SECONDS", 300))
    reservation_hold_seconds: int = field(default_factory=lambda: _int("RESERVATION_HOLD_SECONDS", 600))
    reservation_grace_minutes: int = field(default_factory=lambda: _int("RESERVATION_GRACE_MINUTES", 15))
    recharge_max_balance_cents: int = field(default_factory=lambda: _int("RECHARGE_MAX_BALANCE_CENTS", 500000))
    sms_daily_limit: int = field(default_factory=lambda: _int("SMS_DAILY_LIMIT", 10))
    upload_max_bytes: int = field(default_factory=lambda: _int("UPLOAD_MAX_BYTES", 5 * 1024 * 1024))

    # ---- 限流（每秒）----
    rate_ip_per_second: float = field(default_factory=lambda: float(_env("RATE_IP_PER_SECOND", "20")))
    rate_user_write_per_second: float = field(default_factory=lambda: float(_env("RATE_USER_WRITE_PER_SECOND", "3")))
    rate_user_read_per_second: float = field(default_factory=lambda: float(_env("RATE_USER_READ_PER_SECOND", "20")))
    rate_login_per_minute: int = field(default_factory=lambda: _int("RATE_LOGIN_PER_MINUTE", 5))

    # ---- 弹性（秒）----
    http_timeout_read: float = field(default_factory=lambda: float(_env("HTTP_TIMEOUT_READ", "3")))
    http_timeout_write: float = field(default_factory=lambda: float(_env("HTTP_TIMEOUT_WRITE", "5")))
    http_timeout_pay: float = field(default_factory=lambda: float(_env("HTTP_TIMEOUT_PAY", "8")))
    breaker_window_seconds: int = field(default_factory=lambda: _int("BREAKER_WINDOW_S", 60))
    breaker_min_failures: int = field(default_factory=lambda: _int("BREAKER_MIN_FAILS", 10))
    breaker_fail_rate: float = field(default_factory=lambda: float(_env("BREAKER_FAIL_RATE", "0.5")))
    breaker_open_seconds: int = field(default_factory=lambda: _int("BREAKER_OPEN_S", 30))

    # ---- 请求限制 ----
    max_body_bytes: int = field(default_factory=lambda: _int("LUZ_MAX_BODY_BYTES", 256 * 1024))
    cors_origins: tuple[str, ...] = field(default_factory=lambda: _list("LUZ_CORS_ORIGINS", ""))

    # ---- 功能开关 ----
    features: tuple[str, ...] = field(default_factory=lambda: _list("LUZ_FEATURES", ""))

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"

    def feature_enabled(self, name: str) -> bool:
        return not self.features or name in self.features

    def service_secret(self, service: str) -> str | None:
        for item in self.service_secrets:
            key, _, value = item.partition(":")
            if key == service and value:
                return value
        return None

    def assert_production_safe(self) -> None:
        """生产环境禁止开发默认值；启动时调用，失败即拒绝启动。"""
        if not self.is_prod:
            return
        problems: list[str] = []
        if self.jwt_secret == DEV_SECRET or len(self.jwt_secret) < 32:
            problems.append("LUZ_JWT_SECRET must be >= 32 chars")
        if self.database_url.startswith("sqlite"):
            problems.append("sqlite is not allowed in prod")
        if not self.redis_url:
            problems.append("LUZ_REDIS_URL is required in prod")
        if not self.field_encryption_key:
            problems.append("LUZ_FIELD_KEY is required in prod")
        if self.pay_notify_hmac_secret == "dev-notify-secret" and not self.wechat_platform_cert_pem:
            problems.append("WECHAT_PLATFORM_CERT_PEM is required in prod")
        if any(item.endswith(":dev-agent-secret") for item in self.service_secrets):
            problems.append("LUZ_SERVICE_SECRETS contains a dev secret")
        if problems:
            raise RuntimeError("unsafe production settings: " + "; ".join(problems))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reload_settings() -> Settings:
    """测试或热更新时重新读取环境变量。"""
    get_settings.cache_clear()
    return get_settings()
