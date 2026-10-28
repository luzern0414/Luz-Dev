"""与真实 09-wechat-pay（假渠道）联调：购卡 / 充值 → 调起支付 → 支付回调同事务钩子发卡 / 入账 → 退款提交 → 退款回调回写。"""
import pytest
from sqlalchemy import select

import member_card
import wallet
from luzback import utcnow
from luzback.testing import memory_session
from member_card import service as svc
from member_card.models import CardOrder, MemberCard
from wallet import service as wsvc
from wallet.models import RechargeBatch

wechat_pay = pytest.importorskip("wechat_pay")
from wechat_pay import service as pay  # noqa: E402
from wechat_pay.models import PaymentRecord, RefundRecord  # noqa: E402
from wechat_pay.testing import wechat_pay_kit  # noqa: E402


@pytest.fixture()
def env():
    with wechat_pay_kit() as kit:
        kit.clock.now = utcnow()
        db = memory_session(wechat_pay.models, wallet, member_card)
        wallet.set_payment_port(wallet.WechatPayPort())
        yield kit, db
        db.close()


def notify_paid(kit, db, out_trade_no):
    kit.fake.pay(out_trade_no)
    headers, body = kit.fake.payment_notification(out_trade_no)
    assert pay.handle_pay_notify(db, headers, body, now=kit.clock.now).result == "processed"


def notify_refund(kit, db, business_ref, status="SUCCESS"):
    record = db.execute(select(RefundRecord).where(RefundRecord.business_ref == business_ref)).scalar_one()
    kit.fake.settle_refund(record.out_refund_no, status)
    headers, body = kit.fake.refund_notification(record.out_refund_no)
    pay.handle_refund_notify(db, headers, body, now=kit.clock.now)


def test_member_card_purchase_and_refund_through_wechat_pay(env):
    kit, db = env
    data = svc.create_product(db, 9, {"name": "月卡", "kind": "period", "price_cents": 30000, "period_days": 30,
                                      "store_scope": "all"})
    svc.set_product_status(db, 9, data["id"], "on")
    order = svc.create_order(db, 1, data["id"], 5)
    db.commit()
    paying = svc.start_payment(db, 1, order["id"])
    assert paying["pay_params"]
    assert svc.start_payment(db, 1, order["id"])["payment_id"] == paying["payment_id"]  # 继续支付复用同一支付单
    out_trade_no = db.get(CardOrder, order["id"]).out_trade_no
    notify_paid(kit, db, out_trade_no)
    record = db.execute(select(PaymentRecord).where(PaymentRecord.out_trade_no == out_trade_no)).scalar_one()
    assert record.needs_manual is False and record.goods_kind == "offline_service"
    card = db.execute(select(MemberCard).where(MemberCard.order_id == order["id"])).scalar_one()  # 同事务钩子发卡
    assert card.status == "active" and card.paid_cents == 30000
    # 事件补偿通道重复到达：幂等
    payload = {"business_type": "member_card", "business_id": order["id"], "amount_cents": 30000, "payment_id": record.id}
    assert svc.handle_event(db, "payment.succeeded", payload, "dup") == "duplicate"

    refund = svc.request_refund(db, card, "customer", "七日内不想要了", "staff:7", staff_id=7)
    db.commit()
    submitted = svc.submit_refund(db, refund["id"])
    assert submitted["channel_refund_id"] and submitted["status"] == "processing"
    notify_refund(kit, db, f"member_card_refund:{refund['refund_no']}")
    db.refresh(card)
    assert card.status == "refunded" and svc.get_refund(db, refund["id"]).status == "succeeded"


def test_wallet_recharge_and_refund_through_wechat_pay(env):
    kit, db = env
    plan = wsvc.create_plan(db, 9, {"name": "充100送10", "amount_cents": 10000, "bonus_cents": 1000})
    wsvc.set_plan_status(db, 9, plan["id"], "on")
    batch = wsvc.create_recharge_order(db, 1, None, plan["id"])
    db.commit()
    wsvc.start_payment(db, 1, batch["id"])
    notify_paid(kit, db, db.get(RechargeBatch, batch["id"]).out_trade_no)
    assert wsvc.account_dict(db, 1)["balance_cents"] == 11000
    refund = wsvc.request_refund(db, batch["id"], "customer", "七日内", "staff:7", staff_id=7)
    db.commit()
    wsvc.submit_refund(db, refund["id"])
    notify_refund(kit, db, f"wallet_refund:{refund['refund_no']}")
    assert wsvc.account_dict(db, 1)["balance_cents"] == 0
    assert db.get(RechargeBatch, batch["id"]).status == "refunded" and wsvc.ledger_matches_balance(db, 1)

