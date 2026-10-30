"""admin_rbac 配置：部署参数（环境变量）+ 运行参数（config/runtime 的 `[admin_rbac]`，热加载）+ 可替换时钟 + 扩展钩子。

部署参数只有 TOTP 签发方名称 ADMIN_TOTP_ISSUER（默认 LUZ_SERVICE_NAME，显示在验证器 App 里）。
运行参数（会话、登录锁定、口令、第二因素、保留与导出）的类型、范围与默认值登记在 `luzback/runtime_config.py` 的
AdminRbacSettings，依据见 MODULE.md §0、§9；这里的数据类是读取时的合并结果，属性名与键同名。
`set_config(...)` 只给项目启动与测试用（运行参数转成测试用运行配置，生产环境不能用）；其余定制用 set_* 钩子。
"""
from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial

from luzback import get_settings, utcnow
from luzback.module_config import ModuleConfig, runtime_default
from luzback.runtime_config import DEFAULT_MFA_PERMISSIONS  # noqa: F401  持有这些码的员工必须绑定 MFA（默认值）

_rt = partial(runtime_default, "admin_rbac")


@dataclass(frozen=True)
class AdminAuthConfig:
    # ---- 部署参数（环境变量）----
    totp_issuer: str = field(default_factory=lambda: os.getenv("ADMIN_TOTP_ISSUER", get_settings().service_name))
    # ---- 会话（OWASP 会话管理：空闲 15–30 分钟、绝对 4–8 小时；全部服务端强制）----
    session_idle_seconds: int = field(default_factory=_rt("session_idle_seconds"))
    session_absolute_seconds: int = field(default_factory=_rt("session_absolute_seconds"))
    session_touch_seconds: int = field(default_factory=_rt("session_touch_seconds"))
    session_bind_user_agent: bool = field(default_factory=_rt("session_bind_user_agent"))
    max_sessions_per_staff: int = field(default_factory=_rt("max_sessions_per_staff"))
    fresh_auth_seconds: int = field(default_factory=_rt("fresh_auth_seconds"))
    mfa_challenge_seconds: int = field(default_factory=_rt("mfa_challenge_seconds"))
    # ---- 登录节流（按账号；NIST 800-63B §3.2.2 上限 100 次）----
    login_max_failures: int = field(default_factory=_rt("login_max_failures"))
    login_failure_window_seconds: int = field(default_factory=_rt("login_failure_window_seconds"))
    login_lock_seconds: int = field(default_factory=_rt("login_lock_seconds"))
    login_lock_max_seconds: int = field(default_factory=_rt("login_lock_max_seconds"))
    captcha_after_failures: int = field(default_factory=_rt("captcha_after_failures"))
    # ---- 口令（NIST 800-63B-4 §3.1.1.2；OWASP 口令存储）----
    password_min_length_mfa: int = field(default_factory=_rt("password_min_length_mfa"))
    password_min_length_no_mfa: int = field(default_factory=_rt("password_min_length_no_mfa"))
    password_max_length: int = field(default_factory=_rt("password_max_length"))
    password_blocklist_extra: tuple[str, ...] = field(default_factory=_rt("password_blocklist_extra"))
    password_breach_fail_closed: bool = field(default_factory=_rt("password_breach_fail_closed"))
    argon2_memory_kib: int = field(default_factory=_rt("argon2_memory_kib"))
    argon2_time_cost: int = field(default_factory=_rt("argon2_time_cost"))
    argon2_parallelism: int = field(default_factory=_rt("argon2_parallelism"))
    temp_password_ttl_seconds: int = field(default_factory=_rt("temp_password_ttl_seconds"))
    # ---- MFA（RFC 6238）----
    totp_drift_steps: int = field(default_factory=_rt("totp_drift_steps"))
    recovery_code_count: int = field(default_factory=_rt("recovery_code_count"))
    mfa_required_all: bool = field(default_factory=_rt("mfa_required_all"))
    mfa_required_permissions: tuple[str, ...] = field(default_factory=_rt("mfa_required_permissions"))
    # ---- 保留与导出 ----
    login_log_retention_days: int = field(default_factory=_rt("login_log_retention_days"))
    export_max_rows: int = field(default_factory=_rt("export_max_rows"))


_module = ModuleConfig("admin_rbac", AdminAuthConfig)


def get_config() -> AdminAuthConfig:
    return _module.get()


def set_config(config: AdminAuthConfig | None = None, **overrides) -> AdminAuthConfig:
    """替换配置（项目启动或测试用）。set_config(login_max_failures=3) 在当前配置上覆盖单项（运行参数转成测试用运行配置）。"""
    return _module.set(config, **overrides)


def reset_config() -> None:
    _module.reset()


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
