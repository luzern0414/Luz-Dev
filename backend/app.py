"""后端应用入口：动态装配所有编号模块的路由，配置限流规则、熔断注册表与就绪检查。

运行（开发）：
    cd backend && uvicorn app:app --reload
PYTHONPATH 需包含 00-framework/code 与每个 NN-name/code（Dockerfile 与 conftest.py 已设置；本文件启动时也会自动补齐）。

模块开关：环境变量 LUZ_FEATURES=identity,store,catalog,... 为空表示全部启用；未启用的模块不导入、不建表、不暴露接口。
"""
from __future__ import annotations

import importlib
import logging
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
for code_dir in sorted(BACKEND.glob("*/code")):
    if str(code_dir) not in sys.path:
        sys.path.insert(0, str(code_dir))

from fastapi import APIRouter  # noqa: E402

from luzback import create_app, get_settings  # noqa: E402
from luzback.app import client_ip  # noqa: E402
from luzback.logging import configure_logging  # noqa: E402
from luzback.ratelimit import FixedWindowLimiter, RateLimitRule, TokenBucketLimiter  # noqa: E402
from luzback.resilience import BreakerRegistry  # noqa: E402

log = logging.getLogger("luz.app")


def module_packages(settings) -> list[str]:
    """已启用的模块包名（按目录序）；luzback 与 tests 除外。"""
    names: list[str] = []
    for code_dir in sorted(BACKEND.glob("*/code")):
        for pkg in sorted(p for p in code_dir.iterdir() if p.is_dir() and (p / "__init__.py").exists() and p.name != "tests"):
            if pkg.name != "luzback" and settings.feature_enabled(pkg.name):
                names.append(pkg.name)
    return names


def _api_module(name: str):
    importlib.import_module(name)  # 先导入包本身：没有 api 的模块（如 legal）也要完成导入时装配（钩子、权限码、生产检查）
    try:
        return importlib.import_module(f"{name}.api")
    except ModuleNotFoundError as exc:
        if exc.name and (exc.name == f"{name}.api" or exc.name == name):
            return None  # 模块没有 api 子模块
        raise


def discover_routers(settings) -> list[APIRouter]:
    """导入每个模块包的 api 子模块，收集 `routers`（列表）或 `router`。"""
    routers: list[APIRouter] = []
    for name in module_packages(settings):
        api = _api_module(name)
        if api is None:
            continue
        found = getattr(api, "routers", None) or ([api.router] if hasattr(api, "router") else [])
        routers.extend(found)
        log.info("mounted %s (%d routers)", name, len(found))
    return routers


def module_rate_rules(settings) -> list[RateLimitRule]:
    """模块自带的限流声明：`<pkg>.api.rate_limit_rules(settings) -> list[RateLimitRule]`（例如 identity 的登录 / 短信 / 刷新）。"""
    rules: list[RateLimitRule] = []
    for name in module_packages(settings):
        api = _api_module(name)
        fn = getattr(api, "rate_limit_rules", None) if api is not None else None
        if callable(fn):
            rules.extend(fn(settings))
    return rules


def build_rate_rules(settings) -> list[RateLimitRule]:
    """全局规则（IP 令牌桶、后台登录、抽奖、写操作）+ 模块声明的规则。会员登录 / 短信 / 刷新由 identity 声明。"""
    ip_bucket = TokenBucketLimiter(settings.rate_ip_per_second, settings.rate_ip_per_second * 2)

    def user_key(request):
        auth = request.headers.get("Authorization", "")
        return auth[-32:] if auth.startswith("Bearer ") else None

    return [
        RateLimitRule("ip", ip_bucket, path_prefix="/api/", key_fn=client_ip),
        # 后台：按 IP 限登录与第二因素（按账号的失败锁定在 admin_rbac 内用数据库计数）
        RateLimitRule("admin_login", FixedWindowLimiter(settings.rate_login_per_minute, 60), path_prefix="/api/v1/admin/auth/login",
                      methods=("POST",), key_fn=client_ip, retry_after=60),
        RateLimitRule("admin_mfa", FixedWindowLimiter(10, 60), path_prefix="/api/v1/admin/auth/mfa", methods=("POST",),
                      key_fn=client_ip, retry_after=60),
        RateLimitRule("admin_reauth", FixedWindowLimiter(10, 60), path_prefix="/api/v1/admin/auth/reauth", methods=("POST",),
                      key_fn=client_ip, retry_after=60),
        *module_rate_rules(settings),
        # 抽奖：抽奖与领奖按会员令牌限频（每日次数上限在 lottery 内用数据库计数）
        RateLimitRule("lottery_draw", FixedWindowLimiter(10, 60), path_prefix="/api/v1/app/lottery/activities", methods=("POST",),
                      key_fn=user_key, retry_after=60),
        RateLimitRule("lottery_claim", FixedWindowLimiter(5, 60), path_prefix="/api/v1/app/lottery/records", methods=("POST",),
                      key_fn=user_key, retry_after=60),
        RateLimitRule("user_write", TokenBucketLimiter(settings.rate_user_write_per_second, settings.rate_user_write_per_second * 2),
                      path_prefix="/api/v1/app/", methods=("POST", "PUT", "PATCH", "DELETE"), key_fn=user_key),
    ]


def build() -> "FastAPI":  # noqa: F821
    settings = get_settings()
    configure_logging(settings.log_level)
    from luzback.db import init_engine

    init_engine(settings.database_url, pool_size=settings.db_pool_size, statement_timeout_ms=settings.db_statement_timeout_ms)
    breakers = BreakerRegistry(window_seconds=settings.breaker_window_seconds, min_failures=settings.breaker_min_failures,
                               fail_rate=settings.breaker_fail_rate, open_seconds=settings.breaker_open_seconds)
    routers = discover_routers(settings)  # 导入模块（模块在导入时登记权限码与生产配置检查）
    settings.assert_production_safe()  # 框架通用项 + 各模块 register_production_check 登记的检查
    application = create_app(routers=routers, settings=settings, rate_rules=build_rate_rules(settings), breakers=breakers,
                             title=settings.service_name)
    return application


app = build()
