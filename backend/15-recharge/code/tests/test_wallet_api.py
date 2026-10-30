"""储值 HTTP：路由存在、鉴权 4010、权限 4030、门店范围 4031、参数 4001、信封、幂等重放、列表筛选与导出。"""
import uuid

import pytest
from fastapi.testclient import TestClient

import wallet
from luzback import AppError, ErrorCode
from admin_rbac.testing import staff_headers
from luzback.db import get_session_factory
from luzback.testing import make_test_app
from wallet import api, config as wconfig
from wallet.prepaid import CONSERVATIVE, set_policy_resolver


def user_decoder(token: str) -> int:
    if not token.startswith("user-"):
        raise AppError(ErrorCode.UNAUTHORIZED, "bad token")
    return int(token[5:])


@pytest.fixture()
def client(tmp_path):
    wconfig.reset_config()
    set_policy_resolver(lambda store_id: CONSERVATIVE)
    port = wallet.FakePaymentPort()
    wallet.set_payment_port(port)
    wallet.set_user_token_decoder(user_decoder)
    app = make_test_app(api.routers, wallet, "admin_rbac.models", url=f"sqlite:///{tmp_path / 'api.db'}")
    global _staff_db
    _staff_db = get_session_factory()()
    with TestClient(app) as c:
        yield c
    _staff_db.close()
    wallet.set_user_token_decoder(None)
    wallet.set_payment_port(wallet.WechatPayPort())
    set_policy_resolver(None)


_staff_db = None


def staff(perms=tuple(c for c, _ in wallet.PERMISSIONS), stores=None):
    """真实 admin_rbac 会话令牌（admin_rbac.testing）：stores=None 全部门店，列表 = 仅这些门店。"""
    return staff_headers(_staff_db, *perms, store_ids=stores)


def user(uid=1):
    return {"Authorization": f"Bearer user-{uid}"}


def key():
    return {"Idempotency-Key": str(uuid.uuid4())}


def make_plan(client):
    r = client.post("/api/v1/admin/wallet/plans", json={"name": "充100送10", "amount_cents": 10000, "bonus_cents": 1000},
                    headers=staff() | key())
    assert r.status_code == 200, r.text
    plan_id = r.json()["data"]["id"]
    assert client.post(f"/api/v1/admin/wallet/plans/{plan_id}/on-sale", headers=staff() | key()).json()["code"] == 0
    return plan_id


def test_auth_permission_validation_and_envelope(client):
    assert client.get("/api/v1/app/wallet/account").json()["code"] == 4010
    assert client.get("/api/v1/app/wallet/account", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/api/v1/admin/wallet/plans").json()["code"] == 4010
    r = client.get("/api/v1/admin/wallet/plans", headers=staff(perms=("member:read",)))
    assert r.status_code == 403 and r.json()["code"] == 4030
    r = client.post("/api/v1/admin/wallet/plans", json={"name": "", "amount_cents": 0}, headers=staff() | key())
    assert r.json()["code"] == 4001 and r.json()["data"]["errors"]
    r = client.post("/api/v1/admin/wallet/plans", json={"name": "x", "amount_cents": 100}, headers=staff())
    assert r.json()["code"] == 4001  # 缺幂等键
    body = client.get("/api/v1/app/wallet/account", headers=user()).json()
    assert body == {"code": 0, "message": "ok", "data": body["data"]} and body["data"]["balance_cents"] == 0


def test_recharge_flow_over_http_with_idempotent_replay(client):
    plan_id = make_plan(client)
    plans = client.get("/api/v1/app/wallet/plans", headers=user()).json()["data"]
    assert plans["items"][0]["credited_cents"] == 11000 and plans["has_more"] is False
    headers = user() | key()
    first = client.post("/api/v1/app/wallet/recharge-orders", json={"plan_id": plan_id}, headers=headers).json()
    again = client.post("/api/v1/app/wallet/recharge-orders", json={"plan_id": plan_id}, headers=headers).json()
    assert first["code"] == 0 and first == again
    other = client.post("/api/v1/app/wallet/recharge-orders", json={"plan_id": plan_id, "store_id": 3}, headers=headers).json()
    assert other["code"] == 4093
    order_id = first["data"]["id"]
    from luzback.db import get_session_factory
    with get_session_factory()() as db:
        wallet.handle_event(db, "payment.succeeded", {"biz_type": "wallet_recharge", "biz_id": order_id,
                                                      "amount_cents": 10000, "payment_id": "p1"}, "e1")
    account = client.get("/api/v1/app/wallet/account", headers=user()).json()["data"]
    assert (account["principal_cents"], account["bonus_cents"]) == (10000, 1000)
    ledger = client.get("/api/v1/app/wallet/ledger?limit=1", headers=user()).json()["data"]
    assert ledger["items"][0]["kind"] == "recharge"
    assert client.get(f"/api/v1/app/wallet/recharge-orders/{order_id}", headers=user(2)).json()["code"] == 4040
    quote = client.get(f"/api/v1/app/wallet/recharge-orders/{order_id}/refund-quote", headers=user()).json()["data"]
    assert quote["rule"] == "cooling_off_full" and quote["refund_cents"] == 10000
    r = client.post(f"/api/v1/app/wallet/recharge-orders/{order_id}/refund-request", json={"reason": "不需要"},
                    headers=user() | key()).json()
    assert r["data"]["status"] == "pending_approval"
    refund_id = r["data"]["id"]
    assert client.get("/api/v1/admin/wallet/refunds?status=pending_approval", headers=staff()).json()["data"]["total"] == 1
    r = client.post(f"/api/v1/admin/wallet/refunds/{refund_id}/approve", headers=staff() | key()).json()
    assert r["data"]["status"] == "processing"


def test_admin_lists_filters_and_export(client):
    make_plan(client)
    r = client.get("/api/v1/admin/wallet/plans?status=on&sort=-amount_cents", headers=staff()).json()
    assert r["data"]["total"] == 1
    assert client.get("/api/v1/admin/wallet/plans?bogus=1", headers=staff()).json()["code"] == 4001
    assert client.get("/api/v1/admin/wallet/recharge-orders?status=weird", headers=staff()).json()["code"] == 4001
    r = client.post("/api/v1/admin/wallet/accounts/5/offline-recharge", json={"amount_cents": 2000, "store_id": 3},
                    headers=staff() | key()).json()
    assert r["code"] == 0
    r = client.post("/api/v1/admin/wallet/accounts/5/adjust", json={"principal_delta": 100, "reason": "补偿"},
                    headers=staff() | key()).json()
    assert r["code"] == 0
    assert client.get("/api/v1/admin/wallet/ledger?user_id=5", headers=staff()).json()["data"]["total"] == 2
    csv = client.get("/api/v1/admin/wallet/ledger/export?user_id=5", headers=staff())
    assert csv.status_code == 200 and csv.content.startswith("﻿".encode()) and "流水ID" in csv.text
    csv = client.get("/api/v1/admin/wallet/recharge-orders/export", headers=staff())
    assert csv.status_code == 200 and "601" in csv.text
    assert client.get("/api/v1/admin/wallet/accounts/5", headers=staff()).json()["data"]["principal_cents"] == 2100
    r = client.post("/api/v1/admin/wallet/accounts/5/consume", json={"amount_cents": 100, "ref": "pos-1", "store_id": 3},
                    headers=staff() | key()).json()
    assert r["data"]["kind"] == "consume"
    r = client.post("/api/v1/admin/wallet/reconcile", headers=staff() | key()).json()
    assert r["data"]["mismatched"] == 0
    assert client.get("/api/v1/admin/wallet/mismatches", headers=staff()).json()["data"]["total"] == 0


def test_store_scope_enforced(client):
    r = client.post("/api/v1/admin/wallet/accounts/5/offline-recharge", json={"amount_cents": 2000, "store_id": 3},
                    headers=staff(stores=[4]) | key())
    assert r.status_code == 403 and r.json()["code"] == 4031


def test_app_plan_cursor_pagination(client):
    make_plan(client)
    make_plan(client)
    first = client.get("/api/v1/app/wallet/plans?limit=1", headers=user()).json()["data"]
    assert first["has_more"] is True and first["next_cursor"]
    second = client.get(f"/api/v1/app/wallet/plans?limit=1&cursor={first['next_cursor']}", headers=user()).json()["data"]
    assert second["has_more"] is False and second["items"][0]["id"] != first["items"][0]["id"]


def test_empty_store_scope_means_no_stores(client):
    client.post("/api/v1/admin/wallet/accounts/5/offline-recharge", json={"amount_cents": 2000, "store_id": 3},
                headers=staff() | key())
    assert client.get("/api/v1/admin/wallet/recharge-orders", headers=staff()).json()["data"]["total"] == 1
    assert client.get("/api/v1/admin/wallet/recharge-orders", headers=staff(stores=[])).json()["data"]["total"] == 0
    assert wallet.staff_context(type("S", (), {"id": 1, "permissions": [], "store_scope": []})()).store_scope == ()


def test_admin_auth_is_real_admin_rbac_session(client):
    """真实会话：伪造的 kind=admin 令牌 4010；资金类写要求新鲜认证（超过 fresh_auth_seconds → 4011 reauth_required）。"""
    from datetime import timedelta

    from admin_rbac import config as rbac_config
    from luzback import create_token, get_settings, utcnow

    s = get_settings()
    forged = create_token("7", s.jwt_secret, 600, kind="admin", extra={"sid": 999, "rv": 999, "role": "staff"},
                          issuer=s.jwt_issuer)
    assert client.get("/api/v1/admin/wallet/plans", headers={"Authorization": f"Bearer {forged}"}).json()["code"] == 4010
    h = staff(("wallet:read", "wallet:adjust"))
    assert client.get("/api/v1/admin/wallet/plans", headers=h).json()["code"] == 0
    later = utcnow() + timedelta(seconds=rbac_config.get_config().fresh_auth_seconds + 5)
    rbac_config.set_clock(lambda: later)
    try:
        assert client.get("/api/v1/admin/wallet/plans", headers=h).json()["code"] == 0  # 读不要求新鲜认证
        r = client.post("/api/v1/admin/wallet/accounts/5/adjust", json={"principal_delta": 100, "reason": "补偿"},
                        headers=h | key()).json()
        assert r["code"] == 4011 and r["data"]["reason"] == "reauth_required"
    finally:
        rbac_config.set_clock(None)
    wild = staff(("wallet:*",))  # 模块通配授予
    assert client.get("/api/v1/admin/wallet/accounts/5", headers=wild).json()["code"] in (0, 4040)
