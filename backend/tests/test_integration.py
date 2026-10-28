"""端到端集成测试：通过 HTTP 走通交易主链路。

登录 → 创建订单（幂等）→ 重放幂等 → 发起支付 → 签名回调 → 重复回调 → 查询状态 → 已支付不能取消。
同时检查：未登录拒绝、库存只扣一次。
"""
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

import address.api
import identity.api
import inventory
import coupon.api
import membership.api
import order.api
import wechat_pay.api
import review.api
from identity.api import set_code2session
from inventory import Sku
from luzback import Base, create_app, init_engine
from luzback.db import get_session_factory
from wechat_pay import handle_notify  # noqa: F401  (确保模块导入)

NOTIFY_SECRET = "dev-notify-secret"


@pytest.fixture()
def client(tmp_path):
    engine = init_engine(f"sqlite:///{tmp_path / 'flow.db'}")
    Base.metadata.create_all(engine)
    with get_session_factory()() as s:
        s.add(Sku(id=1, name="剁椒鱼头", price_cents=8800, stock=5))
        s.commit()
    set_code2session(lambda code: {"openid": f"openid-{code}"})
    app = create_app([
        identity.api.router, order.api.router, wechat_pay.api.router, address.api.router,
        membership.api.router, coupon.api.router, review.api.router,
    ])
    yield TestClient(app)
    engine.dispose()


def login(client, code="u1"):
    resp = client.post("/api/v1/app/auth/login", json={"code": code})
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["access_token"]


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def stock_of(sku_id=1):
    with get_session_factory()() as s:
        return s.get(Sku, sku_id).stock


def test_full_order_flow(client):
    token = login(client)

    # 1 创建订单（带幂等键）
    body = {"items": [{"sku_id": 1, "qty": 2}]}
    first = client.post("/api/v1/app/orders", json=body, headers={**auth(token), "Idempotency-Key": "order-k-100"}).json()
    assert first["code"] == 0
    order_id = first["data"]["order_id"]
    assert first["data"]["total_cents"] == 8800 * 2
    assert stock_of() == 3

    # 2 同一幂等键重放：返回同一订单，库存不再扣减
    replay = client.post("/api/v1/app/orders", json=body, headers={**auth(token), "Idempotency-Key": "order-k-100"}).json()
    assert replay["data"]["order_id"] == order_id
    assert stock_of() == 3

    # 3 发起支付
    pay = client.post(f"/api/v1/app/orders/{order_id}/pay", headers={**auth(token), "Idempotency-Key": "pay-0001"}).json()
    assert pay["code"] == 0
    out_trade_no = pay["data"]["out_trade_no"]
    amount = pay["data"]["amount_cents"]
    assert amount == 17600

    # 4 支付平台回调（签名正确）
    raw = json.dumps({"out_trade_no": out_trade_no, "transaction_id": "WX-9", "amount_cents": amount}).encode()
    sig = hmac.new(NOTIFY_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    notify = client.post("/api/v1/payments/notify/wechat", content=raw, headers={"Wechatpay-Signature": sig})
    assert notify.json()["message"] == "processed"

    # 5 重复回调：幂等
    again = client.post("/api/v1/payments/notify/wechat", content=raw, headers={"Wechatpay-Signature": sig})
    assert again.json()["message"] == "duplicate"

    # 6 查询订单与支付状态
    detail = client.get(f"/api/v1/app/orders/{order_id}", headers=auth(token)).json()["data"]
    assert detail["status"] == "paid"
    status = client.get(f"/api/v1/app/payments/{out_trade_no}", headers=auth(token)).json()["data"]
    assert status["status"] == "success" and status["order_status"] == "paid"

    # 7 已支付订单不能取消
    cancel = client.post(f"/api/v1/app/orders/{order_id}/cancel", headers=auth(token)).json()
    assert cancel["code"] == 4090


def test_unauthenticated_request_is_rejected(client):
    resp = client.post("/api/v1/app/orders", json={"items": [{"sku_id": 1, "qty": 1}]})
    assert resp.status_code == 401
    assert resp.json()["code"] == 4010


def test_other_users_order_is_not_found(client):
    owner = login(client, "owner")
    other = login(client, "other")
    created = client.post("/api/v1/app/orders", json={"items": [{"sku_id": 1, "qty": 1}]},
                          headers={**auth(owner), "Idempotency-Key": "own-0001"}).json()["data"]
    resp = client.get(f"/api/v1/app/orders/{created['order_id']}", headers=auth(other))
    assert resp.json()["code"] == 4040


def test_oversell_rejected_over_http(client):
    token = login(client)
    ok_resp = client.post("/api/v1/app/orders", json={"items": [{"sku_id": 1, "qty": 5}]},
                          headers={**auth(token), "Idempotency-Key": "big-0001"}).json()
    assert ok_resp["code"] == 0
    fail = client.post("/api/v1/app/orders", json={"items": [{"sku_id": 1, "qty": 1}]},
                       headers={**auth(token), "Idempotency-Key": "big-0002"})
    assert fail.status_code == 409 and fail.json()["code"] == 4091
    assert stock_of() == 0


def test_address_crud_over_http(client):
    token = login(client)
    body = {"contact": "王", "phone": "13800000000", "province": "上海", "city": "上海",
            "district": "静安", "detail": "南京西路 1788 号", "house_no": "3F"}
    created = client.post("/api/v1/app/addresses", json=body, headers=auth(token)).json()["data"]
    assert created["is_default"] is True and created["phone"] == "138****0000"
    listing = client.get("/api/v1/app/addresses", headers=auth(token)).json()["data"]["items"]
    assert len(listing) == 1
