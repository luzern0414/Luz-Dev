"""纯函数规则：文案变量与字数、客户端可见的运行参数筛选、版本比较。便于单测。"""
from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from luzback.runtime_config import PLATFORMS, KeyInfo, RuntimeConfig, version_tuple

VARIABLE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
TEXT_KEY = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_.]*$")


def variables_in(text: str) -> set[str]:
    """文案里出现的变量：`{store_name}` → store_name。"""
    return set(VARIABLE.findall(text or ""))


def text_problems(text: str, max_length: int, variables: Iterable[str]) -> list[tuple[str, dict[str, Any]]]:
    """[(原因代码, 补充数据)]：超过字数上限、使用未登记的变量。字数按原文计，变量占位符也算字数。"""
    problems: list[tuple[str, dict[str, Any]]] = []
    if len(text) > max_length:
        problems.append(("content_text_too_long", {"max_length": max_length, "length": len(text)}))
    unknown = sorted(variables_in(text) - set(variables))
    if unknown:
        problems.append(("content_variable_unknown", {"unknown": unknown, "allowed": sorted(variables)}))
    return problems


def client_values(config: RuntimeConfig, keys: Iterable[KeyInfo], platform: str | None,
                  client_version: str | None) -> dict[str, Any]:
    """客户端可见的运行参数：只取 client_visible 的键（白名单），按端与客户端版本过滤，新键只给认识它的版本。

    platform 为空时只给各端通用的键。
    """
    current = version_tuple(client_version) or (0, 0, 0)
    dump = config.model_dump(mode="json")
    values: dict[str, Any] = {}
    for info in keys:
        if not info.client_visible:
            continue
        if platform is None and set(info.platforms) != set(PLATFORMS):
            continue
        if platform is not None and platform not in info.platforms:
            continue
        since = version_tuple(info.client_since) or (0, 0, 0)
        if current < since:
            continue
        values[info.path] = dump[info.group][info.name]
    return values


def etag_matches(header: str | None, version: str) -> bool:
    """If-None-Match 是否命中当前版本（支持多个值与 W/ 前缀）。"""
    if not header:
        return False
    tags = [t.strip().removeprefix("W/").strip('"') for t in header.split(",")]
    return version in tags or "*" in tags
