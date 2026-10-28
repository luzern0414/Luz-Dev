"""10-after-sales 测试公共夹具（`from after_sales_fixtures import *`；不用 conftest.py）。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

import after_sales
import order
import wechat_pay
from luzback import AppError
from order.testing import add_sku, drain_events, order_db
from wechat_pay.testing import Kit, wechat_pay_kit

USER = 7
OTHER = 8
SUBSCRIBERS = ("order.events", "wechat_pay.events", "after_sales.events")


@dataclass
class Env:
    db: Any
    kit: Kit

    @property
    def now(self):
        return self.kit.clock.now

    def paid_order(self, items=None, coupon_ids=(), user=USER, method="wechat") -> dict:
        items = items or [{"kind": "goods", "sku_id": 1, "qty": 2}]
        q = order.preview(self.db, user, {"items": items, "coupon_ids": list(coupon_ids)}, self.now)
        self.db.commit()
        placed = order.submit(self.db, user, {"quote_token": q["quote_token"], "terms_accepted": True}, self.now)
        self.db.commit()
        if method == "balance":
            order.pay(self.db, user, placed["order_id"], "balance", self.now)
            self.db.commit()
            return placed
        pay = order.pay(self.db, user, placed["order_id"], "wechat", self.now)
        self.db.commit()
        self.kit.fake.pay(pay["out_trade_no"])
        headers, body = self.kit.fake.payment_notification(pay["out_trade_no"])
        assert wechat_pay.handle_pay_notify(self.db, headers, body, now=self.now).http_status == 204
        self.db.expire_all()
        return {**placed, "out_trade_no": pay["out_trade_no"]}

    def items(self, order_id):
        self.db.expire_all()
        return order.service._items(self.db, order_id)

    def apply(self, order_id, lines, type="refund_only", reason="quality", user=USER, **extra) -> dict:
        body = {"order_id": order_id, "type": type, "reason_code": reason, "lines": lines, **extra}
        result = after_sales.apply(self.db, user, body, self.now)
        self.db.commit()
        return result

    def approve(self, case_id, staff_id=2) -> dict:
        result = after_sales.approve(self.db, staff_id, case_id, self.now)
        self.db.commit()
        return result

    def case(self, case_id) -> after_sales.AfterSale:
        self.db.expire_all()
        return self.db.get(after_sales.AfterSale, case_id)

    def order(self, order_id) -> order.Order:
        self.db.expire_all()
        return self.db.get(order.Order, order_id)

    def settle(self, case_id, status="SUCCESS"):
        """渠道把退款推进到终态并发回调，再把发件箱事件投递给订阅者。"""
        case = self.case(case_id)
        self.kit.fake.settle_refund(case.out_refund_no, status)
        headers, body = self.kit.fake.refund_notification(case.out_refund_no)
        result = wechat_pay.handle_refund_notify(self.db, headers, body, now=self.now)
        drain_events(self.db, SUBSCRIBERS)
        return result

    def refund_flow(self, case_id) -> str:
        outcome = after_sales.submit_refund(self.db, case_id, self.now)
        drain_events(self.db, SUBSCRIBERS)
        return outcome


def err(fn) -> AppError:
    with pytest.raises(AppError) as error:
        fn()
    return error.value


@pytest.fixture()
def env():
    order.reset_config()
    after_sales.reset_config()
    db = order_db("after_sales", "admin_rbac")
    with wechat_pay_kit() as kit:
        order.install_payment_hooks()
        add_sku(db, 1, 5000, stock=10, name="跑鞋")
        add_sku(db, 2, 3000, stock=10, name="袜子")
        yield Env(db=db, kit=kit)
    order.reset_config()
    after_sales.reset_config()
    order.set_member_card_port(None)
    after_sales.set_evidence_checker(None)
    db.close()
