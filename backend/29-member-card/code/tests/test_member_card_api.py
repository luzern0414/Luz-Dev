"""会员卡 HTTP：鉴权 4010、权限 4030、门店范围、参数 4001、信封、幂等重放、顾客端流程、后台列表筛选 / 导出 / 操作。"""
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

import member_card
import wallet
from luzback import AppError, ErrorCode, create_token, get_settings, utcnow
from luzback.db import get_session_factory
from luzback.testing import make_test_app
from member_card import api


def user_decoder(token):
    if not token.startswith("user-"):
        raise AppError(ErrorCode.UNAUTHORIZED, "bad token")
    return int(token[5:])


@pytest.fixture()
def client(tmp_path):
    wallet.set_user_token_decoder(user_decoder)
    wallet.set_grants_resolver(lambda db, sid, claims: (frozenset(claims.get("perms", [])),
                                                         tuple(claims["stores"]) if claims.get("stores") else None))
    app = make_test_app(api.routers, wallet, member_card, url=f"sqlite:///{tmp_path / 'mc-api.db'}")
    with TestClient(app) as c:
        yield c
    wallet.set_user_token_decoder(None)
    wallet.set_grants_resolver(None)


def staff(perms=("member_card:*",), stores=None):
    s = get_settings()
    token = create_token("7", s.jwt_secret, 600, kind="admin", extra={"perms": list(perms), "stores": stores},
                         issuer=s.jwt_issuer)
    return {"Authorization": f"Bearer {token}"}


def user(uid=1):
    return {"Authorization": f"Bearer user-{uid}"}


def key():
    return {"Idempotency-Key": str(uuid.uuid4())}


def make_product(client, **kw):
    body = {"name": "月卡", "kind": "period", "price_cents": 30000, "period_days": 30, "store_scope": "multi",
            "store_ids": [5, 6]} | kw
    r = client.post("/api/v1/admin/member-cards/products", json=body, headers=staff() | key()).json()
    assert r["code"] == 0, r
    pid = r["data"]["id"]
    assert client.post(f"/api/v1/admin/member-cards/products/{pid}/on-sale", headers=staff() | key()).json()["code"] == 0
    return pid


def purchase(client, pid, uid=1):
    order = client.post("/api/v1/app/member-cards/orders", json={"product_id": pid, "store_id": 5},
                        headers=user(uid) | key()).json()["data"]
    with get_session_factory()() as db:
        member_card.handle_event(db, "payment.succeeded", {"biz_type": "member_card", "biz_id": order["id"],
                                                           "amount_cents": order["amount_cents"]}, f"ev{order['id']}")
    cards = client.get("/api/v1/app/member-cards/cards", headers=user(uid)).json()["data"]["items"]
    return cards[0]


def test_auth_permission_validation_envelope(client):
    assert client.get("/api/v1/app/member-cards/cards").json()["code"] == 4010
    assert client.get("/api/v1/admin/member-cards").json()["code"] == 4010
    r = client.get("/api/v1/admin/member-cards", headers=staff(perms=("wallet:read",)))
    assert r.status_code == 403 and r.json()["code"] == 4030
    r = client.post("/api/v1/admin/member-cards/products", json={"name": "x", "kind": "bogus", "price_cents": 1},
                    headers=staff() | key()).json()
    assert r["code"] == 4001 and r["data"]["errors"]
    assert client.post("/api/v1/admin/member-cards/products", json={"name": "x", "kind": "period", "price_cents": 1,
                                                                    "period_days": 1, "store_scope": "all"},
                       headers=staff()).json()["code"] == 4001  # 缺幂等键
    body = client.get("/api/v1/app/member-cards/cards", headers=user()).json()
    assert body["code"] == 0 and body["message"] == "ok" and body["data"]["items"] == []


def test_app_flow(client):
    pid = make_product(client, transfer_fee_cents=0)
    listed = client.get("/api/v1/app/member-cards/products?store_id=5", headers=user()).json()["data"]
    assert [p["id"] for p in listed["items"]] == [pid] and listed["items"][0]["goods_kind"] == "offline_service"
    assert client.get("/api/v1/app/member-cards/products?store_id=8", headers=user()).json()["data"]["items"] == []
    headers = user() | key()
    first = client.post("/api/v1/app/member-cards/orders", json={"product_id": pid, "store_id": 5}, headers=headers).json()
    assert first == client.post("/api/v1/app/member-cards/orders", json={"product_id": pid, "store_id": 5},
                                headers=headers).json()
    card = purchase(client, pid)
    assert card["status"] == "active" and card["remaining_days"] == 30
    assert client.get(f"/api/v1/app/member-cards/cards/{card['id']}", headers=user(2)).json()["code"] == 4040
    detail = client.get(f"/api/v1/app/member-cards/cards/{card['id']}", headers=user()).json()["data"]
    assert detail["card_no"].startswith("MC")
    ledger = client.get(f"/api/v1/app/member-cards/cards/{card['id']}/ledger", headers=user()).json()["data"]
    assert ledger["items"][0]["kind"] == "issue"
    r = client.post(f"/api/v1/app/member-cards/cards/{card['id']}/freeze", json={"days": 3}, headers=user() | key()).json()
    assert r["data"]["reason"] == "freeze_days_out_of_range" and r["code"] == 4220
    r = client.post(f"/api/v1/app/member-cards/cards/{card['id']}/freeze", json={"days": 7}, headers=user() | key()).json()
    assert r["data"]["card"]["status"] == "frozen"
    r = client.post(f"/api/v1/app/member-cards/cards/{card['id']}/unfreeze", headers=user() | key()).json()
    assert r["data"]["card"]["status"] == "active"
    quote = client.get(f"/api/v1/app/member-cards/cards/{card['id']}/refund-quote", headers=user()).json()["data"]
    assert quote["rule"] == "cooling_off_full" and quote["refund_cents"] == 30000
    r = client.post(f"/api/v1/app/member-cards/cards/{card['id']}/transfer", json={"to_member_id": 2, "to_phone": "13800000002"},
                    headers=user() | key()).json()
    assert r["code"] == 4001
    r = client.post(f"/api/v1/app/member-cards/cards/{card['id']}/transfer", json={"to_phone": "13800000002"},
                    headers=user() | key()).json()
    assert r["data"]["status"] == "transferred"
    assert client.get("/api/v1/app/member-cards/cards", headers=user(2)).json()["data"]["items"][0]["id"] == card["id"]


def test_app_refund_request_and_admin_approval(client):
    pid = make_product(client)
    card = purchase(client, pid)
    r = client.post(f"/api/v1/app/member-cards/cards/{card['id']}/refund-request", json={"reason": "不想练了"},
                    headers=user() | key()).json()
    assert r["data"]["status"] == "pending_approval"
    lst = client.get("/api/v1/admin/member-cards/refunds?status=pending_approval", headers=staff()).json()["data"]
    assert lst["total"] == 1
    r = client.post(f"/api/v1/admin/member-cards/refunds/{r['data']['id']}/approve", headers=staff() | key()).json()
    assert r["data"]["status"] == "processing"


def test_admin_list_filters_export_and_actions(client):
    pid = make_product(client, period_days=5)
    card = purchase(client, pid)
    other = purchase(client, make_product(client, name="长卡", period_days=90), uid=2)
    lst = client.get("/api/v1/admin/member-cards?expiring_within_days=7", headers=staff()).json()["data"]
    assert [c["id"] for c in lst["items"]] == [card["id"]]
    assert client.get("/api/v1/admin/member-cards?keyword=会员2", headers=staff()).json()["data"]["items"][0]["id"] == other["id"]
    assert client.get("/api/v1/admin/member-cards?phone=13800000001", headers=staff()).json()["data"]["total"] == 1
    assert client.get("/api/v1/admin/member-cards?status=weird", headers=staff()).json()["code"] == 4001
    assert client.get("/api/v1/admin/member-cards?nope=1", headers=staff()).json()["code"] == 4001
    csv = client.get("/api/v1/admin/member-cards/export?status=active", headers=staff())
    assert csv.status_code == 200 and csv.content.startswith("﻿".encode()) and "138****0001" in csv.text
    assert "13800000001" not in csv.text
    detail = client.get(f"/api/v1/admin/member-cards/{card['id']}", headers=staff()).json()["data"]
    assert detail["ledger"][0]["kind"] == "issue"
    r = client.post(f"/api/v1/admin/member-cards/{card['id']}/consume", json={"store_id": 5, "ref": "gate-1"},
                    headers=staff() | key()).json()
    assert r["data"]["ref"] == "gate-1"
    r = client.post("/api/v1/admin/member-cards/consume-by-no", json={"store_id": 8, "ref": "gate-2",
                                                                      "card_no": detail["card_no"]},
                    headers=staff() | key()).json()
    assert r["data"]["reason"] == "store_not_applicable"
    r = client.post("/api/v1/admin/member-cards/usages/reverse", json={"ref": "gate-1", "reason": "闸机故障"},
                    headers=staff() | key()).json()
    assert r["data"]["status"] == "reversed"
    r = client.post(f"/api/v1/admin/member-cards/{card['id']}/adjust", json={"delta_days": 3, "reason": "补偿"},
                    headers=staff() | key()).json()
    assert r["data"]["remaining_days"] == 8
    for action in ("report-lost", "replace"):
        r = client.post(f"/api/v1/admin/member-cards/{card['id']}/{action}", json={"reason": "丢卡"},
                        headers=staff() | key()).json()
        assert r["code"] == 0, r
    assert r["data"]["card_no"] != detail["card_no"]
    quote = client.get(f"/api/v1/admin/member-cards/{card['id']}/refund-quote?cause=merchant", headers=staff()).json()
    assert quote["data"]["rule"] in ("cooling_off_full", "merchant_discounted")
    r = client.post(f"/api/v1/admin/member-cards/{card['id']}/refunds", json={"cause": "merchant", "reason": "闭店"},
                    headers=staff() | key()).json()
    assert r["data"]["status"] == "processing"
    r = client.post("/api/v1/admin/member-cards/issue", json={"product_id": pid, "user_id": 3, "source": "gift",
                                                              "reason": "活动"}, headers=staff() | key()).json()
    assert r["data"]["source"] == "gift"
    assert client.get("/api/v1/admin/member-cards/orders?kind=purchase", headers=staff()).json()["data"]["total"] == 2


def test_store_scope_hides_cards(client):
    pid = make_product(client, store_scope="single", store_ids=[5])
    card = purchase(client, pid)
    assert client.get(f"/api/v1/admin/member-cards/{card['id']}", headers=staff(stores=[6])).json()["code"] == 4040
    assert client.get("/api/v1/admin/member-cards", headers=staff(stores=[6])).json()["data"]["total"] == 0
    r = client.post("/api/v1/admin/member-cards/products", json={"name": "x", "kind": "period", "price_cents": 100,
                                                                 "period_days": 3, "store_id": 5},
                    headers=staff(stores=[6]) | key())
    assert r.json()["code"] == 4031
