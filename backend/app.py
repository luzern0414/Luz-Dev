"""后端应用入口：先加载运行配置，再动态装配所有编号模块的路由，配置限流规则、熔断注册表、配置目录监听与就绪检查。

运行（开发）：
    cd backend && uvicorn app:app --reload
PYTHONPATH 需包含 00-framework/code 与每个 NN-name/code（Dockerfile 与 conftest.py 已设置；本文件启动时也会自动补齐）。

启动顺序：日志 → 运行配置（config/runtime/base.toml + <LUZ_ENV>.toml，校验失败逐条打印后退出）→ 数据库 → 模块 → 生产检查。
模块开关：环境变量 LUZ_FEATURES=identity,store,catalog,... 为空表示全部启用；未启用的模块不导入、不建表、不暴露接口。
运行配置与文案（app_settings）是基础设施，总是装配。
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
from luzback import log_events as ev  # noqa: E402
from luzback.app import client_ip  # noqa: E402
from luzback.logging import log_event  # noqa: E402
from luzback.ratelimit import RateLimitRule, fixed_window, token_bucket  # noqa: E402
from luzback.resilience import BreakerRegistry  # noqa: E402
from luzback.runtime_loader import start_process, watcher_for  # noqa: E402

log = logging.getLogger("luz.app")

ALWAYS_ON = frozenset({"app_settings"})
UNLIMITED = 1_000_000_000  # 配置为 0 表示不限  # 运行配置接口、加载记录、文案与展示：不受 LUZ_FEATURES 影响


def module_packages(settings) -> list[str]:
    """已启用的模块包名（按目录序）；luzback 与 tests 除外。"""
    names: list[str] = []
    for code_dir in sorted(BACKEND.glob("*/code")):
        for pkg in sorted(p for p in code_dir.iterdir() if p.is_dir() and (p / "__init__.py").exists() and p.name != "tests"):
            if pkg.name != "luzback" and (settings.feature_enabled(pkg.name) or pkg.name in ALWAYS_ON):
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
        log_event(log, ev.APP_MODULE_MOUNTED, logging.DEBUG, module=name, routers=len(found))
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
    """全局规则（IP 令牌桶、后台登录、抽奖、写操作）+ 模块声明的规则。会员登录 / 短信 / 刷新由 identity 声明。

    阈值取运行配置 [rate_limit]（每个请求读当前快照，热加载后立即生效）。
    """
    def user_key(request):
        auth = request.headers.get("Authorization", "")
        return auth[-32:] if auth.startswith("Bearer ") else None

    return [
        RateLimitRule("ip", token_bucket(lambda cfg: cfg.rate_limit.ip_per_s), path_prefix="/api/", key_fn=client_ip),
        # 后台：按 IP 限登录与第二因素（按账号的失败锁定在 admin_rbac 内用数据库计数）
        RateLimitRule("admin_login", fixed_window(lambda cfg: cfg.rate_limit.login_per_min), path_prefix="/api/v1/admin/auth/login",
                      methods=("POST",), key_fn=client_ip, retry_after=60),
        RateLimitRule("admin_mfa", fixed_window(lambda cfg: cfg.rate_limit.admin_mfa_per_min), path_prefix="/api/v1/admin/auth/mfa",
                      methods=("POST",), key_fn=client_ip, retry_after=60),
        RateLimitRule("admin_reauth", fixed_window(lambda cfg: cfg.rate_limit.admin_reauth_per_min),
                      path_prefix="/api/v1/admin/auth/reauth", methods=("POST",), key_fn=client_ip, retry_after=60),
        *module_rate_rules(settings),
        # 抽奖：抽奖与领奖按会员令牌限频（抽奖与 lottery 服务内的限流共用 [lottery] member_draws_per_minute；每日上限在活动里）
        RateLimitRule("lottery_draw", fixed_window(lambda cfg: cfg.lottery.member_draws_per_minute or UNLIMITED),
                      path_prefix="/api/v1/app/lottery/activities", methods=("POST",), key_fn=user_key, retry_after=60),
        RateLimitRule("lottery_claim", fixed_window(lambda cfg: cfg.rate_limit.lottery_claim_per_min),
                      path_prefix="/api/v1/app/lottery/records", methods=("POST",), key_fn=user_key, retry_after=60),
        RateLimitRule("user_write", token_bucket(lambda cfg: cfg.rate_limit.user_write_per_s),
                      path_prefix="/api/v1/app/", methods=("POST", "PUT", "PATCH", "DELETE"), key_fn=user_key),
    ]


def build() -> "FastAPI":  # noqa: F821
    settings = get_settings()
    store = start_process(settings)  # 日志 + 运行配置：校验失败逐条打印并退出，不会带着坏配置就绪
    cfg = store.current().config
    from luzback.db import init_engine

    init_engine(settings.database_url, pool_size=settings.db_pool_size, statement_timeout_ms=cfg.db.statement_timeout_ms)
    breakers = BreakerRegistry(window_seconds=cfg.breaker.window_s, min_failures=cfg.breaker.min_failures,
                               fail_rate=cfg.breaker.fail_rate, open_seconds=cfg.breaker.open_s)
    routers = discover_routers(settings)  # 导入模块（模块在导入时登记权限码与生产配置检查）
    import app_settings  # noqa: PLC0415 - 运行配置加载记录（config_loads），补写启动时的那次加载

    app_settings.install(store)
    settings.assert_production_safe()  # 框架通用项 + 各模块 register_production_check 登记的检查
    application = create_app(routers=routers, settings=settings, rate_rules=build_rate_rules(settings), breakers=breakers,
                             title=settings.service_name, services=[watcher_for(store, settings)], config=store)
    log_event(log, ev.APP_STARTED, outcome="success", modules=len(module_packages(settings)))
    return application


app = build()
