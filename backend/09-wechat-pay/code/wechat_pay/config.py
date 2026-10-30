"""wechat_pay 配置：秘密与部署参数（环境变量，代码里没有真实密钥）+ 运行参数（config/runtime 的 `[wechat_pay]`）。

- 环境变量：客户端模式、AppID、商户号、APIv3 密钥、商户私钥与证书序列号、微信支付公钥或平台证书、回调地址、
  接口地址、User-Agent、商户订单号前缀、时区。
- 运行参数（超时与重试、查单节奏、支付单有效期、退款、账单、发货信息）的类型、范围与默认值登记在
  `luzback/runtime_config.py` 的 WechatPaySettings；这里的数据类是读取时的合并结果，属性名与键同名。
`set_config(WechatPayConfig(...))` 只给项目启动与测试用（其中的运行参数转成测试用运行配置，生产环境不能用）；
`set_config(None)` 恢复为环境变量与运行配置。生产启动时调用 `assert_production_ready()`：缺密钥、仍用假客户端、
回调地址不合规一律拒绝启动。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from functools import partial
from urllib.parse import urlparse

from luzback import get_settings
from luzback.module_config import ModuleConfig, runtime_default

_rt = partial(runtime_default, "wechat_pay")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


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
    # ---- 秘密与部署参数（环境变量）：客户端 ----
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
    out_trade_no_prefix: str = field(default_factory=lambda: _env("WECHAT_PAY_OUT_TRADE_NO_PREFIX", "P"))
    tz: str = field(default_factory=lambda: _env("WECHAT_PAY_TZ", "Asia/Shanghai"))

    # ---- 运行参数（[wechat_pay]）：超时与重试（SPEC §9：下单 8s 0 次重试、查询 5s 2 次、退款 10s 0 次由任务重试）----
    timeout_prepay_s: float = field(default_factory=_rt("timeout_prepay_s"))
    timeout_query_s: float = field(default_factory=_rt("timeout_query_s"))
    timeout_close_s: float = field(default_factory=_rt("timeout_close_s"))
    timeout_refund_s: float = field(default_factory=_rt("timeout_refund_s"))
    timeout_bill_s: float = field(default_factory=_rt("timeout_bill_s"))
    query_attempts: int = field(default_factory=_rt("query_attempts"))
    close_attempts: int = field(default_factory=_rt("close_attempts"))
    bulkhead: int = field(default_factory=_rt("bulkhead"))
    timestamp_skew_s: int = field(default_factory=_rt("timestamp_skew_s"))
    # ---- 支付单 ----
    default_expire_minutes: int = field(default_factory=_rt("default_expire_minutes"))
    prepay_reuse_seconds: int = field(default_factory=_rt("prepay_reuse_seconds"))
    app_sync_min_interval_s: int = field(default_factory=_rt("app_sync_min_interval_s"))
    # 官方主动查单方案一：下单后 5s / 30s / 1m / 3m / 5m / 10m / 30m
    query_schedule_s: tuple[int, ...] = field(default_factory=_rt("query_schedule_s"))
    auto_refund_orphans: bool = field(default_factory=_rt("auto_refund_orphans"))
    # ---- 退款 ----
    refund_max_times: int = field(default_factory=_rt("refund_max_times"))
    refund_min_interval_s: int = field(default_factory=_rt("refund_min_interval_s"))
    refund_window_days: int = field(default_factory=_rt("refund_window_days"))
    refund_submit_max_attempts: int = field(default_factory=_rt("refund_submit_max_attempts"))
    refund_query_fast_s: int = field(default_factory=_rt("refund_query_fast_s"))
    refund_query_fast_window_s: int = field(default_factory=_rt("refund_query_fast_window_s"))
    refund_query_backoff_min: tuple[int, ...] = field(default_factory=_rt("refund_query_backoff_min"))
    # ---- 账单 ----
    bill_ready_hour: int = field(default_factory=_rt("bill_ready_hour"))
    bill_retry_minutes: int = field(default_factory=_rt("bill_retry_minutes"))
    bill_max_days: int = field(default_factory=_rt("bill_max_days"))
    # ---- 小程序发货信息管理 ----
    shipping_enabled: bool = field(default_factory=_rt("shipping_enabled"))
    shipping_max_attempts: int = field(default_factory=_rt("shipping_max_attempts"))

    def with_(self, **changes) -> "WechatPayConfig":
        return replace(self, **changes)


_module = ModuleConfig("wechat_pay", WechatPayConfig)


def get_config() -> WechatPayConfig:
    return _module.get()


def set_config(config: WechatPayConfig | None) -> None:
    """项目启动或测试时替换配置；None 表示恢复为环境变量与运行配置（撤销测试覆盖）。"""
    if config is None:
        _module.reset()
    else:
        _module.set(config)


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
