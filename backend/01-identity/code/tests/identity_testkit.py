"""identity 测试共用夹具与工具（文件名唯一，避免与其他模块的测试模块重名）。"""
from __future__ import annotations

import base64
import json
from datetime import timedelta

import pytest
from sqlalchemy import select

import identity
import wechat_mp
from identity import config as identity_config
from identity import ports
from luzback import AppError, ErrorCode, OutboxEvent, reload_settings, utcnow
from luzback.audit import AuditLog
from luzback.testing import memory_session
from wechat_mp import FakeMiniProgram

FIELD_KEY = base64.b64encode(b"k" * 32).decode()
MODULES = (identity, wechat_mp, "luzback.audit", "luzback.outbox", "luzback.idempotency")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("LUZ_FIELD_KEY", FIELD_KEY)
    reload_settings()
    identity_config.reload_config()
    ports.breakers.get("identity.sms").reset()
    yield
    for name in list(ports._checkers):
        ports.unregister_deletion_checker(name)
    monkeypatch.delenv("LUZ_FIELD_KEY", raising=False)
    reload_settings()
    identity_config.reload_config()


@pytest.fixture
def db():
    session = memory_session(*MODULES)
    yield session
    session.close()


@pytest.fixture
def mp():
    return FakeMiniProgram()


def set_config(monkeypatch, **values):
    for key, value in values.items():
        monkeypatch.setenv(key, str(value))
    identity_config.reload_config()


def login(db, mp, code="c1", openid="o-1", **kw):
    mp.add_login_code(code, openid, session_key=kw.pop("session_key", None))
    return identity.login_with_code(db, code, mp, **kw)


def events(db, topic):
    return [json.loads(e.payload_json) for e in db.execute(select(OutboxEvent).where(OutboxEvent.topic == topic)).scalars()]


def audits(db, action):
    return db.execute(select(AuditLog).where(AuditLog.action == action)).scalars().all()


def raises(expected, fn, /, *args, **kwargs) -> AppError:
    with pytest.raises(AppError) as exc:
        fn(*args, **kwargs)
    assert exc.value.code == expected, (exc.value.code, exc.value.message)
    return exc.value


def fresh_claims(tokens):
    return identity.decode_access(tokens["access_token"])


def stale_claims(tokens, minutes=30):
    claims = dict(fresh_claims(tokens))
    claims["iat"] = (utcnow() - timedelta(minutes=minutes)).timestamp()
    return claims


__all__ = ["FIELD_KEY", "MODULES", "_env", "db", "mp", "set_config", "login", "events", "audits", "raises",
           "fresh_claims", "stale_claims", "ErrorCode", "AppError"]
