"""分页与排序（SPEC §3.4）。

- 顾客端游标：cursor = base64(json{"v": 排序值, "id": id}) + HMAC 签名，防篡改；limit 默认 10，上限 20。
- 后台页码：page ≥ 1，page_size 默认 20，上限 100。
- 排序白名单：sort=-created_at,amount → [("created_at", True), ("amount", False)]。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

from .errors import AppError, ErrorCode


def _sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()[:16]


def encode_cursor(sort_value: Any, row_id: int, secret: str) -> str:
    body = json.dumps({"v": sort_value, "id": row_id}, default=str, separators=(",", ":")).encode()
    token = base64.urlsafe_b64encode(body).rstrip(b"=").decode()
    return f"{token}.{_sign(secret, body)}"


def decode_cursor(cursor: str | None, secret: str) -> tuple[Any, int] | None:
    if not cursor:
        return None
    try:
        token, sig = cursor.split(".")
        body = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        if not hmac.compare_digest(_sign(secret, body), sig):
            raise ValueError("bad signature")
        data = json.loads(body)
        return data["v"], int(data["id"])
    except Exception as exc:  # noqa: BLE001
        raise AppError(ErrorCode.BAD_REQUEST, "invalid cursor") from exc


def parse_limit(limit: int | None, default: int = 10, maximum: int = 20) -> int:
    if limit is None:
        return default
    if limit < 1:
        raise AppError(ErrorCode.BAD_REQUEST, "limit must be >= 1")
    return min(limit, maximum)


def parse_page(page: int | None, page_size: int | None, default_size: int = 20, maximum: int = 100) -> tuple[int, int]:
    page = page or 1
    size = page_size or default_size
    if page < 1 or size < 1:
        raise AppError(ErrorCode.BAD_REQUEST, "page and page_size must be >= 1")
    return page, min(size, maximum)


def parse_sort(sort: str | None, allowed: set[str], default: str = "-created_at") -> list[tuple[str, bool]]:
    """返回 [(字段, 是否降序)]；字段不在白名单抛 4001。"""
    result: list[tuple[str, bool]] = []
    for raw in (sort or default).split(","):
        raw = raw.strip()
        if not raw:
            continue
        desc = raw.startswith("-")
        name = raw[1:] if desc else raw
        if name not in allowed:
            raise AppError(ErrorCode.BAD_REQUEST, f"sort field not allowed: {name}")
        result.append((name, desc))
    if not any(name == "id" for name, _ in result):
        result.append(("id", result[0][1] if result else True))
    return result
