"""事件消费：业务事件 → 通知场景。主题与字段名集中在 EVENT_BINDINGS 一张表里，其他模块改名只改这里。

接入方式（任选其一）：
- 发件箱直连：dispatch_pending(db, outbox_handler)（handler 自己开会话）；
- 消息队列：broker.consume(topic, "notification", consume_message)。
消费幂等：每条绑定生成去重键，accept_request 按 (scene, 受众, 收件人, 去重键) 唯一，重复投递自动跳过。
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from luzback.db import get_session_factory

from . import rules
from .models import AUDIENCE_MEMBER, AUDIENCE_STAFF
from .service import TOPIC_REQUESTED, accept_request, decode_data, encode_data

log = logging.getLogger("luz.notification")


@dataclass(frozen=True)
class EventBinding:
    """topic → scene。recipient_field：会员 id 字段（受众 staff 时为员工 id 列表字段）；
    dedupe：去重键模板（{字段}），缺字段时退回 event_id；fields：None 表示整个 payload 作为模板数据。"""

    scene: str
    recipient_field: str = "user_id"
    dedupe: str = "{event_id}"
    audience: str = AUDIENCE_MEMBER
    store_field: str = "store_id"
    fields: tuple[str, ...] | None = None
    when: tuple[tuple[str, Any], ...] = ()  # 过滤条件：payload[字段] == 值 全部满足才通知


# 事件名以各模块 MODULE.md 为准（09-wechat-pay：payment.succeeded；退款与会员卡事件名在对应模块定稿后在此调整）
EVENT_BINDINGS: dict[str, list[EventBinding]] = {
    "payment.succeeded": [EventBinding("order.paid", "user_id", "pay:{out_trade_no}")],
    "refund.succeeded": [EventBinding("refund.succeeded", "user_id", "refund:{out_refund_no}")],
    "member_card.expiring": [EventBinding("card.expiring", "user_id", "card:{card_id}:{expire_date}")],
    "booking.reminder": [EventBinding("booking.reminder", "user_id", "booking:{booking_id}:remind")],
    "account.security_alert": [EventBinding("security.alert", "user_id", "sec:{event_id}")],
    "lottery.prize_won": [EventBinding("lottery.prize_won", "member_id", "prize:{draw_id}",
                                       when=(("prize_kind", "physical"),))],
    "device.offline": [EventBinding("staff.device_offline", "staff_ids", "device:{device_id}:{offline_at}",
                                    audience=AUDIENCE_STAFF)],
}


def subscriber(topic: str):
    """25-jobs worker 约定的订阅者：handler(db, payload, event_id)，不提交、幂等（去重键唯一）。"""

    def handler(db: Session, payload: dict[str, Any], event_id: str) -> int:
        return handle_event(db, topic, payload, event_id, commit=False)

    handler.__name__ = f"notify_on_{topic.replace('.', '_')}"
    return handler


# events.SUBSCRIPTIONS 就是这个字典（同一对象）；set_event_binding 同步增删
SUBSCRIPTIONS: dict[str, Any] = {}


def _sync_subscriptions() -> None:
    wanted = {"notification.requested", *EVENT_BINDINGS}
    for topic in list(SUBSCRIPTIONS):
        if topic not in wanted:
            SUBSCRIPTIONS.pop(topic)
    for topic in wanted:
        SUBSCRIPTIONS.setdefault(topic, subscriber(topic))


def set_event_binding(topic: str, bindings: list[EventBinding] | None) -> None:
    """项目定制：改主题名 / 字段名 / 新增绑定；None 删除。"""
    if bindings is None:
        EVENT_BINDINGS.pop(topic, None)
    else:
        EVENT_BINDINGS[topic] = list(bindings)
    _sync_subscriptions()


def _dedupe_key(template: str, payload: Mapping[str, Any], event_id: str | None) -> str | None:
    try:
        key = rules.render_text(template, {**payload, "event_id": event_id})
    except (rules.MissingField, ValueError):
        key = event_id
    return key[:128] if key else None


_sync_subscriptions()


def _coerce(data: dict[str, Any]) -> dict[str, Any]:
    """事件 payload 经 JSON 后时间是字符串：*_at → datetime，*_date → date，便于关键词按类型格式化。"""
    out = dict(data)
    for key, value in data.items():
        if not isinstance(value, str):
            continue
        try:
            if key.endswith("_at"):
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                out[key] = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            elif key.endswith("_date") and len(value) == 10:
                out[key] = date.fromisoformat(value)
        except ValueError:
            continue
    return out


def handle_event(db: Session, topic: str, payload: Mapping[str, Any], event_id: str | None = None,
                 commit: bool = True) -> int:
    """处理一个事件，返回新建的通知请求数。未绑定的主题返回 0。commit=False 时不提交（events.SUBSCRIPTIONS）。"""
    event_id = event_id or payload.get("event_id")
    if topic == TOPIC_REQUESTED:
        return len(accept_request(
            db, scene=payload["scene"], audience=payload["audience"], recipient_ids=list(payload["recipient_ids"]),
            data=payload.get("data") or {}, dedupe_key=payload["dedupe_key"], store_id=payload.get("store_id"),
            source_event_id=event_id, commit=commit))
    created = 0
    for binding in EVENT_BINDINGS.get(topic, []):
        if any(payload.get(field) != value for field, value in binding.when):
            continue
        raw = payload.get(binding.recipient_field)
        if raw is None:
            log.warning("event %s missing %s; notification skipped", topic, binding.recipient_field)
            continue
        ids = [int(x) for x in (raw if isinstance(raw, (list, tuple)) else [raw])]
        key = _dedupe_key(binding.dedupe, payload, event_id)
        if not key:
            log.warning("event %s has no dedupe key; notification skipped", topic)
            continue
        data = dict(payload) if binding.fields is None else {f: payload.get(f) for f in binding.fields}
        data = {k: v for k, v in data.items() if k not in ("event_id", "topic", "published_at")}
        created += len(accept_request(
            db, scene=binding.scene, audience=binding.audience, recipient_ids=ids, data=encode_data(_coerce(decode_data(data))),
            dedupe_key=key, store_id=payload.get(binding.store_field), source_event_id=event_id,
            commit=commit))
    return created


def outbox_handler(topic: str, payload: dict[str, Any], event: Any = None) -> None:
    """dispatch_pending 的 handler：自己开会话处理；未绑定主题直接返回（不影响其他消费者）。"""
    if topic != TOPIC_REQUESTED and topic not in EVENT_BINDINGS:
        return
    with get_session_factory()() as db:
        handle_event(db, topic, payload, getattr(event, "event_id", None))


def consume_message(message: Any) -> None:
    """luzback.mq 消费者入口：Message(topic, payload)。"""
    outbox_handler(message.topic, message.payload, None)


# ---------------------------------------------------------------- wechat_mp 消息推送接入

SUBSCRIBE_PUSH_EVENTS = ("subscribe_msg_popup_event", "subscribe_msg_change_event", "subscribe_msg_sent_event")


def install_wechat_mp_push_handlers() -> bool:
    """wechat_mp 提供 register_push_handler(event, fn(db, message)) 时，登记订阅消息三类事件；否则返回 False。

    验签、解密、推送去重由 wechat_mp 负责；本模块只处理已解密的消息 dict（handle_push_event 自身也按事件去重）。
    """
    try:
        import wechat_mp  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return False
    register = getattr(wechat_mp, "register_push_handler", None)
    if not callable(register):
        return False
    from .service import handle_push_event  # noqa: PLC0415

    for name in SUBSCRIBE_PUSH_EVENTS:
        try:
            register(name, handle_push_event)
        except Exception:  # noqa: BLE001  (重复登记等)
            log.warning("register_push_handler(%s) failed", name, exc_info=True)
            return False
    return True
