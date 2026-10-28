"""HTTP 层：路由、信封、4010 / 4011 / 4030 / 4001、幂等重放、临时口令不进幂等记录、审计列表与导出、定时任务。"""
from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from admin_rbac import api, config, jobs
from admin_rbac.models import AdminLoginLog, AdminRole, AdminSession, AdminUser
from luzback.db import get_session_factory
from luzback.idempotency import IdempotencyRecord
from luzback.testing import make_test_app

from admin_rbac_testkit import PASSWORD, code_at, make_user, reset_hooks, role_id


@pytest.fixture()
def env():
    clock = reset_hooks()
    app = make_test_app([api.router], "admin_rbac.models", "luzback.audit", "luzback.outbox", "luzback.idempotency")
    db = get_session_factory()()
    from admin_rbac import ensure_seed

    ensure_seed(db)
    # HTTP 用例里的超级管理员不强制 MFA（MFA 强制另有用例覆盖）
    db.execute(update(AdminRole).where(AdminRole.builtin_code == "super_admin").values(mfa_required=False))
    db.commit()
    config.set_config(mfa_required_permissions=())
    make_user(db, "boss")
    make_user(db, "clerk", roles=("front_desk",), all_stores=False, store_ids=(1,))
    client = TestClient(app)
    yield client, db, clock
    db.close()
    reset_hooks()


def auth_header(client, username: str, password: str = PASSWORD) -> dict:
    res = client.post("/api/v1/admin/auth/login", json={"username": username, "password": password})
    assert res.status_code == 200, res.text
    return {"Authorization": f"Bearer {res.json()['data']['token']}"}


def key(n: str) -> dict:
    return {"Idempotency-Key": f"test-key-{n}-0001"}


def test_login_envelope_and_me(env):
    client, _, _ = env
    res = client.post("/api/v1/admin/auth/login", json={"username": "boss", "password": PASSWORD})
    body = res.json()
    assert res.status_code == 200 and body["code"] == 0 and body["message"] == "ok"
    me = client.get("/api/v1/admin/auth/me", headers={"Authorization": f"Bearer {body['data']['token']}"}).json()["data"]
    assert me["username"] == "boss" and "*" in me["permissions"]


def test_login_failure_generic_and_validation(env):
    client, _, _ = env
    res = client.post("/api/v1/admin/auth/login", json={"username": "boss", "password": "nope-nope"})
    assert res.status_code == 401 and res.json()["code"] == 4010
    res2 = client.post("/api/v1/admin/auth/login", json={"username": "who", "password": "nope-nope"})
    assert res2.json() == res.json()
    bad = client.post("/api/v1/admin/auth/login", json={"username": ""})
    assert bad.status_code == 400 and bad.json()["code"] == 4001


def test_lockout_http_retry_after(env):
    client, _, _ = env
    for _ in range(4):
        client.post("/api/v1/admin/auth/login", json={"username": "boss", "password": "nope-nope"})
    res = client.post("/api/v1/admin/auth/login", json={"username": "boss", "password": "nope-nope"})
    assert res.status_code == 429 and res.json()["code"] == 4290 and res.headers["Retry-After"] == "900"


def test_unauthenticated_and_bad_token(env):
    client, _, _ = env
    assert client.get("/api/v1/admin/staff").json()["code"] == 4010
    res = client.get("/api/v1/admin/staff", headers={"Authorization": "Bearer not-a-real-token"})
    assert res.status_code == 401 and res.json()["code"] == 4010


def test_permission_denied_4030(env):
    client, _, _ = env
    h = auth_header(client, "clerk")
    for method, path in (("get", "/api/v1/admin/staff"), ("get", "/api/v1/admin/roles"), ("get", "/api/v1/admin/audit-logs")):
        res = getattr(client, method)(path, headers=h)
        assert res.status_code == 403 and res.json()["code"] == 4030
    res = client.post("/api/v1/admin/staff", headers={**h, **key("deny")}, json={"username": "abc"})
    assert res.json()["code"] == 4030


def test_restricted_session_4011(env):
    client, db, _ = env
    make_user(db, "fin", roles=("finance",))  # 财务要求 MFA，未绑定
    h = auth_header(client, "fin")
    res = client.get("/api/v1/admin/audit-logs", headers=h)
    assert res.status_code == 401 and res.json()["code"] == 4011
    assert res.json()["data"]["reason"] == "mfa_enrollment_required"
    assert client.get("/api/v1/admin/auth/me", headers=h).json()["data"]["restricted_reason"] == "mfa_enrollment_required"


def test_mfa_enrollment_and_login_over_http(env):
    client, db, clock = env
    make_user(db, "fin", roles=("finance",))
    h = auth_header(client, "fin")
    enrolled = client.post("/api/v1/admin/auth/mfa/enroll", headers=h).json()["data"]
    assert enrolled["otpauth_uri"].startswith("otpauth://totp/")
    confirmed = client.post("/api/v1/admin/auth/mfa/confirm", headers=h,
                            json={"code": code_at(enrolled["secret"], clock.ts)}).json()["data"]
    assert len(confirmed["recovery_codes"]) == 10
    h2 = {"Authorization": f"Bearer {confirmed['token']}"}
    assert client.get("/api/v1/admin/audit-logs", headers=h2).json()["code"] == 0
    clock.advance(30)
    step1 = client.post("/api/v1/admin/auth/login", json={"username": "fin", "password": PASSWORD}).json()["data"]
    assert step1["mfa_required"] is True
    bad = client.post("/api/v1/admin/auth/mfa", json={"mfa_token": step1["mfa_token"], "code": "000000"})
    assert bad.json()["code"] == 4010
    good = client.post("/api/v1/admin/auth/mfa", json={"mfa_token": step1["mfa_token"],
                                                       "code": code_at(enrolled["secret"], clock.ts)})
    assert good.json()["code"] == 0 and good.json()["data"]["token"]


def test_fresh_auth_required_then_reauth(env):
    client, _, clock = env
    h = auth_header(client, "boss")
    clock.advance(301)
    body = {"username": "newbie", "name": "新人", "role_ids": [], "all_stores": True}
    res = client.post("/api/v1/admin/staff", headers={**h, **key("fresh")}, json=body)
    assert res.status_code == 401 and res.json()["code"] == 4011 and res.json()["data"]["reason"] == "reauth_required"
    re = client.post("/api/v1/admin/auth/reauth", headers=h, json={"password": PASSWORD}).json()["data"]
    h = {"Authorization": f"Bearer {re['token']}"}
    assert client.post("/api/v1/admin/staff", headers={**h, **key("fresh")}, json=body).json()["code"] == 0


def test_create_staff_idempotent_and_secret_not_stored(env):
    client, db, _ = env
    h = auth_header(client, "boss")
    body = {"username": "newbie", "name": "新人", "phone": "13900139000", "role_ids": [role_id(db, "front_desk")],
            "store_ids": [1]}
    first = client.post("/api/v1/admin/staff", headers={**h, **key("create")}, json=body).json()
    assert first["code"] == 0 and first["data"]["temporary_password"] and first["data"]["phone"] == "139****9000"
    second = client.post("/api/v1/admin/staff", headers={**h, **key("create")}, json=body).json()
    assert second["data"]["id"] == first["data"]["id"] and second["data"]["temporary_password"] is None
    assert second["data"]["replayed"] is True
    assert db.execute(select(func.count()).select_from(AdminUser).where(AdminUser.username == "newbie")).scalar_one() == 1
    stored = db.execute(select(IdempotencyRecord.response_json).where(IdempotencyRecord.scope == "staff.create")).scalar_one()
    assert first["data"]["temporary_password"] not in stored
    mismatch = client.post("/api/v1/admin/staff", headers={**h, **key("create")}, json={**body, "name": "别人"})
    assert mismatch.json()["code"] == 4093
    missing = client.post("/api/v1/admin/staff", headers=h, json=body)
    assert missing.status_code == 400 and missing.json()["code"] == 4001


def test_staff_endpoints(env):
    client, db, _ = env
    h = auth_header(client, "boss")
    clerk_id = db.execute(select(AdminUser.id).where(AdminUser.username == "clerk")).scalar_one()
    listing = client.get("/api/v1/admin/staff", headers=h, params={"status": "active", "sort": "-created_at"}).json()["data"]
    assert listing["total"] == 2 and listing["page"] == 1
    assert client.get("/api/v1/admin/staff", headers=h, params={"nope": "1"}).json()["code"] == 4001
    assert client.get(f"/api/v1/admin/staff/{clerk_id}", headers=h).json()["data"]["username"] == "clerk"
    assert client.get("/api/v1/admin/staff/999999", headers=h).json()["code"] == 4040
    upd = client.put(f"/api/v1/admin/staff/{clerk_id}", headers={**h, **key("upd")}, json={"name": "张三"}).json()
    assert upd["data"]["name"] == "张三"
    access = client.put(f"/api/v1/admin/staff/{clerk_id}/access", headers={**h, **key("acc")},
                        json={"role_ids": [role_id(db, "operator")], "store_ids": [1, 2]}).json()
    assert access["data"]["store_ids"] == [1, 2]
    for action in ("disable", "enable", "unlock", "sessions/revoke"):
        res = client.post(f"/api/v1/admin/staff/{clerk_id}/{action}", headers={**h, **key(action.replace('/', '-'))},
                          json={"reason": "测试"})
        assert res.json()["code"] == 0, (action, res.text)
    reset = client.post(f"/api/v1/admin/staff/{clerk_id}/reset-password", headers={**h, **key("rp")}).json()
    assert reset["data"]["temporary_password"]
    no_reason = client.post(f"/api/v1/admin/staff/{clerk_id}/reset-mfa", headers={**h, **key("rm")}, json={})
    assert no_reason.json()["code"] == 4001
    assert client.get(f"/api/v1/admin/staff/{clerk_id}/sessions", headers=h).json()["data"]["items"] == []
    reveal = client.post(f"/api/v1/admin/staff/{clerk_id}/phone/reveal", headers=h, json={"reason": "核对"}).json()
    assert reveal["code"] == 0
    export = client.get("/api/v1/admin/staff/export", headers=h)
    assert export.status_code == 200 and export.headers["content-type"].startswith("text/csv")
    deleted = client.delete(f"/api/v1/admin/staff/{clerk_id}", headers={**h, **key("del")}, params={"reason": "离职"}).json()
    assert deleted["data"]["deleted"] is True


def test_role_endpoints(env):
    client, db, _ = env
    h = auth_header(client, "boss")
    assert client.get("/api/v1/admin/roles", headers=h).json()["data"]["total"] == 5
    perms = client.get("/api/v1/admin/permissions", headers=h).json()["data"]["items"]
    assert any(p["code"] == "staff:write" for p in perms)
    created = client.post("/api/v1/admin/roles", headers={**h, **key("role")},
                          json={"name": "夜班", "permissions": ["order:read"]}).json()["data"]
    rid = created["id"]
    upd = client.put(f"/api/v1/admin/roles/{rid}", headers={**h, **key("role-u")}, json={"permissions": ["order:read", "order:print"]})
    assert upd.json()["data"]["permissions"] == ["order:print", "order:read"]
    assert client.get(f"/api/v1/admin/roles/{rid}", headers=h).json()["data"]["name"] == "夜班"
    sa = client.put(f"/api/v1/admin/roles/{role_id(db, 'super_admin')}", headers={**h, **key("role-sa")}, json={"permissions": []})
    assert sa.json()["code"] == 4220
    assert client.delete(f"/api/v1/admin/roles/{rid}", headers={**h, **key("role-d")}).json()["data"]["deleted"] is True


def test_audit_list_filters_and_export(env):
    client, db, clock = env
    h = auth_header(client, "boss")
    boss_id = db.execute(select(AdminUser.id).where(AdminUser.username == "boss")).scalar_one()
    client.post("/api/v1/admin/roles", headers={**h, **key("r1")}, json={"name": "甲", "permissions": []})
    client.post("/api/v1/admin/roles", headers={**h, **key("r2")}, json={"name": "乙", "permissions": []})
    data = client.get("/api/v1/admin/audit-logs", headers=h,
                      params={"actor_id": str(boss_id), "action": "role.create", "resource": "role"}).json()["data"]
    assert data["total"] == 2 and data["items"][0]["action"] == "role.create"
    today = (clock() + timedelta(hours=8)).date().isoformat()  # 门店时区 Asia/Shanghai
    ranged = client.get("/api/v1/admin/audit-logs", headers=h, params={"created_at_from": today, "created_at_to": today}).json()
    assert ranged["data"]["total"] >= 2
    future = client.get("/api/v1/admin/audit-logs", headers=h, params={"created_at_from": "2030-01-01"}).json()
    assert future["data"]["total"] == 0
    kw = client.get("/api/v1/admin/audit-logs", headers=h, params={"keyword": "role.cre"}).json()["data"]
    assert kw["total"] == 2
    one = client.get(f"/api/v1/admin/audit-logs/{data['items'][0]['id']}", headers=h).json()["data"]
    assert one["resource"] == "role"
    assert client.get("/api/v1/admin/audit-logs/999999", headers=h).json()["code"] == 4040
    assert client.get("/api/v1/admin/audit-logs", headers=h, params={"actor_type": "alien"}).json()["code"] == 4001
    csv = client.get("/api/v1/admin/audit-logs/export", headers=h, params={"action": "role.create"})
    text = csv.content.decode("utf-8-sig")
    assert csv.status_code == 200 and text.splitlines()[0].startswith("ID,时间") and text.count("role.create") == 2
    exported = client.get("/api/v1/admin/audit-logs", headers=h, params={"action": "audit.export"}).json()["data"]
    assert exported["total"] == 1
    logins = client.get("/api/v1/admin/login-logs", headers=h, params={"username": "boss", "success": "true"}).json()["data"]
    assert logins["total"] >= 1
    login_csv = client.get("/api/v1/admin/login-logs/export", headers=h)
    assert "boss" in login_csv.content.decode("utf-8-sig")


def test_own_sessions_over_http(env):
    client, _, _ = env
    h1 = auth_header(client, "boss")
    h2 = auth_header(client, "boss")
    items = client.get("/api/v1/admin/auth/sessions", headers=h1).json()["data"]["items"]
    assert len(items) == 2
    other = [s for s in items if not s["current"]][0]
    assert client.delete(f"/api/v1/admin/auth/sessions/{other['id']}", headers=h1).json()["code"] == 0
    assert client.get("/api/v1/admin/auth/me", headers=h2).json()["code"] == 4010
    assert client.post("/api/v1/admin/auth/sessions/revoke-others", headers=h1).json()["data"]["revoked"] == 0
    assert client.post("/api/v1/admin/auth/logout", headers=h1).json()["code"] == 0
    assert client.get("/api/v1/admin/auth/me", headers=h1).json()["code"] == 4010


def test_change_password_over_http(env):
    client, _, _ = env
    h = auth_header(client, "boss")
    weak = client.post("/api/v1/admin/auth/password", headers=h, json={"old_password": PASSWORD, "new_password": "password123"})
    assert weak.json()["code"] == 4001 and weak.json()["data"]["errors"][0]["field"] == "new_password"
    ok = client.post("/api/v1/admin/auth/password", headers=h,
                     json={"old_password": PASSWORD, "new_password": "brand new long passphrase"}).json()
    assert ok["code"] == 0
    assert client.get("/api/v1/admin/auth/me", headers=h).json()["code"] == 4010


def test_jobs_purge(env):
    _, db, clock = env
    from admin_rbac import auth

    token = auth.login(db, "boss", PASSWORD, user_agent="testclient")["token"]
    for _ in range(2):
        try:
            auth.login(db, "ghost", "x-password-x")
        except Exception:  # noqa: BLE001
            pass
    clock.advance(9 * 3600)
    assert jobs.purge_sessions(db) == "deleted=0"  # 刚过期，保留 30 天
    clock.advance(31 * 86400)
    assert jobs.purge_sessions(db) == "deleted=1"
    assert jobs.purge_throttles(db) == "deleted=1"
    clock.advance(150 * 86400)
    assert jobs.purge_login_logs(db).startswith("deleted=")
    assert db.execute(select(func.count()).select_from(AdminLoginLog)).scalar_one() == 0
    assert db.execute(select(func.count()).select_from(AdminSession)).scalar_one() == 0
    from luzback.scheduler import JobRegistry

    registry = JobRegistry()
    jobs.register_jobs(registry)
    assert registry.get("admin_rbac.purge_sessions").interval_seconds == 3600
    assert token


def test_wiring_exports():
    import admin_rbac
    from admin_rbac.events import SUBSCRIPTIONS
    from luzback.scheduler import Job

    assert all(isinstance(j, Job) and j.name.startswith("admin_rbac.") for j in jobs.JOBS)
    assert SUBSCRIPTIONS == {}
    for name in ("require_permission", "current_staff", "require_fresh_auth", "load_staff", "declare_permissions",
                 "authenticated_staff", "require_fresh"):
        assert callable(getattr(admin_rbac, name))
    assert admin_rbac.require_fresh_auth is api.require_fresh
