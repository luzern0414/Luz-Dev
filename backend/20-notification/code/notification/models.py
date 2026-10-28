"""notification 表结构。表名统一前缀 notification_，避免与其他模块冲突。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from luzback import Base, BigIdMixin, TimestampMixin, utcnow
from luzback.statemachine import StateMachine

# ---- 枚举 ----
CHANNEL_SUBSCRIBE = "subscribe"
CHANNEL_SMS = "sms"
CHANNEL_INAPP = "inapp"
CHANNELS = (CHANNEL_SUBSCRIBE, CHANNEL_SMS, CHANNEL_INAPP)

AUDIENCE_MEMBER = "member"
AUDIENCE_STAFF = "staff"
AUDIENCES = (AUDIENCE_MEMBER, AUDIENCE_STAFF)

CATEGORIES = ("transaction", "reminder", "security", "marketing", "staff_alert", "system")
SMS_MODES = ("off", "fallback", "always")

ST_PENDING = "pending"
ST_SENDING = "sending"
ST_SENT = "sent"
ST_FAILED = "failed"
ST_SKIPPED = "skipped"
DELIVERY_STATUSES = (ST_PENDING, ST_SENDING, ST_SENT, ST_FAILED, ST_SKIPPED)

DELIVERY_SM = StateMachine(
    "notification_delivery",
    {
        ST_PENDING: {ST_SENDING, ST_SENT, ST_SKIPPED},      # sent / skipped：站内信直接写入、无收件人直接跳过
        ST_SENDING: {ST_SENT, ST_PENDING, ST_FAILED, ST_SKIPPED},
        ST_FAILED: {ST_PENDING},                            # 后台重发
    },
    terminal=frozenset({ST_SENT, ST_SKIPPED}),
)

SUB_ACTIVE = "active"
SUB_REJECTED = "rejected"
SUB_BANNED = "banned"

BigId = BigInteger().with_variant(Integer, "sqlite")


class NotificationScene(Base, BigIdMixin, TimestampMixin):
    """业务场景：order.paid、card.expiring … 决定类别、受众、是否紧急、短信策略。"""

    __tablename__ = "notification_scenes"

    scene: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(64))
    category: Mapped[str] = mapped_column(String(16), default="transaction")
    audience: Mapped[str] = mapped_column(String(8), default=AUDIENCE_MEMBER)
    urgent: Mapped[bool] = mapped_column(Boolean, default=False)          # 紧急：不受静默时段与每会员频控
    sms_mode: Mapped[str] = mapped_column(String(8), default="off")       # off | fallback（订阅失败后补发）| always
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[int | None] = mapped_column(BigId, nullable=True)
    updated_by: Mapped[int | None] = mapped_column(BigId, nullable=True)


class NotificationTemplate(Base, BigIdMixin, TimestampMixin):
    """场景 × 渠道的模板映射（后台可配）。

    subscribe：template_ref = 微信模板 id，keywords_json = [{"key": "thing1", "source": "{product_name}"}]，long_term。
    sms：template_ref = 短信模板 code，keywords_json = [{"name": "p1", "source": "order_no"}]。
    inapp：title_tpl / body_tpl（{field} 占位符）。page：小程序跳转页（订阅消息与站内信共用）。
    """

    __tablename__ = "notification_templates"
    __table_args__ = (UniqueConstraint("scene", "channel", name="uq_notification_templates_scene_channel"),)

    scene: Mapped[str] = mapped_column(String(64), index=True)
    channel: Mapped[str] = mapped_column(String(16))
    template_ref: Mapped[str] = mapped_column(String(128), default="")
    keywords_json: Mapped[str] = mapped_column(Text, default="[]")
    title_tpl: Mapped[str] = mapped_column(String(200), default="")
    body_tpl: Mapped[str] = mapped_column(String(1000), default="")
    page: Mapped[str] = mapped_column(String(256), default="")
    long_term: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[int | None] = mapped_column(BigId, nullable=True)
    updated_by: Mapped[int | None] = mapped_column(BigId, nullable=True)


class NotificationRequest(Base, BigIdMixin):
    """一次 notify 调用（每个收件人一行）。去重键 (scene, audience, recipient_id, dedupe_key) 唯一。"""

    __tablename__ = "notification_requests"
    __table_args__ = (
        UniqueConstraint("scene", "audience", "recipient_id", "dedupe_key", name="uq_notification_requests_dedupe"),
    )

    scene: Mapped[str] = mapped_column(String(64))
    audience: Mapped[str] = mapped_column(String(8))
    recipient_id: Mapped[int] = mapped_column(BigId)
    store_id: Mapped[int | None] = mapped_column(BigId, nullable=True)
    dedupe_key: Mapped[str] = mapped_column(String(128))
    data_json: Mapped[str] = mapped_column(Text, default="{}")
    source_event_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class NotificationDelivery(Base, BigIdMixin, TimestampMixin):
    """每个请求每个渠道一行；投递状态、错误码、重试时间都在这里。"""

    __tablename__ = "notification_deliveries"
    __table_args__ = (
        UniqueConstraint("request_id", "channel", name="uq_notification_deliveries_request_channel"),
        Index("ix_notification_deliveries_due", "status", "next_attempt_at"),
        Index("ix_notification_deliveries_recipient", "audience", "recipient_id"),
    )

    request_id: Mapped[int] = mapped_column(BigId, index=True)
    scene: Mapped[str] = mapped_column(String(64))
    channel: Mapped[str] = mapped_column(String(16))
    audience: Mapped[str] = mapped_column(String(8))
    recipient_id: Mapped[int] = mapped_column(BigId)
    recipient_ref: Mapped[str] = mapped_column(String(32), default="")   # 关键词检索用：会员号 / staff:id（不含 PII）
    store_id: Mapped[int | None] = mapped_column(BigId, nullable=True)
    urgent: Mapped[bool] = mapped_column(Boolean, default=False)
    template_ref: Mapped[str] = mapped_column(String(128), default="")
    status: Mapped[str] = mapped_column(String(16), default=ST_PENDING)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    errcode: Mapped[str | None] = mapped_column(String(64), nullable=True)
    errmsg: Mapped[str | None] = mapped_column(String(200), nullable=True)
    provider_msg_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    quota_reserved: Mapped[bool] = mapped_column(Boolean, default=False)
    counted_keys: Mapped[str | None] = mapped_column(String(400), nullable=True)  # 已占用的频控计数键（失败时退回）
    fallback_from_id: Mapped[int | None] = mapped_column(BigId, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class NotificationSubscription(Base, BigIdMixin):
    """订阅额度：每个 (会员, 模板) 一行；一次性模板每次 accept 可下发一条。"""

    __tablename__ = "notification_subscriptions"
    __table_args__ = (UniqueConstraint("member_id", "template_id", name="uq_notification_subscriptions_member_tpl"),)

    member_id: Mapped[int] = mapped_column(BigId)
    template_id: Mapped[str] = mapped_column(String(128))
    available: Mapped[int] = mapped_column(Integer, default=0)
    long_term: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default=SUB_ACTIVE)
    accepted_total: Mapped[int] = mapped_column(Integer, default=0)
    consumed_total: Mapped[int] = mapped_column(Integer, default=0)
    last_accept_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_reject_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class NotificationSubscribeEvent(Base, BigIdMixin):
    """授权 / 拒收 / 下发结果的原始记录（前端上报与消息推送），dedupe_key 唯一防重复推送。"""

    __tablename__ = "notification_subscribe_events"

    dedupe_key: Mapped[str] = mapped_column(String(200), unique=True)
    source: Mapped[str] = mapped_column(String(16))       # frontend | push
    event: Mapped[str] = mapped_column(String(16))        # report | popup | change | sent
    member_id: Mapped[int | None] = mapped_column(BigId, nullable=True, index=True)
    template_id: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), default="")
    msg_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    counted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class NotificationInbox(Base, BigIdMixin):
    """站内信（会员与员工共用）。"""

    __tablename__ = "notification_inbox"
    __table_args__ = (
        UniqueConstraint("request_id", name="uq_notification_inbox_request"),
        Index("ix_notification_inbox_owner", "recipient_type", "recipient_id", "deleted_at", "read_at"),
    )

    recipient_type: Mapped[str] = mapped_column(String(8))
    recipient_id: Mapped[int] = mapped_column(BigId)
    request_id: Mapped[int | None] = mapped_column(BigId, nullable=True)
    scene: Mapped[str] = mapped_column(String(64))
    category: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(String(1000))
    page: Mapped[str] = mapped_column(String(256), default="")
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class NotificationCounter(Base, BigIdMixin):
    """频控与日上限计数：key（如 sms:phone_hash / member:12 / app:subscribe）× bucket（窗口）。"""

    __tablename__ = "notification_counters"
    __table_args__ = (UniqueConstraint("key", "bucket", name="uq_notification_counters_key_bucket"),)

    key: Mapped[str] = mapped_column(String(128))
    bucket: Mapped[str] = mapped_column(String(32))
    count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
