"""装配检查：app.py 挂载每个模块的路由且无冲突；限流规则含 identity 声明；`python -m jobs list` 列出全部任务与订阅。

在子进程里导入 app（它在导入时就 build，会替换全局数据库引擎、装配各模块默认适配器），不污染本进程的其他测试。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]

# 每个有 api 的模块至少一条代表性路由（方法, 路径）
EXPECTED_ROUTES = {
    "identity": ("POST", "/api/v1/app/auth/login"),
    "identity-admin": ("GET", "/api/v1/admin/members"),
    "address": ("GET", "/api/v1/app/addresses"),
    "order": ("POST", "/api/v1/app/orders"),
    "wechat_pay": ("POST", "/api/v1/app/orders/{order_id}/pay"),
    "after_sales": ("POST", "/api/v1/app/after-sales"),
    "membership": ("GET", "/api/v1/app/points"),
    "wallet": ("GET", "/api/v1/app/wallet/plans"),
    "coupon": ("GET", "/api/v1/app/coupon-templates"),
    "lottery": ("POST", "/api/v1/app/lottery/activities/{activity_id}/draw"),
    "review": ("POST", "/api/v1/app/orders/{order_id}/review"),
    "notification": ("GET", "/api/v1/app/notifications/subscriptions"),
    "admin_rbac": ("POST", "/api/v1/admin/auth/login"),
    "wechat_mp": ("POST", "/api/v1/webhooks/wechat-mp/push"),
    "member_card": ("GET", "/api/v1/app/member-cards/products"),
    "app_settings": ("GET", "/api/v1/app/config"),
    "app_settings-content": ("GET", "/api/v1/app/content"),
    "app_settings-admin": ("PUT", "/api/v1/admin/content/texts/{key}"),
    "app_settings-internal": ("POST", "/internal/config/reload"),
}

PROBE = r"""
import json, os, sys
sys.path.insert(0, os.getcwd())
import app as appmod
from fastapi.routing import APIRoute
s = appmod.get_settings()
routes = []
for router in appmod.discover_routers(s):  # 与 build() 交给 create_app 的同一批路由
    for r in router.routes:
        if isinstance(r, APIRoute):
            for m in sorted(r.methods - {"HEAD", "OPTIONS"}):
                routes.append([m, r.path, r.endpoint.__module__])
schema_paths = sorted(appmod.app.openapi()["paths"])  # 应用里实际挂上的（文档可见的）路径
rules = [r.name for r in appmod.build_rate_rules(s)]
api_modules = [name for name in appmod.module_packages(s) if appmod._api_module(name) is not None]
from luzback.logging import flush_logging
flush_logging()  # 启动日志先写完，探针输出单独成行
sys.stdout.write(json.dumps({"routes": routes, "schema_paths": schema_paths, "rules": rules, "api_modules": api_modules}) + "\n")
"""


def _run(code_or_args: list[str], tmp_path: Path) -> str:
    env = {**os.environ, "LUZ_ENV": "dev", "LUZ_DATABASE_URL": f"sqlite:///{(tmp_path / 'wiring.db').as_posix()}",
           "LUZ_FEATURES": "", "PYTHONUTF8": "1", "PYTHONPATH": os.pathsep.join(
               [str(BACKEND / "00-framework" / "code"), *map(str, sorted(BACKEND.glob("*/code")))])}
    proc = subprocess.run([sys.executable, "-X", "utf8", *code_or_args], cwd=BACKEND, env=env, capture_output=True,
                          text=True, encoding="utf-8", timeout=180)
    assert proc.returncode == 0, proc.stderr[-4000:]
    return proc.stdout


@pytest.fixture(scope="module")
def wiring(tmp_path_factory):
    out = _run(["-c", PROBE], tmp_path_factory.mktemp("app"))
    # 应用启动日志也写 stdout（JSON 一行一条，经队列异步写出，可能晚于探针输出）：按内容找探针那一行
    for line in reversed(out.strip().splitlines()):
        data = json.loads(line)
        if "routes" in data:
            return data
    raise AssertionError("probe output not found")


def test_every_module_with_api_is_mounted(wiring):
    modules_with_routes = {endpoint.split(".", 1)[0] for _m, _p, endpoint in wiring["routes"]}
    missing = [m for m in wiring["api_modules"] if m not in modules_with_routes]
    assert not missing, f"modules with an api package but no mounted route: {missing}"
    schema = set(wiring["schema_paths"])
    for module in wiring["api_modules"]:  # 每个模块至少有一条路由真的挂进了应用
        mine = {p for _m, p, e in wiring["routes"] if e.split(".", 1)[0] == module}
        assert mine & schema or not mine, f"{module}: routes built but not mounted in app"
    paths = {(m, p) for m, p, _e in wiring["routes"]}
    for name, expected in EXPECTED_ROUTES.items():
        if expected is not None:
            assert tuple(expected) in paths, f"{name}: {expected} not mounted"


def test_no_route_collides_on_method_and_path(wiring):
    seen: dict[tuple[str, str], str] = {}
    dupes = []
    for method, path, endpoint in wiring["routes"]:
        key = (method, path)
        if key in seen:
            dupes.append((method, path, seen[key], endpoint))
        seen[key] = endpoint
    assert not dupes, dupes


def test_rate_rules_include_identity_and_no_stale_paths(wiring):
    rules = wiring["rules"]
    for name in ("ip", "admin_login", "identity_login", "identity_sms_send", "identity_refresh", "lottery_draw", "user_write"):
        assert name in rules, name
    assert "login" not in rules and "sms" not in rules  # 旧的 app.py 自带规则已由 identity 声明取代
    assert len(rules) == len(set(rules))


def _jobs_list(tmp_path: Path) -> dict:
    return json.loads(_run(["-m", "jobs", "list"], tmp_path))


def test_jobs_list_covers_every_module(tmp_path):
    listed = _jobs_list(tmp_path)
    job_names = {j["name"] for j in listed["jobs"]}
    subs = listed["subscriptions"]
    # 每个声明了 jobs.JOBS / events.SUBSCRIPTIONS 的模块都要出现在清单里
    sys.path[:0] = [str(BACKEND / "00-framework" / "code"), *map(str, sorted(BACKEND.glob("*/code")))]
    import importlib

    from jobs.runner import module_packages
    from luzback.config import Settings

    for pkg in module_packages(Settings(features=())):
        for sub, attr in (("jobs", "JOBS"), ("events", "SUBSCRIPTIONS")):
            spec = importlib.util.find_spec(f"{pkg}.{sub}")
            if spec is None:
                continue
            declared = getattr(importlib.import_module(f"{pkg}.{sub}"), attr, None)
            if not declared:
                continue
            if attr == "JOBS":
                names = {getattr(j, "name", None) for j in declared}
                assert names <= job_names, (pkg, names - job_names)
            else:
                for topic in declared:
                    assert topic in subs and any(n.startswith(pkg) for n in subs[topic]), (pkg, topic, subs.get(topic))
    assert len(job_names) == len(listed["jobs"]), "duplicate job names"
    for expected_topic in ("payment.succeeded", "refund.succeeded", "lottery.prize_won", "order.cancelled"):
        assert expected_topic in subs, expected_topic
    assert "payment.refunded" not in subs  # 旧主题名：退款结果统一为 refund.succeeded / refund.failed / refund.abnormal


def test_runtime_config_module_is_always_on():
    """运行配置接口与加载记录（app_settings）不受 LUZ_FEATURES 影响：只启用 identity 时也装配。"""
    sys.path[:0] = [str(BACKEND / "00-framework" / "code"), *map(str, sorted(BACKEND.glob("*/code")))]
    from jobs.runner import module_packages
    from luzback.config import Settings

    packages = module_packages(Settings(features=("identity",)))
    assert "app_settings" in packages and "identity" in packages
    assert "order" not in packages
