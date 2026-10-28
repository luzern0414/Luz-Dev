"""wechat_mp 数据模型：access_token 中控存储（多实例共享）、消息推送去重记录。

- access_token 字段按官方要求至少保留 512 字符，这里给 1024。
- 推送去重表只存事件类型与去重键，不存消息正文（含 openid 等个人信息）。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, String
from sqlalchemy.orm import Mapped, mapped_column

from luzback import Base, BigIdMixin, utcnow


class MpAccessToken(Base, BigIdMixin):
    __tablename__ = "wechat_mp_access_tokens"

    cache_key: Mapped[str] = mapped_column(String(128), unique=True)
    token: Mapped[str] = mapped_column(String(1024))
    expires_at: Mapped[float] = mapped_column(Float)   # epoch 秒
    refresh_at: Mapped[float] = mapped_column(Float)   # epoch 秒，到点后再取
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class MpPushEvent(Base, BigIdMixin):
    __tablename__ = "wechat_mp_push_events"

    dedup_key: Mapped[str] = mapped_column(String(200), unique=True)
    event: Mapped[str] = mapped_column(String(64), index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
