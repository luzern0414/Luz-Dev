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


def discover_routers(settings) -> list[APIRouter]:
    """导入每个模块包的 api 子模块，收集 `router` 或 `routers`。"""
    routers: list[APIRouter] = []
    for code_dir in sorted(BACKEND.glob("*/code")):
        for pkg in sorted(p for p in code_dir.iterdir() if p.is_dir() and (p / "__init__.py").exists() and p.name != "tests"):
            name = pkg.name
            if name == "luzback" or not settings.feature_enabled(name):
                continue
            try:
                api = importlib.import_module(f"{name}.api")
            except ModuleNotFoundError as exc:
                if exc.name and exc.name.startswith(name):
                    continue  # 模块没有 api 子模块
                raise
            found = getattr(api, "routers", None) or ([api.router] if hasattr(api, "router") else [])
            routers.extend(found)
            log.info("mounted %s (%d routers)", name, len(found))
    return routers


def build_rate_rules(settings) -> list[RateLimitRule]:
    ip_bucket = TokenBucketLimiter(settings.rate_ip_per_second, settings.rate_ip_per_second * 2)
    login_window = FixedWindowLimiter(settings.rate_login_per_minute, 60)
    sms_window = FixedWindowLimiter(1, 60)
    lottery_window = FixedWindowLimiter(5, 60)

    def user_key(request):
        auth = request.headers.get("Authorization", "")
        return auth[-32:] if auth.startswith("Bearer ") else None

    return [
        RateLimitRule("ip", ip_bucket, path_prefix="/api/", key_fn=client_ip),
        RateLimitRule("login", login_window, path_prefix="/api/v1/app/auth/login", methods=("POST",), key_fn=client_ip, retry_after=60),
        RateLimitRule("admin_login", login_window, path_prefix="/api/v1/admin/auth/login", methods=("POST",), key_fn=client_ip, retry_after=60),
        RateLimitRule("sms", sms_window, path_prefix="/api/v1/app/auth/sms/send", methods=("POST",), key_fn=client_ip, retry_after=60),
        RateLimitRule("lottery", lottery_window, path_prefix="/api/v1/app/lottery", methods=("POST",), key_fn=user_key, retry_after=60),
        RateLimitRule("user_write", TokenBucketLimiter(settings.rate_user_write_per_second, settings.rate_user_write_per_second * 2),
                      path_prefix="/api/v1/app/", methods=("POST", "PUT", "PATCH", "DELETE"), key_fn=user_key),
    ]


def build() -> "FastAPI":  # noqa: F821
    settings = get_settings()
    settings.assert_production_safe()
    configure_logging(settings.log_level)
    from luzback.db import init_engine

    init_engine(settings.database_url, pool_size=settings.db_pool_size, statement_timeout_ms=settings.db_statement_timeout_ms)
    breakers = BreakerRegistry(window_seconds=settings.breaker_window_seconds, min_failures=settings.breaker_min_failures,
                               fail_rate=settings.breaker_fail_rate, open_seconds=settings.breaker_open_seconds)
    routers = discover_routers(settings)
    application = create_app(routers=routers, settings=settings, rate_rules=build_rate_rules(settings), breakers=breakers,
                             title=settings.service_name)
    return application


app = build()
