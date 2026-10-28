"""测试工具：内存数据库会话、可控时钟、测试应用。"""
from __future__ import annotations

import importlib
import time
from collections.abc import Iterable

from fastapi import APIRouter, FastAPI
from sqlalchemy.orm import Session

from .app import create_app
from .config import Settings
from .db import Base, get_session_factory, init_engine


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
                  rate_rules: Iterable = (), breakers=None) -> FastAPI:
    """测试应用：建表 + create_app（默认无限流规则）。

    指标：每个应用实例有自己的 `app.state.metrics`（进程内计数，新应用从 0 开始）；测试直接读
    `client.app.state.metrics.counters[...]`。/metrics 端点只对内网地址或 LUZ_METRICS_TOKEN 开放，
    TestClient 的对端是 "testclient"（不是 IP）会得到 404——要测端点请传 settings=Settings(metrics_token=...) 并带 Bearer。
    """
    _import_all(modules)
    engine = init_engine(url)
    Base.metadata.create_all(engine)
    return create_app(routers=routers, settings=settings or Settings(), rate_rules=rate_rules, breakers=breakers)


class FakeClock:
    """可推进的时钟，用于限流、熔断、过期测试。"""

    def __init__(self, start: float | None = None):
        self.now = start if start is not None else time.monotonic()

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds
