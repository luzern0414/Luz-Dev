"""经营设置的存储与后台维护（键在各业务模块里用 luzback.business 登记）。

- 读取：导入本模块时 `set_business_store(lookup)`，业务模块的 `business_value(db, key, store_id)` 先查门店行、再查全局行。
- 修改：按 version 条件更新（没有行时 version 传 0），同事务写审计（修改前后的值），记 business.setting_updated 日志；
  高风险键（risk = high）的接口要求重新验证身份（见 api.py）。不进内容版本，小程序不下发。
- 隐藏的键（admin_visible = false）不在后台列出、接口拒绝修改，由开发改登记的默认值。
- 写操作不提交：由接口层 run_idempotent 统一提交。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from luzback import AppError, ErrorCode, utcnow, write_audit
from luzback import log_events as ev
from luzback.business import BusinessSetting, business_settings, get_business_setting, set_business_store

__all__ = ["get_business_setting", "list_settings", "lookup", "reset_setting", "update_setting", "view"]
from luzback.logging import log_event

from .models import BusinessSettingValue

log = logging.getLogger("luz.app_settings")


def _row(db: Session, key: str, store_id: int | None) -> BusinessSettingValue | None:
    cond = BusinessSettingValue.store_id.is_(None) if store_id is None else BusinessSettingValue.store_id == store_id
    return db.scalar(select(BusinessSettingValue).where(BusinessSettingValue.key == key, cond))


def lookup(db: Session, key: str, store_id: int | None) -> tuple[bool, Any]:
    """luzback.business 的存储：门店行 → 全局行；都没有时 (False, None)。"""
    for scope in ((store_id, None) if store_id is not None else (None,)):
        row = _row(db, key, scope)
        if row is not None:
            return True, json.loads(row.value_json)
    return False, None


set_business_store(lookup)


def _item(key: str, store_id: int | None) -> BusinessSetting:
    item = get_business_setting(key)
    if item is None:
        raise AppError(ErrorCode.NOT_FOUND, "setting key not found")
    if not item.admin_visible:
        raise AppError(ErrorCode.FORBIDDEN, "this setting is maintained by developers",
                       {"reason": "business_key_hidden"})
    if store_id is not None and not item.store_scope:
        raise AppError(ErrorCode.BAD_REQUEST, "this setting applies to all stores and cannot be set per store",
                       {"reason": "store_override_not_allowed"})
    return item


def view(db: Session, item: BusinessSetting, store_id: int | None = None) -> dict[str, Any]:
    global_row = _row(db, item.key, None)
    store_row = _row(db, item.key, store_id) if store_id is not None and item.store_scope else None
    global_value = json.loads(global_row.value_json) if global_row else None
    store_value = json.loads(store_row.value_json) if store_row else None
    if store_row is not None:
        effective, source = store_value, "store"
    elif global_row is not None:
        effective, source = global_value, "global"
    else:
        effective, source = item.default, "default"
    level = store_row if store_id is not None and item.store_scope else global_row
    return {**item.describe(), "global_value": global_value, "store_value": store_value, "value": effective,
            "source": source, "version": level.version if level is not None else 0,
            "updated_at": level.updated_at.isoformat() if level is not None and level.updated_at else None}


def list_settings(db: Session, store_id: int | None = None) -> list[dict[str, Any]]:
    return [view(db, item, store_id) for item in business_settings() if item.admin_visible]


def _conflict(db: Session, item: BusinessSetting, store_id: int | None) -> AppError:
    return AppError(ErrorCode.CONFLICT, "this setting was changed by someone else; reload and try again",
                    {"reason": "setting_conflict", "current": view(db, item, store_id)})


def _audit(db: Session, staff_id: int, action: str, item: BusinessSetting, store_id: int | None, old: Any, new: Any,
           audit: dict[str, Any]) -> None:
    write_audit(db, "staff", staff_id, action, "business_setting", item.key,
                before={"store_id": store_id, "value": old}, after={"store_id": store_id, "value": new},
                reason=audit.get("reason"), ip=audit.get("ip"), user_agent=audit.get("user_agent"),
                trace_id=audit.get("trace_id"))


def update_setting(db: Session, staff_id: int, key: str, value: Any, version: int, store_id: int | None = None,
                   **audit: Any) -> dict[str, Any]:
    item = _item(key, store_id)
    try:
        value = item.validate(value)
    except (TypeError, ValueError) as exc:
        raise AppError(ErrorCode.BAD_REQUEST, f"invalid value: {exc}",
                       {"reason": "validation_failed", "errors": [{"field": "value", "message": str(exc)}]}) from None
    row = _row(db, item.key, store_id)
    old = None if row is None else json.loads(row.value_json)
    now = utcnow()
    if row is None:
        if version != 0:
            raise _conflict(db, item, store_id)
        try:
            with db.begin_nested():  # 并发首次写入由部分唯一索引兜底
                db.add(BusinessSettingValue(store_id=store_id, key=item.key, value_json=json.dumps(value), version=1,
                                            updated_by=staff_id, created_at=now, updated_at=now))
        except IntegrityError:
            raise _conflict(db, item, store_id) from None
    else:
        result = db.execute(update(BusinessSettingValue)
                            .where(BusinessSettingValue.id == row.id, BusinessSettingValue.version == version)
                            .values(value_json=json.dumps(value), version=BusinessSettingValue.version + 1,
                                    updated_by=staff_id, updated_at=now)
                            .execution_options(synchronize_session=False))
        if result.rowcount != 1:
            db.refresh(row)
            raise _conflict(db, item, store_id)
        db.refresh(row)
    _audit(db, staff_id, "business.setting.update", item, store_id, old, value, audit)
    log_event(log, ev.BUSINESS_SETTING_UPDATED, outcome="success", key=item.key, store_id=store_id)
    return view(db, item, store_id)


def reset_setting(db: Session, staff_id: int, key: str, store_id: int | None = None, **audit: Any) -> dict[str, Any]:
    """全局：恢复登记的默认值；门店：取消门店覆盖，回到全局值。本来就没有值时不写审计。"""
    item = _item(key, store_id)
    row = _row(db, item.key, store_id)
    if row is None:
        return view(db, item, store_id)
    old = json.loads(row.value_json)
    result = db.execute(delete(BusinessSettingValue).where(BusinessSettingValue.id == row.id,
                                                           BusinessSettingValue.version == row.version)
                        .execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise _conflict(db, item, store_id)
    db.expunge(row)
    _audit(db, staff_id, "business.setting.reset", item, store_id, old, None, audit)
    log_event(log, ev.BUSINESS_SETTING_UPDATED, outcome="success", key=item.key, store_id=store_id, reset=True)
    return view(db, item, store_id)
