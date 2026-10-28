"""响应信封与分页包装（API.md §1.3、§1.4）。"""
from __future__ import annotations

from typing import Any


def ok(data: Any = None, message: str = "ok") -> dict[str, Any]:
    return {"code": 0, "message": message, "data": data}


def cursor_page(items: list[Any], next_cursor: str | None, has_more: bool, total: int | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"items": items, "next_cursor": next_cursor, "has_more": has_more}
    if total is not None:
        body["total"] = total
    return ok(body)


def offset_page(items: list[Any], page: int, page_size: int, total: int) -> dict[str, Any]:
    return ok({"items": items, "page": page, "page_size": page_size, "total": total})
