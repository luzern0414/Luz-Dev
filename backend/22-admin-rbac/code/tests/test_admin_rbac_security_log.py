"""安全事件与请求上下文：登录失败写 security.authn_login_fail、锁定写 security.authn_login_lock、越权写 security.authz_fail
（只记员工内部编号，不记用户名与口令）；员工请求的 http.request 汇总行带 user_ref（staff:<id>）与单门店员工的 store_id。
"""
from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from admin_rbac import api, config
from admin_rbac.models import AdminRole
from luzback import log_events as ev
from luzback.db import get_session_factory
from luzback.testing import make_test_app

from admin_rbac_testkit import PASSWORD, make_user, reset_hooks


@pytest.fixture()
def env():
    reset_hooks()
    app = make_test_app([api.router], "admin_rbac.models", "luzback.audit", "luzback.outbox", "luzback.idempotency")
    db = get_session_factory()()
    from admin_rbac import ensure_seed

    ensure_seed(db)
    db.execute(update(AdminRole).where(AdminRole.builtin_code == "super_admin").values(mfa_required=False))
    db.commit()
    config.set_config(mfa_required_permissions=(), login_max_failures=3)
    boss = make_user(db, "boss")
    clerk = make_user(db, "clerk", roles=("front_desk",), all_stores=False, store_ids=(7,))
    yield TestClient(app), db, boss, clerk
    db.close()
    reset_hooks()
    config.reset_config()


def _events(caplog, name: str) -> list[dict]:
    return [getattr(r, "fields", {}) for r in caplog.records if getattr(r, "fields", {}).get("event") == name]


def _login(client, username: str, password: str):
    return client.post("/api/v1/admin/auth/login", json={"username": username, "password": password})


def test_failed_logins_and_lock_are_security_events_without_secrets(env, caplog):
    client, _db, boss, _clerk = env
    caplog.set_level(logging.INFO)
    assert _login(client, "nobody", "guess-123456789").status_code == 401
    for _ in range(3):
        _login(client, "boss", "wrong-password-123")
    fails = _events(caplog, ev.SECURITY_AUTHN_LOGIN_FAIL)
    locks = _events(caplog, ev.SECURITY_AUTHN_LOGIN_LOCK)
    assert [f["reason"] for f in fails] == ["unknown_user", "bad_password", "bad_password"]
    assert fails[0]["staff_ref"] is None and fails[1]["staff_ref"] == f"staff:{boss.id}"
    assert len(locks) == 1 and locks[0]["staff_ref"] == f"staff:{boss.id}"  # 第 3 次失败触发锁定
    text = " ".join(repr(r.__dict__) for r in caplog.records)
    assert "wrong-password-123" not in text and "guess-123456789" not in text and "nobody" not in text


def test_permission_and_scope_denials_are_authz_fail(env, caplog):
    client, _db, _boss, clerk = env
    caplog.set_level(logging.INFO)
    token = _login(client, "clerk", PASSWORD).json()["data"]["token"]
    denied = client.get("/api/v1/admin/roles", headers={"Authorization": f"Bearer {token}"})
    assert denied.status_code == 403
    [event] = _events(caplog, ev.SECURITY_AUTHZ_FAIL)
    assert event["reason"] == "permission_missing" and event["staff_ref"] == f"staff:{clerk.id}" and event["required"]
    summary = _events(caplog, ev.HTTP_REQUEST)[-1]
    assert summary["user_ref"] == f"staff:{clerk.id}" and summary["store_id"] == 7  # 单门店员工带门店


def test_staff_request_summary_carries_user_ref(env, caplog):
    client, _db, boss, _clerk = env
    caplog.set_level(logging.INFO)
    token = _login(client, "boss", PASSWORD).json()["data"]["token"]
    assert client.get("/api/v1/admin/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    summary = _events(caplog, ev.HTTP_REQUEST)[-1]
    assert summary["user_ref"] == f"staff:{boss.id}" and summary.get("store_id") is None  # 全门店员工不带门店
    assert summary["outcome"] == "success" and summary["http.route"] == "/api/v1/admin/auth/me"
    assert _events(caplog, ev.SECURITY_AUTHN_LOGIN_FAIL) == []
