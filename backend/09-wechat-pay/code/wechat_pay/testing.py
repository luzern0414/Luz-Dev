"""测试工具：一键装好假渠道、测试配置、可控时钟与隔离的注册表（只用于测试与本地联调）。"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from luzback import BreakerRegistry

from . import service
from .config import WechatPayConfig, set_config
from .fake import FAKE_APIV3_KEY, FakeShippingClient, FakeWechatPay

TEST_APPID = "wx-test-appid"
TEST_MCHID = "1900000001"


@dataclass
class Clock:
    """可推进的 UTC 时钟：服务函数用 clock.now 作为 now，假渠道用同一时间签名与生成 success_time。"""

    now: datetime = field(default_factory=lambda: datetime(2026, 10, 28, 4, 0, tzinfo=timezone.utc))  # 北京时间 12:00

    def advance(self, **kwargs) -> datetime:
        self.now = self.now + timedelta(**kwargs)
        return self.now

    def ts(self) -> float:
        return self.now.timestamp()


@dataclass
class Kit:
    clock: Clock
    fake: FakeWechatPay
    shipping: FakeShippingClient
    config: WechatPayConfig


def test_config(**overrides) -> WechatPayConfig:
    base = dict(client_mode="fake", appid=TEST_APPID, mchid=TEST_MCHID, apiv3_key=FAKE_APIV3_KEY,
                notify_url="https://pay.example.org/api/v1/webhooks/wechat/pay",
                refund_notify_url="https://pay.example.org/api/v1/webhooks/wechat/refund", query_attempts=1,
                close_attempts=1)
    base.update(overrides)
    return WechatPayConfig(**base)


test_config.__test__ = False  # 不是 pytest 用例


@contextmanager
def wechat_pay_kit(**config_overrides) -> Iterator[Kit]:
    """安装假渠道与测试配置；退出时恢复全局注册表与配置。"""
    saved = (dict(service._payables), dict(service._on_paid), dict(service._on_refunded), service._openid_resolver,
             service._client, service._shipping_client, service._breakers, service._sleep, service._bulkhead)
    clock = Clock()
    config = test_config(**config_overrides)
    set_config(config)
    fake = FakeWechatPay(appid=TEST_APPID, mchid=TEST_MCHID, clock=clock.ts)
    shipping = FakeShippingClient()
    service.set_client(fake)
    service.set_shipping_client(shipping)
    service.set_breakers(BreakerRegistry())
    service.set_sleep(lambda _s: None)
    service.set_openid_resolver(lambda _db, user_id: f"openid-{user_id}")
    service._bulkhead = None
    try:
        yield Kit(clock=clock, fake=fake, shipping=shipping, config=config)
    finally:
        service._payables.clear()
        service._payables.update(saved[0])
        service._on_paid.clear()
        service._on_paid.update(saved[1])
        service._on_refunded.clear()
        service._on_refunded.update(saved[2])
        (service._openid_resolver, service._client, service._shipping_client, service._breakers, service._sleep,
         service._bulkhead) = saved[3:]
        set_config(None)
