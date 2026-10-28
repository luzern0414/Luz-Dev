"""08-order 测试公共夹具（测试文件 `from order_fixtures import *`；不用 conftest.py，避免与其他模块冲突）。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pytest

import order
import wechat_pay
from luzback import AppError
from order.testing import add_sku, order_db
from wechat_pay.testing import Kit, wechat_pay_kit

USER = 7
OTHER = 8


@dataclass
class Env:
    db: Any
    kit: Kit

    @property
    def now(self) -> datetime:
        return self.kit.clock.now

    def preview(self, items, coupon_ids=(), user=USER, **extra):
        result = order.preview(self.db, user, {"items": items, "coupon_ids": list(coupon_ids), **extra}, self.now)
        self.db.commit()
        return result

    def submit(self, quote, user=USER, **extra):
        body = {"quote_token": quote["quote_token"], "terms_accepted": True, **extra}
        result = order.submit(self.db, user, body, self.now)
        self.db.commit()
        return result

    def place(self, items=None, coupon_ids=(), user=USER) -> dict:
        items = items or [{"kind": "goods", "sku_id": 1, "qty": 1}]
        return self.submit(self.preview(items, coupon_ids, user=user), user=user)

    def order(self, order_id) -> order.Order:
        self.db.expire_all()
        return self.db.get(order.Order, order_id)

    def items(self, order_id) -> list:
        self.db.expire_all()
        return order.service._items(self.db, order_id)

    def pay_wechat(self, order_id, user=USER) -> dict:
        result = order.pay(self.db, user, order_id, "wechat", self.now)
        self.db.commit()
        return result

    def notify_paid(self, out_trade_no: str):
        self.kit.fake.pay(out_trade_no)
        headers, body = self.kit.fake.payment_notification(out_trade_no)
        result = wechat_pay.handle_pay_notify(self.db, headers, body, now=self.now)
        self.db.expire_all()
        return result

    def paid_order(self, items=None, coupon_ids=()) -> dict:
        placed = self.place(items, coupon_ids)
        pay = self.pay_wechat(placed["order_id"])
        assert self.notify_paid(pay["out_trade_no"]).http_status == 204
        return {**placed, "out_trade_no": pay["out_trade_no"]}


def err(fn) -> AppError:
    with pytest.raises(AppError) as error:
        fn()
    return error.value


@pytest.fixture()
def env():
    order.reset_config()
    db = order_db()
    with wechat_pay_kit() as kit:
        order.install_payment_hooks()
        add_sku(db, 1, 6000, stock=10, name="跑鞋")
        add_sku(db, 2, 4000, stock=10, name="袜子")
        yield Env(db=db, kit=kit)
    order.reset_config()
    order.set_promotions_provider(None)
    order.set_service_catalog(None)
    order.set_member_card_port(None)
    order.set_terms_checker(None)
    order.set_buyer_contact_provider(None)
    order.set_wallet_port(None)
    db.close()
