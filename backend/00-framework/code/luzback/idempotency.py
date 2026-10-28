"""幂等执行器 v2：同一 (actor, scope, key) 只执行一次，重复请求返回首次结果。

用法：
    result = run_idempotent(db, scope="order.create", key=header, fn=lambda: do_work(db),
                            actor=f"user:{user_id}", fingerprint=sha256(body), require_key=True)

规则（SPEC §6.5）：
- 作用域含操作者：不同用户用同一个键互不影响。
- 指纹：同键不同请求体 → 4093；同键同请求体 → 返回首次响应。
- 处理中：另一请求未完成 → 4092；处理记录超过 stale_seconds 视为僵尸，可被接管。
- 记录与业务写同事务提交；失败回滚并删除占位，允许同键重试。
- 记录保留 24 小时，由 purge_idempotency 定时清理。
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import String, Text, UniqueConstraint, delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from .db import Base, BigIdMixin, utcnow
from .errors import AppError, ErrorCode

KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-_:.]{7,127}$")
LEGACY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-_:.]{0,127}$")


class IdempotencyRecord(Base, BigIdMixin):
    __tablename__ = "idempotency_records"
    __table_args__ = (UniqueConstraint("actor", "scope", "key", name="uq_idempotency_actor_scope_key"),)

    actor: Mapped[str] = mapped_column(String(64), default="")
    scope: Mapped[str] = mapped_column(String(128))
    key: Mapped[str] = mapped_column(String(128))
    fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16))  # processing | done
    response_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)


def validate_key(key: str | None, require: bool) -> str | None:
    """require=True（v2 写接口）要求 8–128 位；require=False 兼容旧模块的短键。"""
    if not key:
        if require:
            raise AppError(ErrorCode.BAD_REQUEST, "Idempotency-Key header is required")
        return None
    pattern = KEY_PATTERN if require else LEGACY_KEY_PATTERN
    if not pattern.match(key):
        raise AppError(ErrorCode.BAD_REQUEST, "Idempotency-Key must be 8-128 chars of [A-Za-z0-9-_:.]")
    return key


def run_idempotent(
    db: Session,
    scope: str,
    key: str | None,
    fn: Callable[[], Any],
    actor: str = "",
    fingerprint: str | None = None,
    require_key: bool = False,
    stale_seconds: int = 60,
    now: datetime | None = None,
) -> Any:
    key = validate_key(key, require_key)
    if key is None:
        try:
            result = fn()
            db.commit()
            return result
        except Exception:
            db.rollback()
            raise

    now = now or utcnow()
    existing = db.execute(
        select(IdempotencyRecord).where(
            IdempotencyRecord.actor == actor, IdempotencyRecord.scope == scope, IdempotencyRecord.key == key
        )
    ).scalar_one_or_none()

    if existing is not None:
        if fingerprint and existing.fingerprint and existing.fingerprint != fingerprint:
            raise AppError(ErrorCode.IDEMPOTENCY_MISMATCH, "idempotency key reused with a different request body")
        if existing.status == "done" and existing.response_json is not None:
            return json.loads(existing.response_json)
        stale_before = now - timedelta(seconds=stale_seconds)
        updated_at = existing.updated_at if existing.updated_at.tzinfo else existing.updated_at.replace(tzinfo=timezone.utc)
        if updated_at > stale_before:
            raise AppError(ErrorCode.DUPLICATE_REQUEST, "request with this idempotency key is still processing", retry_after=1)
        # 僵尸记录：条件更新接管，只有一个请求能赢
        taken = db.execute(
            update(IdempotencyRecord)
            .where(IdempotencyRecord.id == existing.id, IdempotencyRecord.status == "processing",
                   IdempotencyRecord.updated_at == existing.updated_at)
            .values(updated_at=now)
        ).rowcount
        db.commit()
        if taken != 1:
            raise AppError(ErrorCode.DUPLICATE_REQUEST, "request with this idempotency key is still processing", retry_after=1)
        record = existing
    else:
        record = IdempotencyRecord(actor=actor, scope=scope, key=key, fingerprint=fingerprint, status="processing",
                                   created_at=now, updated_at=now)
        db.add(record)
        try:
            db.flush()  # 唯一索引在这里裁决竞争
        except IntegrityError as exc:
            db.rollback()
            raise AppError(ErrorCode.DUPLICATE_REQUEST, "request with this idempotency key is still processing",
                           retry_after=1) from exc

    try:
        result = fn()
        record.status = "done"
        record.response_json = json.dumps(result, default=str, ensure_ascii=False)
        record.updated_at = utcnow()
        db.commit()
        return result
    except Exception:
        db.rollback()
        db.execute(delete(IdempotencyRecord).where(IdempotencyRecord.id == record.id))
        db.commit()
        raise


def purge_idempotency(db: Session, older_than_seconds: int = 86400, now: datetime | None = None) -> int:
    cutoff = (now or utcnow()) - timedelta(seconds=older_than_seconds)
    deleted = db.execute(delete(IdempotencyRecord).where(IdempotencyRecord.created_at < cutoff)).rowcount
    db.commit()
    return deleted
