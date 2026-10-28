"""HTTP：顾客端支付 / 查询、回调应答格式、后台列表筛选 / 导出 / 权限 / 门店范围 / 退款 / 对账，信封与错误码。"""
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

import wechat_pay.models
from luzback import AppError, ErrorCode, create_token, get_settings
from luzback.db import get_session_factory
from luzback.testing import make_test_app
from wechat_pay import api, service
from wechat_pay.models import PaymentRecord
from wechat_pay.testing import wechat_pay_kit

BT = "test_order"
PERMS = {1: {"payment:read", "payment:refund", "payment:reconcile"}, 2: {"payment:read"}, 3: set(), 4: {"payment:read"}}
SCOPES = {4: [2]}


def authorizer(db, claims, code):
    staff_id = int(claims["sub"])
    if code not in PERMS.get(staff_id, set()):
        raise AppError(ErrorCode.FORBIDDEN, f"permission required: {code}")
    return api.StaffContext(id=staff_id, store_ids=SCOPES.get(staff_id))


def staff(staff_id):
    s = get_settings()
    token = create_token(str(staff_id), s.jwt_secret, 600, kind="admin", extra={"role": "staff", "rv": 0}, issuer=s.jwt_issuer)
    return {"Authorization": f"Bearer {token}"}


def user(user_id, key=None):
    headers = {"X-Test-User": str(user_id)}
    if key:
        headers["Idempotency-Key"] = key
    return headers


@pytest.fixture()
def env():
    with wechat_pay_kit() as kit:
        kit.clock.now = datetime.now(timezone.utc)  # 接口内部用真实时间验签
        app = make_test_app([api.router], wechat_pay.models)
        app.dependency_overrides[api.current_user] = _test_user
        api.set_staff_authorizer(authorizer)
        kit.client = TestClient(app)
        kit.db = get_session_factory()()
        kit.goods = {}

        def resolve(db, user_id, business_id):
            if business_id == 404 or user_id == 99:
                raise AppError(ErrorCode.NOT_FOUND, "order not found")
            return service.Payable(amount_cents=1000 * business_id, description=f"订单{business_id}",
                                   expires_at=datetime.now(timezone.utc) + timedelta(minutes=15), store_id=1,
                                   goods_kind=kit.goods.get(business_id, "physical"), business_no=f"NO{business_id}")

        service.register_payable(BT, resolve)
        service.register_on_paid(BT, lambda db, info: None)
        yield kit
        kit.db.close()
        api.set_staff_authorizer(None)


def _test_user(request: Request) -> int:
    value = request.headers.get("X-Test-User")
    if not value:
        raise AppError(ErrorCode.UNAUTHORIZED, "missing bearer token")
    return int(value)


def pay(env, business_id=1, user_id=7, key=None):
    resp = env.client.post("/api/v1/app/payments", json={"business_type": BT, "business_id": business_id},
                           headers=user(user_id, key or f"pay-key-{business_id}-{user_id}"))
    return resp


def paid(env, business_id=1, store_id=1):
    data = pay(env, business_id).json()["data"]
    env.db.query(PaymentRecord).filter(PaymentRecord.id == data["payment_id"]).update({"store_id": store_id})
    env.db.commit()
    env.fake.pay(data["out_trade_no"])
    headers, body = env.fake.payment_notification(data["out_trade_no"])
    assert env.client.post("/api/v1/webhooks/wechat/pay", content=body, headers=headers).status_code == 204
    return data


# ---------------------------------------------------------------- 顾客端


def test_app_requires_login_and_idempotency_key(env):
    app = make_test_app([api.router], wechat_pay.models)
    resp = TestClient(app).post("/api/v1/app/payments", json={"business_type": BT, "business_id": 1})
    assert resp.status_code == 401 and resp.json()["code"] == 4010
    resp = env.client.post("/api/v1/app/payments", json={"business_type": BT, "business_id": 1}, headers=user(7))
    assert resp.status_code == 400 and resp.json()["code"] == 4001


def test_app_pay_envelope_and_idempotent_replay(env):
    first = pay(env, key="same-key-123")
    body = first.json()
    assert first.status_code == 200 and body["code"] == 0 and body["message"] == "ok"
    data = body["data"]
    assert data["amount_cents"] == 1000 and data["pay_params"]["signType"] == "RSA" and data["status"] == "paying"
    replay = pay(env, key="same-key-123").json()["data"]
    assert replay == data and env.fake.count("jsapi_prepay") == 1
    mismatch = env.client.post("/api/v1/app/payments", json={"business_type": BT, "business_id": 2},
                               headers=user(7, "same-key-123"))
    assert mismatch.json()["code"] == 4093


def test_app_pay_validation_and_rules(env):
    bad = env.client.post("/api/v1/app/payments", json={"business_type": "Bad Type", "business_id": 0},
                          headers=user(7, "validation-1"))
    assert bad.status_code == 400 and bad.json()["code"] == 4001
    assert pay(env, business_id=404).json()["code"] == 4040
    env.goods[3] = "virtual_content"
    resp = pay(env, business_id=3)
    assert resp.status_code == 422 and resp.json()["code"] == 4220
    assert resp.json()["data"]["reason"] == "virtual_goods_require_virtual_payment"
    unknown = env.client.post("/api/v1/app/payments", json={"business_type": "nothing", "business_id": 1},
                              headers=user(7, "unknown-type-1"))
    assert unknown.json()["code"] == 4040


def test_app_pay_upstream_failure_envelope(env):
    env.fake.fail_next("jsapi_prepay", "SYSTEM_ERROR")
    resp = pay(env)
    assert resp.status_code == 502 and resp.json()["code"] == 5020 and resp.json()["data"]["payment_unavailable"]


def test_app_payment_status_and_ownership(env):
    data = pay(env).json()["data"]
    other = env.client.get(f"/api/v1/app/payments/{data['out_trade_no']}", headers=user(8))
    assert other.status_code == 404 and other.json()["code"] == 4040
    env.fake.pay(data["out_trade_no"])
    mine = env.client.get(f"/api/v1/app/payments/{data['out_trade_no']}", headers=user(7)).json()["data"]
    assert mine["status"] == "paid"


# ---------------------------------------------------------------- 回调


def test_notify_endpoints_reply_in_channel_format(env):
    data = pay(env).json()["data"]
    env.fake.pay(data["out_trade_no"])
    headers, body = env.fake.payment_notification(data["out_trade_no"], tamper=True)
    bad = env.client.post("/api/v1/webhooks/wechat/pay", content=body, headers=headers)
    assert bad.status_code == 401 and bad.json() == {"code": "FAIL", "message": "签名验证失败"}
    headers, body = env.fake.payment_notification(data["out_trade_no"])
    good = env.client.post("/api/v1/webhooks/wechat/pay", content=body, headers=headers)
    assert good.status_code == 204 and good.content == b""
    legacy = env.client.post("/api/v1/payments/notify/wechat", content=body, headers=headers)
    assert legacy.status_code == 204  # 重复通知照样应答成功
    no_headers = env.client.post("/api/v1/webhooks/wechat/pay", content=body)
    assert no_headers.status_code == 401


def test_refund_notify_endpoint(env):
    data = paid(env)
    info = service.create_refund(env.db, data["payment_id"], 500, "r", "after_sales:1")
    env.fake.settle_refund(info.out_refund_no)
    headers, body = env.fake.refund_notification(info.out_refund_no)
    assert env.client.post("/api/v1/webhooks/wechat/refund", content=body, headers=headers).status_code == 204
    assert service.get_refund_status(env.db, info.refund_id).status == "succeeded"


# ---------------------------------------------------------------- 后台


def test_admin_auth_and_permissions(env):
    assert env.client.get("/api/v1/admin/payments").json()["code"] == 4010
    bad_token = env.client.get("/api/v1/admin/payments", headers={"Authorization": "Bearer nope"})
    assert bad_token.status_code == 401
    user_token = create_token("7", get_settings().jwt_secret, 600, kind="access", issuer=get_settings().jwt_issuer)
    assert env.client.get("/api/v1/admin/payments", headers={"Authorization": f"Bearer {user_token}"}).status_code == 401
    forbidden = env.client.get("/api/v1/admin/payments", headers=staff(3))
    assert forbidden.status_code == 403 and forbidden.json()["code"] == 4030
    refund = env.client.post("/api/v1/admin/payments/1/refunds", json={"amount_cents": 1, "reason": "x"},
                             headers={**staff(2), "Idempotency-Key": "admin-refund-1"})
    assert refund.json()["code"] == 4030


def test_admin_payment_list_filters_sort_keyword(env):
    first = paid(env, 1)
    paid(env, 2)
    pay(env, 3)
    h = staff(1)
    page = env.client.get("/api/v1/admin/payments", headers=h).json()["data"]
    assert page["total"] == 3 and page["page"] == 1 and page["page_size"] == 20
    assert {i["status"] for i in page["items"]} == {"paid", "paying"}
    assert env.client.get("/api/v1/admin/payments?status=paid", headers=h).json()["data"]["total"] == 2
    assert env.client.get(f"/api/v1/admin/payments?business_type={BT}&amount_cents_min=2000",
                          headers=h).json()["data"]["total"] == 2
    keyword = env.client.get(f"/api/v1/admin/payments?keyword={first['out_trade_no'][-8:]}", headers=h).json()["data"]
    assert [i["out_trade_no"] for i in keyword["items"]] == [first["out_trade_no"]]
    assert keyword["items"][0]["payer_openid"] == "****"  # 短 openid 全部掩码
    ordered = env.client.get("/api/v1/admin/payments?sort=amount_cents", headers=h).json()["data"]["items"]
    assert [i["amount_cents"] for i in ordered] == [1000, 2000, 3000]
    today = datetime.now(timezone.utc).date()
    ranged = env.client.get(f"/api/v1/admin/payments?created_at_from={today - timedelta(days=1)}&created_at_to={today + timedelta(days=1)}",
                            headers=h).json()["data"]
    assert ranged["total"] == 3
    assert env.client.get("/api/v1/admin/payments?statuss=paid", headers=h).json()["code"] == 4001
    assert env.client.get("/api/v1/admin/payments?status=weird", headers=h).json()["code"] == 4001
    assert env.client.get("/api/v1/admin/payments?amount_cents_min=5&amount_cents_max=1", headers=h).json()["code"] == 4001
    assert env.client.get("/api/v1/admin/payments?needs_manual=false", headers=h).json()["data"]["total"] == 3


def test_admin_payment_export_csv(env):
    paid(env, 1)
    resp = env.client.get("/api/v1/admin/payments/export?status=paid", headers=staff(1))
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/csv")
    text = resp.content.decode("utf-8")
    assert text.startswith("﻿支付单ID,业务类型") and "10.00" in text and len(text.strip().splitlines()) == 2


def test_admin_store_scope(env):
    data = paid(env, 1, store_id=1)
    scoped = staff(4)
    assert env.client.get("/api/v1/admin/payments", headers=scoped).json()["data"]["total"] == 0
    assert env.client.get(f"/api/v1/admin/payments/{data['payment_id']}", headers=scoped).json()["code"] == 4040
    detail = env.client.get(f"/api/v1/admin/payments/{data['payment_id']}", headers=staff(1)).json()["data"]
    assert detail["out_trade_no"] == data["out_trade_no"] and detail["notify_logs"][0]["result"] == "processed"


def test_admin_refund_flow_and_lists(env):
    data = paid(env, 2)
    h = {**staff(1), "Idempotency-Key": "admin-refund-ok-1"}
    bad = env.client.post(f"/api/v1/admin/payments/{data['payment_id']}/refunds", json={"amount_cents": 0, "reason": "x"}, headers=h)
    assert bad.json()["code"] == 4001
    created = env.client.post(f"/api/v1/admin/payments/{data['payment_id']}/refunds",
                              json={"amount_cents": 500, "reason": "客户要求"}, headers=h).json()
    assert created["code"] == 0 and created["data"]["status"] == "processing"
    assert created["data"]["requested_by"] == "staff:1"
    again = env.client.post(f"/api/v1/admin/payments/{data['payment_id']}/refunds",
                            json={"amount_cents": 500, "reason": "客户要求"}, headers=h).json()
    assert again["data"]["id"] == created["data"]["id"]  # 同键重放
    too_soon = env.client.post(f"/api/v1/admin/payments/{data['payment_id']}/refunds",
                               json={"amount_cents": 500, "reason": "再退"}, headers={**staff(1), "Idempotency-Key": "admin-refund-ok-2"})
    assert too_soon.status_code == 422 and too_soon.json()["data"]["reason"] == "refund_interval"
    listing = env.client.get(f"/api/v1/admin/refunds?keyword={data['out_trade_no']}", headers=staff(2)).json()["data"]
    assert listing["total"] == 1 and listing["items"][0]["out_trade_no"] == data["out_trade_no"]
    assert env.client.get("/api/v1/admin/refunds?status=processing&amount_cents_max=500", headers=staff(2)).json()["data"]["total"] == 1
    detail = env.client.get(f"/api/v1/admin/refunds/{created['data']['id']}", headers=staff(2)).json()["data"]
    assert detail["payment"]["id"] == data["payment_id"]
    export = env.client.get("/api/v1/admin/refunds/export", headers=staff(2)).content.decode("utf-8")
    assert created["data"]["out_refund_no"] in export
    sync = env.client.post(f"/api/v1/admin/refunds/{created['data']['id']}/sync",
                           headers={**staff(1), "Idempotency-Key": "refund-sync-1"}).json()
    assert sync["data"]["status"] == "processing"


def test_admin_sync_and_close_payment(env):
    data = pay(env, 5).json()["data"]
    env.fake.pay(data["out_trade_no"])
    synced = env.client.post(f"/api/v1/admin/payments/{data['payment_id']}/sync",
                             headers={**staff(1), "Idempotency-Key": "sync-1-abc"}).json()["data"]
    assert synced["status"] == "paid"
    other = pay(env, 6).json()["data"]
    closed = env.client.post(f"/api/v1/admin/payments/{other['payment_id']}/close",
                             headers={**staff(1), "Idempotency-Key": "close-1-abc"}).json()["data"]
    assert closed["status"] == "closed"
    assert env.client.post(f"/api/v1/admin/payments/{other['payment_id']}/close",
                           headers={**staff(2), "Idempotency-Key": "close-2-abc"}).json()["code"] == 4030


def test_admin_reconciliation_endpoints(env):
    paid(env, 1)
    yesterday = date.today() - timedelta(days=1)
    from wechat_pay.models import ReconBatch, ReconDiff

    batch = ReconBatch(bill_date=yesterday, status="done", diff_count=1)
    env.db.add(batch)
    env.db.flush()
    env.db.add(ReconDiff(batch_id=batch.id, bill_date=yesterday, diff_type="missing_remote", ref_type="payment",
                         ref_no="PXYZ", out_trade_no="PXYZ", local_amount_cents=100))
    env.db.commit()
    h = staff(1)
    assert env.client.get("/api/v1/admin/reconciliation/batches?status=done", headers=h).json()["data"]["total"] == 1
    diffs = env.client.get("/api/v1/admin/reconciliation/diffs?diff_type=missing_remote&resolved=false", headers=h).json()["data"]
    assert diffs["total"] == 1
    diff_id = diffs["items"][0]["id"]
    export = env.client.get("/api/v1/admin/reconciliation/diffs/export?keyword=PXYZ", headers=h).content.decode("utf-8")
    assert "PXYZ" in export and "1.00" in export
    assert env.client.get("/api/v1/admin/reconciliation/diffs/export", headers=staff(2)).json()["code"] == 4030
    resolved = env.client.post(f"/api/v1/admin/reconciliation/diffs/{diff_id}/resolve", json={"note": "已线下核实"},
                               headers={**h, "Idempotency-Key": "resolve-1-abc"}).json()["data"]
    assert resolved["resolved"] is True and resolved["resolved_by"] == "staff:1"
    run_today = env.client.post("/api/v1/admin/reconciliation/run", json={"bill_date": date.today().isoformat()},
                                headers={**h, "Idempotency-Key": "recon-run-1"})
    assert run_today.status_code == 422 and run_today.json()["code"] == 4220


def test_admin_shipping_list(env):
    data = paid(env, 1)
    service.request_shipping_upload(env.db, data["payment_id"], "储值卡")
    listing = env.client.get("/api/v1/admin/payment-shipping?status=uploaded", headers=staff(2)).json()["data"]
    assert listing["total"] == 1 and listing["items"][0]["out_trade_no"] == data["out_trade_no"]


def test_permissions_declared():
    assert set(service.PERMISSIONS) == {"payment:read", "payment:refund", "payment:reconcile"}


def test_verify_failure_increments_metric(env):
    data = pay(env).json()["data"]
    env.fake.pay(data["out_trade_no"])
    headers, body = env.fake.payment_notification(data["out_trade_no"], signature="WECHATPAY/SIGNTEST/probe")
    assert env.client.post("/api/v1/webhooks/wechat/pay", content=body, headers=headers).status_code == 401
    headers, body = env.fake.payment_notification(data["out_trade_no"], tamper=True)
    env.client.post("/api/v1/webhooks/wechat/refund", content=body, headers=headers)
    assert env.client.app.state.metrics.counters[api.VERIFY_FAILED_METRIC] == 2
