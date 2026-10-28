"""用例：受理与去重、订阅额度、投递（重试 / 永久失败 / 43101 / 静默时段 / 频控 / 短信兜底）、站内信、后台。"""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from luzback import AppError, AuditLog, Base, ErrorCode, OutboxEvent, dispatch_pending
from luzback.resilience import TransientError

import notif_kit as kit
from notification import channels, consumers, service
from notification.config import set_config
from notification.models import (
    NotificationDelivery as D, NotificationInbox, NotificationRequest, NotificationSubscribeEvent,
    NotificationSubscription as S,
)


@pytest.fixture()
def env():
    e = kit.make_env()
    yield e
    e.db.close()
    kit.teardown()


def deliveries(env, **where):
    stmt = select(D).order_by(D.id)
    for k, v in where.items():
        stmt = stmt.where(getattr(D, k) == v)
    env.db.expire_all()
    return env.db.execute(stmt).scalars().all()


def sub_row(env, member, tpl):
    env.db.expire_all()
    return env.db.execute(select(S).where(S.member_id == member, S.template_id == tpl)).scalar_one_or_none()


def run(env):
    return service.dispatch_due(env.db, now=env.now)


# ======================================================================== notify / 受理 / 去重


def test_notify_writes_outbox_event_only(env):
    service.notify(env.db, "order.paid", 1, kit.paid_data(), "pay:1")
    env.db.commit()
    ev = env.db.execute(select(OutboxEvent)).scalar_one()
    assert ev.topic == service.TOPIC_REQUESTED
    payload = json.loads(ev.payload_json)
    assert payload["recipient_ids"] == [1] and payload["data"]["paid_at"] == {"__dt__": kit.T0.isoformat()}
    assert env.db.execute(select(NotificationRequest)).first() is None


@pytest.mark.parametrize("scene,key,data", [("Order Paid", "k", {}), ("order.paid", "", {}), ("order.paid", "a b", {}),
                                            ("order.paid", "k", [1])])
def test_notify_validates_arguments(env, scene, key, data):
    with pytest.raises(AppError) as exc:
        service.notify(env.db, scene, 1, data, key)
    assert exc.value.code == ErrorCode.BAD_REQUEST


def test_outbox_to_delivery_end_to_end(tmp_path):
    e = kit.make_env(url=f"sqlite:///{tmp_path / 'n.db'}")
    try:
        service.notify(e.db, "order.paid", 1, kit.paid_data(), "pay:1")
        service.notify(e.db, "order.paid", 1, kit.paid_data(), "pay:1")  # 业务重复调用
        e.db.commit()
        dispatch_pending(e.db, consumers.outbox_handler, now=datetime.now(timezone.utc) + timedelta(seconds=5))
        e.db.expire_all()
        assert len(e.db.execute(select(NotificationRequest)).scalars().all()) == 1
        assert {d.channel for d in deliveries(e)} == {"inapp", "subscribe"}
    finally:
        e.db.close()
        kit.teardown()


def test_accept_dedupes_and_creates_inbox(env):
    assert len(kit.accept(env, "order.paid", key="pay:1")) == 1
    assert kit.accept(env, "order.paid", key="pay:1") == []
    msg = env.db.execute(select(NotificationInbox)).scalar_one()
    assert msg.title == "支付成功" and msg.body == "订单 NO20261028001 已支付 12.34 元"
    assert msg.page == "pages/order/detail?id=77" and msg.category == "transaction"
    inapp, sub = deliveries(env)
    assert (inapp.channel, inapp.status) == ("inapp", "sent")
    assert (sub.channel, sub.status, sub.template_ref) == ("subscribe", "pending", "TPL_PAID")


def test_disabled_or_unknown_scene_is_skipped(env):
    assert kit.accept(env, "nope.scene") == []
    service.update_scene(env.db, 1, "order.paid", {"enabled": False})
    env.db.commit()
    assert kit.accept(env, "order.paid") == []


def test_audience_mismatch_skipped(env):
    assert kit.accept(env, "staff.device_offline", member=1, data={"device_name": "x"}) == []


def test_inapp_missing_field_recorded(env):
    kit.accept(env, "order.paid", data={"order_id": 1})
    inapp = deliveries(env, channel="inapp")[0]
    assert inapp.status == "skipped" and inapp.errcode == "missing_field"


# ======================================================================== 订阅额度


def test_send_consumes_one_quota_and_formats_data(env):
    kit.grant(env, 1, "TPL_PAID")
    kit.accept(env, "order.paid")
    assert run(env) == {"sent": 1}
    call = env.sub.calls[0]
    assert call["touser"] == "o-1" and call["template_id"] == "TPL_PAID" and call["page"] == "pages/order/detail?id=77"
    assert call["data"] == {"character_string1": {"value": "NO20261028001"}, "amount2": {"value": "¥12.34"},
                            "thing3": {"value": "剁椒鱼头"}, "time4": {"value": "2026年10月28日 10:00"}}
    assert call["miniprogram_state"] == "formal" and call["lang"] == "zh_CN"
    row = sub_row(env, 1, "TPL_PAID")
    assert (row.available, row.consumed_total, row.accepted_total) == (0, 1, 1)
    d = deliveries(env, channel="subscribe")[0]
    assert d.status == "sent" and d.provider_msg_id == "msg-1" and d.attempts == 1


def test_no_quota_skips_without_calling_wechat(env):
    kit.accept(env, "order.paid")
    assert run(env) == {"skipped": 1}
    assert env.sub.calls == []
    assert deliveries(env, channel="subscribe")[0].errcode == "no_quota"


def test_43101_does_not_consume_and_resets_local_quota(env):
    kit.grant(env, 1, "TPL_PAID", times=3)
    env.sub.script = [43101]
    kit.accept(env, "order.paid")
    run(env)
    row = sub_row(env, 1, "TPL_PAID")
    assert row.consumed_total == 0 and row.available == 0  # 以微信为准清零，不算消耗
    d = deliveries(env, channel="subscribe")[0]
    assert (d.status, d.errcode, d.quota_reserved) == ("skipped", "43101", False)


def test_43101_keep_mode_only_returns_reservation(env):
    set_config(on_43101="keep")
    kit.grant(env, 1, "TPL_PAID", times=2)
    env.sub.script = [43101]
    kit.accept(env, "order.paid")
    run(env)
    row = sub_row(env, 1, "TPL_PAID")
    assert (row.available, row.consumed_total) == (2, 0)


def test_43101_falls_back_to_sms_for_critical_scene(env):
    kit.grant(env, 1, "TPL_REFUND")
    env.sub.script = [43101]
    kit.accept(env, "refund.succeeded", data={"order_no": "NO1", "amount_cents": 500, "order_id": 1}, key="r1")
    assert run(env) == {"skipped": 1}
    sms = deliveries(env, channel="sms")
    assert len(sms) == 1 and sms[0].fallback_from_id is not None
    assert run(env) == {"sent": 1}
    assert env.sms.calls[0] == {"phone": "13800000001", "template_code": "SMS_REFUND", "params": {"amount": "5.00"}}


def test_failed_send_returns_quota(env):
    kit.grant(env, 1, "TPL_PAID")
    env.sub.script = [47003]
    kit.accept(env, "order.paid")
    assert run(env) == {"failed": 1}
    row = sub_row(env, 1, "TPL_PAID")
    assert (row.available, row.consumed_total) == (1, 0)
    d = deliveries(env, channel="subscribe")[0]
    assert (d.status, d.errcode, d.attempts) == ("failed", "47003", 1)


def test_long_term_template_not_decremented_and_43101_unsubscribes(env):
    service.upsert_template(env.db, 1, "order.paid", "subscribe", {**kit.TEMPLATES[("order.paid", "subscribe")],
                                                                   "long_term": True})
    env.db.commit()
    kit.grant(env, 1, "TPL_PAID")
    kit.accept(env, "order.paid", key="a")
    kit.accept(env, "order.paid", key="b")
    assert run(env) == {"sent": 2}
    row = sub_row(env, 1, "TPL_PAID")
    assert row.long_term and row.available == 1
    env.sub.script = [43101]
    kit.accept(env, "order.paid", key="c")
    run(env)
    assert sub_row(env, 1, "TPL_PAID").status == "rejected"
    kit.accept(env, "order.paid", key="d")
    assert run(env) == {"skipped": 1} and len(env.sub.calls) == 3


def test_report_reject_ban_filter_and_unknown(env):
    kit.grant(env, 1, "TPL_PAID", times=2)
    out = service.report_subscriptions(env.db, 1, {"TPL_PAID": "reject", "TPL_X": "accept", "TPL_CARD": "filter"},
                                       "key-reject-1", now=env.now)
    env.db.commit()
    assert out["ignored"] == ["TPL_X"] and set(out["applied"]) == {"TPL_PAID", "TPL_CARD"}
    row = sub_row(env, 1, "TPL_PAID")
    assert (row.available, row.status) == (0, "rejected")
    assert sub_row(env, 1, "TPL_CARD") is None
    kit.grant(env, 1, "TPL_PAID")
    assert (sub_row(env, 1, "TPL_PAID").available, sub_row(env, 1, "TPL_PAID").status) == (1, "active")
    service.report_subscriptions(env.db, 1, {"TPL_PAID": "ban"}, "key-ban-1", now=env.now)
    env.db.commit()
    assert sub_row(env, 1, "TPL_PAID").status == "banned"


def test_report_limits(env):
    with pytest.raises(AppError):
        service.report_subscriptions(env.db, 1, {f"T{i}": "accept" for i in range(6)}, "k-limit-1")
    with pytest.raises(AppError):
        service.report_subscriptions(env.db, 1, {}, "k-limit-2")


def test_quota_cap(env):
    set_config(quota_cap_per_template=2)
    kit.grant(env, 1, "TPL_PAID", times=4)
    row = sub_row(env, 1, "TPL_PAID")
    assert (row.available, row.accepted_total) == (2, 4)


def test_acceptwithaudio_counts(env):
    service.report_subscriptions(env.db, 1, {"TPL_PAID": "acceptWithAudio"}, "k-audio-1", now=env.now)
    env.db.commit()
    assert sub_row(env, 1, "TPL_PAID").available == 1


# ---------------------------------------------------------------- 推送事件


def _popup(items, created=1000, openid="o-1"):
    return {"ToUserName": "gh", "FromUserName": openid, "CreateTime": created, "MsgType": "event",
            "Event": "subscribe_msg_popup_event", "List": items}


def test_push_popup_counts_only_in_push_mode(env):
    msg = _popup({"TemplateId": "TPL_PAID", "SubscribeStatusString": "accept", "PopupScene": "0"})
    service.handle_push_event(env.db, msg, now=env.now)
    assert sub_row(env, 1, "TPL_PAID") is None  # 默认 quota_source=frontend：只记录
    set_config(quota_source="push")
    service.handle_push_event(env.db, _popup(msg["List"], created=1001), now=env.now)
    assert sub_row(env, 1, "TPL_PAID").available == 1
    # frontend 上报在 push 模式下不计数（避免翻倍）
    service.report_subscriptions(env.db, 1, {"TPL_PAID": "accept"}, "k-front-1", now=env.now)
    env.db.commit()
    assert sub_row(env, 1, "TPL_PAID").available == 1


def test_push_array_and_duplicate_redelivery(env):
    set_config(quota_source="push")
    msg = _popup([{"TemplateId": "TPL_PAID", "SubscribeStatusString": "accept"},
                  {"TemplateId": "TPL_CARD", "SubscribeStatusString": "accept"}])
    assert service.handle_push_event(env.db, msg, now=env.now)["applied"] == 2
    assert service.handle_push_event(env.db, msg, now=env.now)["duplicate"] == 2  # 微信重推
    assert sub_row(env, 1, "TPL_PAID").available == 1 and sub_row(env, 1, "TPL_CARD").available == 1


def test_push_change_reject_and_unknown_openid(env):
    kit.grant(env, 1, "TPL_PAID", times=2)
    change = {"FromUserName": "o-1", "CreateTime": 5, "Event": "subscribe_msg_change_event",
              "List": {"TemplateId": "TPL_PAID", "SubscribeStatusString": "reject"}}
    service.handle_push_event(env.db, change, now=env.now)
    assert (sub_row(env, 1, "TPL_PAID").available, sub_row(env, 1, "TPL_PAID").status) == (0, "rejected")
    out = service.handle_push_event(env.db, {**change, "FromUserName": "o-unknown", "CreateTime": 6}, now=env.now)
    assert out["ignored"] == 1


def test_push_sent_event_records_async_failure(env):
    kit.grant(env, 1, "TPL_PAID")
    kit.accept(env, "order.paid")
    run(env)
    sent = {"FromUserName": "o-1", "CreateTime": 9, "Event": "subscribe_msg_sent_event",
            "List": {"TemplateId": "TPL_PAID", "MsgID": "msg-1", "ErrorCode": "10005", "ErrorStatus": "send failed"}}
    service.handle_push_event(env.db, sent, now=env.now)
    d = deliveries(env, channel="subscribe")[0]
    assert d.status == "sent" and d.errcode == "async:10005"
    assert env.db.execute(select(NotificationSubscribeEvent).where(NotificationSubscribeEvent.event == "sent")).scalar_one()


# ======================================================================== 重试 / 永久失败 / 日上限


def test_system_busy_retries_with_backoff_then_succeeds(env):
    kit.grant(env, 1, "TPL_PAID")
    env.sub.script = [-1, 0]
    kit.accept(env, "order.paid")
    assert run(env) == {"retry": 1}
    d = deliveries(env, channel="subscribe")[0]
    assert (d.status, d.attempts, d.errcode) == ("pending", 1, "-1")
    assert service._aware(d.next_attempt_at) == env.now + timedelta(seconds=60)
    assert sub_row(env, 1, "TPL_PAID").available == 1  # 失败退回占用
    assert run(env) == {}  # 未到期
    env.clock.advance(60)
    assert run(env) == {"sent": 1}
    assert (sub_row(env, 1, "TPL_PAID").available, sub_row(env, 1, "TPL_PAID").consumed_total) == (0, 1)


def test_transport_error_retries_until_max_then_fails_and_falls_back(env):
    set_config(max_attempts=3, retry_backoff_seconds=(10, 20))
    kit.grant(env, 1, "TPL_CARD")
    env.sub.script = [TransientError("timeout")] * 3
    kit.accept(env, "card.expiring", data={"card_name": "年卡", "expire_date": "2026-11-01"}, key="c1")
    assert run(env) == {"retry": 1}
    env.clock.advance(10)
    assert run(env) == {"retry": 1}
    env.clock.advance(20)
    assert run(env) == {"failed": 1}
    d = deliveries(env, channel="subscribe")[0]
    assert (d.status, d.attempts, d.errcode) == ("failed", 3, "transport")
    assert sub_row(env, 1, "TPL_CARD").available == 1
    assert deliveries(env, channel="sms")[0].status == "pending"  # 关键场景兜底短信


def test_unexpected_exception_is_permanent(env):
    kit.grant(env, 1, "TPL_PAID")
    env.sub.script = [ValueError("bug")]
    kit.accept(env, "order.paid")
    assert run(env) == {"failed": 1}
    assert deliveries(env, channel="subscribe")[0].errcode == "exception"


def test_daily_api_limit_defers_to_next_local_day(env):
    kit.grant(env, 1, "TPL_PAID")
    env.sub.script = [45009]
    kit.accept(env, "order.paid")
    assert run(env) == {"deferred": 1}
    d = deliveries(env, channel="subscribe")[0]
    assert d.status == "pending" and service._aware(d.next_attempt_at) == datetime(2026, 10, 28, 16, 0, 5, tzinfo=timezone.utc)


def test_app_daily_cap_defers(env):
    set_config(subscribe_daily_cap=1)
    kit.grant(env, 1, "TPL_PAID", times=2)
    kit.accept(env, "order.paid", key="a")
    kit.accept(env, "order.paid", key="b")
    assert run(env) == {"sent": 1, "deferred": 1}
    assert sub_row(env, 1, "TPL_PAID").available == 1


def test_format_error_never_sent(env):
    kit.grant(env, 1, "TPL_PAID")
    kit.accept(env, "order.paid", data=kit.paid_data(order_no="订单号中文"))
    assert run(env) == {"skipped": 1}
    assert env.sub.calls == []
    d = deliveries(env, channel="subscribe")[0]
    assert d.errcode == "format" and "character_string1" in d.errmsg
    assert sub_row(env, 1, "TPL_PAID").available == 1


def test_missing_openid_skips(env):
    kit.accept(env, "order.paid", member=3)
    assert run(env) == {"skipped": 1}
    assert deliveries(env, channel="subscribe")[0].errcode == "no_openid"


def test_dispatch_is_idempotent(env):
    kit.grant(env, 1, "TPL_PAID")
    kit.accept(env, "order.paid")
    run(env)
    assert run(env) == {}
    assert len(env.sub.calls) == 1


def test_reclaim_stuck_keeps_reservation(env):
    kit.grant(env, 1, "TPL_PAID")
    kit.accept(env, "order.paid")
    d = deliveries(env, channel="subscribe")[0]
    service._reserve_quota(env.db, 1, "TPL_PAID")
    env.db.execute(D.__table__.update().where(D.id == d.id).values(status="sending", quota_reserved=True,
                                                                     updated_at=env.now - timedelta(minutes=10)))
    env.db.commit()
    assert service.reclaim_stuck(env.db, now=env.now) == 1
    assert run(env) == {"sent": 1}
    row = sub_row(env, 1, "TPL_PAID")
    assert (row.available, row.consumed_total) == (0, 1)  # 没有重复扣


# ======================================================================== 静默时段 / 频控


def test_quiet_hours_defer_non_urgent_but_not_urgent(env):
    env.clock.advance(13 * 3600)  # 23:00 北京
    kit.grant(env, 1, "TPL_PAID")
    kit.grant(env, 1, "TPL_CARD")
    kit.accept(env, "order.paid", key="p")
    kit.accept(env, "card.expiring", data={"card_name": "年卡", "expire_date": "2026-11-01"}, key="c")
    assert run(env) == {"sent": 1, "deferred": 1}
    card = deliveries(env, scene="card.expiring", channel="subscribe")[0]
    assert service._aware(card.next_attempt_at) == datetime(2026, 10, 29, 0, 0, tzinfo=timezone.utc)
    assert card.errcode == "quiet_hours"
    env.clock.advance(9 * 3600)  # 08:00
    assert run(env) == {"sent": 1}


def test_store_timezone_hook(env):
    channels.set_store_timezone_resolver(lambda store_id: "UTC")
    kit.grant(env, 1, "TPL_CARD")
    service.accept_request(env.db, scene="card.expiring", audience="member", recipient_ids=[1], store_id=5,
                           data={"card_name": "年卡", "expire_date": "2026-11-01"}, dedupe_key="tz", now=env.now)
    # 北京 10:00 = UTC 02:00：按门店时区（UTC）在 22:00–08:00 静默时段内 → 延后到 UTC 08:00
    assert run(env) == {"deferred": 1}
    card = deliveries(env, channel="subscribe")[0]
    assert service._aware(card.next_attempt_at) == datetime(2026, 10, 28, 8, 0, tzinfo=timezone.utc)


def test_member_nonurgent_daily_limit(env):
    set_config(member_nonurgent_per_day=1)
    kit.grant(env, 1, "TPL_CARD", times=2)
    data = {"card_name": "年卡", "expire_date": "2026-11-01"}
    kit.accept(env, "card.expiring", data=data, key="c1")
    kit.accept(env, "card.expiring", data=data, key="c2")
    second = deliveries(env, scene="card.expiring", channel="subscribe")[1]
    assert (second.status, second.errcode) == ("skipped", "member_rate_limited")
    assert len(env.db.execute(select(NotificationInbox)).scalars().all()) == 2  # 站内信不受限
    # 紧急场景不受限
    kit.accept(env, "order.paid", key="p1")
    assert deliveries(env, scene="order.paid", channel="subscribe")[0].status == "pending"


# ======================================================================== 短信 / 员工告警


def _offline(env, key="d1", staff=(9,)):
    return service.accept_request(env.db, scene="staff.device_offline", audience="staff", recipient_ids=list(staff),
                                  data={"device_name": "闸机1", "store_name": "一店"}, dedupe_key=key, now=env.now)


def test_staff_alert_sms_and_inbox(env):
    env.clock.advance(13 * 3600)  # 静默时段也发（紧急）
    _offline(env, staff=(9, 10))
    assert run(env) == {"sent": 1, "skipped": 1}
    assert env.sms.calls == [{"phone": "13900000009", "template_code": "SMS_DEVICE", "params": {"device": "闸机1"}}]
    assert deliveries(env, recipient_id=10, channel="sms")[0].errcode == "no_phone"
    assert service.unread_counts(env.db, "staff", 9) == {"total": 1, "by_category": {"staff_alert": 1}}


def test_sms_minute_and_daily_limits(env):
    set_config(sms_per_day=2)
    _offline(env, "a")
    _offline(env, "b")
    _offline(env, "c")
    assert run(env) == {"sent": 1, "deferred": 2}  # 每分钟 1 条
    env.clock.advance(60)
    assert run(env) == {"sent": 1, "skipped": 1}  # b 发出后当日 2 条已满，c 跳过
    assert deliveries(env, channel="sms")[2].errcode == "sms_rate_limited"
    env.clock.advance(60)
    assert run(env) == {}
    assert len(env.sms.calls) == 2


def test_sms_vendor_rate_limit_and_retry(env):
    env.sms.script = ["LimitExceeded.PhoneNumberDailyLimit", "InternalError.Timeout", ""]
    _offline(env, "a")
    assert run(env) == {"skipped": 1}
    env.clock.advance(60)
    _offline(env, "b")
    assert run(env) == {"retry": 1}
    env.clock.advance(60)
    assert run(env) == {"sent": 1}


def test_sms_disabled_template_skips(env):
    service.upsert_template(env.db, 1, "staff.device_offline", "sms", {"enabled": False, "template_code": "",
                                                                      "params": []})
    env.db.commit()
    _offline(env)
    assert deliveries(env, channel="sms") == []


def test_notify_staff_validation(env):
    with pytest.raises(AppError):
        service.notify_staff(env.db, "staff.device_offline", [], {}, "k")


# ======================================================================== 事件绑定


def test_event_bindings(env):
    # 09 的真实字段：business_no / business_id 映射为模板的 order_no / order_id；只有订单支付发“支付成功”
    payload = {"payment_id": 9, "business_type": "order", "business_id": 5, "business_no": "NO5", "user_id": 1,
               "store_id": None, "amount_cents": 990, "out_trade_no": "P1", "transaction_id": "T1",
               "paid_at": "2026-10-28T02:00:00+00:00", "needs_manual": False, "source": "notify", "event_id": "e-1"}
    assert consumers.handle_event(env.db, "payment.succeeded", payload) == 1
    assert consumers.handle_event(env.db, "payment.succeeded", {**payload, "event_id": "e-2"}) == 0  # 同订单重复事件
    assert consumers.handle_event(env.db, "payment.succeeded",
                                  {**payload, "business_type": "wallet_recharge", "out_trade_no": "P2"}) == 0
    req = env.db.execute(select(NotificationRequest)).scalar_one()
    assert (req.scene, req.dedupe_key) == ("order.paid", "pay:P1")
    kit.grant(env, 1, "TPL_PAID")
    run(env)
    call = env.sub.calls[0]
    assert call["data"]["time4"] == {"value": "2026年10月28日 10:00"}
    assert call["data"]["character_string1"] == {"value": "NO5"} and call["page"] == "pages/order/detail?id=5"
    assert consumers.handle_event(env.db, "unknown.topic", {"user_id": 1}) == 0
    assert consumers.handle_event(env.db, "refund.succeeded", {"out_refund_no": "R1"}) == 0  # 09 退款事件不再绑定
    refund = {"order_id": 5, "order_no": "NO5", "user_id": 1, "store_id": None, "status": "paid", "total_cents": 990,
              "amount_cents": 300, "refunded_cents": 300, "full": False, "ref": "after_sales:7"}
    assert consumers.handle_event(env.db, "order.refunded", refund, "e-4") == 1
    assert consumers.handle_event(env.db, "order.refunded", refund, "e-5") == 0  # 同一结算重投
    card = {"card_id": 3, "user_id": 1, "days_left": 3, "tier": 3, "ends_at": "2026-10-31T16:00:00+00:00"}
    assert consumers.handle_event(env.db, "member_card.expiring", card, "e-6") == 1
    req = env.db.execute(select(NotificationRequest).where(NotificationRequest.scene == "card.expiring")).scalar_one()
    assert req.dedupe_key == "card:3:3" and '"expire_date": {"__d__": "2026-11-01"}' in req.data_json  # 上海时区日期
    assert "device.offline" not in consumers.EVENT_BINDINGS and "booking.reminder" not in consumers.EVENT_BINDINGS


def test_event_binding_override(env):
    consumers.set_event_binding("pay.done", [consumers.EventBinding("order.paid", "member_id", "x:{no}")])
    try:
        assert consumers.handle_event(env.db, "pay.done", {"member_id": 2, "no": "9", **kit.paid_data(paid_at="10:00")}) == 1
    finally:
        consumers.set_event_binding("pay.done", None)
    assert "pay.done" not in consumers.EVENT_BINDINGS


# ======================================================================== 站内信


def test_inbox_lifecycle(env):
    for i in range(3):
        kit.accept(env, "order.paid", key=f"k{i}")
    kit.accept(env, "order.paid", member=2, key="other")
    assert service.unread_counts(env.db, "member", 1)["total"] == 3
    page = service.list_inbox(env.db, "member", 1, {"limit": "2"})
    assert len(page["items"]) == 2 and page["has_more"]
    rest = service.list_inbox(env.db, "member", 1, {"limit": "2", "cursor": page["next_cursor"]})
    assert len(rest["items"]) == 1 and not rest["has_more"]
    first = page["items"][0]["id"]
    service.mark_read(env.db, "member", 1, first)
    env.db.commit()
    assert service.unread_counts(env.db, "member", 1)["total"] == 2
    unread = service.list_inbox(env.db, "member", 1, {"read_at_null": "true"})
    assert first not in [m["id"] for m in unread["items"]]
    other = env.db.execute(select(NotificationInbox.id).where(NotificationInbox.recipient_id == 2)).scalar_one()
    for fn in (service.mark_read, service.delete_message):
        with pytest.raises(AppError) as exc:
            fn(env.db, "member", 1, other)
        assert exc.value.code == ErrorCode.NOT_FOUND
    assert service.mark_all_read(env.db, "member", 1, "transaction")["updated"] == 2
    service.delete_message(env.db, "member", 1, first)
    env.db.commit()
    assert len(service.list_inbox(env.db, "member", 1, {})["items"]) == 2
    with pytest.raises(AppError):
        service.mark_all_read(env.db, "member", 1, "bogus")
    with pytest.raises(AppError):
        service.list_inbox(env.db, "member", 1, {"bogus": "1"})


# ======================================================================== 后台


def test_resend_failed_only(env):
    kit.grant(env, 1, "TPL_PAID")
    env.sub.script = [40037]
    kit.accept(env, "order.paid")
    run(env)
    d = deliveries(env, channel="subscribe")[0]
    assert d.status == "failed"
    assert service.resend_delivery(env.db, 7, d.id, "模板已修正", now=env.now)["status"] == "pending"
    env.db.commit()
    audit = env.db.execute(select(AuditLog).where(AuditLog.action == "notification.delivery.resend")).scalar_one()
    assert audit.actor_id == "7" and audit.reason == "模板已修正"
    assert run(env) == {"sent": 1}
    with pytest.raises(AppError) as exc:
        service.resend_delivery(env.db, 7, d.id)
    assert exc.value.code == ErrorCode.CONFLICT
    with pytest.raises(AppError) as exc:
        service.resend_delivery(env.db, 7, 999)
    assert exc.value.code == ErrorCode.NOT_FOUND


def test_list_and_stats(env):
    kit.grant(env, 1, "TPL_PAID")
    kit.accept(env, "order.paid", key="a")
    kit.accept(env, "order.paid", key="b")
    kit.accept(env, "order.paid", member=2, key="c")
    run(env)  # a 发出；b、c 无额度跳过
    page = service.list_deliveries(env.db, {"channel": "subscribe", "sort": "-created_at"})
    assert page["total"] == 3
    assert service.list_deliveries(env.db, {"status": "failed,skipped"})["total"] == 2
    assert service.list_deliveries(env.db, {"keyword": "2", "channel": "subscribe"})["total"] == 1
    assert service.list_deliveries(env.db, {"errcode": "no_quota"})["total"] == 2
    with pytest.raises(AppError):
        service.list_deliveries(env.db, {"status": "weird"})
    s = service.stats(env.db, {"created_at_from": "2026-10-28", "created_at_to": "2026-10-28"})
    assert s["sent"] == 4  # 3 站内信 + 1 订阅
    assert s["by_channel"]["subscribe"] == {"pending": 0, "sending": 0, "sent": 1, "failed": 0, "skipped": 2}
    assert s["failed_by_errcode"] == [{"channel": "subscribe", "errcode": "no_quota", "count": 2}]
    assert (s["subscribe_total"], s["quota_exhausted"], s["quota_exhaustion_rate"]) == (3, 2, 0.6667)


def test_export_csv(env):
    kit.accept(env, "order.paid")
    content = service.export_deliveries(env.db, 3, {"channel": "inapp"})
    text = content.decode("utf-8-sig")
    assert text.splitlines()[0].startswith("ID,场景,渠道") and "order.paid" in text
    assert env.db.execute(select(AuditLog).where(AuditLog.action == "notification.delivery.export")).scalar_one()


def test_template_crud_and_validation(env):
    out = service.create_scene(env.db, 1, {"scene": "queue.called", "name": "叫号", "category": "transaction",
                                           "urgent": True})
    env.db.commit()
    assert out["scene"] == "queue.called"
    with pytest.raises(AppError) as exc:
        service.create_scene(env.db, 1, {"scene": "queue.called", "name": "x"})
    assert exc.value.code == ErrorCode.CONFLICT
    for bad in ({"template_id": "T", "keywords": [{"key": "foo1", "source": "x"}]},
                {"template_id": "T", "keywords": [{"key": "thing1", "source": "x"}], "page": "https://evil"},
                {"keywords": [{"key": "thing1", "source": "x"}]}):
        with pytest.raises(AppError) as exc:
            service.upsert_template(env.db, 1, "queue.called", "subscribe", bad)
        assert exc.value.code == ErrorCode.BAD_REQUEST
    with pytest.raises(AppError):
        service.upsert_template(env.db, 1, "staff.device_offline", "subscribe",
                                {"template_id": "T", "keywords": [{"key": "thing1", "source": "x"}]})
    with pytest.raises(AppError):
        service.upsert_template(env.db, 1, "queue.called", "inapp", {"title_tpl": "{a.b}", "body_tpl": "x"})
    t = service.upsert_template(env.db, 1, "queue.called", "subscribe",
                                {"template_id": "TPL_Q", "keywords": [{"key": "character_string1", "source": "ticket"}]})
    t2 = service.upsert_template(env.db, 1, "queue.called", "subscribe",
                                 {"template_id": "TPL_Q2", "keywords": [{"key": "character_string1", "source": "ticket"}]})
    assert (t["version"], t2["version"], t2["template_id"]) == (1, 2, "TPL_Q2")
    service.update_scene(env.db, 1, "queue.called", {"sms_mode": "fallback"})
    with pytest.raises(AppError):
        service.update_scene(env.db, 1, "queue.called", {"sms_mode": "sometimes"})
    detail = service.get_scene(env.db, "queue.called")
    assert detail["sms_mode"] == "fallback" and "subscribe" in detail["templates"]
    service.delete_template(env.db, 1, "queue.called", "subscribe")
    with pytest.raises(AppError):
        service.delete_template(env.db, 1, "queue.called", "subscribe")
    service.delete_scene(env.db, 1, "queue.called")
    env.db.commit()
    with pytest.raises(AppError):
        service.get_scene(env.db, "queue.called")
    actions = {a for (a,) in env.db.execute(select(AuditLog.action))}
    assert {"notification.scene.create", "notification.template.update", "notification.scene.delete"} <= actions
    assert service.list_scenes(env.db, {"audience": "staff"})["total"] == 2


def test_subscribe_request_plan(env):
    assert service.subscribe_request_plan(env.db, ["order.paid", "card.expiring"]) == {
        "template_ids": ["TPL_PAID", "TPL_CARD"], "long_term": False}
    with pytest.raises(AppError):
        service.subscribe_request_plan(env.db, [f"s.{i}" for i in range(6)])
    service.upsert_template(env.db, 1, "card.expiring", "subscribe", {**kit.TEMPLATES[("card.expiring", "subscribe")],
                                                                       "long_term": True})
    with pytest.raises(AppError) as exc:
        service.subscribe_request_plan(env.db, ["order.paid", "card.expiring"])
    assert exc.value.code == ErrorCode.BUSINESS_RULE


def test_seed_is_idempotent(env):
    assert service.ensure_default_scenes(env.db) == 0


def test_purge_old(env):
    kit.accept(env, "order.paid")
    service.purge_old(env.db, now=env.now + timedelta(days=400))
    assert env.db.execute(select(NotificationInbox)).first() is None


# ======================================================================== wechat_mp 适配器


def test_wechat_mp_adapter_maps_results():
    calls = []

    class WechatError(Exception):
        def __init__(self, errcode, errmsg):
            self.errcode, self.errmsg = errcode, errmsg

    def fake(**kw):
        calls.append(kw)
        if kw["touser"] == "bad":
            raise WechatError(47003, "argument invalid! data.thing1.value")
        if kw["touser"] == "net":
            raise TransientError("timeout")
        return {"errcode": 0, "errmsg": "ok", "msgid": 42}

    sender = channels.WechatMpSubscribeSender(fake)
    args = {"template_id": "T", "data": {}, "page": None, "miniprogram_state": "formal", "lang": "zh_CN"}
    ok = sender.send(touser="o", **args)
    assert ok.ok and ok.msg_id == "42" and "page" not in calls[0]
    bad = sender.send(touser="bad", **args)
    assert (bad.ok, bad.errcode) == (False, "47003")
    with pytest.raises(TransientError):
        sender.send(touser="net", **args)


# ======================================================================== 并发


def _file_env(tmp_path):
    url = f"sqlite:///{tmp_path / 'race.db'}"
    engine = create_engine(url, future=True, connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def test_concurrent_quota_reservation_never_overdraws(tmp_path):
    factory = _file_env(tmp_path)
    with factory() as s:
        s.add(S(member_id=1, template_id="T", available=10, status="active"))
        s.commit()

    def take(_):
        with factory() as s:
            state = service._reserve_quota(s, 1, "T")
            s.commit()
            return state

    with ThreadPoolExecutor(max_workers=50) as pool:
        results = list(pool.map(take, range(50)))
    assert results.count("reserved") == 10
    with factory() as s:
        assert s.execute(select(S.available)).scalar_one() == 0


def test_concurrent_counter_never_exceeds_limit(tmp_path):
    factory = _file_env(tmp_path)

    def bump(_):
        with factory() as s:
            return service._bump(s, "sms:x", "d:1", 7)

    with ThreadPoolExecutor(max_workers=50) as pool:
        results = list(pool.map(bump, range(50)))
    assert results.count(True) == 7


def test_concurrent_workers_send_once(tmp_path):
    e = kit.make_env(url=f"sqlite:///{tmp_path / 'w.db'}")
    try:
        kit.grant(e, 1, "TPL_PAID", times=5)
        kit.accept(e, "order.paid")
        target = deliveries(e, channel="subscribe")[0].id
        from luzback.db import get_session_factory
        factory = get_session_factory()

        def work(_):
            with factory() as s:
                return service.process_delivery(s, target, e.now)

        with ThreadPoolExecutor(max_workers=10) as pool:
            outcomes = list(pool.map(work, range(10)))
        assert outcomes.count("sent") == 1 and len(e.sub.calls) == 1
        assert sub_row(e, 1, "TPL_PAID").available == 4
    finally:
        e.db.close()
        kit.teardown()


# ======================================================================== 任务与默认渠道


def test_jobs_registered_and_idempotent(env):
    from luzback.scheduler import JobRegistry
    from notification import jobs

    registry = JobRegistry()
    jobs.register(registry)
    assert {j.name for j in registry.all()} == {"notification.dispatch", "notification.reclaim", "notification.purge"}
    kit.grant(env, 1, "TPL_PAID")
    kit.accept(env, "order.paid")
    from notification.config import set_clock
    set_clock(env.clock)
    assert jobs.dispatch_deliveries(env.db) == "sent=1"
    assert jobs.dispatch_deliveries(env.db) == "idle"
    assert jobs.reclaim_stuck_deliveries(env.db) == "reclaimed=0"
    assert jobs.purge_notifications(env.db).startswith("counters=")


def test_default_senders_without_wechat_mp(env, monkeypatch):
    monkeypatch.setattr(channels, "wechat_mp_available", lambda: False)
    channels.set_subscribe_sender(None)
    channels.set_sms_sender(None)
    monkeypatch.setenv("LUZ_ENV", "prod")
    assert isinstance(channels.get_subscribe_sender(), channels.NotConfiguredSender)
    assert isinstance(channels.get_sms_sender(), channels.NotConfiguredSender)
    channels.set_subscribe_sender(None)
    monkeypatch.setenv("LUZ_ENV", "dev")
    assert isinstance(channels.get_subscribe_sender(), channels.FakeSubscribeSender)


def test_not_configured_channel_fails_permanently(env):
    channels.set_subscribe_sender(channels.NotConfiguredSender())
    kit.grant(env, 1, "TPL_PAID")
    kit.accept(env, "order.paid")
    assert run(env) == {"failed": 1}
    d = deliveries(env, channel="subscribe")[0]
    assert d.errcode == "channel_not_configured" and sub_row(env, 1, "TPL_PAID").available == 1


# ======================================================================== 事件订阅约定（25-jobs worker）


def test_events_subscriptions_contract(env):
    from notification import events

    assert {"notification.requested", "payment.succeeded", "order.refunded", "member_card.expiring",
            "lottery.prize_won"} <= set(events.SUBSCRIPTIONS)
    handler = events.SUBSCRIPTIONS["lottery.prize_won"]
    payload = {"draw_id": 31, "activity_id": 2, "member_id": 1, "prize_kind": "physical", "prize_name": "保温杯",
               "delivery_key": "lottery:31"}
    assert handler(env.db, payload, "ev-1") == 1
    env.db.rollback()  # 不提交：worker 回滚则什么都不留下
    assert env.db.execute(select(NotificationRequest)).first() is None
    assert handler(env.db, payload, "ev-1") == 1
    env.db.commit()
    assert handler(env.db, payload, "ev-1") == 0  # 重投幂等
    msg = env.db.execute(select(NotificationInbox)).scalar_one()
    assert msg.body == "您抽中了保温杯，请尽快填写收货信息领取" and msg.page == "pages/lottery/prize?draw_id=31"
    # 非实物奖品（券、积分）不发领取提醒
    assert handler(env.db, {**payload, "draw_id": 32, "prize_kind": "coupon"}, "ev-2") == 0
    consumers.set_event_binding("lottery.won", [consumers.EventBinding("lottery.prize_won", "member_id", "p:{draw_id}")])
    assert "lottery.won" in events.SUBSCRIPTIONS
    consumers.set_event_binding("lottery.won", None)
    assert "lottery.won" not in events.SUBSCRIPTIONS
