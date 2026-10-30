"""测试工具：内存数据库会话、可控时钟、测试应用、测试用运行配置与运行参数覆盖。"""
from __future__ import annotations

import copy
import importlib
import json
import shutil
import sys
import tempfile
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI
from sqlalchemy.orm import Session

from .app import create_app
from .config import Settings, get_settings
from .db import Base, get_session_factory, init_engine
from .runtime_loader import DEFAULT_DIR, ConfigStore, set_config_store


def _import_all(modules: tuple) -> None:
    for item in modules:
        if isinstance(item, str):
            importlib.import_module(item)


def memory_session(*modules, url: str = "sqlite://") -> Session:
    """导入模块（包名字符串或已导入的模块对象，用于注册 ORM 模型）后建表并返回会话。"""
    _import_all(modules)
    engine = init_engine(url)
    Base.metadata.create_all(engine)
    return get_session_factory()()


def make_test_app(routers: Iterable[APIRouter], *modules, url: str = "sqlite://", settings: Settings | None = None,
                  rate_rules: Iterable = (), breakers=None, config: ConfigStore | None = None) -> FastAPI:
    """测试应用：建表 + create_app（默认无限流规则）。

    指标：每个应用实例有自己的 `app.state.metrics`（进程内计数，新应用从 0 开始）；测试直接读
    `client.app.state.metrics.counters[...]`。/metrics 端点只对内网地址或 LUZ_METRICS_TOKEN 开放，
    TestClient 的对端是 "testclient"（不是 IP）会得到 404——要测端点请传 settings=Settings(metrics_token=...) 并带 Bearer。
    config：测试用运行配置（make_config_store）；不传时用进程内的 config_store()（仓库 config/runtime 的 dev 配置）。
    """
    _import_all(modules)
    engine = init_engine(url)
    Base.metadata.create_all(engine)
    return create_app(routers=routers, settings=settings or Settings(), rate_rules=rate_rules, breakers=breakers,
                      config=config)


class FakeClock:
    """可推进的时钟，用于限流、熔断、过期测试。"""

    def __init__(self, start: float | None = None):
        self.now = start if start is not None else time.monotonic()

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def dump_toml(data: dict[str, Any]) -> str:
    """把简单的嵌套字典写成 TOML（测试造配置文件用；值只支持字符串、数字、布尔、数组）。"""
    lines: list[str] = []

    def value(v: Any) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return repr(v)
        if isinstance(v, (list, tuple)):
            return "[" + ", ".join(value(x) for x in v) + "]"
        return json.dumps(str(v), ensure_ascii=False)

    def table(prefix: str, body: dict[str, Any]) -> None:
        scalars = {k: v for k, v in body.items() if not isinstance(v, dict)}
        if prefix:
            lines.append(f"[{prefix}]")
        for k, v in scalars.items():
            lines.append(f"{json.dumps(k) if '.' in k else k} = {value(v)}")
        for k, v in body.items():
            if isinstance(v, dict):
                table(f"{prefix}.{k}" if prefix else k, v)

    table("", data)
    return "\n".join(lines) + "\n"


def make_config_store(directory: str | Path | None = None, env: str = "dev", values: dict[str, Any] | None = None,
                      load: bool = True) -> ConfigStore:
    """测试用运行配置：复制仓库的 base.toml，环境文件写 values（如 {"http": {"max_body_kb": 1}}），加载后返回。"""
    target = Path(directory or tempfile.mkdtemp(prefix="luz-config-"))
    target.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(DEFAULT_DIR / "base.toml", target / "base.toml")
    (target / f"{env}.toml").write_text(dump_toml(values or {}), encoding="utf-8")
    store = ConfigStore(target, env, instance="test-instance")
    if load:
        store.load()
    return store


# ---------- 运行参数的测试覆盖（运行参数只有运行配置一个来源：测试也通过一份测试配置注入）----------

_overrides: dict[str, dict[str, Any]] = {}
_saved: list[ConfigStore | None] = []
_override_dir: Path | None = None
_override_lock = threading.Lock()


def _dir() -> Path:
    global _override_dir
    if _override_dir is None:
        _override_dir = Path(tempfile.mkdtemp(prefix="luz-runtime-override-"))
    return _override_dir


def override_runtime(values: dict[str, dict[str, Any]] | None = None, **groups: dict[str, Any]) -> ConfigStore:
    """测试：把运行参数写进一份测试配置（make_config_store）并设为进程内配置；同一测试里多次调用会累积。

        override_runtime(order={"pay_timeout_minutes": 5}, coupon={"max_coupons_per_order": 1})

    值不合法时抛 ConfigInvalid（ValueError 的子类），原来的配置不变。backend/conftest.py 在每个测试结束后撤销。
    生产环境不能用（运行参数只改 config/runtime/*.toml）。
    """
    if get_settings().is_prod and "pytest" not in sys.modules:
        raise RuntimeError("override_runtime is for tests only; change config/runtime/*.toml instead")
    with _override_lock:
        merged = copy.deepcopy(_overrides)
        for group, items in {**(values or {}), **groups}.items():
            merged.setdefault(group, {}).update(items)
        store = make_config_store(_dir(), values=merged)  # 不合法时在这里抛出，进程内配置不变
        previous = set_config_store(store)
        if not _saved:
            _saved.append(previous)
        _overrides.clear()
        _overrides.update(merged)
        return store


def reset_runtime_overrides(*groups: str) -> None:
    """撤销测试覆盖：给了分组只撤销这些分组，否则全部撤销并还原原来的进程内配置。"""
    with _override_lock:
        if groups:
            for group in groups:
                _overrides.pop(group, None)
        else:
            _overrides.clear()
        if not _saved:
            return
        if _overrides:
            set_config_store(make_config_store(_dir(), values=copy.deepcopy(_overrides)))
        else:
            set_config_store(_saved.pop())


def override_business(**values: Any) -> None:
    """测试：覆盖经营设置（键为 "模块.名称"，用 dict 解包传入），值按登记的类型与范围校验；backend/conftest.py 每个测试结束后撤销。

        override_business(**{"coupon.return_policy": "never"})
    """
    if get_settings().is_prod and "pytest" not in sys.modules:
        raise RuntimeError("override_business is for tests only; change business settings in the admin console")
    from . import business  # noqa: PLC0415

    business._set_overrides(values)


def reset_business_overrides() -> None:
    from . import business  # noqa: PLC0415

    business._clear_overrides()
