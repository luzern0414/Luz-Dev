"""admin_rbac 配置：模块级配置数据类（环境变量读取，带安全默认值）+ 可替换时钟 + 扩展钩子。

项目定制只改这里列出的配置键（环境变量）或调用 set_config / set_* 钩子，不改业务代码。
默认值依据见 MODULE.md §0、§9。
"""
from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime

from luzback import get_settings, utcnow


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, "true" if default else "false").lower() in {"1", "true", "yes", "on"}


def _list(name: str, default: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in os.getenv(name, default).split(",") if x.strip())


DEFAULT_MFA_PERMISSIONS = "staff:write,role:write,order:refund,aftersales:approve,report:export,recharge:adjust"


@dataclass(frozen=True)
class AdminAuthConfig:
    # ---- 会话（OWASP 会话管理：空闲 15–30 分钟、绝对 4–8 小时；全部服务端强制）----
    session_idle_seconds: int = field(default_factory=lambda: _int("ADMIN_SESSION_IDLE_SECONDS", 900))
    session_absolute_seconds: int = field(default_factory=lambda: _int("ADMIN_SESSION_ABSOLUTE_SECONDS", 8 * 3600))
    session_touch_seconds: int = field(default_factory=lambda: _int("ADMIN_SESSION_TOUCH_SECONDS", 60))
    session_bind_user_agent: bool = field(default_factory=lambda: _bool("ADMIN_SESSION_BIND_UA", True))
    max_sessions_per_staff: int = field(default_factory=lambda: _int("ADMIN_MAX_SESSIONS", 5))
    fresh_auth_seconds: int = field(default_factory=lambda: _int("ADMIN_FRESH_AUTH_SECONDS", 300))
    mfa_challenge_seconds: int = field(default_factory=lambda: _int("ADMIN_MFA_CHALLENGE_SECONDS", 300))
    # ---- 登录节流（按账号；NIST 800-63B §3.2.2 上限 100 次）----
    login_max_failures: int = field(default_factory=lambda: _int("ADMIN_LOGIN_MAX_FAILURES", get_settings().login_max_failures))
    login_failure_window_seconds: int = field(default_factory=lambda: _int("ADMIN_LOGIN_FAILURE_WINDOW_SECONDS", 900))
    login_lock_seconds: int = field(default_factory=lambda: _int("ADMIN_LOGIN_LOCK_SECONDS", get_settings().login_lock_seconds))
    login_lock_max_seconds: int = field(default_factory=lambda: _int("ADMIN_LOGIN_LOCK_MAX_SECONDS", 86400))
    captcha_after_failures: int = field(default_factory=lambda: _int("ADMIN_CAPTCHA_AFTER_FAILURES", 3))
    # ---- 口令（NIST 800-63B-4 §3.1.1.2；OWASP 口令存储）----
    password_min_length_mfa: int = field(default_factory=lambda: _int("ADMIN_PASSWORD_MIN_MFA", 8))
    password_min_length_no_mfa: int = field(default_factory=lambda: _int("ADMIN_PASSWORD_MIN_NO_MFA", 15))
    password_max_length: int = field(default_factory=lambda: _int("ADMIN_PASSWORD_MAX", 128))
    password_blocklist_extra: tuple[str, ...] = field(default_factory=lambda: _list("ADMIN_PASSWORD_BLOCKLIST", ""))
    password_breach_fail_closed: bool = field(default_factory=lambda: _bool("ADMIN_PASSWORD_BREACH_FAIL_CLOSED", False))
    argon2_memory_kib: int = field(default_factory=lambda: _int("ADMIN_ARGON2_MEMORY_KIB", 19456))
    argon2_time_cost: int = field(default_factory=lambda: _int("ADMIN_ARGON2_TIME_COST", 2))
    argon2_parallelism: int = field(default_factory=lambda: _int("ADMIN_ARGON2_PARALLELISM", 1))
    temp_password_ttl_seconds: int = field(default_factory=lambda: _int("ADMIN_TEMP_PASSWORD_TTL_SECONDS", 72 * 3600))
    # ---- MFA（RFC 6238）----
    totp_issuer: str = field(default_factory=lambda: os.getenv("ADMIN_TOTP_ISSUER", get_settings().service_name))
    totp_drift_steps: int = field(default_factory=lambda: _int("ADMIN_TOTP_DRIFT_STEPS", 1))
    recovery_code_count: int = field(default_factory=lambda: _int("ADMIN_RECOVERY_CODE_COUNT", 10))
    mfa_required_all: bool = field(default_factory=lambda: _bool("ADMIN_MFA_REQUIRED_ALL", False))
    mfa_required_permissions: tuple[str, ...] = field(
        default_factory=lambda: _list("ADMIN_MFA_REQUIRED_PERMISSIONS", DEFAULT_MFA_PERMISSIONS))
    # ---- 保留与导出 ----
    login_log_retention_days: int = field(default_factory=lambda: _int("ADMIN_LOGIN_LOG_RETENTION_DAYS", 180))
    export_max_rows: int = field(default_factory=lambda: _int("ADMIN_EXPORT_MAX_ROWS", 50000))


_config: AdminAuthConfig | None = None


def get_config() -> AdminAuthConfig:
    global _config
    if _config is None:
        _config = AdminAuthConfig()
    return _config


def set_config(config: AdminAuthConfig | None = None, **overrides) -> AdminAuthConfig:
    """替换配置（项目启动或测试用）。set_config(login_max_failures=3) 在当前配置上覆盖单项。"""
    global _config
    base = config or get_config()
    _config = replace(base, **overrides) if overrides else base
    return _config


def reset_config() -> None:
    global _config
    _config = None


# ---------- 时钟（测试用 FakeClock 替换）----------

_clock: Callable[[], datetime] = utcnow


def now() -> datetime:
    return _clock()


def set_clock(fn: Callable[[], datetime] | None) -> None:
    global _clock
    _clock = fn or utcnow


# ---------- 扩展钩子 ----------

BreachChecker = Callable[[str], bool]                # 返回 True = 口令在泄露库中
CaptchaVerifier = Callable[[str, str | None], bool]  # (captcha_token, ip) -> 是否通过

_breach_checker: BreachChecker | None = None
_captcha_verifier: CaptchaVerifier | None = None


def set_breach_checker(fn: BreachChecker | None) -> None:
    """注入泄露口令检查（例如 Pwned Passwords k-匿名接口的客户端）；None 关闭。"""
    global _breach_checker
    _breach_checker = fn


def get_breach_checker() -> BreachChecker | None:
    return _breach_checker


def set_captcha_verifier(fn: CaptchaVerifier | None) -> None:
    """注入人机验证校验；None 表示不启用验证码（连续失败后不要求）。"""
    global _captcha_verifier
    _captcha_verifier = fn


def get_captcha_verifier() -> CaptchaVerifier | None:
    return _captcha_verifier
