"""notification 测试公用：内存库 + 假渠道 + 可控时钟 + 已启用的模板。"""
from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import datetime, timezone

from luzback.testing import FakeClock, memory_session

import notification
from notification import channels, service
from notification.auth import set_member_checker, set_permission_resolver
from notification.config import reset_config, set_clock, set_config

# 2026-10-28 10:00 北京时间 = 02:00 UTC
T0 = datetime(2026, 10, 28, 2, 0, tzinfo=timezone.utc)

TEMPLATES = {
    ("order.paid", "subscribe"): {"template_id": "TPL_PAID", "page": "pages/order/detail?id={order_id}", "keywords": [
        {"key": "character_string1", "source": "order_no"}, {"key": "amount2", "source": "amount_cents"},
        {"key": "thing3", "source": "{goods_name}", "default": "商品"}, {"key": "time4", "source": "paid_at"}]},
    ("card.expiring", "subscribe"): {"template_id": "TPL_CARD", "keywords": [
        {"key": "thing1", "source": "{card_name}"}, {"key": "date2", "source": "expire_date"}]},
    ("card.expiring", "sms"): {"template_code": "SMS_CARD", "params": [
        {"name": "card", "source": "card_name"}, {"name": "date", "source": "expire_date"}]},
    ("refund.succeeded", "subscribe"): {"template_id": "TPL_REFUND", "keywords": [
        {"key": "character_string1", "source": "order_no"}, {"key": "amount2", "source": "amount_cents"}]},
    ("refund.succeeded", "sms"): {"template_code": "SMS_REFUND", "params": [{"name": "amount", "source": "amount_yuan"}]},
    ("staff.device_offline", "sms"): {"template_code": "SMS_DEVICE", "params": [{"name": "device", "source": "device_name"}]},
}


@dataclass
class Env:
    db: object
    sub: channels.FakeSubscribeSender
    sms: channels.FakeSmsSender
    people: channels.InMemoryRecipients
    clock: FakeClock

    @property
    def now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), timezone.utc)


def make_env(url: str = "sqlite://", **config) -> Env:
    reset_config()
    set_config(**config) if config else None
    db = memory_session(notification, url=url)
    service.ensure_default_scenes(db)
    for (scene, channel), body in TEMPLATES.items():
        service.upsert_template(db, 0, scene, channel, body)
    db.commit()
    clock = FakeClock(start=T0.timestamp())
    set_clock(clock)
    people = channels.InMemoryRecipients(
        members={1: channels.Recipient(openid="o-1", phone="13800000001", label="1"),
                 2: channels.Recipient(openid="o-2", phone="13800000002", label="2"),
                 3: channels.Recipient(openid=None, phone=None, label="3")},
        staff_map={9: channels.Recipient(phone="13900000009"), 10: channels.Recipient(phone=None)},
    )
    channels.set_recipient_resolver(people)
    sub, sms = channels.FakeSubscribeSender(), channels.FakeSmsSender()
    channels.set_subscribe_sender(sub)
    channels.set_sms_sender(sms)
    service.breakers._items.clear()
    return Env(db=db, sub=sub, sms=sms, people=people, clock=clock)


def teardown() -> None:
    channels.set_subscribe_sender(None)
    channels.set_sms_sender(None)
    channels.set_recipient_resolver(None)
    channels.set_store_timezone_resolver(None)
    set_permission_resolver(None)
    set_member_checker(None)
    set_clock(None)
    reset_config()


def paid_data(**extra):
    data = {"order_id": 77, "order_no": "NO20261028001", "amount_cents": 1234, "goods_name": "剁椒鱼头",
            "paid_at": T0}
    data.update(extra)
    return data


def accept(env: Env, scene: str, member: int = 1, data: dict | None = None, key: str = "k1", audience: str = "member"):
    ids = service.accept_request(env.db, scene=scene, audience=audience, recipient_ids=[member],
                                 data=service.encode_data(data if data is not None else paid_data()), dedupe_key=key,
                                 now=env.now)
    return ids


_seq = itertools.count()


def grant(env: Env, member: int, template_id: str, times: int = 1):
    for _ in range(times):
        service.report_subscriptions(env.db, member, {template_id: "accept"}, f"grant-{next(_seq)}", now=env.now)
    env.db.commit()
