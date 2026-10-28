"""审计日志：只追加，与业务写同事务。覆盖所有写操作、敏感查看、登录与权限变更（SPEC §13）。"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, Session, mapped_column

from .db import Base, BigIdMixin, utcnow


class AuditLog(Base, BigIdMixin):
    __tablename__ = "audit_logs"

    actor_type: Mapped[str] = mapped_column(String(16))  # user | staff | system | agent
    actor_id: Mapped[str] = mapped_column(String(64), index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)  # order.accept / member.points.adjust / staff.login
    resource: Mapped[str] = mapped_column(String(32))
    resource_id: Mapped[str] = mapped_column(String(64), index=True)
    before_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    after_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(200), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)


def _dump(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, default=str, ensure_ascii=False, sort_keys=True)


def write_audit(
    db: Session,
    actor_type: str,
    actor_id: str | int,
    action: str,
    resource: str,
    resource_id: str | int,
    before: Any = None,
    after: Any = None,
    reason: str | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
    trace_id: str | None = None,
) -> AuditLog:
    """只 add 不 commit，与业务写同事务。"""
    row = AuditLog(
        actor_type=actor_type,
        actor_id=str(actor_id),
        action=action,
        resource=resource,
        resource_id=str(resource_id),
        before_json=_dump(before),
        after_json=_dump(after),
        reason=reason,
        ip=ip,
        user_agent=(user_agent or "")[:200] or None,
        trace_id=trace_id,
    )
    db.add(row)
    return row


def diff(before: dict[str, Any] | None, after: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """返回变化的字段：{field: {"from": x, "to": y}}。"""
    before = before or {}
    after = after or {}
    changed: dict[str, dict[str, Any]] = {}
    for key in sorted(set(before) | set(after)):
        if before.get(key) != after.get(key):
            changed[key] = {"from": before.get(key), "to": after.get(key)}
    return changed
