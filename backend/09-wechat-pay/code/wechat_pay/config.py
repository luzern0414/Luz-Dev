"""wechat_pay 配置：全部来自环境变量，代码里没有真实密钥。

项目定制只改环境变量，或在启动时 `set_config(WechatPayConfig(...))`。
生产启动时调用 `assert_production_ready()`：缺密钥、仍用假客户端、回调地址不合规一律拒绝启动。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from urllib.parse import urlparse

from luzback import get_settings


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, "true" if default else "false").lower() in {"1", "true", "yes", "on"}


def _ints(name: str, default: str) -> tuple[int, ...]:
    return tuple(int(x) for x in os.getenv(name, default).split(",") if x.strip())


def _pairs(name: str) -> tuple[tuple[str, str], ...]:
    """`序列号=路径,序列号=路径` → ((序列号, 路径), ...)。"""
    out = []
    for item in os.getenv(name, "").split(","):
        serial, _, path = item.strip().partition("=")
        if serial and path:
            out.append((serial.strip(), path.strip()))
    return tuple(out)


@dataclass(frozen=True)
class WechatPayConfig:
    # ---- 客户端 ----
    client_mode: str = field(default_factory=lambda: _env("WECHAT_PAY_CLIENT", "fake"))  # fake | http
    appid: str = field(default_factory=lambda: _env("WECHAT_PAY_APPID", get_settings().wechat_appid))
    mchid: str = field(default_factory=lambda: _env("WECHAT_PAY_MCHID", get_settings().wechat_mch_id))
    apiv3_key: str = field(default_factory=lambda: _env("WECHAT_PAY_APIV3_KEY", get_settings().wechat_api_v3_key))
    private_key_path: str = field(default_factory=lambda: _env("WECHAT_PAY_PRIVATE_KEY_PATH"))
    private_key_pem: str = field(default_factory=lambda: _env("WECHAT_PAY_PRIVATE_KEY_PEM", get_settings().wechat_mch_private_key_pem))
    merchant_serial_no: str = field(default_factory=lambda: _env("WECHAT_PAY_CERT_SERIAL", get_settings().wechat_mch_serial_no))
    # 微信支付公钥模式（推荐）：PUB_KEY_ID_ + 公钥文件
    public_key_id: str = field(default_factory=lambda: _env("WECHAT_PAY_PUBLIC_KEY_ID"))
    public_key_path: str = field(default_factory=lambda: _env("WECHAT_PAY_PUBLIC_KEY_PATH"))
    # 平台证书模式（兼容 / 灰度期并存）：序列号=证书文件,…
    platform_certs: tuple[tuple[str, str], ...] = field(default_factory=lambda: _pairs("WECHAT_PAY_PLATFORM_CERTS"))
    notify_url: str = field(default_factory=lambda: _env("WECHAT_PAY_NOTIFY_URL", "https://example.com/api/v1/webhooks/wechat/pay"))
    refund_notify_url: str = field(default_factory=lambda: _env("WECHAT_PAY_REFUND_NOTIFY_URL", "https://example.com/api/v1/webhooks/wechat/refund"))
    base_url: str = field(default_factory=lambda: _env("WECHAT_PAY_BASE_URL", "https://api.mch.weixin.qq.com"))
    backup_base_url: str = field(default_factory=lambda: _env("WECHAT_PAY_BACKUP_BASE_URL", "https://api2.mch.weixin.qq.com"))
    user_agent: str = field(default_factory=lambda: _env("WECHAT_PAY_USER_AGENT", "luz-backend-wechatpay/2.0"))

    # ---- 超时与重试（SPEC §9：下单 8s 0 次重试、查询 5s 2 次、退款 10s 0 次由任务重试）----
    timeout_prepay_s: float = field(default_factory=lambda: _float("WECHAT_PAY_TIMEOUT_PREPAY", 8))
    timeout_query_s: float = field(default_factory=lambda: _float("WECHAT_PAY_TIMEOUT_QUERY", 5))
    timeout_close_s: float = field(default_factory=lambda: _float("WECHAT_PAY_TIMEOUT_CLOSE", 5))
    timeout_refund_s: float = field(default_factory=lambda: _float("WECHAT_PAY_TIMEOUT_REFUND", 10))
    timeout_bill_s: float = field(default_factory=lambda: _float("WECHAT_PAY_TIMEOUT_BILL", 30))
    query_attempts: int = field(default_factory=lambda: _int("WECHAT_PAY_QUERY_ATTEMPTS", 3))
    close_attempts: int = field(default_factory=lambda: _int("WECHAT_PAY_CLOSE_ATTEMPTS", 2))
    bulkhead: int = field(default_factory=lambda: _int("WECHAT_PAY_BULKHEAD", 30))
    timestamp_skew_s: int = field(default_factory=lambda: _int("WECHAT_PAY_TIMESTAMP_SKEW", 300))

    # ---- 支付单 ----
    out_trade_no_prefix: str = field(default_factory=lambda: _env("WECHAT_PAY_OUT_TRADE_NO_PREFIX", "P"))
    default_expire_minutes: int = field(default_factory=lambda: _int("WECHAT_PAY_EXPIRE_MINUTES", 15))
    prepay_reuse_seconds: int = field(default_factory=lambda: _int("WECHAT_PAY_PREPAY_REUSE_SECONDS", 7200 - 300))
    app_sync_min_interval_s: int = field(default_factory=lambda: _int("WECHAT_PAY_APP_SYNC_INTERVAL", 3))
    # 官方主动查单方案一：下单后 5s / 30s / 1m / 3m / 5m / 10m / 30m
    query_schedule_s: tuple[int, ...] = field(default_factory=lambda: _ints("WECHAT_PAY_QUERY_SCHEDULE", "5,30,60,180,300,600,1800"))
    auto_refund_orphans: bool = field(default_factory=lambda: _bool("WECHAT_PAY_AUTO_REFUND_ORPHANS", True))
    tz: str = field(default_factory=lambda: _env("WECHAT_PAY_TZ", "Asia/Shanghai"))

    # ---- 退款 ----
    refund_max_times: int = field(default_factory=lambda: _int("WECHAT_PAY_REFUND_MAX_TIMES", 50))
    refund_min_interval_s: int = field(default_factory=lambda: _int("WECHAT_PAY_REFUND_MIN_INTERVAL", 60))
    refund_window_days: int = field(default_factory=lambda: _int("WECHAT_PAY_REFUND_WINDOW_DAYS", 365))
    refund_submit_max_attempts: int = field(default_factory=lambda: _int("WECHAT_PAY_REFUND_MAX_ATTEMPTS", 5))
    refund_query_fast_s: int = field(default_factory=lambda: _int("WECHAT_PAY_REFUND_QUERY_FAST", 60))
    refund_query_fast_window_s: int = field(default_factory=lambda: _int("WECHAT_PAY_REFUND_QUERY_FAST_WINDOW", 300))
    refund_query_backoff_min: tuple[int, ...] = field(default_factory=lambda: _ints("WECHAT_PAY_REFUND_QUERY_BACKOFF", "5,10,20,30"))

    # ---- 账单 ----
    bill_ready_hour: int = field(default_factory=lambda: _int("WECHAT_PAY_BILL_READY_HOUR", 10))
    bill_retry_minutes: int = field(default_factory=lambda: _int("WECHAT_PAY_BILL_RETRY_MINUTES", 30))
    bill_max_days: int = field(default_factory=lambda: _int("WECHAT_PAY_BILL_MAX_DAYS", 90))

    # ---- 小程序发货信息管理 ----
    shipping_enabled: bool = field(default_factory=lambda: _bool("WECHAT_PAY_SHIPPING_ENABLED", True))
    shipping_max_attempts: int = field(default_factory=lambda: _int("WECHAT_PAY_SHIPPING_MAX_ATTEMPTS", 8))

    def with_(self, **changes) -> "WechatPayConfig":
        return replace(self, **changes)


_config: WechatPayConfig | None = None


def get_config() -> WechatPayConfig:
    global _config
    if _config is None:
        _config = WechatPayConfig()
    return _config


def set_config(config: WechatPayConfig | None) -> None:
    """项目启动或测试时替换配置；None 表示下次重新从环境变量读取。"""
    global _config
    _config = config


def _https_without_query(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    is_ip = host.replace(".", "").isdigit() or ":" in host
    return parsed.scheme == "https" and bool(host) and not parsed.query and host != "localhost" and not is_ip and "example.com" not in host


def production_problems(cfg: WechatPayConfig) -> list[str]:
    """返回生产配置问题（空列表表示可上线）。"""
    problems: list[str] = []
    if cfg.client_mode != "http":
        problems.append("WECHAT_PAY_CLIENT must be http in prod")
    if len(cfg.apiv3_key.encode()) != 32:
        problems.append("WECHAT_PAY_APIV3_KEY must be 32 bytes")
    if not (cfg.private_key_path or cfg.private_key_pem):
        problems.append("WECHAT_PAY_PRIVATE_KEY_PATH is required")
    if not cfg.merchant_serial_no:
        problems.append("WECHAT_PAY_CERT_SERIAL is required")
    if not ((cfg.public_key_id and cfg.public_key_path) or cfg.platform_certs):
        problems.append("WECHAT_PAY_PUBLIC_KEY_ID/PATH or WECHAT_PAY_PLATFORM_CERTS is required")
    if cfg.public_key_id and not cfg.public_key_id.startswith("PUB_KEY_ID_"):
        problems.append("WECHAT_PAY_PUBLIC_KEY_ID must start with PUB_KEY_ID_")
    for name, url in (("WECHAT_PAY_NOTIFY_URL", cfg.notify_url), ("WECHAT_PAY_REFUND_NOTIFY_URL", cfg.refund_notify_url)):
        if not _https_without_query(url):
            problems.append(f"{name} must be a public https URL without query string")
    return problems


def assert_production_ready(cfg: WechatPayConfig | None = None) -> None:
    cfg = cfg or get_config()
    if not get_settings().is_prod:
        return
    problems = production_problems(cfg)
    if problems:
        raise RuntimeError("unsafe wechat pay settings: " + "; ".join(problems))
