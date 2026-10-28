"""售后 HTTP：鉴权 / 权限 / 参数 / 幂等 / 信封、顾客端申请与列表、后台筛选导出、二次验证、职责分离、门店范围。"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import after_sales
import after_sales.api
import order
import wechat_pay
from identity.testing import member_tokens
from luzback import AuditLog, create_app, utcnow
from order.testing import add_sku, as_staff, order_db, staff
from wechat_pay.testing import wechat_pay_kit

ALL = ["after_sale:read", "after_sale:export", "after_sale:create", "after_sale:approve", "after_sale:receive",
       "after_sale:refund"]


@pytest.fixture()
def http():
    order.reset_config()
    after_sales.reset_config()
    db = order_db("after_sales", "admin_rbac")
    with wechat_pay_kit() as kit:
        kit.clock.now = utcnow()
        order.install_payment_hooks()
        add_sku(db, 1, 5000, stock=10)
        app = create_app(after_sales.api.routers)
        me = member_tokens(db)
        other = member_tokens(db)
        yield {"db": db, "kit": kit, "app": app, "client": TestClient(app), "user_id": me["user_id"],
               "me": {"Authorization": f"Bearer {me['access_token']}"},
               "other": {"Authorization": f"Bearer {other['access_token']}"}}
    db.close()


def key(value):
    return {"Idempotency-Key": value}


def paid_order(h, qty=2):
    db = h["db"]
    q = order.preview(db, h["user_id"], {"items": [{"kind": "goods", "sku_id": 1, "qty": qty}]})
    db.commit()
    placed = order.submit(db, h["user_id"], {"quote_token": q["quote_token"], "terms_accepted": True})
    db.commit()
    pay = order.pay(db, h["user_id"], placed["order_id"], "wechat")
    db.commit()
    h["kit"].fake.pay(pay["out_trade_no"])
    headers, body = h["kit"].fake.payment_notification(pay["out_trade_no"])
    assert wechat_pay.handle_pay_notify(db, headers, body).http_status == 204
    db.expire_all()
    return placed["order_id"], order.service._items(db, placed["order_id"])[0].id


def test_app_flow_and_errors(http):
    c = http["client"]
    order_id, item_id = paid_order(http)
    assert c.get(f"/api/v1/app/after-sales/orders/{order_id}/refundable").json()["code"] == 4010
    view = c.get(f"/api/v1/app/after-sales/orders/{order_id}/refundable", headers=http["me"]).json()["data"]
    assert view["lines"][0]["refundable_cents"] == 10000
    assert c.get(f"/api/v1/app/after-sales/orders/{order_id}/refundable", headers=http["other"]).json()["code"] == 4040
    body = {"order_id": order_id, "type": "refund_only", "reason_code": "quality", "reason_text": "破损",
            "evidence": [3], "lines": [{"item_id": item_id, "qty": 1}]}
    assert c.post("/api/v1/app/after-sales", json=body, headers=http["me"]).json()["code"] == 4001  # 缺幂等键
    bad = {**body, "type": "exchange"}
    assert c.post("/api/v1/app/after-sales", json=bad, headers={**http["me"], **key("apply-bad-01")}).json()["code"] == 4001
    first = c.post("/api/v1/app/after-sales", json=body, headers={**http["me"], **key("apply-key-01")}).json()
    again = c.post("/api/v1/app/after-sales", json=body, headers={**http["me"], **key("apply-key-01")}).json()
    assert first == again and first["data"]["amount_cents"] == 5000 and set(first) == {"code", "message", "data"}
    r = c.post("/api/v1/app/after-sales", json={**body, "reason_text": "x"}, headers={**http["me"], **key("apply-key-01")})
    assert r.json()["code"] == 4093
    case_id = first["data"]["id"]
    page = c.get("/api/v1/app/after-sales", params={"tab": "processing"}, headers=http["me"]).json()["data"]
    assert [x["id"] for x in page["items"]] == [case_id] and page["counts"]["processing"] == 1
    assert c.get("/api/v1/app/after-sales", params={"tab": "x"}, headers=http["me"]).json()["code"] == 4001
    assert c.get(f"/api/v1/app/after-sales/{case_id}", headers=http["other"]).json()["code"] == 4040
    assert c.get(f"/api/v1/app/after-sales/{case_id}", headers=http["me"]).json()["data"]["lines"][0]["qty"] == 1
    r = c.post(f"/api/v1/app/after-sales/{case_id}/return-shipment", json={"company": "顺丰", "tracking_no": "SF1"},
               headers={**http["me"], **key("ship-key-01")})
    assert r.json()["code"] == 4001  # 单号太短
    r = c.post(f"/api/v1/app/after-sales/{case_id}/return-shipment", json={"company": "顺丰", "tracking_no": "SF1234"},
               headers={**http["me"], **key("ship-key-02")})
    assert r.json()["code"] == 4090  # 仅退款不需要寄回
    r = c.post(f"/api/v1/app/after-sales/{case_id}/cancel", headers={**http["me"], **key("cancel-key-01")})
    assert r.json()["data"]["status"] == "cancelled"


def test_admin_review_with_fresh_auth_and_refund(http):
    c, app = http["client"], http["app"]
    order_id, item_id = paid_order(http)
    body = {"order_id": order_id, "type": "refund_only", "reason_code": "quality", "lines": [{"item_id": item_id, "qty": 1}]}
    case_id = c.post("/api/v1/app/after-sales", json=body, headers={**http["me"], **key("apply-key-02")}).json()["data"]["id"]
    assert c.get("/api/v1/admin/after-sales").json()["code"] == 4010
    as_staff(app, staff(["after_sale:read"]))
    assert c.post(f"/api/v1/admin/after-sales/{case_id}/approve", headers=key("approve-key-01")).json()["code"] == 4030
    as_staff(app, staff(ALL, fresh=False))
    r = c.post(f"/api/v1/admin/after-sales/{case_id}/approve", headers=key("approve-key-01"))
    assert r.status_code == 401 and r.json()["code"] == 4011  # 退款审批需近期二次验证
    as_staff(app, staff(ALL, staff_id=9))
    r = c.post(f"/api/v1/admin/after-sales/{case_id}/approve", json={"note": "同意"}, headers=key("approve-key-02")).json()
    assert r["data"]["status"] == "refunding" and r["data"]["out_refund_no"]  # 提交后立即原路退款
    page = c.get("/api/v1/admin/after-sales", params={"status": "refunding", "sort": "-amount_cents"}).json()["data"]
    assert page["total"] == 1 and page["items"][0]["id"] == case_id
    no = page["items"][0]["after_sale_no"]
    assert c.get("/api/v1/admin/after-sales", params={"keyword": no}).json()["data"]["total"] == 1
    assert c.get("/api/v1/admin/after-sales", params={"amount_cents_min": 6000}).json()["data"]["total"] == 0
    assert c.get("/api/v1/admin/after-sales", params={"oops": 1}).json()["code"] == 4001
    detail = c.get(f"/api/v1/admin/after-sales/{case_id}").json()["data"]
    assert detail["reviewed_by"] == 9 and detail["refund_id"]
    csv = c.get("/api/v1/admin/after-sales/export")
    assert csv.status_code == 200 and "售后单号" in csv.text and no in csv.text
    db = http["db"]
    db.expire_all()
    assert db.scalar(select(AuditLog.id).where(AuditLog.action == "after_sale.export"))
    assert c.post(f"/api/v1/admin/after-sales/{case_id}/reject", json={"reason": "x"}, headers=key("reject-key-01")).json()["code"] == 4001
    as_staff(app, staff(ALL, store_ids=(42,)))
    assert c.get(f"/api/v1/admin/after-sales/{case_id}").json()["code"] == 4040


def test_admin_create_on_behalf_enforces_separation(http):
    c, app = http["client"], http["app"]
    order_id, item_id = paid_order(http)
    as_staff(app, staff(ALL, staff_id=4))
    body = {"order_id": order_id, "type": "return_and_refund", "reason_code": "wrong_item",
            "lines": [{"item_id": item_id, "qty": 1}]}
    case = c.post("/api/v1/admin/after-sales", json=body, headers=key("admin-apply-01")).json()["data"]
    assert case["applied_by"] == "staff"
    r = c.post(f"/api/v1/admin/after-sales/{case['id']}/approve", headers=key("approve-key-03"))
    assert r.json()["code"] == 4030 and r.json()["data"]["reason"] == "separation_of_duties"
    as_staff(app, staff(ALL, staff_id=5))
    assert c.post(f"/api/v1/admin/after-sales/{case['id']}/approve",
                  headers=key("approve-key-04")).json()["data"]["status"] == "awaiting_return"
    r = c.post(f"/api/v1/app/after-sales/{case['id']}/return-shipment", json={"company": "圆通", "tracking_no": "YT123456"},
               headers={**http["me"], **key("ship-key-03")})
    assert r.json()["data"]["status"] == "return_shipped"
    r = c.post(f"/api/v1/admin/after-sales/{case['id']}/receive", headers=key("receive-key-01")).json()["data"]
    assert r["status"] == "refunding" and r["out_refund_no"]
    for path in ("retry-refund", "close"):
        resp = c.post(f"/api/v1/admin/after-sales/{case['id']}/{path}", json={"reason": "测试关闭"}, headers=key(f"{path}-key-1"))
        assert resp.json()["code"] == 4090
    resp = c.post(f"/api/v1/admin/after-sales/{case['id']}/manual-settle", json={"note": "线下结清说明"},
                  headers=key("settle-key-1"))
    assert resp.json()["code"] == 4090
    codes = {p.code for p in __import__("admin_rbac").registered()}
    assert set(ALL) <= codes
