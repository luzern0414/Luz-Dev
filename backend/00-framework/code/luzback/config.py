"""部署参数与秘密：来自环境变量（/etc/luz/api.env，模板 assets/deploy/api.env.example），代码里没有真实密钥。

用法：
    settings = get_settings()          # 进程内缓存
    settings.assert_production_safe()  # 启动时调用，prod 下拒绝开发默认值

五类设置分开放（references/config-logging.md §1）：
- 秘密（JWT 密钥、字段密钥、AppSecret、商户私钥）与部署参数（数据库与 Redis 地址、连接池、可信代理网段、服务名）在这里，改了要重启；
- 运行参数（超时、上限、限流阈值、开关、日志级别）在 `backend/config/runtime/*.toml`，字段说明见 `runtime_config.py`，
  服务端热加载，读取用 `luzback.runtime_loader.current_config()`；环境变量不覆盖运行参数，同一个值只有一个来源；
- 经营与运营内容（营业时间、起送价、公告）与前台文案、展示设置在数据库，运营在后台维护；
  admin_visible 为否的文案与展示由开发经 23-app-settings 的种子维护。
"""
from __future__ import annotations

import os
import socket
import threading
from collections.abc import Callable, Iterable, Mapping
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
    app_version: str = field(default_factory=lambda: _env("APP_VERSION", "dev"))  # 镜像构建时写入（Dockerfile ARG）
    instance_id: str = field(default_factory=lambda: _env("LUZ_INSTANCE_ID", socket.gethostname())[:64])
    timezone: str = field(default_factory=lambda: _env("LUZ_TIMEZONE", "Asia/Shanghai"))

    # ---- 运行配置文件（内容见 config/runtime/*.toml）----
    config_dir: str = field(default_factory=lambda: _env("LUZ_CONFIG_DIR", ""))  # 空 = backend/config/runtime
    config_watch: str = field(default_factory=lambda: _env("LUZ_CONFIG_WATCH", "auto"))  # auto | poll | off

    # ---- 网关与内网 ----
    # 只有来自这些地址的 X-Forwarded-For 才可信（nginx 所在网段）；其他请求一律取 TCP 对端地址
    trusted_proxies: tuple[str, ...] = field(default_factory=lambda: _list("LUZ_TRUSTED_PROXIES", "127.0.0.1/32,::1/128"))
    # /metrics 与 /internal/* 只对内网开放；/metrics 也可用 LUZ_METRICS_TOKEN 以 Bearer 访问
    internal_networks: tuple[str, ...] = field(default_factory=lambda: _list(
        "LUZ_INTERNAL_NETWORKS", "127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"))
    metrics_token: str = field(default_factory=lambda: _env("LUZ_METRICS_TOKEN", ""))
    # /internal/config 与 /internal/config/reload 的运维令牌（内网 + Bearer 同时满足）；为空时这两个接口一律拒绝
    ops_token: str = field(default_factory=lambda: _env("LUZ_OPS_TOKEN", ""))

    # ---- 存储（部署参数，改了要重启）----
    database_url: str = field(default_factory=lambda: _env("LUZ_DATABASE_URL", "sqlite:///./luz-dev.db"))
    db_pool_size: int = field(default_factory=lambda: _int("LUZ_DB_POOL_SIZE", 20))
    redis_url: str = field(default_factory=lambda: _env("LUZ_REDIS_URL", ""))

    # ---- 令牌 ----
    jwt_secret: str = field(default_factory=lambda: _env("LUZ_JWT_SECRET", DEV_SECRET))
    jwt_issuer: str = field(default_factory=lambda: _env("LUZ_JWT_ISSUER", "luz"))

    # ---- 字段加密 ----
    field_encryption_key: str = field(default_factory=lambda: _env("LUZ_FIELD_KEY", ""))  # 32 字节 base64

    # ---- 服务间签名 ----
    service_secrets: tuple[str, ...] = field(default_factory=lambda: _list("LUZ_SERVICE_SECRETS", "agent:dev-agent-secret"))

    # ---- 微信 ----
    wechat_appid: str = field(default_factory=lambda: _env("WECHAT_APPID", "wx-dev-appid"))
    wechat_secret: str = field(default_factory=lambda: _env("WECHAT_SECRET", "wx-dev-secret"))
    wechat_mch_id: str = field(default_factory=lambda: _env("WECHAT_MCH_ID", "1900000000"))
    wechat_api_v3_key: str = field(default_factory=lambda: _env("WECHAT_API_V3_KEY", ""))  # 32 字节
    wechat_mch_serial_no: str = field(default_factory=lambda: _env("WECHAT_MCH_SERIAL_NO", ""))
    wechat_mch_private_key_pem: str = field(default_factory=lambda: _env("WECHAT_MCH_PRIVATE_KEY_PEM", ""))
    wechat_notify_url: str = field(default_factory=lambda: _env("WECHAT_NOTIFY_URL", "https://example.com/api/v1/webhooks/wechat/pay"))

    # ---- 跨域（部署参数：后台域名）----
    cors_origins: tuple[str, ...] = field(default_factory=lambda: _list("LUZ_CORS_ORIGINS", ""))

    # ---- 模块装配（部署参数：未启用的模块不导入、不暴露接口，改了要重启）----
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
        """生产环境禁止开发默认值；启动时调用，失败即拒绝启动（列出全部问题）。

        框架只检查通用项；各模块用 `register_production_check(name, fn)` 登记自己的检查
        （例如 wechat_pay 的商户证书 / APIv3 密钥、wechat_mp 的 AppSecret、identity 的字段密钥），这里统一执行。
        微信支付回调只走 APIv3 验签（平台公钥 / 平台证书由 wechat_pay 配置），没有 HMAC 联调通道。
        """
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
        if any(item.endswith(":dev-agent-secret") for item in self.service_secrets):
            problems.append("LUZ_SERVICE_SECRETS contains a dev secret")
        if self.ops_token and len(self.ops_token) < 32:
            problems.append("LUZ_OPS_TOKEN must be >= 32 chars when set")
        if self.config_watch not in ("auto", "poll", "off"):
            problems.append("LUZ_CONFIG_WATCH must be auto, poll or off")
        problems.extend(run_production_checks(self))
        if problems:
            raise RuntimeError("unsafe production settings: " + "; ".join(problems))


# ---------- 环境变量清单：启动时扫一遍，不认识或已迁走的变量打警告（拼错的名字会让默认值悄悄生效）----------

# 框架里已迁到运行配置文件或已删除的变量 → 去处。各模块迁走的变量登记在字段说明的 legacy_env 里（runtime_config.py），
# env_warnings 两处一起查。
MOVED_ENV: dict[str, str] = {
    # 经营规则：后台“经营设置”页维护（luzback.business，见 references/config-logging.md §1）
    "WALLET_SPLIT_POLICY": "后台经营设置 wallet.split_policy",
    "WALLET_CUSTOMER_REFUND_FEE_BP": "后台经营设置 wallet.customer_refund_fee_bp",
    "MEMBER_CARD_REPLACE_FEE_CENTS": "后台经营设置 member_card.replace_fee_cents",
    "COUPON_RETURN_POLICY": "后台经营设置 coupon.return_policy",
    "COUPON_RETURN_WINDOW_HOURS": "后台经营设置 coupon.return_window_hours",
    "LOG_LEVEL": "config/runtime [logging] level",
    "LUZ_DB_STATEMENT_TIMEOUT_MS": "config/runtime [db] statement_timeout_ms",
    "LUZ_MAX_BODY_BYTES": "config/runtime [http] max_body_kb",
    "LUZ_FRESH_WINDOW": "config/runtime [auth] fresh_window_s",
    "LUZ_SERVICE_SKEW": "config/runtime [auth] service_signature_skew_s",
    "RATE_IP_PER_SECOND": "config/runtime [rate_limit] ip_per_s",
    "RATE_USER_WRITE_PER_SECOND": "config/runtime [rate_limit] user_write_per_s",
    "RATE_LOGIN_PER_MINUTE": "config/runtime [rate_limit] login_per_min",
    "BREAKER_WINDOW_S": "config/runtime [breaker] window_s",
    "BREAKER_MIN_FAILS": "config/runtime [breaker] min_failures",
    "BREAKER_FAIL_RATE": "config/runtime [breaker] fail_rate",
    "BREAKER_OPEN_S": "config/runtime [breaker] open_s",
    "UPLOAD_MAX_BYTES": "config/runtime [upload] doc_max_mb",
    "LUZ_ACCESS_TTL": "removed: config/runtime [identity] access_ttl_seconds",
    "LUZ_REFRESH_TTL": "removed: config/runtime [identity] refresh_idle_days / refresh_absolute_days",
    "LUZ_ADMIN_ACCESS_TTL": "removed: config/runtime [admin_rbac] session_idle_seconds / session_absolute_seconds",
    "LUZ_ADMIN_REFRESH_TTL": "removed: config/runtime [admin_rbac] session_absolute_seconds",
    "LUZ_PASSWORD_MIN_LENGTH": "removed: config/runtime [admin_rbac] password_min_length_no_mfa",
    "ORDER_AUTO_ACCEPT": "removed: auto-accept is a store setting (stores.auto_accept)",
    "ORDER_AUTO_COMPLETE_HOURS": "removed: config/runtime [order] auto_complete_days",
    "ORDER_ACCEPT_OVERDUE_MINUTES": "removed: not read by any module",
    "ADDRESS_MAX_PER_USER": "removed: address uses MAX_ADDRESSES_PER_USER",
    "POINTS_REDEEM_RATE": "removed: not read by any module",
    "POINTS_REDEEM_CAP_BP": "removed: not read by any module",
    "QUEUE_MISS_SECONDS": "removed: not read by any module",
    "RESERVATION_HOLD_SECONDS": "removed: not read by any module",
    "RESERVATION_GRACE_MINUTES": "removed: not read by any module",
    "RECHARGE_MAX_BALANCE_CENTS": "removed: config/runtime [prepaid] policy sets the balance cap",
    "RATE_USER_READ_PER_SECOND": "removed: no read rate rule",
    "HTTP_TIMEOUT_READ": "removed: each client passes its timeout to resilient()",
    "HTTP_TIMEOUT_WRITE": "removed: each client passes its timeout to resilient()",
    "HTTP_TIMEOUT_PAY": "removed: config/runtime [wechat_pay] timeout_prepay_s and related timeouts",
}

_known_env: set[str] = {
    "LUZ_ENV", "LUZ_SERVICE_NAME", "LUZ_INSTANCE_ID", "LUZ_TIMEZONE", "LUZ_CONFIG_DIR", "LUZ_CONFIG_WATCH",
    "LUZ_TRUSTED_PROXIES", "LUZ_INTERNAL_NETWORKS", "LUZ_METRICS_TOKEN", "LUZ_OPS_TOKEN", "LUZ_DATABASE_URL",
    "LUZ_DB_POOL_SIZE", "LUZ_REDIS_URL", "LUZ_JWT_SECRET", "LUZ_JWT_ISSUER", "LUZ_FIELD_KEY", "LUZ_SERVICE_SECRETS",
    "LUZ_CORS_ORIGINS", "LUZ_FEATURES", "LUZ_WORKER_HEARTBEAT_FILE",
}


def register_env_names(*names: str) -> None:
    """模块登记自己读取的 LUZ_ 环境变量，启动扫描时不再报“不认识”。"""
    _known_env.update(names)


def moved_env() -> dict[str, str]:
    """全部已迁走的环境变量 → 去处：框架的 MOVED_ENV + 字段说明里各键的 legacy_env。"""
    from .runtime_config import legacy_env_index  # noqa: PLC0415 - 字段说明只依赖 pydantic

    moved = dict(MOVED_ENV)
    for name, path in legacy_env_index().items():
        group, _, key = path.partition(".")
        moved[name] = f"config/runtime [{group}] {key}"
    return moved


def env_warnings(environ: Mapping[str, str] | None = None) -> list[tuple[str, str]]:
    """[(变量名, 说明)]：已迁到运行配置 / 已删除的变量，以及不认识的 LUZ_ 变量。只返回名字，不返回值。"""
    environ = os.environ if environ is None else environ
    moved = moved_env()
    out: list[tuple[str, str]] = []
    for name in sorted(environ):
        if name in moved:
            out.append((name, moved[name]))
        elif name.startswith("LUZ_") and name not in _known_env:
            out.append((name, "unknown LUZ_ variable (misspelt?)"))
    return out


# ---------- 模块生产配置检查（启动时由 assert_production_safe 统一执行）----------

ProductionCheck = Callable[["Settings"], "Iterable[str] | None"]
_checks_lock = threading.Lock()
_production_checks: dict[str, ProductionCheck] = {}


def register_production_check(name: str, fn: ProductionCheck) -> None:
    """登记模块的生产配置检查：fn(settings) 返回问题列表（空 / None = 通过），或抛 RuntimeError / ValueError。

    在模块包导入时调用（同名覆盖）；只在 LUZ_ENV=prod 时执行。
    """
    with _checks_lock:
        _production_checks[name] = fn


def unregister_production_check(name: str) -> None:
    with _checks_lock:
        _production_checks.pop(name, None)


def production_checks() -> dict[str, ProductionCheck]:
    with _checks_lock:
        return dict(_production_checks)


def run_production_checks(settings: "Settings") -> list[str]:
    """执行全部已登记检查，返回带模块名前缀的问题列表（不抛异常）。"""
    problems: list[str] = []
    for name, fn in sorted(production_checks().items()):
        try:
            result = fn(settings)
        except (RuntimeError, ValueError) as exc:
            problems.append(f"{name}: {exc}")
            continue
        problems.extend(f"{name}: {item}" for item in (result or ()))
    return problems


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reload_settings() -> Settings:
    """测试时重新读取环境变量（部署参数改了要重启进程，不做热加载）。"""
    get_settings.cache_clear()
    return get_settings()
