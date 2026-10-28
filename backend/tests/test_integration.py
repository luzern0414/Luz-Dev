"""端到端集成测试：真实模块 + 假渠道，通过 HTTP 走通交易主链路。

会员登录（identity 测试工具）→ 试算（活动后用券）→ 带幂等键下单 → 微信支付（09 路由 + FakeWechatPay 签名回调）
→ 已支付、券核销、库存确认 → 申请售后 → 员工审批（二次验证）→ 退款回调 → 事件 worker（25-jobs 路由）→ 售后完成；
另一条：超时关单任务（先查单再关单）→ 库存与券释放。
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import address.api
import after_sales
import after_sales.api
import identity.api
import order
import order.api
import wechat_pay
import wechat_pay.api
from identity.testing import member_tokens
from jobs.runner import EventRouter, discover_subscriptions
from luzback import create_app, dispatch_pending, utcnow
from luzback.db import get_session_factory
from luzback.scheduler import InMemoryLock, run_job
from order.testing import add_sku, as_staff, coupon_status, give_coupon, order_db, sku_stock, staff
from wechat_pay.testing import wechat_pay_kit

PACKAGES = ["identity", "coupon", "wallet", "wechat_pay", "order", "after_sales"]


@pytest.fixture()
def env():
    order.reset_config()
    after_sales.reset_config()
    db = order_db("after_sales", "admin_rbac")
    with wechat_pay_kit() as kit:
        kit.clock.now = utcnow()  # 回调验签用真实时间（±5 分钟）
        order.install_payment_hooks()
        add_sku(db, 1, 8800, stock=5, name="剁椒鱼头")
        app = create_app([*identity.api.routers, *order.api.routers, *wechat_pay.api.routers, *after_sales.api.routers,
                          address.api.router])
        tokens = member_tokens(db)
        yield {"db": db, "kit": kit, "app": app, "client": TestClient(app), "user_id": tokens["user_id"],
               "auth": {"Authorization": f"Bearer {tokens['access_token']}"}}
    db.close()


def key(value: str) -> dict:
    assert len(value) >= 8
    return {"Idempotency-Key": value}


def worker_drain() -> list[str]:
    """与 25-jobs worker 相同的投递：发件箱 → 各模块 events.SUBSCRIPTIONS（每个订阅者独立会话、成功提交）。"""
    factory = get_session_factory()
    router = EventRouter(discover_subscriptions(PACKAGES), factory)
    seen: list[str] = []

    def handler(topic, payload, event=None):
        router(topic, payload, event)
        seen.append(topic)

    for _ in range(5):
        with factory() as s:
            counts = dispatch_pending(s, handler, now=utcnow() + timedelta(days=1), limit=500)
        assert counts["retried"] == 0 and counts["failed"] == 0, counts
        if counts["sent"] == 0:
            break
    return seen


def test_order_pay_refund_end_to_end(env):
    c, db, kit, auth = env["client"], env["db"], env["kit"], env["auth"]
    coupon_id = give_coupon(db, env["user_id"], utcnow())            # 满 100 减 20

    # 1 试算：服务端价格 + 券；返回报价令牌
    q = c.post("/api/v1/app/orders/preview", json={"items": [{"kind": "goods", "sku_id": 1, "qty": 2}],
                                                    "coupon_ids": [coupon_id]}, headers=auth).json()
    assert q["code"] == 0 and q["data"]["total_cents"] == 17600 - 2000 and q["data"]["coupon_cents"] == 2000

    # 2 下单（幂等）：重放返回同一订单，库存只占一次
    body = {"quote_token": q["data"]["quote_token"], "terms_accepted": True}
    first = c.post("/api/v1/app/orders", json=body, headers={**auth, **key("order-key-0001")}).json()
    replay = c.post("/api/v1/app/orders", json=body, headers={**auth, **key("order-key-0001")}).json()
    assert first == replay and first["data"]["status"] == "pending_payment"
    order_id = first["data"]["order_id"]
    assert sku_stock(db, 1) == 3 and coupon_status(db, coupon_id) == "locked"

    # 3 去支付（09 路由，金额来自订单解析器）
    pay = c.post(f"/api/v1/app/orders/{order_id}/pay", headers={**auth, **key("pay-key-0001")}).json()
    assert pay["code"] == 0 and pay["data"]["amount_cents"] == 15600 and pay["data"]["pay_params"]["signType"] == "RSA"
    out_trade_no = pay["data"]["out_trade_no"]

    # 4 用户在微信完成支付 → 签名回调（APIv3 验签 + 解密）；重复回调幂等
    kit.fake.pay(out_trade_no)
    headers, raw = kit.fake.payment_notification(out_trade_no)
    assert c.post("/api/v1/webhooks/wechat/pay", content=raw, headers=headers).status_code == 204
    assert c.post("/api/v1/webhooks/wechat/pay", content=raw, headers=headers).status_code == 204
    detail = c.get(f"/api/v1/app/orders/{order_id}", headers=auth).json()["data"]
    assert detail["status"] == "paid" and detail["paid_cents"] == 15600 and detail["pay_method"] == "wechat"
    assert coupon_status(db, coupon_id) == "used" and sku_stock(db, 1) == 3
    status = c.get(f"/api/v1/app/payments/{out_trade_no}", headers=auth).json()["data"]
    assert status["status"] == "paid"
    assert c.post(f"/api/v1/app/orders/{order_id}/cancel", headers={**auth, **key("cancel-key-01")}).json()["code"] == 4090

    # 5 申请售后（整单仅退款）
    item_id = detail["items"][0]["id"]
    apply = c.post("/api/v1/app/after-sales", json={"order_id": order_id, "type": "refund_only", "reason_code": "quality",
                                                    "lines": [{"item_id": item_id, "qty": 2}]},
                   headers={**auth, **key("apply-key-001")}).json()
    assert apply["code"] == 0 and apply["data"]["amount_cents"] == 15600
    case_id = apply["data"]["id"]

    # 6 员工审批（权限 + 二次验证）→ 立即原路退款
    as_staff(env["app"], staff(["after_sale:approve", "after_sale:read"], staff_id=3))
    approved = c.post(f"/api/v1/admin/after-sales/{case_id}/approve", headers=key("approve-key-01")).json()["data"]
    assert approved["status"] == "refunding" and approved["out_refund_no"]

    # 7 渠道退款成功 → 退款回调 → 事件 worker → 售后完成、订单退款、库存回补、券退回
    kit.fake.settle_refund(approved["out_refund_no"])
    headers, raw = kit.fake.refund_notification(approved["out_refund_no"])
    assert c.post("/api/v1/webhooks/wechat/refund", content=raw, headers=headers).status_code == 204
    topics = worker_drain()
    assert "refund.succeeded" in topics and "payment.succeeded" in topics
    case = c.get(f"/api/v1/app/after-sales/{case_id}", headers=auth).json()["data"]
    assert case["status"] == "completed"
    detail = c.get(f"/api/v1/app/orders/{order_id}", headers=auth).json()["data"]
    assert detail["status"] == "refunded" and detail["refund_status"] == "refunded" and detail["refunded_cents"] == 15600
    assert sku_stock(db, 1) == 5 and coupon_status(db, coupon_id) == "unused"
    assert worker_drain() == []  # 事件已投递完，重跑无副作用


def test_timeout_close_path(env):
    c, db, kit, auth = env["client"], env["db"], env["kit"], env["auth"]
    coupon_id = give_coupon(db, env["user_id"], utcnow(), threshold_cents=0, amount_cents=500)
    q = c.post("/api/v1/app/orders/preview", json={"items": [{"kind": "goods", "sku_id": 1, "qty": 1}],
                                                    "coupon_ids": [coupon_id]}, headers=auth).json()["data"]
    placed = c.post("/api/v1/app/orders", json={"quote_token": q["quote_token"], "terms_accepted": True},
                    headers={**auth, **key("order-key-0002")}).json()["data"]
    pay = c.post(f"/api/v1/app/orders/{placed['order_id']}/payments", json={"method": "wechat"},
                 headers={**auth, **key("pay-key-0002")}).json()["data"]
    assert sku_stock(db, 1) == 4
    db.get(order.Order, placed["order_id"]).pay_deadline_at = utcnow() - timedelta(minutes=1)
    db.commit()
    job = next(j for j in __import__("order.jobs", fromlist=["JOBS"]).JOBS if j.name == "order.close_expired")
    assert run_job(job, get_session_factory(), InMemoryLock()) == "ok"
    db.expire_all()
    o = db.get(order.Order, placed["order_id"])
    assert o.status == "closed" and o.cancel_type == "timeout"
    assert sku_stock(db, 1) == 5 and coupon_status(db, coupon_id) == "unused"
    p = db.scalar(select(wechat_pay.PaymentRecord).where(wechat_pay.PaymentRecord.out_trade_no == pay["out_trade_no"]))
    assert p.status == "closed" and kit.fake.orders[pay["out_trade_no"]]["trade_state"] == "CLOSED"
    assert worker_drain().count("order.cancelled") == 1  # 09 订阅关单事件：已关闭，空操作
    detail = c.get(f"/api/v1/app/orders/{placed['order_id']}", headers=auth).json()["data"]
    assert detail["status"] == "closed" and not detail["can_pay"]


def test_unauthenticated_and_foreign_orders(env):
    c = env["client"]
    assert c.post("/api/v1/app/orders/preview", json={"items": [{"kind": "goods", "sku_id": 1, "qty": 1}]}).json()["code"] == 4010
    other = member_tokens(env["db"])
    q = c.post("/api/v1/app/orders/preview", json={"items": [{"kind": "goods", "sku_id": 1, "qty": 1}]},
               headers=env["auth"]).json()["data"]
    placed = c.post("/api/v1/app/orders", json={"quote_token": q["quote_token"], "terms_accepted": True},
                    headers={**env["auth"], **key("order-key-0003")}).json()["data"]
    r = c.get(f"/api/v1/app/orders/{placed['order_id']}", headers={"Authorization": f"Bearer {other['access_token']}"})
    assert r.json()["code"] == 4040
