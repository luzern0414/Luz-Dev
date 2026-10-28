"""notification：通知中心（订阅消息 / 短信 / 站内信）。

业务模块只用：notify(db, scene, member_id, data, dedupe_key)、notify_staff(db, scene, staff_ids, data, dedupe_key)。
消费者：consumers.outbox_handler / consume_message / handle_event；wechat_mp 推送：handle_push_message(db, message)（不提交，已登记到 wechat_mp）/ handle_push_event。
扩展点：channels.set_subscribe_sender / set_sms_sender / set_recipient_resolver / set_store_timezone_resolver，
consumers.set_event_binding，config.set_config，auth.set_member_checker（后台鉴权固定走 admin_rbac）。
"""
from . import models  # noqa: F401  (注册 ORM 表)
from .channels import (
    FakeSmsSender, FakeSubscribeSender, InMemoryRecipients, Recipient, SendResult, SmsSender, SubscribeMessageSender,
    WechatMpSubscribeSender, set_recipient_resolver, set_sms_sender, set_store_timezone_resolver, set_subscribe_sender,
    wechat_mp_available,
)
from .config import NotificationConfig, get_config, set_clock, set_config
from .consumers import EVENT_BINDINGS, EventBinding, consume_message, handle_event, outbox_handler, set_event_binding
from .models import (
    NotificationDelivery, NotificationInbox, NotificationRequest, NotificationScene, NotificationSubscription,
    NotificationTemplate,
)
from .service import (
    TOPIC_REQUESTED, accept_request, dispatch_due, ensure_default_scenes, handle_push_event, handle_push_message, notify, notify_staff,
    process_delivery, reclaim_stuck, report_subscriptions, unread_counts,
)
from .auth import PERMISSIONS, set_member_checker

__all__ = [
    "FakeSmsSender", "FakeSubscribeSender", "InMemoryRecipients", "Recipient", "SendResult", "SmsSender",
    "SubscribeMessageSender", "WechatMpSubscribeSender", "set_recipient_resolver", "set_sms_sender",
    "set_store_timezone_resolver", "set_subscribe_sender", "wechat_mp_available",
    "NotificationConfig", "get_config", "set_clock", "set_config",
    "EVENT_BINDINGS", "EventBinding", "consume_message", "handle_event", "outbox_handler", "set_event_binding",
    "NotificationDelivery", "NotificationInbox", "NotificationRequest", "NotificationScene", "NotificationSubscription",
    "NotificationTemplate",
    "TOPIC_REQUESTED", "accept_request", "dispatch_due", "ensure_default_scenes", "handle_push_event", "handle_push_message", "notify",
    "notify_staff", "process_delivery", "reclaim_stuck", "report_subscriptions", "unread_counts",
    "PERMISSIONS", "set_member_checker",
]
