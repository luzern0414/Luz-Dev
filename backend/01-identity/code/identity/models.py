"""identity 数据模型：用户、短信验证码。

- 手机号加密存储（phone_enc）并保存可查询哈希（phone_hash，唯一）；响应只给脱敏值。
- refresh_version 用于刷新令牌轮换与强制下线；status 控制冻结与注销。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from luzback import Base, BigIdMixin, SoftDeleteMixin, TimestampMixin

STATUS_ACTIVE = "active"
STATUS_FROZEN = "frozen"
STATUS_DEACTIVATED = "deactivated"


class User(Base, BigIdMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "users"
    __table_args__ = (Index("ix_users_phone_hash", "phone_hash", unique=True),)

    openid: Mapped[str] = mapped_column(String(64), unique=True)
    unionid: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    nickname: Mapped[str] = mapped_column(String(64), default="")
    avatar_url: Mapped[str] = mapped_column(String(500), default="")
    gender: Mapped[int] = mapped_column(Integer, default=0)  # 0 未知 1 男 2 女
    birthday: Mapped[str | None] = mapped_column(String(10), nullable=True)  # YYYY-MM-DD
    phone_enc: Mapped[str | None] = mapped_column(String(200), nullable=True)
    phone_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    phone_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=STATUS_ACTIVE, index=True)
    refresh_version: Mapped[int] = mapped_column(Integer, default=0)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SmsCode(Base, BigIdMixin):
    __tablename__ = "sms_codes"
    __table_args__ = (Index("ix_sms_codes_phone_purpose", "phone_hash", "purpose"),)

    phone_hash: Mapped[str] = mapped_column(String(64))
    purpose: Mapped[str] = mapped_column(String(16))  # bind | login
    code_hash: Mapped[str] = mapped_column(String(64))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
