"""售后用例：可退额度、申请规则、审核、退货流程、原路退款（微信 / 余额）、退款失败 / 异常 / 重试 / 人工、券与库存、超时任务。"""
from datetime import timedelta

import pytest
from sqlalchemy import select

import after_sales
import order
import wallet
import wechat_pay
from after_sales import rules
from luzback import AuditLog, ErrorCode
from order.testing import coupon_status, drain_events, events, give_coupon, sku_stock, topics

from after_sales_fixtures import *  # noqa: F401,F403
from after_sales_fixtures import OTHER, SUBSCRIBERS, USER, err


# ---------------------------------------------------------------- 申请


def test_refundable_view(env):
    paid = env.paid_order([{"kind": "goods", "sku_id": 1, "qty": 3}])
    view = after_sales.refundable(env.db, USER, paid["order_id"], env.now)
    assert view["open"] and view["lines"][0]["refundable_qty"] == 3 and view["lines"][0]["refundable_cents"] == 15000
    assert view["no_reason_deadline"] is None  # 未签收：随时可申请
    assert err(lambda: after_sales.refundable(env.db, OTHER, paid["order_id"], env.now)).code == ErrorCode.NOT_FOUND


def test_apply_amount_rules(env):
    cid = give_coupon(env.db, USER, env.now, amount_cents=1000, threshold_cents=0)
    paid = env.paid_order([{"kind": "goods", "sku_id": 1, "qty": 3}], [cid])   # 15000 − 1000 = 14000
    item = env.items(paid["order_id"])[0]
    e = err(lambda: env.apply(paid["order_id"], [{"item_id": item.id, "qty": 1, "amount_cents": 4667}]))
    assert e.data["reason"] == "exceeds_refundable" and e.data["max_cents"] == 4666
    case = env.apply(paid["order_id"], [{"item_id": item.id, "qty": 1}], reason_text="鞋底开胶", evidence=[11, 12])
    assert case["amount_cents"] == 4666 and case["status"] == rules.PENDING_REVIEW and case["evidence"] == [11, 12]
    assert case["route"] == "wechat" and events(env.db, "after_sale.applied")[0]["after_sale_id"] == case["id"]
    o = env.order(paid["order_id"])
    assert o.refund_status == order.REFUNDING and o.refund_reserved_cents == 4666
    e = err(lambda: env.apply(paid["order_id"], [{"item_id": item.id, "qty": 1}]))
    assert e.code == ErrorCode.CONFLICT and e.data["reason"] == "after_sale_open"  # 同一行同时只有一个进行中售后


def test_apply_rejections(env):
    paid = env.paid_order()
    item = env.items(paid["order_id"])[0]
    assert err(lambda: env.apply(paid["order_id"], [{"item_id": item.id, "qty": 1}], user=OTHER)).code == ErrorCode.NOT_FOUND
    assert err(lambda: env.apply(paid["order_id"], [{"item_id": 999, "qty": 1}])).code == ErrorCode.NOT_FOUND
    assert err(lambda: env.apply(paid["order_id"], [{"item_id": item.id, "qty": 3}])).data["reason"] == "qty_exceeds"
    assert err(lambda: env.apply(paid["order_id"], [{"item_id": item.id, "qty": 0}])).code == ErrorCode.BAD_REQUEST
    assert err(lambda: env.apply(paid["order_id"], [{"item_id": item.id, "qty": 1}], reason="bogus")).code == ErrorCode.BAD_REQUEST
    assert err(lambda: env.apply(paid["order_id"], [{"item_id": item.id, "qty": 1}],
                                 evidence=list(range(1, 11)))).code == ErrorCode.BAD_REQUEST
    after_sales.set_evidence_checker(lambda db, uid, ids: (_ for _ in ()).throw(
        order.service.AppError(ErrorCode.NOT_FOUND, "media not found")))
    assert err(lambda: env.apply(paid["order_id"], [{"item_id": item.id, "qty": 1}], evidence=[5])).code == ErrorCode.NOT_FOUND
    unpaid = order.preview(env.db, USER, {"items": [{"kind": "goods", "sku_id": 2, "qty": 1}]}, env.now)
    placed = order.submit(env.db, USER, {"quote_token": unpaid["quote_token"], "terms_accepted": True}, env.now)
    env.db.commit()
    unpaid_item = env.items(placed["order_id"])[0]
    assert err(lambda: env.apply(placed["order_id"], [{"item_id": unpaid_item.id, "qty": 1}])).code == ErrorCode.CONFLICT


def test_window_and_no_reason_rules(env):
    paid = env.paid_order()
    item = env.items(paid["order_id"])[0]
    order.mark_fulfilled(env.db, 1, paid["order_id"], now=env.now)   # 北京时间 10-28 12:00 签收
    env.db.commit()
    env.db.execute(order.OrderItem.__table__.update().where(order.OrderItem.id == item.id).values(returnable=False))
    env.db.commit()
    env.db.expire_all()
    e = err(lambda: env.apply(paid["order_id"], [{"item_id": item.id, "qty": 1}], reason="no_reason"))
    assert e.data["reason"] == "not_returnable"
    env.db.execute(order.OrderItem.__table__.update().where(order.OrderItem.id == item.id).values(returnable=True))
    env.db.commit()
    env.db.expire_all()
    env.kit.clock.advance(days=7, hours=11)    # 11-04 23:00：七日内（签收次日起算）
    assert env.apply(paid["order_id"], [{"item_id": item.id, "qty": 1}], type="return_and_refund",
                     reason="no_reason")["status"] == rules.PENDING_REVIEW
    env.kit.clock.advance(hours=2)             # 11-05 01:00：超过七日
    e = err(lambda: env.apply(paid["order_id"], [{"item_id": item.id, "qty": 1}], reason="no_reason"))
    assert e.data["reason"] == "window_closed"
    env.kit.clock.advance(days=9)              # 质量问题窗口 15 天也已过
    assert err(lambda: env.apply(paid["order_id"], [{"item_id": item.id, "qty": 1}])).data["reason"] == "window_closed"


def test_return_requires_goods_and_quantity(env):
    order.set_service_catalog(lambda db, pid, store: {"name": "体验课", "price_cents": 5000})
    paid = env.paid_order([{"kind": "service", "product_id": 1, "qty": 1}])
    item = env.items(paid["order_id"])[0]
    e = err(lambda: env.apply(paid["order_id"], [{"item_id": item.id, "qty": 1}], type="return_and_refund"))
    assert e.data["reason"] == "return_not_applicable"
    case = env.apply(paid["order_id"], [{"item_id": item.id, "qty": 0, "amount_cents": 1000}], reason="service_issue")
    assert case["amount_cents"] == 1000  # 仅退部分金额（服务瑕疵补偿）
    order.set_service_catalog(None)


# ---------------------------------------------------------------- 仅退款（微信原路）


def test_refund_only_full_flow_restocks_unshipped_and_returns_coupon(env):
    cid = give_coupon(env.db, USER, env.now, amount_cents=1000, threshold_cents=0)
    paid = env.paid_order([{"kind": "goods", "sku_id": 1, "qty": 2}], [cid])    # 实付 9000
    assert sku_stock(env.db, 1) == 8 and coupon_status(env.db, cid) == "used"
    item = env.items(paid["order_id"])[0]
    case = env.apply(paid["order_id"], [{"item_id": item.id, "qty": 2}])
    assert case["amount_cents"] == 9000
    assert err(lambda: after_sales.approve(env.db, 2, case["id"], env.now, store_ids=[3])).code == ErrorCode.NOT_FOUND
    approved = env.approve(case["id"])
    assert approved["status"] == rules.REFUNDING
    assert env.refund_flow(case["id"]) == "submitted"
    c = env.case(case["id"])
    assert c.refund_id and c.out_refund_no.startswith("R" + paid["out_trade_no"])
    refund = env.db.get(wechat_pay.RefundRecord, c.refund_id)
    assert refund.business_ref == f"after_sales:{case['id']}" and refund.amount_cents == 9000
    assert env.refund_flow(case["id"]) == "skipped"  # 已提交：不会重复退款
    assert env.settle(case["id"]).result == "processed"
    c = env.case(case["id"])
    assert c.status == rules.COMPLETED and c.refunded_at
    o = env.order(paid["order_id"])
    assert o.status == order.REFUNDED and o.refund_status == order.REFUND_DONE and o.refunded_cents == 9000
    assert sku_stock(env.db, 1) == 10                 # 未发货的仅退款：货未出库，回库
    assert coupon_status(env.db, cid) == "unused"     # 整单全额退款：券按原方式退回
    done = events(env.db, "after_sale.completed")[0]
    assert done["route"] == "wechat" and done["order_full_refund"] and done["lines"][0]["restock"] is True
    # 重复回调 / 重复事件：幂等
    headers, body = env.kit.fake.refund_notification(c.out_refund_no)
    assert wechat_pay.handle_refund_notify(env.db, headers, body, now=env.now).result == "duplicate"
    assert topics(env.db).count("after_sale.completed") == 1


def test_partial_refund_after_shipping_does_not_restock(env):
    paid = env.paid_order([{"kind": "goods", "sku_id": 1, "qty": 2}])
    order.ship(env.db, 1, paid["order_id"], "顺丰", "SF0001", now=env.now)
    env.db.commit()
    item = env.items(paid["order_id"])[0]
    case = env.apply(paid["order_id"], [{"item_id": item.id, "qty": 0, "amount_cents": 1500}], reason="not_as_described")
    env.approve(case["id"])
    env.refund_flow(case["id"])
    env.settle(case["id"])
    o = env.order(paid["order_id"])
    assert o.refund_status == order.PARTIALLY_REFUNDED and o.status == order.FULFILLING and o.refunded_cents == 1500
    assert sku_stock(env.db, 1) == 8


# ---------------------------------------------------------------- 退货退款


def test_return_and_refund_flow(env):
    paid = env.paid_order([{"kind": "goods", "sku_id": 1, "qty": 2}, {"kind": "goods", "sku_id": 2, "qty": 1}])
    order.ship(env.db, 1, paid["order_id"], "顺丰", "SF0002", now=env.now)
    env.db.commit()
    shoe = env.items(paid["order_id"])[0]
    case = env.apply(paid["order_id"], [{"item_id": shoe.id, "qty": 1}], type="return_and_refund", reason="no_reason")
    approved = env.approve(case["id"])
    assert approved["status"] == rules.AWAITING_RETURN and approved["due_at"]
    assert err(lambda: after_sales.receive_return(env.db, 2, case["id"], env.now)).code == ErrorCode.CONFLICT
    assert err(lambda: after_sales.ship_return(env.db, OTHER, case["id"], "顺丰", "SF9", env.now)).code == ErrorCode.NOT_FOUND
    after_sales.ship_return(env.db, USER, case["id"], "顺丰", "SF9001", env.now)
    env.db.commit()
    assert env.case(case["id"]).status == rules.RETURN_SHIPPED
    after_sales.receive_return(env.db, 3, case["id"], env.now)
    env.db.commit()
    assert env.case(case["id"]).status == rules.REFUNDING
    env.refund_flow(case["id"])
    env.settle(case["id"])
    c = env.case(case["id"])
    assert c.status == rules.COMPLETED and c.received_by == 3
    assert sku_stock(env.db, 1) == 9  # 收到退货 → 回库 1 件
    o = env.order(paid["order_id"])
    assert o.refunded_cents == 5000 and o.refund_status == order.PARTIALLY_REFUNDED


def test_reject_cancel_and_close_release_quota(env):
    paid = env.paid_order([{"kind": "goods", "sku_id": 1, "qty": 2}])
    item = env.items(paid["order_id"])[0]
    a = env.apply(paid["order_id"], [{"item_id": item.id, "qty": 2}])
    assert err(lambda: after_sales.reject(env.db, 2, a["id"], "", env.now)).code == ErrorCode.BAD_REQUEST
    after_sales.reject(env.db, 2, a["id"], "已核实商品无质量问题", env.now)
    env.db.commit()
    assert env.case(a["id"]).status == rules.REJECTED
    assert env.order(paid["order_id"]).refund_reserved_cents == 0
    assert env.order(paid["order_id"]).refund_status == order.REFUND_NONE
    assert err(lambda: env.approve(a["id"])).code == ErrorCode.CONFLICT  # 终态不可变
    b = env.apply(paid["order_id"], [{"item_id": item.id, "qty": 2}])
    after_sales.cancel(env.db, USER, b["id"], env.now)
    env.db.commit()
    assert env.case(b["id"]).status == rules.CANCELLED and env.order(paid["order_id"]).refund_reserved_cents == 0
    c = env.apply(paid["order_id"], [{"item_id": item.id, "qty": 1}], type="return_and_refund")
    env.approve(c["id"])
    assert after_sales.close_unshipped(env.db, env.now + timedelta(days=6)) == []
    assert after_sales.close_unshipped(env.db, env.now + timedelta(days=7, seconds=1)) == [c["id"]]
    assert env.case(c["id"]).status == rules.CLOSED and env.order(paid["order_id"]).refund_reserved_cents == 0
    assert after_sales.close_unshipped(env.db, env.now + timedelta(days=8)) == []
    logs = env.db.execute(select(AuditLog.action).where(AuditLog.resource == "after_sale")).scalars().all()
    assert "after_sale.reject" in logs and "after_sale.approve" in logs


# ---------------------------------------------------------------- 退款失败 / 异常 / 重试 / 人工


def test_refund_failed_then_retry_succeeds(env):
    paid = env.paid_order()
    item = env.items(paid["order_id"])[0]
    case = env.apply(paid["order_id"], [{"item_id": item.id, "qty": 1}])
    env.approve(case["id"])
    env.refund_flow(case["id"])
    first = env.case(case["id"]).refund_id
    env.settle(case["id"], "CLOSED")
    c = env.case(case["id"])
    assert c.status == rules.REFUND_FAILED and "after_sale.refund_failed" in topics(env.db)
    assert env.order(paid["order_id"]).refund_reserved_cents == 5000  # 额度仍占用，等待人工
    env.kit.clock.advance(seconds=61)  # 微信：同一笔订单两次退款间隔至少 1 分钟
    after_sales.retry_refund(env.db, 2, case["id"], env.now)
    env.db.commit()
    assert env.refund_flow(case["id"]) == "submitted"
    c = env.case(case["id"])
    assert c.refund_id != first and c.out_refund_no.endswith("02")
    env.settle(case["id"])
    assert env.case(case["id"]).status == rules.COMPLETED
    assert err(lambda: after_sales.retry_refund(env.db, 2, case["id"], env.now)).code == ErrorCode.CONFLICT


def test_refund_abnormal_then_success_and_manual_settle(env):
    paid = env.paid_order([{"kind": "goods", "sku_id": 1, "qty": 2}])
    item = env.items(paid["order_id"])[0]
    case = env.apply(paid["order_id"], [{"item_id": item.id, "qty": 1}])
    env.approve(case["id"])
    env.refund_flow(case["id"])
    env.settle(case["id"], "ABNORMAL")
    assert env.case(case["id"]).status == rules.REFUND_ABNORMAL
    env.settle(case["id"], "SUCCESS")  # 09 后台发起异常退款后成功
    assert env.case(case["id"]).status == rules.COMPLETED
    env.kit.clock.advance(seconds=61)
    other = env.apply(paid["order_id"], [{"item_id": item.id, "qty": 1}])
    env.approve(other["id"])
    env.refund_flow(other["id"])
    env.settle(other["id"], "CLOSED")
    assert err(lambda: after_sales.manual_settle(env.db, 2, other["id"], "x", env.now)).code == ErrorCode.BAD_REQUEST
    after_sales.manual_settle(env.db, 2, other["id"], "原卡注销，经顾客同意银行转账", env.now)
    env.db.commit()
    c = env.case(other["id"])
    assert c.status == rules.COMPLETED and c.settle_note
    assert env.order(paid["order_id"]).status == order.REFUNDED


def test_admin_close_failed_refund(env):
    paid = env.paid_order()
    item = env.items(paid["order_id"])[0]
    case = env.apply(paid["order_id"], [{"item_id": item.id, "qty": 1}])
    env.approve(case["id"])
    env.refund_flow(case["id"])
    env.settle(case["id"], "CLOSED")
    after_sales.admin_close(env.db, 2, case["id"], "顾客撤回退款要求", env.now)
    env.db.commit()
    assert env.case(case["id"]).status == rules.CLOSED and env.order(paid["order_id"]).refund_reserved_cents == 0


def test_refund_interval_schedules_resubmission(env):
    paid = env.paid_order([{"kind": "goods", "sku_id": 1, "qty": 2}])
    item = env.items(paid["order_id"])[0]
    a = env.apply(paid["order_id"], [{"item_id": item.id, "qty": 1}])
    env.approve(a["id"])
    env.refund_flow(a["id"])
    second = env.items(paid["order_id"])[0]
    assert second.refund_reserved_qty == 1
    env.settle(a["id"])
    b = env.apply(paid["order_id"], [{"item_id": item.id, "qty": 1}])
    env.approve(b["id"])
    assert env.refund_flow(b["id"]) == "scheduled"   # 距上次退款不足 1 分钟
    c = env.case(b["id"])
    assert c.status == rules.REFUNDING and c.refund_id is None and c.refund_error
    assert after_sales.submit_due_refunds(env.db, env.now) == {}
    env.kit.clock.advance(seconds=61)
    assert after_sales.submit_due_refunds(env.db, env.now) == {"submitted": 1}
    env.settle(b["id"])
    assert env.case(b["id"]).status == rules.COMPLETED and env.order(paid["order_id"]).status == order.REFUNDED


def test_submit_adopts_existing_refund_after_crash(env):
    paid = env.paid_order()
    item = env.items(paid["order_id"])[0]
    case = env.apply(paid["order_id"], [{"item_id": item.id, "qty": 1}])
    env.approve(case["id"])
    p = env.db.scalar(select(wechat_pay.PaymentRecord).where(wechat_pay.PaymentRecord.out_trade_no == paid["out_trade_no"]))
    created = wechat_pay.create_refund(env.db, p.id, 5000, "售后", after_sales.business_ref(case["id"]), now=env.now)
    # 进程在记录 refund_id 之前崩溃：补偿任务按 business_ref 采纳已有退款，不重复退款
    assert after_sales.submit_refund(env.db, case["id"], env.now) == "submitted"
    assert env.case(case["id"]).refund_id == created.refund_id
    assert env.db.query(wechat_pay.RefundRecord).count() == 1


def test_hard_refund_error_goes_to_manual(env):
    paid = env.paid_order()
    item = env.items(paid["order_id"])[0]
    case = env.apply(paid["order_id"], [{"item_id": item.id, "qty": 1}])
    env.approve(case["id"])
    p = env.db.scalar(select(wechat_pay.PaymentRecord).where(wechat_pay.PaymentRecord.out_trade_no == paid["out_trade_no"]))
    env.db.execute(wechat_pay.PaymentRecord.__table__.update().where(wechat_pay.PaymentRecord.id == p.id)
                   .values(paid_at=env.now - timedelta(days=400)))
    env.db.commit()
    assert after_sales.submit_refund(env.db, case["id"], env.now) == "failed"
    c = env.case(case["id"])
    assert c.status == rules.REFUND_FAILED and c.refund_error.startswith("4220")  # 超过 365 天：不可重试，转人工
    assert "after_sale.refund_failed" in topics(env.db)


# ---------------------------------------------------------------- 余额原路


def test_balance_order_refund_reverses_wallet(env):
    wallet.recharge(env.db, USER, "seed-a", 30000)
    env.db.commit()
    paid = env.paid_order([{"kind": "goods", "sku_id": 1, "qty": 2}, {"kind": "goods", "sku_id": 2, "qty": 1}], method="balance")
    assert wallet.service.get_account(env.db, USER).principal_cents == 17000
    shoe, socks = env.items(paid["order_id"])
    # 部分退：同一行先退 1 件再退 1 件，各自按售后单冲回对应金额（wallet.reverse 部分冲回，按 reversal_ref 幂等）
    first = env.apply(paid["order_id"], [{"item_id": shoe.id, "qty": 1}])
    assert first["route"] == "balance"
    env.approve(first["id"])
    assert env.refund_flow(first["id"]) == "completed"
    env.db.expire_all()
    assert wallet.service.get_account(env.db, USER).principal_cents == 22000
    assert env.refund_flow(first["id"]) == "skipped"  # 重放不再冲回
    case = env.apply(paid["order_id"], [{"item_id": shoe.id, "qty": 1}])
    env.approve(case["id"])
    assert env.refund_flow(case["id"]) == "completed"
    assert env.case(case["id"]).status == rules.COMPLETED
    env.db.expire_all()
    assert wallet.service.get_account(env.db, USER).principal_cents == 27000
    assert env.order(paid["order_id"]).refunded_cents == 10000
    e = err(lambda: env.apply(paid["order_id"], [{"item_id": shoe.id, "qty": 1}]))  # 已退完
    assert e.code in (ErrorCode.CONFLICT, ErrorCode.BUSINESS_RULE, ErrorCode.BAD_REQUEST)


# ---------------------------------------------------------------- 会员卡


def test_member_card_refund_capped_by_card_module(env):
    from types import SimpleNamespace

    class Cards(order.MemberCardPort):
        def product(self, db, product_id):
            return SimpleNamespace(id=1, name="次卡", price_cents=100000, status="on", deleted_at=None,
                                   goods_kind="offline_service", per_member_limit=None)

        def issue(self, db, product, user_id, store_id, ref, paid_cents, paid_at):
            return 31

        def refund_quote(self, db, card_id, now):
            assert card_id == 31
            return 40000  # 已使用部分按法释〔2025〕4号由会员卡模块扣减

    order.set_member_card_port(Cards())
    paid = env.paid_order([{"kind": "member_card", "product_id": 1, "qty": 1}])
    item = env.items(paid["order_id"])[0]
    case = env.apply(paid["order_id"], [{"item_id": item.id, "qty": 1}], reason="service_issue")
    assert case["amount_cents"] == 40000
    env.approve(case["id"])
    env.refund_flow(case["id"])
    env.settle(case["id"])
    done = events(env.db, "after_sale.completed")[0]
    assert done["lines"][0]["fulfil_ref"] == "member_card:31" and done["lines"][0]["kind"] == "member_card"


# ---------------------------------------------------------------- 职责分离 / 自动同意 / 状态机 / 事件


def test_separation_of_duties_for_staff_created_case(env):
    paid = env.paid_order()
    item = env.items(paid["order_id"])[0]
    body = {"order_id": paid["order_id"], "type": "refund_only", "reason_code": "quality",
            "lines": [{"item_id": item.id, "qty": 1}]}
    case = after_sales.apply(env.db, 0, body, env.now, staff_id=5)
    env.db.commit()
    assert case["applied_by"] == "staff"
    e = err(lambda: after_sales.approve(env.db, 5, case["id"], env.now))
    assert e.code == ErrorCode.FORBIDDEN and e.data["reason"] == "separation_of_duties"
    assert env.approve(case["id"], staff_id=6)["status"] == rules.REFUNDING


def test_auto_approve_when_enabled(env):
    paid = env.paid_order()
    item = env.items(paid["order_id"])[0]
    off = env.apply(paid["order_id"], [{"item_id": item.id, "qty": 1}])
    assert after_sales.auto_approve(env.db, env.now + timedelta(days=30)) == []   # 默认关闭
    after_sales.cancel(env.db, USER, off["id"], env.now)
    env.db.commit()
    after_sales.set_config(auto_approve_days=2)
    case = env.apply(paid["order_id"], [{"item_id": item.id, "qty": 1}])
    assert after_sales.auto_approve(env.db, env.now + timedelta(days=1)) == []
    assert after_sales.auto_approve(env.db, env.now + timedelta(days=2)) == [case["id"]]
    c = env.case(case["id"])
    assert c.status == rules.REFUNDING and c.reviewed_by is None
    assert events(env.db, "after_sale.approved")[-1]["auto"] is True


def test_after_sale_state_machine():
    flow = rules.AFTER_SALE_FLOW
    for terminal in (rules.COMPLETED, rules.REJECTED, rules.CANCELLED, rules.CLOSED):
        assert not flow.transitions.get(terminal)
    legal = {(s, t) for s, ts in flow.transitions.items() for t in ts}
    for s in rules.STATUSES:
        for t in rules.STATUSES:
            assert flow.can(s, t) == ((s, t) in legal)
    assert not flow.can(rules.PENDING_REVIEW, rules.COMPLETED)


def test_event_subscribers_are_idempotent(env):
    from after_sales import events as as_events
    assert set(as_events.SUBSCRIPTIONS) == {"refund.succeeded", "refund.failed", "refund.abnormal"}
    paid = env.paid_order()
    item = env.items(paid["order_id"])[0]
    case = env.apply(paid["order_id"], [{"item_id": item.id, "qty": 1}])
    env.approve(case["id"])
    after_sales.submit_refund(env.db, case["id"], env.now)
    c = env.case(case["id"])
    payload = {"business_ref": after_sales.business_ref(c.id), "refund_id": c.refund_id, "out_refund_no": c.out_refund_no}
    assert as_events.on_refund_succeeded(env.db, payload, "evt-1") == "completed"
    env.db.commit()
    assert as_events.on_refund_succeeded(env.db, payload, "evt-1") == "duplicate"
    env.db.rollback()
    assert as_events.on_refund_succeeded(env.db, payload, "evt-2") == "duplicate"
    assert as_events.on_refund_failed(env.db, {"business_ref": "order:1"}, "evt-3") == "ignored"
    assert as_events.on_refund_failed(env.db, {**payload, "refund_id": 999}, "evt-4") == "stale"


def test_jobs_wired_and_run(env):
    from after_sales import jobs
    assert [j.name for j in jobs.JOBS] == ["after_sales.submit_refunds", "after_sales.close_unshipped",
                                           "after_sales.auto_approve"]
    assert jobs.submit_refunds_job(env.db) == {} and jobs.close_unshipped_job(env.db) == 0
    assert jobs.auto_approve_job(env.db) == 0
