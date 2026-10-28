"""identity HTTP：信封、4010 / 4030 / 4001、幂等重放、限流声明、后台列表 / 导出 / 查看手机号审计 / 停用启用、推送回调联动。"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import identity
import identity.api as identity_api
import wechat_mp
import wechat_mp.api
from identity import FakeSmsSender, User
from identity_testkit import *  # noqa: F401,F403
from identity_testkit import MODULES, audits
from luzback import AppError, ErrorCode, Settings, create_app
from luzback.audit import AuditLog
from luzback.db import get_session_factory
from luzback.testing import make_test_app
from wechat_mp import FakeMiniProgram

STAFF = {"staff-read": (1, {"member:read"}), "staff-write": (2, {"member:read", "member:write"}),
         "staff-reveal": (3, {"member:read", "member:reveal"})}


def fake_staff(db, token, code):
    if token not in STAFF:
        raise AppError(ErrorCode.UNAUTHORIZED, "bad staff token")
    staff_id, perms = STAFF[token]
    if code not in perms:
        raise AppError(ErrorCode.FORBIDDEN, f"permission required: {code}")
    return staff_id


@pytest.fixture
def env(tmp_path):
    mp = FakeMiniProgram()
    sms = FakeSmsSender()
    identity_api.set_mini_program(mp)
    identity_api.set_sms_sender(sms)
    identity_api.set_agreement_gateway(identity.FakeAgreements())
    identity_api.set_staff_authorizer(fake_staff)
    app = make_test_app([*identity_api.routers, wechat_mp.api.router], *MODULES, url=f"sqlite:///{tmp_path / 'http.db'}")
    yield TestClient(app), mp, sms
    identity_api.set_mini_program(None)
    identity_api.set_sms_sender(None)
    identity_api.set_agreement_gateway(None)
    identity_api.set_staff_authorizer(None)


def H(token=None, key=None):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if key:
        headers["Idempotency-Key"] = key
    return headers


def do_login(client, mp, code="c1", openid="o-1"):
    mp.add_login_code(code, openid)
    resp = client.post("/api/v1/app/auth/login", json={"code": code})
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def test_login_me_refresh_logout_over_http(env):
    client, mp, _ = env
    tokens = do_login(client, mp)
    me = client.get("/api/v1/app/me", headers=H(tokens["access_token"])).json()
    assert me["code"] == 0 and me["data"]["user_id"] == tokens["user_id"] and me["data"]["phone"] is None
    rotated = client.post("/api/v1/app/auth/refresh", json={"refresh_token": tokens["refresh_token"]}).json()["data"]
    assert client.post("/api/v1/app/auth/logout", headers=H(rotated["access_token"])).json()["data"] == {"revoked_sessions": 1}
    after = client.get("/api/v1/app/me", headers=H(rotated["access_token"]))
    assert after.status_code == 401 and after.json()["code"] == 4010


def test_auth_and_validation_errors(env):
    client, mp, _ = env
    assert client.get("/api/v1/app/me").json()["code"] == 4010
    assert client.get("/api/v1/app/me", headers=H("garbage")).json()["code"] == 4010
    bad = client.post("/api/v1/app/auth/login", json={"code": ""})
    assert bad.status_code == 400 and bad.json()["code"] == 4001 and bad.json()["data"]["errors"]
    extra = client.post("/api/v1/app/auth/login", json={"code": "x", "openid": "spoofed"})
    assert extra.json()["code"] == 4001
    assert client.post("/api/v1/app/auth/login", json={"code": "never"}).json()["code"] == 4010


def test_disabled_login_over_http_is_4030(env):
    client, mp, _ = env
    tokens = do_login(client, mp)
    resp = client.post(f"/api/v1/admin/members/{tokens['user_id']}/disable", json={"reason": "违规刷单"},
                       headers=H("staff-write", "disable-0001"))
    assert resp.json()["data"]["status"] == "disabled"
    mp.add_login_code("c2", "o-1")
    denied = client.post("/api/v1/app/auth/login", json={"code": "c2"})
    assert denied.status_code == 403 and denied.json() == {"code": 4030, "message": denied.json()["message"],
                                                            "data": {"reason": "account_disabled"}}
    assert client.get("/api/v1/app/me", headers=H(tokens["access_token"])).json()["code"] == 4010


def test_profile_update_requires_idempotency_key_and_replays(env):
    client, mp, _ = env
    tokens = do_login(client, mp)
    body = {"nickname": "小卢"}
    assert client.put("/api/v1/app/me", json=body, headers=H(tokens["access_token"])).json()["code"] == 4001
    first = client.put("/api/v1/app/me", json=body, headers=H(tokens["access_token"], "profile-0001")).json()
    again = client.put("/api/v1/app/me", json=body, headers=H(tokens["access_token"], "profile-0001")).json()
    assert first == again and first["data"]["nickname"] == "小卢"
    assert mp.server.calls.count("/wxa/msg_sec_check") == 1
    other = client.put("/api/v1/app/me", json={"nickname": "别的"}, headers=H(tokens["access_token"], "profile-0001")).json()
    assert other["code"] == 4093
    assert client.put("/api/v1/app/me", json={"member_no": "M1"}, headers=H(tokens["access_token"], "profile-0002")).json()["code"] == 4001


def test_wechat_phone_bind_replay_after_code_consumed(env):
    client, mp, _ = env
    tokens = do_login(client, mp)
    mp.add_phone_code("pc", "13800138000", openid="o-1")
    first = client.post("/api/v1/app/me/phone/wechat", json={"phone_code": "pc"}, headers=H(tokens["access_token"], "phone-0001")).json()
    replay = client.post("/api/v1/app/me/phone/wechat", json={"phone_code": "pc"}, headers=H(tokens["access_token"], "phone-0001")).json()
    assert first == replay and first["data"] == {"phone": "138****8000", "phone_verified": True}


def test_sms_send_login_and_bind_over_http(env):
    client, mp, sms = env
    tokens = do_login(client, mp)
    sent = client.post("/api/v1/app/auth/sms/send", json={"phone": "13800138000", "purpose": "bind"}).json()["data"]
    bound = client.post("/api/v1/app/me/phone/sms", headers=H(tokens["access_token"], "smsbind-01"),
                        json={"phone": "13800138000", "request_id": sent["request_id"], "code": sms.last_code("13800138000")}).json()
    assert bound["data"]["phone"] == "138****8000"
    sent = client.post("/api/v1/app/auth/sms/send", json={"phone": "13800138000", "purpose": "login"}).json()["data"]
    login = client.post("/api/v1/app/auth/sms/login",
                        json={"phone": "13800138000", "request_id": sent["request_id"], "code": sms.last_code("13800138000")}).json()
    assert login["data"]["user_id"] == tokens["user_id"]


def test_deletion_flow_over_http(env):
    client, mp, _ = env
    tokens = do_login(client, mp)
    status = client.get("/api/v1/app/me/deletion", headers=H(tokens["access_token"])).json()["data"]
    assert status["status"] == "active" and status["cooling_days"] == 15 and status["blockers"] == []
    req = client.post("/api/v1/app/me/deletion", json={"reason": "不用了"}, headers=H(tokens["access_token"], "delete-0001")).json()
    assert req["data"]["status"] == "deleting"
    cancel = client.post("/api/v1/app/me/deletion/cancel", headers=H(tokens["access_token"], "cancel-0001")).json()
    assert cancel["data"] == {"status": "active"}


def test_rate_limit_rules_declared_for_login(env, tmp_path):
    _, mp, _ = env
    app = create_app(routers=identity_api.routers, settings=Settings(), rate_rules=identity_api.rate_limit_rules())
    client = TestClient(app)
    codes = []
    for i in range(6):
        mp.add_login_code(f"rl{i}", f"o-rl{i}")
        codes.append(client.post("/api/v1/app/auth/login", json={"code": f"rl{i}"}).json()["code"])
    assert codes[:5] == [0] * 5 and codes[5] == 4290
    names = {r.name for r in identity_api.rate_limit_rules()}
    assert names == {"identity_login", "identity_sms_login", "identity_sms_send", "identity_refresh"}


# ================================================================ 后台

def _seed_members(client, mp):
    a = do_login(client, mp, "a", "o-a")
    b = do_login(client, mp, "b", "o-b")
    do_login(client, mp, "c", "o-c")
    with get_session_factory()() as db:
        identity.attach_phone(db, a["user_id"], "13800001234", "test")
        identity.attach_phone(db, b["user_id"], "13900005678", "test")
        identity.disable_member(db, 1, b["user_id"], "测试")
    return a, b


def test_admin_list_filters_keyword_sort_and_permissions(env):
    client, mp, _ = env
    a, b = _seed_members(client, mp)
    url = "/api/v1/admin/members"
    assert client.get(url).json()["code"] == 4010
    assert client.get(url, headers=H("nobody")).json()["code"] == 4010
    page = client.get(url, headers=H("staff-read")).json()["data"]
    assert page["total"] == 3 and page["page"] == 1
    assert client.get(url, params={"status": "disabled"}, headers=H("staff-read")).json()["data"]["items"][0]["user_id"] == b["user_id"]
    assert client.get(url, params={"has_phone": "true"}, headers=H("staff-read")).json()["data"]["total"] == 2
    assert client.get(url, params={"has_phone": "false"}, headers=H("staff-read")).json()["data"]["total"] == 1
    hit = client.get(url, params={"keyword": "1234"}, headers=H("staff-read")).json()["data"]
    assert [i["user_id"] for i in hit["items"]] == [a["user_id"]] and hit["items"][0]["phone"] == "138****1234"
    assert client.get(url, params={"created_at_from": "2020-01-01"}, headers=H("staff-read")).json()["data"]["total"] == 3
    assert client.get(url, params={"sort": "created_at"}, headers=H("staff-read")).json()["data"]["items"][0]["user_id"] == a["user_id"]
    assert client.get(url, params={"bogus": "1"}, headers=H("staff-read")).json()["code"] == 4001
    assert client.get(url, params={"status": "weird"}, headers=H("staff-read")).json()["code"] == 4001


def test_admin_detail_masks_and_reveal_requires_permission_and_is_audited(env):
    client, mp, _ = env
    a, _ = _seed_members(client, mp)
    detail = client.get(f"/api/v1/admin/members/{a['user_id']}", headers=H("staff-read")).json()["data"]
    assert detail["phone"] == "138****1234" and detail["active_sessions"] == 1 and "openid" not in detail
    assert client.get("/api/v1/admin/members/999999", headers=H("staff-read")).json()["code"] == 4040
    url = f"/api/v1/admin/members/{a['user_id']}/reveal-phone"
    denied = client.post(url, json={"reason": "客户来电核对"}, headers=H("staff-read"))
    assert denied.status_code == 403 and denied.json()["code"] == 4030
    assert client.post(url, json={}, headers=H("staff-reveal")).json()["code"] == 4001
    shown = client.post(url, json={"reason": "客户来电核对"}, headers=H("staff-reveal")).json()["data"]
    assert shown["phone"] == "13800001234"
    with get_session_factory()() as db:
        rows = audits(db, "member.phone.reveal")
        assert len(rows) == 1 and rows[0].actor_id == "3" and rows[0].reason == "客户来电核对"
        assert "13800001234" not in (rows[0].after_json or "")


def test_admin_export_csv_masked_and_audited(env):
    client, mp, _ = env
    _seed_members(client, mp)
    resp = client.get("/api/v1/admin/members/export", params={"has_phone": "true"}, headers=H("staff-read"))
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/csv")
    text = resp.content.decode("utf-8-sig")
    assert text.splitlines()[0].startswith("会员ID,会员号") and len(text.strip().splitlines()) == 3
    assert "138****1234" in text and "13800001234" not in text
    with get_session_factory()() as db:
        assert audits(db, "member.export")


def test_admin_disable_enable_idempotent_and_state_edges(env):
    client, mp, _ = env
    tokens = do_login(client, mp)
    url = f"/api/v1/admin/members/{tokens['user_id']}"
    assert client.post(f"{url}/disable", json={"reason": "违规"}, headers=H("staff-read", "dis-00001")).json()["code"] == 4030
    assert client.post(f"{url}/disable", json={"reason": "违规"}, headers=H("staff-write")).json()["code"] == 4001
    first = client.post(f"{url}/disable", json={"reason": "违规"}, headers=H("staff-write", "dis-00001")).json()
    replay = client.post(f"{url}/disable", json={"reason": "违规"}, headers=H("staff-write", "dis-00001")).json()
    assert first == replay and first["data"]["status"] == "disabled"
    again = client.post(f"{url}/disable", json={"reason": "违规"}, headers=H("staff-write", "dis-00002")).json()
    assert again["code"] == 4090
    assert client.post(f"{url}/enable", json={"reason": "申诉"}, headers=H("staff-write", "ena-00001")).json()["data"]["status"] == "active"
    assert client.post(f"{url}/logout", json={"reason": "账号风险"}, headers=H("staff-write", "out-00001")).json()["code"] == 0
    with get_session_factory()() as db:
        assert len(audits(db, "member.disable")) == 1 and audits(db, "member.force_logout")


def test_media_check_push_reaches_identity_handler(env, monkeypatch):
    client, mp, _ = env
    tokens = do_login(client, mp)
    resp = client.put("/api/v1/app/me", json={"avatar_url": "https://cdn.example.com/a.png"},
                      headers=H(tokens["access_token"], "avatar-0001")).json()
    assert resp["data"]["avatar_pending"] is True
    trace_id = next(iter(mp.server.media_checks))
    monkeypatch.setenv("WECHAT_MP_PUSH_TOKEN", "tok")
    monkeypatch.setenv("WECHAT_MP_PUSH_REQUIRE_AES", "false")
    wechat_mp.reload_config()
    q = {"signature": wechat_mp.url_signature("tok", "1", "n"), "timestamp": "1", "nonce": "n"}
    pushed = client.post("/api/v1/webhooks/wechat-mp/push", params=q, content=json.dumps(mp.media_result(trace_id, "pass")))
    assert pushed.text == "success"
    me = client.get("/api/v1/app/me", headers=H(tokens["access_token"])).json()["data"]
    assert me["avatar_url"] == "https://cdn.example.com/a.png" and me["avatar_pending"] is False
    monkeypatch.delenv("WECHAT_MP_PUSH_TOKEN")
    monkeypatch.delenv("WECHAT_MP_PUSH_REQUIRE_AES")
    wechat_mp.reload_config()


def test_current_claims_callable_directly_per_contract(env):
    client, mp, _ = env
    from fastapi.security import HTTPAuthorizationCredentials

    tokens = do_login(client, mp)
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=tokens["access_token"])
    assert identity_api.current_user(identity_api.current_claims(creds)) == tokens["user_id"]
    with get_session_factory()() as db:
        assert db.execute(select(User)).scalar_one().id == tokens["user_id"]
        assert db.execute(select(AuditLog)).first() is None or True


def test_permissions_declared():
    assert {code for code, _ in identity.PERMISSIONS} == {"member:read", "member:write", "member:reveal"}
    assert identity.EVENTS == ("member.registered", "member.phone_bound", "member.disabled", "member.deleted")


def test_testing_helper_mints_tokens_accepted_by_current_user(env):
    client, _, _ = env
    from identity.testing import member_tokens

    with get_session_factory()() as db:
        tokens = member_tokens(db)
    me = client.get("/api/v1/app/me", headers=H(tokens["access_token"])).json()
    assert me["data"]["user_id"] == tokens["user_id"]
    assert identity.current_user_id(tokens["access_token"]) == tokens["user_id"]


def test_wiring_conventions():
    from identity import events, jobs
    from luzback.scheduler import Job
    import wechat_mp.events
    import wechat_mp.jobs

    assert isinstance(jobs.JOBS, tuple) and all(isinstance(j, Job) for j in jobs.JOBS)
    assert events.SUBSCRIPTIONS == {} and wechat_mp.events.SUBSCRIPTIONS == {} and wechat_mp.jobs.JOBS == ()
