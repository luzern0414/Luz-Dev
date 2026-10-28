"""HTTP：鉴权 4010、权限 4030、参数 4001、幂等、信封；会员站内信与订阅上报；后台日志、导出、重发、模板、员工站内信。"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from luzback import create_app, create_token, get_settings
from luzback.db import get_session_factory

import notif_kit as kit
from notification import api, service
from notification.auth import _token_kind, set_permission_resolver
from notification.models import NotificationDelivery, NotificationInbox

PERMS = {
    1: {"notification:*"},
    2: set(),
    3: {"notification:read"},
}


@pytest.fixture()
def ctx(tmp_path):
    env = kit.make_env(url=f"sqlite:///{tmp_path / 'api.db'}")
    set_permission_resolver(lambda db, staff_id, claims: PERMS.get(staff_id, set()))
    client = TestClient(create_app(api.routers), raise_server_exceptions=False)
    yield env, client
    env.db.close()
    kit.teardown()


def member(uid: int) -> dict:
    s = get_settings()
    return {"Authorization": f"Bearer {create_token(str(uid), s.jwt_secret, 600, kind='access', issuer=s.jwt_issuer)}"}


def staff(sid: int) -> dict:
    s = get_settings()
    return {"Authorization": f"Bearer {create_token(str(sid), s.jwt_secret, 600, kind=_token_kind(), issuer=s.jwt_issuer)}"}


def key() -> dict:
    return {"Idempotency-Key": str(uuid.uuid4())}


# ---------------------------------------------------------------- 会员端


def test_member_auth_required(ctx):
    _, c = ctx
    for method, path in (("get", "/api/v1/app/notifications/messages"), ("get", "/api/v1/app/notifications/subscriptions"),
                         ("post", "/api/v1/app/notifications/messages/read-all")):
        r = getattr(c, method)(path)
        assert r.status_code == 401 and r.json()["code"] == 4010
    r = c.get("/api/v1/app/notifications/messages", headers=staff(1))  # 员工令牌不能当会员令牌
    assert r.json()["code"] == 4010


def test_subscription_report_idempotent(ctx):
    env, c = ctx
    h = {**member(1), **key()}
    body = {"results": {"TPL_PAID": "accept", "TPL_CARD": "reject"}}
    r1 = c.post("/api/v1/app/notifications/subscriptions", json=body, headers=h)
    assert r1.status_code == 200 and r1.json()["code"] == 0
    r2 = c.post("/api/v1/app/notifications/subscriptions", json=body, headers=h)
    assert r2.json() == r1.json()
    items = {i["template_id"]: i for i in r1.json()["data"]["subscriptions"]}
    assert items["TPL_PAID"]["available"] == 1 and items["TPL_CARD"]["status"] == "rejected"
    r3 = c.post("/api/v1/app/notifications/subscriptions", json={"results": {"TPL_PAID": "reject"}}, headers=h)
    assert r3.json()["code"] == 4093
    r4 = c.post("/api/v1/app/notifications/subscriptions", json=body, headers=member(1))
    assert r4.status_code == 400 and r4.json()["code"] == 4001
    r5 = c.post("/api/v1/app/notifications/subscriptions", json={"results": {f"T{i}": "accept" for i in range(6)}},
                headers={**member(1), **key()})
    assert r5.json()["code"] == 4001
    r6 = c.get("/api/v1/app/notifications/subscriptions", headers=member(1))
    assert any(i["template_id"] == "TPL_PAID" and i["available"] == 1 for i in r6.json()["data"]["items"])


def test_subscribe_plan(ctx):
    _, c = ctx
    r = c.get("/api/v1/app/notifications/subscribe-plan", params={"scenes": "order.paid,card.expiring"}, headers=member(1))
    assert r.json()["data"] == {"template_ids": ["TPL_PAID", "TPL_CARD"], "long_term": False}
    r = c.get("/api/v1/app/notifications/subscribe-plan", headers=member(1))
    assert r.json()["code"] == 4001


def test_member_inbox_flow(ctx):
    env, c = ctx
    kit.accept(env, "order.paid", key="a")
    kit.accept(env, "order.paid", key="b")
    kit.accept(env, "order.paid", member=2, key="c")
    r = c.get("/api/v1/app/notifications/messages", params={"limit": 1}, headers=member(1))
    data = r.json()["data"]
    assert len(data["items"]) == 1 and data["has_more"] and data["next_cursor"]
    assert c.get("/api/v1/app/notifications/messages/unread-count", headers=member(1)).json()["data"]["total"] == 2
    mid = data["items"][0]["id"]
    h = {**member(1), **key()}
    assert c.post(f"/api/v1/app/notifications/messages/{mid}/read", headers=h).json()["data"] == {"id": mid, "read": True}
    assert c.post(f"/api/v1/app/notifications/messages/{mid}/read", headers=h).json()["code"] == 0  # 重放
    other = env.db.execute(select(NotificationInbox.id).where(NotificationInbox.recipient_id == 2)).scalar_one()
    r = c.post(f"/api/v1/app/notifications/messages/{other}/read", headers={**member(1), **key()})
    assert r.status_code == 404 and r.json()["code"] == 4040
    r = c.post("/api/v1/app/notifications/messages/read-all", json={}, headers={**member(1), **key()})
    assert r.json()["data"]["updated"] == 1
    r = c.delete(f"/api/v1/app/notifications/messages/{mid}", headers={**member(1), **key()})
    assert r.json()["data"]["deleted"] is True
    assert c.get("/api/v1/app/notifications/messages", params={"bogus": 1}, headers=member(1)).json()["code"] == 4001
    read = c.get("/api/v1/app/notifications/messages", params={"category": "transaction", "read_at_null": "false"},
                 headers=member(1)).json()["data"]["items"]
    assert len(read) == 1 and read[0]["read"] and read[0]["created_at"].endswith("+00:00")


# ---------------------------------------------------------------- 后台


def _seed_failed(env):
    kit.grant(env, 1, "TPL_PAID")
    env.sub.script = [40037]
    kit.accept(env, "order.paid")
    service.dispatch_due(env.db, now=env.now)
    return env.db.execute(select(NotificationDelivery.id).where(NotificationDelivery.status == "failed")).scalar_one()


def test_admin_auth_and_permissions(ctx):
    _, c = ctx
    r = c.get("/api/v1/admin/notifications/deliveries")
    assert r.status_code == 401 and r.json()["code"] == 4010
    r = c.get("/api/v1/admin/notifications/deliveries", headers=member(1))
    assert r.json()["code"] == 4010
    r = c.get("/api/v1/admin/notifications/deliveries", headers=staff(2))
    assert r.status_code == 403 and r.json()["code"] == 4030
    assert c.get("/api/v1/admin/notifications/deliveries", headers=staff(3)).json()["code"] == 0
    r = c.post("/api/v1/admin/notifications/deliveries/1/resend", json={}, headers={**staff(3), **key()})
    assert r.json()["code"] == 4030
    assert c.get("/api/v1/admin/notifications/deliveries/export", headers=staff(3)).json()["code"] == 4030


def test_admin_delivery_log_export_resend_stats(ctx):
    env, c = ctx
    failed = _seed_failed(env)
    h = staff(1)
    r = c.get("/api/v1/admin/notifications/deliveries",
              params={"status": "failed", "errcode": "40037", "channel": "subscribe", "keyword": "1",
                      "created_at_from": "2026-10-28", "created_at_to": "2026-10-28", "page": 1, "page_size": 10}, headers=h)
    body = r.json()["data"]
    assert body["total"] == 1 and body["items"][0]["id"] == failed and body["page_size"] == 10
    assert c.get("/api/v1/admin/notifications/deliveries", params={"nope": 1}, headers=h).json()["code"] == 4001
    assert c.get("/api/v1/admin/notifications/deliveries", params={"sort": "recipient_ref"}, headers=h).json()["code"] == 4001
    r = c.get("/api/v1/admin/notifications/deliveries/export", params={"status": "failed"}, headers=h)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "40037" in r.content.decode("utf-8-sig")
    r = c.get("/api/v1/admin/notifications/deliveries/export",
              params={"created_at_from": "2026-01-01", "created_at_to": "2026-10-28"}, headers=h)
    assert r.json()["code"] == 4001  # 导出跨度上限 31 天
    hk = {**h, **key()}
    r = c.post(f"/api/v1/admin/notifications/deliveries/{failed}/resend", json={"reason": "fixed"}, headers=hk)
    assert r.json()["data"] == {"id": failed, "status": "pending"}
    assert c.post(f"/api/v1/admin/notifications/deliveries/{failed}/resend", json={"reason": "fixed"}, headers=hk).json() == r.json()
    r = c.post(f"/api/v1/admin/notifications/deliveries/{failed}/resend", json={}, headers={**h, **key()})
    assert r.status_code == 409 and r.json()["code"] == 4090
    r = c.post("/api/v1/admin/notifications/deliveries/999/resend", json={}, headers={**h, **key()})
    assert r.json()["code"] == 4040
    s = c.get("/api/v1/admin/notifications/stats", headers=h).json()["data"]
    assert "quota_exhaustion_rate" in s and s["by_channel"]["inapp"]["sent"] == 1


def test_admin_scene_and_template_crud(ctx):
    _, c = ctx
    h = staff(1)
    r = c.post("/api/v1/admin/notifications/scenes", json={"scene": "queue.called", "name": "叫号", "urgent": True},
               headers={**h, **key()})
    assert r.json()["code"] == 0
    r = c.post("/api/v1/admin/notifications/scenes", json={"scene": "queue.called", "name": "叫号"}, headers={**h, **key()})
    assert r.json()["code"] == 4090
    r = c.post("/api/v1/admin/notifications/scenes", json={"scene": "Bad Scene", "name": "x"}, headers={**h, **key()})
    assert r.json()["code"] == 4001
    r = c.put("/api/v1/admin/notifications/scenes/queue.called/templates/subscribe",
              json={"template_id": "TPL_Q", "keywords": [{"key": "thing1", "source": "{ticket}"}], "page": "pages/q/index"},
              headers={**h, **key()})
    assert r.json()["data"]["template_id"] == "TPL_Q"
    r = c.put("/api/v1/admin/notifications/scenes/queue.called/templates/subscribe",
              json={"template_id": "TPL_Q", "keywords": [{"key": "thingy1", "source": "x"}]}, headers={**h, **key()})
    assert r.json()["code"] == 4001
    r = c.put("/api/v1/admin/notifications/scenes/queue.called/templates/fax", json={}, headers={**h, **key()})
    assert r.json()["code"] == 4001
    r = c.put("/api/v1/admin/notifications/scenes/queue.called", json={"sms_mode": "fallback"}, headers={**h, **key()})
    assert r.json()["data"]["sms_mode"] == "fallback"
    r = c.get("/api/v1/admin/notifications/scenes/queue.called", headers=h)
    assert r.json()["data"]["templates"]["subscribe"]["keywords"][0]["key"] == "thing1"
    r = c.get("/api/v1/admin/notifications/scenes", params={"keyword": "叫号"}, headers=h)
    assert r.json()["data"]["total"] == 1
    assert c.delete("/api/v1/admin/notifications/scenes/queue.called/templates/subscribe",
                    headers={**h, **key()}).json()["code"] == 0
    assert c.delete("/api/v1/admin/notifications/scenes/queue.called", headers={**h, **key()}).json()["code"] == 0
    assert c.get("/api/v1/admin/notifications/scenes/queue.called", headers=h).json()["code"] == 4040
    r = c.put("/api/v1/admin/notifications/scenes/order.paid", json={"name": "x"}, headers=staff(3) | key())
    assert r.json()["code"] == 4030


def test_staff_inbox(ctx):
    env, c = ctx
    service.accept_request(env.db, scene="staff.device_offline", audience="staff", recipient_ids=[9],
                           data={"device_name": "闸机1", "store_name": "一店"}, dedupe_key="d1", now=env.now)
    h = staff(9)
    r = c.get("/api/v1/admin/notifications/inbox", headers=h)
    items = r.json()["data"]["items"]
    assert items[0]["title"] == "设备离线" and items[0]["body"] == "闸机1 已离线（一店）"
    assert c.get("/api/v1/admin/notifications/inbox/unread-count", headers=h).json()["data"]["total"] == 1
    assert c.get("/api/v1/admin/notifications/inbox", headers=staff(1)).json()["data"]["items"] == []
    mid = items[0]["id"]
    assert c.post(f"/api/v1/admin/notifications/inbox/{mid}/read", headers={**staff(1), **key()}).json()["code"] == 4040
    assert c.post(f"/api/v1/admin/notifications/inbox/{mid}/read", headers={**h, **key()}).json()["code"] == 0
    assert c.post("/api/v1/admin/notifications/inbox/read-all", json={"category": "staff_alert"},
                  headers={**h, **key()}).json()["data"]["updated"] == 0
    assert c.delete(f"/api/v1/admin/notifications/inbox/{mid}", headers={**h, **key()}).json()["code"] == 0
    assert c.get("/api/v1/admin/notifications/inbox").json()["code"] == 4010
