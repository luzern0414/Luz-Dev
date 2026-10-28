"""identity 后台用例：会员列表 / 导出（ListSpec）、详情（脱敏）、查看完整手机号（权限 + 审计）、停用 / 启用、强制下线。

权限码：member:read（列表、详情、导出）、member:write（停用、启用、强制下线）、member:reveal（查看完整手机号）。
所有写操作与敏感查看同事务写审计；导出脱敏并审计。
"""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from luzback import AppError, ErrorCode, Field, ListSpec, enqueue_event, export_csv, get_settings, utcnow, write_audit
from luzback.crypto import FieldCipher

from .models import SESSION_ACTIVE, STATUS_ACTIVE, STATUS_DISABLED, STATUSES, User, UserSession
from .service import revoke_sessions

PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("member:read", "会员查看 / 导出（手机号脱敏）"),
    ("member:write", "会员停用 / 启用 / 强制下线"),
    ("member:reveal", "查看会员完整手机号（写审计）"),
)

MEMBER_LIST = ListSpec(
    fields={
        "status": Field(User.status, ops=("eq", "in"), choices=frozenset(STATUSES)),
        "created_at": Field(User.created_at, kind="datetime", ops=("range",), sortable=True),
        "last_login_at": Field(User.last_login_at, kind="datetime", ops=("range",), sortable=True),
        "member_no": Field(User.member_no),
        "content_review": Field(User.content_review, kind="bool"),
    },
    id_column=User.id,
    keyword_columns=(User.nickname, User.phone_masked, User.member_no),
    default_sort="-created_at",
    extra_params=frozenset({"has_phone", "include_deleted"}),
)

SYNC_EXPORT_LIMIT = 5000  # SPEC §3.4：超过转 24-report 异步导出
EXPORT_HEADERS = ("会员ID", "会员号", "昵称", "手机号", "状态", "注册时间", "最近登录")


def _bool_param(params: Mapping[str, Any], name: str) -> bool | None:
    raw = params.get(name)
    if raw is None or raw == "":
        return None
    lowered = str(raw).strip().lower()
    if lowered in ("true", "1"):
        return True
    if lowered in ("false", "0"):
        return False
    raise AppError(ErrorCode.BAD_REQUEST, f"{name}: expected true or false")


def _base_query(params: Mapping[str, Any]):
    stmt = select(User)
    if not _bool_param(params, "include_deleted"):
        stmt = stmt.where(User.deleted_at.is_(None))
    has_phone = _bool_param(params, "has_phone")
    if has_phone is True:
        stmt = stmt.where(User.phone_hash.is_not(None))
    elif has_phone is False:
        stmt = stmt.where(User.phone_hash.is_(None))
    return stmt


def _row(user: User) -> dict:
    return {"user_id": user.id, "member_no": user.member_no, "nickname": user.nickname, "avatar_url": user.avatar_url,
            "phone": user.phone_masked, "phone_verified": user.phone_verified_at is not None, "status": user.status,
            "content_review": user.content_review, "registered_at": user.created_at, "last_login_at": user.last_login_at}


def list_members(db: Session, params: Mapping[str, Any]) -> dict:
    return MEMBER_LIST.page(db, _base_query(params), params, serialize=_row)


def export_members(db: Session, staff_id: int, params: Mapping[str, Any], ip: str | None = None,
                   trace_id: str | None = None) -> Iterator[bytes]:
    """CSV 导出（与列表同一筛选，手机号脱敏）；先写审计并提交，再流式输出。"""
    stream = export_csv(db, MEMBER_LIST, _base_query(params), params, EXPORT_HEADERS,
                        lambda u: (u.id, u.member_no, u.nickname, u.phone_masked or "", u.status, u.created_at, u.last_login_at),
                        hard_limit=SYNC_EXPORT_LIMIT)
    write_audit(db, "staff", staff_id, "member.export", "user", "*",
                after={k: str(v) for k, v in params.items() if k not in ("page", "page_size")}, ip=ip, trace_id=trace_id)
    db.commit()
    return stream


def _get(db: Session, user_id: int) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise AppError(ErrorCode.NOT_FOUND, "member not found")
    return user


def member_detail(db: Session, user_id: int) -> dict:
    user = _get(db, user_id)
    sessions = db.execute(select(func.count()).select_from(UserSession).where(
        UserSession.user_id == user.id, UserSession.status == SESSION_ACTIVE)).scalar_one()
    return {**_row(user), "unionid_bound": user.unionid is not None, "gender": user.gender, "birthday": user.birthday,
            "register_channel": user.register_channel, "agreed_document_ids": user.agreed_document_ids,
            "disabled_reason": user.disabled_reason, "disabled_at": user.disabled_at, "active_sessions": sessions,
            "deletion": {"requested_at": user.deletion_requested_at, "due_at": user.deletion_due_at,
                         "hold": user.deletion_hold} if user.deletion_requested_at else None,
            "deleted_at": user.deleted_at}


def reveal_phone(db: Session, staff_id: int, user_id: int, reason: str, ip: str | None = None,
                 trace_id: str | None = None) -> dict:
    """查看完整手机号：必须给出原因；审计里只记脱敏值。"""
    if not reason or len(reason.strip()) < 2:
        raise AppError(ErrorCode.BAD_REQUEST, "reason required", {"field": "reason"})
    user = _get(db, user_id)
    if not user.phone_enc:
        raise AppError(ErrorCode.NOT_FOUND, "member has no phone")
    phone = FieldCipher(get_settings().field_encryption_key).decrypt(user.phone_enc)
    write_audit(db, "staff", staff_id, "member.phone.reveal", "user", user.id, after={"phone": user.phone_masked},
                reason=reason.strip()[:200], ip=ip, trace_id=trace_id)
    db.commit()
    return {"user_id": user.id, "phone": phone}


def disable_member(db: Session, staff_id: int, user_id: int, reason: str, now: datetime | None = None,
                   ip: str | None = None, trace_id: str | None = None, commit: bool = True) -> dict:
    """停用：active → disabled（条件 UPDATE）；立即吊销全部会话（ASVS 7.4.2）；发 member.disabled。"""
    if not reason or len(reason.strip()) < 2:
        raise AppError(ErrorCode.BAD_REQUEST, "reason required", {"field": "reason"})
    now = now or utcnow()
    _get(db, user_id)
    rows = db.execute(update(User).where(User.id == user_id, User.status == STATUS_ACTIVE, User.deleted_at.is_(None))
                      .values(status=STATUS_DISABLED, disabled_reason=reason.strip()[:200], disabled_at=now)).rowcount
    if rows != 1:
        raise AppError(ErrorCode.CONFLICT, "only active members can be disabled")
    revoked = revoke_sessions(db, user_id, "disabled", now)
    enqueue_event(db, "member.disabled", {"user_id": user_id}, "user", user_id)
    write_audit(db, "staff", staff_id, "member.disable", "user", user_id, before={"status": STATUS_ACTIVE},
                after={"status": STATUS_DISABLED, "revoked_sessions": revoked}, reason=reason.strip()[:200], ip=ip, trace_id=trace_id)
    if commit:
        db.commit()
    return {"user_id": user_id, "status": STATUS_DISABLED, "revoked_sessions": revoked}


def enable_member(db: Session, staff_id: int, user_id: int, reason: str, ip: str | None = None,
                  trace_id: str | None = None, commit: bool = True) -> dict:
    if not reason or len(reason.strip()) < 2:
        raise AppError(ErrorCode.BAD_REQUEST, "reason required", {"field": "reason"})
    _get(db, user_id)
    rows = db.execute(update(User).where(User.id == user_id, User.status == STATUS_DISABLED, User.deleted_at.is_(None))
                      .values(status=STATUS_ACTIVE, disabled_reason=None, disabled_at=None)).rowcount
    if rows != 1:
        raise AppError(ErrorCode.CONFLICT, "only disabled members can be enabled")
    write_audit(db, "staff", staff_id, "member.enable", "user", user_id, before={"status": STATUS_DISABLED},
                after={"status": STATUS_ACTIVE}, reason=reason.strip()[:200], ip=ip, trace_id=trace_id)
    if commit:
        db.commit()
    return {"user_id": user_id, "status": STATUS_ACTIVE}


def force_logout(db: Session, staff_id: int, user_id: int, reason: str, now: datetime | None = None,
                 ip: str | None = None, trace_id: str | None = None, commit: bool = True) -> dict:
    """管理员终止某会员全部会话（ASVS 7.4.5）。"""
    if not reason or len(reason.strip()) < 2:
        raise AppError(ErrorCode.BAD_REQUEST, "reason required", {"field": "reason"})
    _get(db, user_id)
    revoked = revoke_sessions(db, user_id, "admin_logout", now or utcnow())
    write_audit(db, "staff", staff_id, "member.force_logout", "user", user_id, after={"revoked_sessions": revoked},
                reason=reason.strip()[:200], ip=ip, trace_id=trace_id)
    if commit:
        db.commit()
    return {"user_id": user_id, "revoked_sessions": revoked}


def freeze(db: Session, user_id: int, frozen: bool, staff_id: int, reason: str) -> dict:
    """CONTRACTS L1 兼容入口：frozen=True 停用、False 启用。"""
    return disable_member(db, staff_id, user_id, reason) if frozen else enable_member(db, staff_id, user_id, reason)
