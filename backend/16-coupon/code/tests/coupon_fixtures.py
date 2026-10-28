"""coupon 测试公共夹具（测试文件 `from coupon_fixtures import *` 引入；不用 conftest.py，避免与其他模块同名冲突）：内存库会话、固定时钟、快速建模板、文件 SQLite（并发用）。"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

import coupon
from coupon import service
from luzback import Base
from luzback.testing import memory_session

NOW = datetime(2026, 10, 28, 4, 0, tzinfo=timezone.utc)  # 北京时间 12:00
ALL_CHANNELS = ["claim_center", "admin", "event", "lottery", "compensation", "code"]


def template_data(**overrides):
    data = dict(name="满100减20", type="amount_off", amount_cents=2000, threshold_cents=10000,
                valid_type="relative", valid_days=30, channels=list(ALL_CHANNELS), per_member_limit=1)
    data.update(overrides)
    return data


def make_template(db, publish: bool = True, staff: int = 1, now: datetime = NOW, **overrides):
    t = service.create_template(db, staff, template_data(**overrides), now=now)
    if publish:
        service.publish_template(db, staff, t.id, now=now)
    db.commit()
    return t


@pytest.fixture(autouse=True)
def reset_coupon_hooks():
    coupon.reset_config()
    coupon.set_member_profile_provider(None)
    coupon.set_audience_checker(None)
    coupon.set_cart_lines_provider(None)
    coupon.set_staff_authenticator(None)
    coupon.set_member_resolver(coupon.jwt_member_resolver)
    yield
    coupon.reset_config()
    coupon.set_member_profile_provider(None)
    coupon.set_audience_checker(None)
    coupon.set_cart_lines_provider(None)
    coupon.set_staff_authenticator(None)
    coupon.set_member_resolver(None)


@pytest.fixture()
def db():
    session = memory_session(coupon)
    yield session
    session.close()


@pytest.fixture()
def file_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'coupon.db'}", future=True,
                           connect_args={"check_same_thread": False, "timeout": 30})

    @event.listens_for(engine, "connect")
    def _pragmas(conn, _record):
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    yield factory
    engine.dispose()
