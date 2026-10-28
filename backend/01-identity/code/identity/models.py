"""identity 数据模型：会员、会话（刷新令牌家族）、刷新令牌、短信验证码、资料内容审核。

- 手机号：phone_enc（AES-GCM 密文）+ phone_hash（HMAC，唯一，用于查询）+ phone_masked（展示与关键词搜索）。
- session_key：只在服务端加密保存（wx_session_key_enc），任何接口都不返回。
- 会话 = 刷新令牌家族（RFC 9700 §4.14.2）：sid 写进 access 令牌；家族吊销即该会话全部令牌失效。
- 刷新令牌、验证码只存哈希。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from luzback import Base, BigId, BigIdMixin, SoftDeleteMixin, TimestampMixin, utcnow

STATUS_ACTIVE = "active"
STATUS_DISABLED = "disabled"
STATUS_DELETING = "deleting"   # 注销冷静期
STATUS_DELETED = "deleted"     # 已匿名化（终态）
STATUSES = (STATUS_ACTIVE, STATUS_DISABLED, STATUS_DELETING, STATUS_DELETED)
# 兼容旧名
STATUS_FROZEN = STATUS_DISABLED
STATUS_DEACTIVATED = STATUS_DELETED

SESSION_ACTIVE = "active"
SESSION_REVOKED = "revoked"

TOKEN_ACTIVE = "active"
TOKEN_ROTATED = "rotated"
TOKEN_REVOKED = "revoked"

SMS_ACTIVE = "active"
SMS_USED = "used"
SMS_SUPERSEDED = "superseded"
SMS_FAILED = "failed"
SMS_PURPOSES = ("login", "bind")

CHECK_PENDING = "pending"
CHECK_PASS = "pass"
CHECK_RISKY = "risky"
CHECK_REVIEW = "review"
CHECK_FAILED = "failed"
CHECK_TIMEOUT = "timeout"
CHECK_SUPERSEDED = "superseded"


class User(Base, BigIdMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "users"
    __table_args__ = (
        Index("ix_users_phone_hash", "phone_hash", unique=True),
        Index("ix_users_status_created", "status", "created_at"),
    )

    member_no: Mapped[str] = mapped_column(String(16), unique=True)
    openid: Mapped[str] = mapped_column(String(64), unique=True)
    unionid: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    wx_session_key_enc: Mapped[str | None] = mapped_column(String(200), nullable=True)
    nickname: Mapped[str] = mapped_column(String(64), default="")
    avatar_url: Mapped[str] = mapped_column(String(500), default="")
    gender: Mapped[int] = mapped_column(Integer, default=0)  # 0 未知 1 男 2 女
    birthday: Mapped[str | None] = mapped_column(String(10), nullable=True)  # YYYY-MM-DD
    phone_enc: Mapped[str | None] = mapped_column(String(200), nullable=True)
    phone_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    phone_masked: Mapped[str | None] = mapped_column(String(24), nullable=True)
    phone_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=STATUS_ACTIVE)
    disabled_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    register_channel: Mapped[str] = mapped_column(String(16), default="miniprogram")
    agreed_document_ids: Mapped[str] = mapped_column(String(200), default="")  # 注册时同意的协议版本 id，逗号分隔
    content_review: Mapped[bool] = mapped_column(default=False)  # 昵称内容安全结果为 review，待人工复核
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deletion_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deletion_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deletion_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    deletion_hold: Mapped[str | None] = mapped_column(String(200), nullable=True)  # 到期复查时的阻断原因


class UserSession(Base, BigIdMixin):
    """会话 = 刷新令牌家族。"""

    __tablename__ = "user_sessions"

    sid: Mapped[str] = mapped_column(String(32), unique=True)
    user_id: Mapped[int] = mapped_column(BigId, index=True)
    status: Mapped[str] = mapped_column(String(16), default=SESSION_ACTIVE)
    channel: Mapped[str] = mapped_column(String(16), default="wechat")  # wechat | sms
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoke_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(200), nullable=True)


class RefreshToken(Base, BigIdMixin):
    __tablename__ = "user_refresh_tokens"

    session_id: Mapped[int] = mapped_column(BigId, index=True)
    user_id: Mapped[int] = mapped_column(BigId)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    parent_id: Mapped[int | None] = mapped_column(BigId, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=TOKEN_ACTIVE)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SmsCode(Base, BigIdMixin):
    __tablename__ = "sms_codes"
    __table_args__ = (
        Index("ix_sms_codes_phone_purpose", "phone_hash", "purpose"),
        UniqueConstraint("phone_hash", "purpose", "send_slot", name="uq_sms_codes_send_slot"),
    )

    request_id: Mapped[str] = mapped_column(String(32), unique=True)
    phone_hash: Mapped[str] = mapped_column(String(64))
    purpose: Mapped[str] = mapped_column(String(16))  # login | bind
    code_hash: Mapped[str] = mapped_column(String(64))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default=SMS_ACTIVE)
    send_slot: Mapped[int] = mapped_column(Integer)  # epoch // 重发间隔：同一时间片只允许一条（并发兜底）
    ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class ContentCheck(Base, BigIdMixin):
    """资料字段的异步内容安全检测（头像）：通过后才生效。"""

    __tablename__ = "user_content_checks"

    user_id: Mapped[int] = mapped_column(BigId, index=True)
    field: Mapped[str] = mapped_column(String(16))  # avatar
    value: Mapped[str] = mapped_column(String(500))
    trace_id: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(16), default=CHECK_PENDING)
    label: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
