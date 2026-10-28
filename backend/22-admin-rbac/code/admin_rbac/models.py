"""admin_rbac 数据模型：后台账号、角色、权限码、角色权限、用户角色、门店范围、服务端会话、登录节流、
登录日志、MFA 恢复码。审计表复用框架 luzback.audit.AuditLog（只追加）。

- 手机号 AES-GCM 加密（phone_enc）+ HMAC 哈希（phone_hash，查询用），响应脱敏。
- 会话只存令牌的 SHA-256（token_hash），明文令牌只在签发时返回一次。
- 登录节流按“规范化账号名”记录（账号存在与否同样处理，防枚举）。
- 无外键约束（SPEC §3.5），关联列都有索引。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from luzback import Base, BigId, BigIdMixin, SoftDeleteMixin, TimestampMixin

STATUS_ACTIVE = "active"
STATUS_DISABLED = "disabled"
STAFF_STATUSES = frozenset({STATUS_ACTIVE, STATUS_DISABLED})

STAGE_MFA_PENDING = "mfa_pending"   # 口令已过，等待第二因素（只能调用 /auth/mfa）
STAGE_ACTIVE = "active"

TOKEN_KIND_ACCESS = "admin"      # 会话令牌（JWT kind），与员工端 / 顾客端令牌互不通用
TOKEN_KIND_MFA = "admin_mfa"     # 口令已过、等待第二因素的挑战令牌

AMR_PASSWORD = "pwd"
AMR_OTP = "pwd+otp"
AMR_RECOVERY = "pwd+recovery"


class AdminUser(Base, BigIdMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "admin_users"
    __table_args__ = (Index("ix_admin_users_phone_hash", "phone_hash"),)

    username: Mapped[str] = mapped_column(String(32), unique=True)  # 小写；软删除后不复用（审计可追溯）
    password_hash: Mapped[str] = mapped_column(String(255))
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True)  # 临时口令
    temp_password_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    name: Mapped[str] = mapped_column(String(32), default="")
    phone_enc: Mapped[str | None] = mapped_column(String(200), nullable=True)
    phone_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=STATUS_ACTIVE, index=True)
    all_stores: Mapped[bool] = mapped_column(Boolean, default=False)  # True=全部门店；False=仅 admin_store_scopes 列表
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    mfa_secret_enc: Mapped[str | None] = mapped_column(String(200), nullable=True)
    mfa_pending_secret_enc: Mapped[str | None] = mapped_column(String(200), nullable=True)
    mfa_last_step: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # 防重放：最后一次成功的时间步
    mfa_enrolled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_login_ua: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


class AdminRole(Base, BigIdMixin, TimestampMixin):
    __tablename__ = "admin_roles"

    name: Mapped[str] = mapped_column(String(32), unique=True)
    builtin_code: Mapped[str | None] = mapped_column(String(32), unique=True, nullable=True)  # 内置角色稳定标识
    description: Mapped[str] = mapped_column(String(200), default="")
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    mfa_required: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


class AdminPermission(Base):
    __tablename__ = "admin_permissions"

    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    module: Mapped[str] = mapped_column(String(32), default="")
    description: Mapped[str] = mapped_column(String(200), default="")


class AdminRolePermission(Base):
    __tablename__ = "admin_role_permissions"

    role_id: Mapped[int] = mapped_column(BigId, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), primary_key=True)


class AdminUserRole(Base):
    __tablename__ = "admin_user_roles"
    __table_args__ = (Index("ix_admin_user_roles_role_id", "role_id"),)

    user_id: Mapped[int] = mapped_column(BigId, primary_key=True)
    role_id: Mapped[int] = mapped_column(BigId, primary_key=True)


class AdminStoreScope(Base):
    __tablename__ = "admin_store_scopes"
    __table_args__ = (Index("ix_admin_store_scopes_store_id", "store_id"),)

    user_id: Mapped[int] = mapped_column(BigId, primary_key=True)
    store_id: Mapped[int] = mapped_column(BigId, primary_key=True)


class AdminSession(Base, BigIdMixin):
    """服务端会话：空闲超时按 last_seen_at，绝对超时按 expires_at；revoked_at 非空即失效。"""

    __tablename__ = "admin_sessions"
    __table_args__ = (Index("ix_admin_sessions_staff_id", "staff_id"),)

    staff_id: Mapped[int] = mapped_column(BigId)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    stage: Mapped[str] = mapped_column(String(16), default=STAGE_ACTIVE)
    amr: Mapped[str] = mapped_column(String(16), default=AMR_PASSWORD)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    auth_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))       # 最近一次完整认证（step-up 用）
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))    # 绝对过期
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoke_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(200), nullable=True)


class AdminLoginThrottle(Base):
    """按规范化账号名计数失败（含不存在的账号名）；窗口内达到阈值即锁定，锁定时长逐次翻倍。"""

    __tablename__ = "admin_login_throttles"

    username: Mapped[str] = mapped_column(String(64), primary_key=True)
    failures: Mapped[int] = mapped_column(Integer, default=0)
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lock_count: Mapped[int] = mapped_column(Integer, default=0)
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AdminLoginLog(Base, BigIdMixin):
    __tablename__ = "admin_login_logs"
    __table_args__ = (Index("ix_admin_login_logs_username_created", "username", "created_at"),)

    user_id: Mapped[int | None] = mapped_column(BigId, nullable=True, index=True)
    username: Mapped[str] = mapped_column(String(64))
    factor: Mapped[str] = mapped_column(String(16), default="password")  # password | totp | recovery | reauth
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    # ok | mfa_pending | bad_password | unknown_user | disabled | temp_expired | locked | captcha | bad_otp | replay
    reason: Mapped[str] = mapped_column(String(32), default="")
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class AdminRecoveryCode(Base, BigIdMixin):
    __tablename__ = "admin_recovery_codes"
    __table_args__ = (Index("ix_admin_recovery_codes_staff_id", "staff_id"),)

    staff_id: Mapped[int] = mapped_column(BigId)
    code_hash: Mapped[str] = mapped_column(String(255))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
