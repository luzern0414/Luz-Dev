"""admin_rbac 数据模型：后台账号、角色、权限码、角色权限、用户角色、门店范围、登录日志。

- 手机号与 MFA 密钥加密存储（phone_enc / mfa_secret_enc），手机号另存哈希用于查询。
- refresh_version 用于刷新令牌轮换与强制下线；停用 / 改密 / 重置 MFA 都会 +1。
- 审计表复用框架 luzback.audit.AuditLog（只追加）。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from luzback import Base, BigId, BigIdMixin, SoftDeleteMixin, TimestampMixin

STATUS_ACTIVE = "active"
STATUS_DISABLED = "disabled"

TOKEN_KIND_ACCESS = "admin"
TOKEN_KIND_REFRESH = "admin_refresh"
TOKEN_KIND_MFA = "mfa"


class AdminUser(Base, BigIdMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "admin_users"
    __table_args__ = (Index("ix_admin_users_phone_hash", "phone_hash"),)

    username: Mapped[str] = mapped_column(String(32), unique=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    name: Mapped[str] = mapped_column(String(32), default="")
    phone_enc: Mapped[str | None] = mapped_column(String(200), nullable=True)
    phone_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=STATUS_ACTIVE, index=True)
    mfa_secret_enc: Mapped[str | None] = mapped_column(String(200), nullable=True)
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True)
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    refresh_version: Mapped[int] = mapped_column(Integer, default=0)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


class AdminRole(Base, BigIdMixin, TimestampMixin):
    __tablename__ = "admin_roles"

    name: Mapped[str] = mapped_column(String(32), unique=True)
    description: Mapped[str] = mapped_column(String(200), default="")
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)


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

    user_id: Mapped[int] = mapped_column(BigId, primary_key=True)
    store_id: Mapped[int] = mapped_column(BigId, primary_key=True)


class AdminLoginLog(Base, BigIdMixin):
    __tablename__ = "admin_login_logs"
    __table_args__ = (Index("ix_admin_login_logs_username_created", "username", "created_at"),)

    user_id: Mapped[int | None] = mapped_column(BigId, nullable=True, index=True)
    username: Mapped[str] = mapped_column(String(32))
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str] = mapped_column(String(32), default="")  # ok | mfa | bad_password | locked | disabled | unknown_user | bad_mfa
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
