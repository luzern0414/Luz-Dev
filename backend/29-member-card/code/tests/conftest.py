"""会员卡测试公共替身：会员目录、支付端口、预付策略、配置。"""
import pytest

import member_card  # noqa: F401 - 注册模型
import wallet
from member_card import config as mconfig
from wallet import MemberRef
from wallet import config as wconfig
from wallet.prepaid import CONSERVATIVE, set_policy_resolver

MEMBERS = {i: MemberRef(user_id=i, name=f"会员{i}", phone=f"138{i:08d}") for i in range(1, 60)}
PHONES = {m.phone: m for m in MEMBERS.values()}


def resolver(db, user_id=None, phone=None):
    if user_id is not None:
        return MEMBERS.get(int(user_id))
    return PHONES.get(phone)


@pytest.fixture(autouse=True)
def doubles():
    mconfig.reset_config()
    wconfig.reset_config()
    set_policy_resolver(lambda store_id: CONSERVATIVE)
    port = wallet.FakePaymentPort()
    wallet.set_payment_port(port)
    wallet.set_member_resolver(resolver)
    wallet.set_store_timezone_resolver(lambda db, store_id: "Asia/Shanghai")
    yield port
    wallet.set_member_resolver(None)
    wallet.set_store_timezone_resolver(None)
    wallet.set_payment_port(wallet.WechatPayPort())
    set_policy_resolver(None)
