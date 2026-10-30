"""文案与展示设置的用例（运营在后台实时维护，小程序经 /api/v1/app/content 拉取）。

- 写操作只 add / 条件 UPDATE，不提交：由接口层 run_idempotent 统一提交（或调用方的事务）。
- 每次修改：按 version 条件更新 → 追加 content_revisions（id 即新的内容版本）→ 同事务写审计 → 记 content.* 日志。
- 隐藏的键（admin_visible = false）照常下发，但后台接口拒绝修改（reason = content_key_hidden）。
- 内容安全：`set_text_checker(fn)` 接入检测服务；没接时不检测（微信 msgSecCheck 要求用户 openid，后台员工改的文案用不了）。
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from luzback import AppError, ErrorCode, Field, ListSpec, UpstreamError, utcnow, write_audit
from luzback import log_events as ev
from luzback.logging import log_event

from . import rules
from .display import DisplayKey, display_keys, get_display_key
from .models import (
    SOURCE_ADMIN, SOURCE_MIGRATION, SOURCE_RESTORE, TARGET_DISPLAY, TARGET_TEXT, ContentRevision, DisplaySetting, UiText,
)

log = logging.getLogger("luz.app_settings")

TextChecker = Callable[[str], "bool | str | None"]
_text_checker: TextChecker | None = None


def set_text_checker(fn: TextChecker | None) -> None:
    """内容安全检测钩子：fn(text) 返回 True / None 表示通过，False 或原因字符串表示不通过；
    抛异常表示检测服务不可用，按失败处理（不保存未检测的文案）。None 取消检测。"""
    global _text_checker
    _text_checker = fn


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _load(raw: str | None) -> Any:
    return None if raw is None else json.loads(raw)


def _hidden() -> AppError:
    return AppError(ErrorCode.FORBIDDEN, "this item is maintained by developers and cannot be changed here",
                    {"reason": "content_key_hidden"})


def _check_safety(text: str) -> None:
    if _text_checker is None or not text:
        return
    try:
        verdict = _text_checker(text)
    except AppError:
        raise
    except Exception as exc:  # noqa: BLE001 - 检测服务不可用：不保存
        raise UpstreamError("content safety check unavailable, please retry later") from exc
    if verdict is False or isinstance(verdict, str):
        raise AppError(ErrorCode.BUSINESS_RULE, "the text did not pass the content safety check",
                       {"reason": "content_unsafe", "detail": verdict if isinstance(verdict, str) else None})


def content_version(db: Session) -> int:
    """内容版本 = content_revisions 的最大编号（没有任何修改时为 0）。"""
    return int(db.execute(select(func.max(ContentRevision.id))).scalar() or 0)


def _revision(db: Session, target_type: str, key: str, old: Any, new: Any, staff_id: int | None, source: str,
              store_id: int | None = None, *, old_absent: bool = False, new_absent: bool = False) -> ContentRevision:
    row = ContentRevision(target_type=target_type, target_key=key, store_id=store_id,
                          old_value_json=None if old_absent else _dump(old),
                          new_value_json=None if new_absent else _dump(new), changed_by=staff_id, source=source)
    db.add(row)
    db.flush()
    return row


def _audit(db: Session, staff_id: int | None, action: str, resource: str, key: str, before: Any, after: Any,
           audit: dict[str, Any]) -> None:
    if staff_id is not None:
        write_audit(db, "staff", staff_id, action, resource, key, before=before, after=after,
                    ip=audit.get("ip"), user_agent=audit.get("user_agent"), trace_id=audit.get("trace_id"))


# ================================================================ 文案

TEXT_SPEC = ListSpec(
    fields={
        "page_name": Field(UiText.page, ops=("eq", "in"), sortable=True),  # 小程序页面（page 是分页页码）
        "key": Field(UiText.key, sortable=True),
        "sort_order": Field(UiText.sort_order, kind="int", sortable=True),
        "updated_at": Field(UiText.updated_at, kind="datetime", ops=("range",), sortable=True),
    },
    id_column=UiText.id,
    keyword_columns=(UiText.key, UiText.slot_label, UiText.text, UiText.default_text),
    default_sort="page_name,sort_order",
)


def variables_of(row: UiText) -> list[str]:
    return list(json.loads(row.variables_json or "[]"))


def effective_text(row: UiText) -> str:
    return row.default_text if row.text is None else row.text


def text_view(row: UiText) -> dict[str, Any]:
    return {"key": row.key, "page": row.page, "slot_label": row.slot_label, "text": row.text,
            "default_text": row.default_text, "effective_text": effective_text(row), "max_length": row.max_length,
            "variables": variables_of(row), "allow_empty": row.allow_empty, "sort_order": row.sort_order,
            "version": row.version, "updated_by": row.updated_by,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None}


def get_text(db: Session, key: str) -> UiText:
    row = db.execute(select(UiText).where(UiText.key == key)).scalar_one_or_none()
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, "text key not found")
    return row


def check_text(row: UiText, text: str | None) -> str | None:
    """规范化并校验：None 或空白 = 用默认文案（可留空的键存空串，该位置不显示）；超长、未登记变量、内容不安全都拒绝。"""
    if text is None:
        return None
    text = text.strip()
    if not text:
        return "" if row.allow_empty else None
    problems = rules.text_problems(text, row.max_length, variables_of(row))
    if problems:
        reason, data = problems[0]
        message = (f"at most {row.max_length} characters" if reason == "content_text_too_long"
                   else "only these variables can be used: " + ", ".join("{" + v + "}" for v in variables_of(row)))
        raise AppError(ErrorCode.BAD_REQUEST, message, {"reason": reason, **data,
                                                        "errors": [{"field": "text", "message": message}]})
    _check_safety(text)
    return text


def _write_text(db: Session, row: UiText, new: str | None, expected_version: int, staff_id: int | None, source: str,
                action: str, audit: dict[str, Any]) -> dict[str, Any]:
    old = row.text
    result = db.execute(update(UiText).where(UiText.id == row.id, UiText.version == expected_version)
                        .values(text=new, version=UiText.version + 1, updated_by=staff_id, updated_at=utcnow())
                        .execution_options(synchronize_session=False))
    if result.rowcount != 1:
        db.refresh(row)
        raise AppError(ErrorCode.CONFLICT, "this text was changed by someone else; reload and try again",
                       {"reason": "content_conflict", "current": text_view(row)})
    db.refresh(row)
    revision = _revision(db, TARGET_TEXT, row.key, old, new, staff_id, source)
    _audit(db, staff_id, action, "ui_text", row.key, {"text": old, "version": expected_version},
           {"text": new, "version": row.version}, audit)
    log_event(log, ev.CONTENT_TEXT_UPDATED, outcome="success", key=row.key, source=source, content_version=revision.id)
    return {**text_view(row), "content_version": revision.id}


def update_text(db: Session, staff_id: int, key: str, text: str | None, version: int, **audit: Any) -> dict[str, Any]:
    row = get_text(db, key)
    if not row.admin_visible:
        raise _hidden()
    return _write_text(db, row, check_text(row, text), version, staff_id, SOURCE_ADMIN, "content.text.update", audit)


def reset_text(db: Session, staff_id: int, key: str, **audit: Any) -> dict[str, Any]:
    """恢复默认文案；已经是默认时不生成新版本。"""
    row = get_text(db, key)
    if not row.admin_visible:
        raise _hidden()
    if row.text is None:
        return {**text_view(row), "content_version": content_version(db)}
    return _write_text(db, row, None, row.version, staff_id, SOURCE_ADMIN, "content.text.reset", audit)


# ================================================================ 展示设置

def _display_row(db: Session, key: str, store_id: int | None) -> DisplaySetting | None:
    scope = DisplaySetting.store_id.is_(None) if store_id is None else DisplaySetting.store_id == store_id
    return db.execute(select(DisplaySetting).where(DisplaySetting.key == key, scope)).scalar_one_or_none()


def _stored(item: DisplayKey, row: DisplaySetting | None) -> Any:
    """库里的值仍按当前登记校验；登记改了范围后不合法的旧值退回上一级（并记日志），不下发坏值。"""
    if row is None:
        return None
    try:
        return item.validate(json.loads(row.value_json))
    except (ValueError, TypeError):
        log_event(log, ev.CONTENT_DISPLAY_UPDATED, logging.WARNING, outcome="failure", reason="stored_value_invalid",
                  key=item.key, store_id=row.store_id)
        return None


def resolve_display(db: Session, store_id: int | None = None, *, public_only: bool = True) -> dict[str, Any]:
    """展示设置的生效值：登记的默认值 < 全局值 < 门店覆盖（只对可按门店覆盖的键）。"""
    items = [k for k in display_keys() if k.public or not public_only]
    scope = DisplaySetting.store_id.is_(None) if store_id is None else or_(DisplaySetting.store_id.is_(None),
                                                                           DisplaySetting.store_id == store_id)
    rows = db.execute(select(DisplaySetting).where(DisplaySetting.key.in_([k.key for k in items]), scope)).scalars().all()
    global_rows = {r.key: r for r in rows if r.store_id is None}
    store_rows = {r.key: r for r in rows if r.store_id is not None}
    values: dict[str, Any] = {}
    for item in items:
        value = item.default
        stored = _stored(item, global_rows.get(item.key))
        if stored is not None:
            value = stored
        if store_id is not None and item.store_scope:
            stored = _stored(item, store_rows.get(item.key))
            if stored is not None:
                value = stored
        values[item.key] = value
    return values


def display_view(db: Session, item: DisplayKey, store_id: int | None = None) -> dict[str, Any]:
    global_row = _display_row(db, item.key, None)
    store_row = _display_row(db, item.key, store_id) if store_id is not None and item.store_scope else None
    global_value = _stored(item, global_row)
    store_value = _stored(item, store_row)
    value, source = item.default, "default"
    if global_value is not None:
        value, source = global_value, "global"
    if store_value is not None:
        value, source = store_value, "store"
    scope_row = global_row if store_id is None else store_row
    return {**item.describe(), "store_id": store_id, "global_value": global_value, "store_value": store_value,
            "value": value, "source": source, "version": scope_row.version if scope_row else 0,
            "updated_at": scope_row.updated_at.isoformat() if scope_row and scope_row.updated_at else None}


def list_display(db: Session, store_id: int | None = None) -> list[dict[str, Any]]:
    """后台展示设置：只列 admin_visible 的键；带 store_id 时给出门店值、全局值与生效来源。"""
    return [display_view(db, item, store_id) for item in display_keys() if item.admin_visible]


def _display_item(key: str, store_id: int | None) -> DisplayKey:
    item = get_display_key(key)
    if item is None:
        raise AppError(ErrorCode.NOT_FOUND, "setting key not found")
    if not item.admin_visible:
        raise _hidden()
    if store_id is not None and not item.store_scope:
        raise AppError(ErrorCode.BAD_REQUEST, "this setting applies to all stores and cannot be set per store",
                       {"reason": "store_override_not_allowed"})
    return item


def _conflict_display(db: Session, item: DisplayKey, store_id: int | None) -> AppError:
    return AppError(ErrorCode.CONFLICT, "this setting was changed by someone else; reload and try again",
                    {"reason": "content_conflict", "current": display_view(db, item, store_id)})


def _write_display(db: Session, item: DisplayKey, store_id: int | None, value: Any, expected_version: int,
                   staff_id: int | None, source: str, action: str, audit: dict[str, Any]) -> dict[str, Any]:
    row = _display_row(db, item.key, store_id)
    old_absent = row is None
    old = None if row is None else json.loads(row.value_json)
    now = utcnow()
    if row is None:
        if expected_version != 0:
            raise _conflict_display(db, item, store_id)
        try:
            with db.begin_nested():  # 并发首次写入由部分唯一索引兜底：只撤销这一条插入
                db.add(DisplaySetting(store_id=store_id, key=item.key, value_json=_dump(value),
                                      admin_visible=item.admin_visible, version=1, updated_by=staff_id,
                                      created_at=now, updated_at=now))
        except IntegrityError:
            raise _conflict_display(db, item, store_id) from None
    else:
        result = db.execute(update(DisplaySetting).where(DisplaySetting.id == row.id, DisplaySetting.version == expected_version)
                            .values(value_json=_dump(value), admin_visible=item.admin_visible,
                                    version=DisplaySetting.version + 1, updated_by=staff_id, updated_at=now)
                            .execution_options(synchronize_session=False))
        if result.rowcount != 1:
            db.refresh(row)
            raise _conflict_display(db, item, store_id)
        db.refresh(row)
    revision = _revision(db, TARGET_DISPLAY, item.key, old, value, staff_id, source, store_id, old_absent=old_absent)
    _audit(db, staff_id, action, "display_setting", item.key, {"store_id": store_id, "value": old},
           {"store_id": store_id, "value": value}, audit)
    log_event(log, ev.CONTENT_DISPLAY_UPDATED, outcome="success", key=item.key, store_id=store_id, source=source,
              content_version=revision.id)
    return {**display_view(db, item, store_id), "content_version": revision.id}


def _delete_display(db: Session, item: DisplayKey, store_id: int | None, expected_version: int | None,
                    staff_id: int | None, source: str, action: str, audit: dict[str, Any]) -> dict[str, Any]:
    row = _display_row(db, item.key, store_id)
    if row is None:
        if expected_version not in (None, 0):
            raise _conflict_display(db, item, store_id)
        return {**display_view(db, item, store_id), "content_version": content_version(db)}
    old = json.loads(row.value_json)
    version = row.version if expected_version is None else expected_version
    result = db.execute(delete(DisplaySetting).where(DisplaySetting.id == row.id, DisplaySetting.version == version)
                        .execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise _conflict_display(db, item, store_id)
    db.expunge(row)
    revision = _revision(db, TARGET_DISPLAY, item.key, old, None, staff_id, source, store_id, new_absent=True)
    _audit(db, staff_id, action, "display_setting", item.key, {"store_id": store_id, "value": old},
           {"store_id": store_id, "value": None}, audit)
    log_event(log, ev.CONTENT_DISPLAY_UPDATED, outcome="success", key=item.key, store_id=store_id, source=source,
              content_version=revision.id)
    return {**display_view(db, item, store_id), "content_version": revision.id}


def update_display(db: Session, staff_id: int, key: str, value: Any, version: int, store_id: int | None = None,
                   **audit: Any) -> dict[str, Any]:
    item = _display_item(key, store_id)
    try:
        value = item.validate(value)
    except (ValueError, TypeError) as exc:
        raise AppError(ErrorCode.BAD_REQUEST, f"invalid value: {exc}",
                       {"reason": "validation_failed", "errors": [{"field": "value", "message": str(exc)}]}) from None
    if item.kind == "text":
        _check_safety(value)
    return _write_display(db, item, store_id, value, version, staff_id, SOURCE_ADMIN, "content.display.update", audit)


def reset_display(db: Session, staff_id: int, key: str, store_id: int | None = None, **audit: Any) -> dict[str, Any]:
    """恢复默认（全局）或取消门店覆盖（门店）；本来就没有值时不生成新版本。"""
    item = _display_item(key, store_id)
    return _delete_display(db, item, store_id, None, staff_id, SOURCE_ADMIN, "content.display.reset", audit)


# ================================================================ 修改历史与恢复

REVISION_SPEC = ListSpec(
    fields={
        "key": Field(ContentRevision.target_key, ops=("eq", "in")),
        "target_type": Field(ContentRevision.target_type, choices=frozenset({TARGET_TEXT, TARGET_DISPLAY})),
        "store_id": Field(ContentRevision.store_id, kind="int", ops=("eq", "null")),
        "source": Field(ContentRevision.source, choices=frozenset({SOURCE_ADMIN, SOURCE_MIGRATION, SOURCE_RESTORE})),
        "created_at": Field(ContentRevision.created_at, kind="datetime", ops=("range",), sortable=True),
    },
    id_column=ContentRevision.id,
    default_sort="-created_at",
)


def revision_view(row: ContentRevision) -> dict[str, Any]:
    return {"id": row.id, "content_version": row.id, "target_type": row.target_type, "key": row.target_key,
            "store_id": row.store_id, "old_value": _load(row.old_value_json), "new_value": _load(row.new_value_json),
            "old_absent": row.old_value_json is None, "new_absent": row.new_value_json is None,
            "changed_by": row.changed_by, "source": row.source, "restorable": row.source != SOURCE_MIGRATION,
            "created_at": row.created_at.isoformat() if row.created_at else None}


def visible_keys(db: Session) -> tuple[list[str], list[str]]:
    """后台可见的文案键与展示键（修改历史只列这些）。"""
    texts = list(db.execute(select(UiText.key).where(UiText.admin_visible.is_(True))).scalars().all())
    return texts, [k.key for k in display_keys() if k.admin_visible]


def restore_revision(db: Session, staff_id: int, revision_id: int, version: int, **audit: Any) -> dict[str, Any]:
    """把这次修改撤销：目标恢复为这次修改之前的值；恢复本身生成新版本并写审计。种子同步产生的记录不能恢复。"""
    revision = db.get(ContentRevision, revision_id)
    if revision is None:
        raise AppError(ErrorCode.NOT_FOUND, "revision not found")
    if revision.source == SOURCE_MIGRATION:
        raise AppError(ErrorCode.CONFLICT, "changes made by developers through the seed cannot be restored here",
                       {"reason": "revision_not_restorable"})
    if revision.target_type == TARGET_TEXT:
        row = get_text(db, revision.target_key)
        if not row.admin_visible:
            raise _hidden()
        value = check_text(row, _load(revision.old_value_json))
        return _write_text(db, row, value, version, staff_id, SOURCE_RESTORE, "content.text.restore", audit)
    item = _display_item(revision.target_key, revision.store_id)
    if revision.old_value_json is None:
        return _delete_display(db, item, revision.store_id, version, staff_id, SOURCE_RESTORE,
                               "content.display.restore", audit)
    try:
        value = item.validate(_load(revision.old_value_json))
    except (ValueError, TypeError) as exc:
        raise AppError(ErrorCode.CONFLICT, f"the old value is no longer valid: {exc}",
                       {"reason": "revision_not_restorable"}) from None
    return _write_display(db, item, revision.store_id, value, version, staff_id, SOURCE_RESTORE,
                          "content.display.restore", audit)


# ================================================================ 小程序下发

def public_content(db: Session, store_id: int | None = None) -> dict[str, Any]:
    """小程序拉取：全部文案（含隐藏键）与公开的展示配置（门店覆盖全局）。版本先取，避免把新内容配上旧版本号。"""
    version = content_version(db)
    texts = {row.key: effective_text(row) for row in db.execute(select(UiText).order_by(UiText.key)).scalars()}
    return {"content_version": version, "texts": texts, "display": resolve_display(db, store_id, public_only=True)}
