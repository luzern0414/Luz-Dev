"""last_entities（SPEC §4.1）：从工具结果里提取 order_id / product_id / address_id（最多 20 个）供指代消解。"""
from __future__ import annotations

from typing import Any

_ID_KEYS = ("order_id", "product_id", "address_id")
_NAME_KEYS = ("name", "order_no", "status", "title", "contact")
MAX_ENTITIES = 20


def _walk(node: Any, out: list[dict], hint: str | None) -> None:
    if isinstance(node, dict):
        found = {k: node[k] for k in _ID_KEYS if k in node and node[k] is not None}
        if not found and "id" in node and hint:
            found = {hint: node["id"]}
        if found:
            ent = dict(found)
            for k in _NAME_KEYS:
                if k in node and isinstance(node[k], (str, int)):
                    ent[k] = node[k]
            out.append(ent)
        for k, v in node.items():
            child_hint = hint
            if k in ("items", "products", "orders", "addresses"):
                child_hint = {"products": "product_id", "orders": "order_id", "addresses": "address_id"}.get(k, hint)
            _walk(v, out, child_hint)
    elif isinstance(node, list):
        for v in node:
            _walk(v, out, hint)


def extract_entities(data: Any, tool: str | None = None, limit: int = MAX_ENTITIES) -> list[dict]:
    hint = None
    if tool:
        if "order" in tool:
            hint = "order_id"
        elif "product" in tool:
            hint = "product_id"
        elif "address" in tool:
            hint = "address_id"
    out: list[dict] = []
    _walk(data, out, hint)
    uniq: list[dict] = []
    seen: set[tuple] = set()
    for e in out:
        key = tuple(sorted((k, str(v)) for k, v in e.items() if k in _ID_KEYS))
        if key and key not in seen:
            seen.add(key)
            uniq.append(e)
    return uniq[:limit]


def merge_entities(old: list[dict], new: list[dict], limit: int = MAX_ENTITIES) -> list[dict]:
    """新结果优先；总数不超过 limit。"""
    merged = list(new)
    keys = {tuple(sorted((k, str(v)) for k, v in e.items() if k in _ID_KEYS)) for e in new}
    for e in old:
        key = tuple(sorted((k, str(v)) for k, v in e.items() if k in _ID_KEYS))
        if key not in keys:
            merged.append(e)
            keys.add(key)
    return merged[:limit]
