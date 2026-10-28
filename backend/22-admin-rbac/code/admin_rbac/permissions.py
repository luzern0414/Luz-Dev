"""权限码注册表：各模块在导入时声明自己的权限码（`<module>:<action>`），角色编辑器与校验都以注册表为准。

用法（业务模块 __init__.py 或 api.py 顶部）：

    from admin_rbac import declare_permissions
    declare_permissions("order", [("order:read", "订单查看"), ("order:refund", "订单退款")])

`require_permission(code)` 遇到未声明的码会自动登记（模块名取冒号前缀），保证路由上用到的码都能在角色编辑器里授予。
数据库表 admin_permissions 由 sync_permissions(db) 从注册表同步（种子 / 启动时调用）。
"""
from __future__ import annotations

import re
import threading
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,30}:[a-z][a-z0-9_]{0,30}$")
WILDCARD = "*"


@dataclass(frozen=True)
class PermissionDef:
    code: str
    module: str
    description: str


_lock = threading.Lock()
_registry: dict[str, PermissionDef] = {}


def validate_code(code: str) -> str:
    if not isinstance(code, str) or not CODE_PATTERN.match(code):
        raise ValueError(f"permission code must look like 'module:action', got {code!r}")
    return code


def declare_permission(code: str, description: str = "", module: str | None = None) -> PermissionDef:
    """登记一个权限码；重复登记以首次的描述为准（描述为空时用后来的补上）。"""
    validate_code(code)
    with _lock:
        existing = _registry.get(code)
        if existing is not None:
            if not existing.description and description:
                existing = PermissionDef(code, existing.module, description)
                _registry[code] = existing
            return existing
        item = PermissionDef(code, module or code.split(":", 1)[0], description)
        _registry[code] = item
        return item


def declare_permissions(module: str, items: Iterable[tuple[str, str]]) -> list[PermissionDef]:
    return [declare_permission(code, description, module) for code, description in items]


def is_registered(code: str) -> bool:
    return code in _registry


def registered_codes() -> frozenset[str]:
    with _lock:
        return frozenset(_registry)


def registered() -> list[PermissionDef]:
    with _lock:
        return sorted(_registry.values(), key=lambda p: (p.module, p.code))


def expand(patterns: Iterable[str]) -> list[str]:
    """`prefix:*` 展开为注册表中该前缀的全部码；普通码必须已登记。"""
    codes = registered_codes()
    result: set[str] = set()
    for pattern in patterns:
        if pattern.endswith(":*"):
            prefix = pattern[:-1]
            matched = {c for c in codes if c.startswith(prefix)}
            if not matched:
                raise ValueError(f"no permission matches {pattern}")
            result |= matched
        else:
            if pattern not in codes:
                raise ValueError(f"unknown permission {pattern}")
            result.add(pattern)
    return sorted(result)


def sync_permissions(db: Session) -> int:
    """把注册表写入 admin_permissions（新增或补描述）；返回新增条数。不 commit。"""
    from .models import AdminPermission

    existing = {p.code: p for p in db.execute(select(AdminPermission)).scalars().all()}
    added = 0
    for item in registered():
        row = existing.get(item.code)
        if row is None:
            db.add(AdminPermission(code=item.code, module=item.module, description=item.description))
            added += 1
        elif item.description and row.description != item.description:
            row.description = item.description
    db.flush()
    return added
