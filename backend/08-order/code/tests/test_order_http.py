"""HTTP：鉴权 4010、权限 4030、参数 4001、幂等重放 / 指纹 4093、信封、我的订单页签与游标、后台列表筛选 / 关键词 / 导出 / 范围。"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import admin_rbac
import order
import order.api
from identity.testing import member_tokens
from luzback import AuditLog, create_app
from order.testing import add_sku, as_staff, order_db, staff
from wechat_pay.testing import wechat_pay_kit

ITEMS = [{"kind": "goods", "sku_id": 1, "qty": 2}]


@pytest.fixture()
def http():
    order.reset_config()
    db = order_db("admin_rbac")
    order.set_buyer_contact_provider(lambda _db, uid: "13800001111" if uid == 1 else None)
    with wechat_pay_kit() as kit:
        from luzback import utcnow
        kit.clock.now = utcnow()
        order.install_payment_hooks()
        add_sku(db, 1, 5000, stock=20, name="咖啡豆")
        app = create_app(order.api.routers)
        client = TestClient(app)
        me = member_tokens(db)
        other = member_tokens(db)
        yield {"db": db, "kit": kit, "app": app, "client": client,
               "me": {"Authorization": f"Bearer {me['access_token']}"}, "me_id": me["user_id"],
               "other": {"Authorization": f"Bearer {other['access_token']}"}}
    order.set_buyer_contact_provider(None)
    db.close()


def key(value):
    return {"Idempotency-Key": value}


def place(h, headers=None, items=ITEMS, idem="submit-key-0001"):
    c = h["client"]
    headers = headers or h["me"]
    q = c.post("/api/v1/app/orders/preview", json={"items": items}, headers=headers)
    assert q.status_code == 200, q.text
    r = c.post("/api/v1/app/orders", json={"quote_token": q.json()["data"]["quote_token"], "terms_accepted": True},
               headers={**headers, **key(idem)})
    assert r.status_code == 200, r.text
    return r.json()["data"]


def test_auth_validation_and_envelope(http):
    c = http["client"]
    r = c.post("/api/v1/app/orders/preview", json={"items": ITEMS})
    assert r.status_code == 401 and r.json()["code"] == 4010
    assert c.get("/api/v1/app/orders").json()["code"] == 4010
    r = c.post("/api/v1/app/orders/preview", json={"items": [{"kind": "goods", "sku_id": 1, "qty": 0}]}, headers=http["me"])
    assert r.status_code == 400 and r.json()["code"] == 4001
    r = c.post("/api/v1/app/orders/preview", json={"items": ITEMS, "price_cents": 1}, headers=http["me"])
    assert r.status_code == 200 and r.json()["data"]["total_cents"] == 10000  # 客户端价格字段被忽略
    assert set(r.json()) == {"code", "message", "data"} and r.json()["code"] == 0


def test_submit_idempotency(http):
    c = http["client"]
    q = c.post("/api/v1/app/orders/preview", json={"items": ITEMS}, headers=http["me"]).json()["data"]
    body = {"quote_token": q["quote_token"], "terms_accepted": True}
    assert c.post("/api/v1/app/orders", json=body, headers=http["me"]).json()["code"] == 4001  # 缺键
    assert c.post("/api/v1/app/orders", json=body, headers={**http["me"], **key("short")}).json()["code"] == 4001
    first = c.post("/api/v1/app/orders", json=body, headers={**http["me"], **key("order-key-0001")})
    again = c.post("/api/v1/app/orders", json=body, headers={**http["me"], **key("order-key-0001")})
    assert first.json() == again.json() and first.json()["data"]["status"] == "pending_payment"
    other_body = {**body, "remark": "不同请求体"}
    r = c.post("/api/v1/app/orders", json=other_body, headers={**http["me"], **key("order-key-0001")})
    assert r.json()["code"] == 4093
    db = http["db"]
    db.expire_all()
    assert len(db.execute(select(order.Order)).scalars().all()) == 1


def test_my_orders_tabs_cursor_detail_cancel_pay(http):
    c = http["client"]
    ids = [place(http, idem=f"submit-key-{i:04d}")["order_id"] for i in range(3)]
    r = c.get("/api/v1/app/orders", params={"limit": 2, "tab": "unpaid"}, headers=http["me"]).json()["data"]
    assert [o["id"] for o in r["items"]] == ids[::-1][:2] and r["has_more"] and r["counts"]["unpaid"] == 3
    nxt = c.get("/api/v1/app/orders", params={"limit": 2, "tab": "unpaid", "cursor": r["next_cursor"]},
                headers=http["me"]).json()["data"]
    assert [o["id"] for o in nxt["items"]] == [ids[0]] and not nxt["has_more"]
    assert c.get("/api/v1/app/orders", params={"tab": "bogus"}, headers=http["me"]).json()["code"] == 4001
    assert c.get("/api/v1/app/orders", params={"status": "paid"}, headers=http["me"]).json()["code"] == 4001
    assert c.get(f"/api/v1/app/orders/{ids[0]}", headers=http["other"]).json()["code"] == 4040
    d = c.get(f"/api/v1/app/orders/{ids[0]}", headers=http["me"]).json()["data"]
    assert d["can_cancel"] and d["items"][0]["qty"] == 2 and d["timeline"][0]["to"] == "pending_payment"
    r = c.post(f"/api/v1/app/orders/{ids[0]}/cancel", json={"reason": "不要了"}, headers={**http["me"], **key("cancel-0001")})
    assert r.json()["data"]["status"] == "cancelled"
    pay = c.post(f"/api/v1/app/orders/{ids[1]}/payments", json={"method": "wechat"},
                 headers={**http["me"], **key("pay-key-0001")}).json()["data"]
    assert pay["pay_params"]["signType"] == "RSA"
    import wechat_pay
    http["kit"].fake.pay(pay["out_trade_no"])
    headers, body = http["kit"].fake.payment_notification(pay["out_trade_no"])
    wechat_pay.handle_pay_notify(http["db"], headers, body)
    counts = c.get("/api/v1/app/orders", headers=http["me"]).json()["data"]["counts"]
    assert counts == {**counts, "unpaid": 1, "to_fulfil": 1, "closed": 1, "all": 3}
    r = c.post(f"/api/v1/app/orders/{ids[1]}/confirm-receipt", headers={**http["me"], **key("confirm-0001")})
    assert r.json()["code"] == 4090


def test_admin_permissions_filters_export_scope(http):
    c, app = http["client"], http["app"]
    a = place(http, idem="submit-key-a001")
    place(http, headers=http["other"], idem="submit-key-b001")
    assert c.get("/api/v1/admin/orders").json()["code"] == 4010
    as_staff(app, staff(["order:remark"]))
    assert c.get("/api/v1/admin/orders").json()["code"] == 4030
    as_staff(app, staff(["order:read", "order:export", "order:remark", "order:close", "order:fulfil"]))
    page = c.get("/api/v1/admin/orders", params={"status": "pending_payment", "sort": "-total_cents"}).json()["data"]
    assert page["total"] == 2 and {o["buyer_phone"] for o in page["items"]} == {"138****1111", None}
    assert c.get("/api/v1/admin/orders", params={"keyword": "13800001111"}).json()["data"]["total"] == 1
    assert c.get("/api/v1/admin/orders", params={"keyword": "****1111"}).json()["data"]["total"] == 1
    assert c.get("/api/v1/admin/orders", params={"keyword": a["order_no"]}).json()["data"]["items"][0]["id"] == a["order_id"]
    assert c.get("/api/v1/admin/orders", params={"total_cents_min": 1, "total_cents_max": 9999}).json()["data"]["total"] == 0
    assert c.get("/api/v1/admin/orders", params={"bogus": 1}).json()["code"] == 4001
    assert c.get("/api/v1/admin/orders", params={"status": "nope"}).json()["code"] == 4001
    csv = c.get("/api/v1/admin/orders/export", params={"status": "pending_payment"})
    assert csv.status_code == 200 and csv.text.lstrip("﻿").startswith("订单ID,订单号") and "138****1111" in csv.text
    db = http["db"]
    db.expire_all()
    assert db.scalar(select(AuditLog.action).where(AuditLog.action == "order.export")) == "order.export"
    r = c.post(f"/api/v1/admin/orders/{a['order_id']}/notes", json={"text": "客户来电"}, headers=key("note-key-0001"))
    assert r.json()["data"]["text"] == "客户来电"
    detail = c.get(f"/api/v1/admin/orders/{a['order_id']}").json()["data"]
    assert detail["notes"][0]["text"] == "客户来电" and detail["user_id"] == http["me_id"]
    r = c.post(f"/api/v1/admin/orders/{a['order_id']}/ship", json={"express_company": "顺丰", "tracking_no": "SF0001"},
               headers=key("ship-key-0001"))
    assert r.json()["code"] == 4090  # 未支付不能发货
    r = c.post(f"/api/v1/admin/orders/{a['order_id']}/close", json={"reason": "缺货关闭"}, headers=key("close-key-0001"))
    assert r.json()["data"]["status"] == "cancelled"
    as_staff(app, staff(["order:read"], store_ids=(9,)))
    assert c.get(f"/api/v1/admin/orders/{a['order_id']}").json()["code"] == 4040
    assert c.get("/api/v1/admin/orders").json()["data"]["total"] == 0
    codes = {p.code: p.description for p in admin_rbac.registered()}
    assert codes["order:read"] and codes["order:export"] and codes["order:close"]
