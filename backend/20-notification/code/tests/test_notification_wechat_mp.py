"""端到端：notification 的默认订阅消息发送器 → wechat_mp.send_subscribe_message → FakeMiniProgram（假微信服务）；
wechat_mp 消息推送分发 → notification 的推送处理函数（不提交，由 dispatch 一起提交）。"""
import pytest
from sqlalchemy import select

import wechat_mp
import wechat_mp.models  # noqa: F401  推送去重表
from notification import channels, consumers, service
from notification.models import NotificationDelivery, NotificationSubscribeEvent
from wechat_mp import FakeMiniProgram, FakeWechatServer

import notif_kit as kit


@pytest.fixture()
def env():
    e = kit.make_env()
    server = FakeWechatServer(clock=lambda: e.clock())
    wechat_mp.set_client(FakeMiniProgram(server))
    channels.set_subscribe_sender(None)  # 走默认：wechat_mp 可用 → WechatMpSubscribeSender
    e.server = server
    yield e
    wechat_mp.set_client(None)
    e.db.close()
    kit.teardown()


def _deliveries(env, channel="subscribe"):
    env.db.expire_all()
    return env.db.execute(select(NotificationDelivery).where(NotificationDelivery.channel == channel)
                          .order_by(NotificationDelivery.id)).scalars().all()


def test_default_sender_is_wechat_mp_and_delivers_through_fake_server(env):
    assert isinstance(channels.get_subscribe_sender(), channels.WechatMpSubscribeSender)
    env.server.grant_subscription("o-1", "TPL_PAID", times=1)
    kit.grant(env, 1, "TPL_PAID", times=2)  # 本地额度 2，平台额度 1
    kit.accept(env, "order.paid", key="p1")
    service.dispatch_due(env.db, now=env.now)
    sent = env.server.sent
    assert len(sent) == 1 and sent[0]["touser"] == "o-1" and sent[0]["template_id"] == "TPL_PAID"
    assert sent[0]["data"]["character_string1"] == {"value": "NO20261028001"}
    assert sent[0]["page"] == "pages/order/detail?id=77" and sent[0]["miniprogram_state"] == "formal"
    assert _deliveries(env)[0].status == "sent"
    # 平台侧额度用完：43101 → 该次投递失败并记录错误码（不无限重试）
    kit.accept(env, "order.paid", data=kit.paid_data(order_id=78, order_no="NO2"), key="p2")
    service.dispatch_due(env.db, now=env.now)
    second = _deliveries(env)[-1]
    assert second.errcode == "43101" and second.status != "sent"


def test_module_level_send_signature_matches_adapter(env):
    env.server.grant_subscription("o-9", "TPL-ORDER")
    sender = channels.WechatMpSubscribeSender()
    result = sender.send(touser="o-9", template_id="TPL-ORDER", data={"thing1": {"value": "x"}}, page=None,
                         miniprogram_state="developer", lang="zh_CN")
    assert result.ok and env.server.sent[-1]["miniprogram_state"] == "developer"
    refused = sender.send(touser="o-9", template_id="TPL-ORDER", data={"thing1": {"value": "x"}}, page="/p",
                          miniprogram_state="formal", lang="zh_CN")
    assert not refused.ok and refused.errcode == "43101"


def test_push_dispatch_calls_notification_handler_without_commit(env):
    assert consumers.install_wechat_mp_push_handlers()
    msg = {"ToUserName": "gh", "FromUserName": "o-1", "CreateTime": 2000, "MsgType": "event",
           "Event": "subscribe_msg_popup_event",
           "List": {"TemplateId": "TPL_PAID", "SubscribeStatusString": "accept", "PopupScene": "0"}}
    assert wechat_mp.dispatch(env.db, msg) == "processed"
    assert wechat_mp.dispatch(env.db, msg) == "duplicate"  # wechat_mp 推送去重
    rows = env.db.execute(select(NotificationSubscribeEvent)).scalars().all()
    assert len(rows) == 1 and rows[0].event == "popup" and rows[0].member_id == 1


def test_production_check_flags_fake_client(env):
    problems = wechat_mp.production_problems()
    assert any("FakeMiniProgram" in p for p in problems)
    assert wechat_mp.invalidate_access_token(wechat_mp.access_token())  # 刷新后返回新 token
