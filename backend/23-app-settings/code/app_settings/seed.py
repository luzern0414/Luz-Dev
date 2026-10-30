"""文案种子：开发维护的 `23-app-settings/seed/texts.toml` → ui_texts（幂等同步）。

种子登记每个文案键的页面、位置说明、默认文案、字数上限、变量、能否留空、是否后台可见与排序；默认文案与小程序安装包
内置的兜底值保持一致。隐藏键（admin_visible = false）的文案只能经种子修改：它们不存自定义文案，种子的默认文案就是下发值。

同步规则（`python -m app_settings sync`，发布时在迁移之后执行）：
- 新键插入；已有键更新页面、位置说明、默认文案、上限、变量、可见性与排序；种子里没有的键删除。
- 运营改过的文案保留；新的上限或变量让它不合法时清空为默认文案并记日志（避免撑破版面）。
- 改变了小程序看到的文字（生效文案变化、新增、删除）的键各写一条 content_revisions（source = migration），内容版本随之增加。
"""
from __future__ import annotations

import json
import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from luzback import log_events as ev
from luzback.logging import log_event

from . import rules
from .content import _revision, effective_text
from .models import SOURCE_MIGRATION, TARGET_TEXT, UiText

log = logging.getLogger("luz.app_settings")

SEED_PATH = Path(__file__).resolve().parents[2] / "seed" / "texts.toml"
_FIELDS = {"page", "slot", "default", "max_length", "variables", "allow_empty", "admin_visible", "sort"}


@dataclass(frozen=True)
class SeedText:
    key: str
    page: str
    slot_label: str
    default_text: str
    max_length: int
    variables: tuple[str, ...]
    allow_empty: bool = False
    admin_visible: bool = True
    sort_order: int = 0


def _check(key: str, raw: dict[str, Any]) -> tuple[SeedText | None, list[str]]:
    errors: list[str] = []
    unknown = set(raw) - _FIELDS
    if unknown:
        errors.append(f"{key}: unknown fields {sorted(unknown)}")
    if len(key) > 80 or not rules.TEXT_KEY.match(key):
        errors.append(f"{key}: key must be page.slot in lower snake case, at most 80 characters")
    page, slot, default = raw.get("page"), raw.get("slot"), raw.get("default")
    max_length, variables = raw.get("max_length"), raw.get("variables", [])
    if not isinstance(page, str) or not page or len(page) > 48 or not key.startswith(page + "."):
        errors.append(f"{key}: page must be the key prefix (at most 48 characters)")
    if not isinstance(slot, str) or not slot or len(slot) > 60:
        errors.append(f"{key}: slot is required (at most 60 characters)")
    if not isinstance(max_length, int) or isinstance(max_length, bool) or not 1 <= max_length <= 2000:
        errors.append(f"{key}: max_length must be an integer 1–2000")
    if not isinstance(variables, list) or not all(isinstance(v, str) and rules.VARIABLE.fullmatch("{" + v + "}")
                                                    for v in variables):
        errors.append(f"{key}: variables must be a list of identifiers")
        variables = []
    if not isinstance(default, str) or not default.strip():
        errors.append(f"{key}: default text is required")
    elif isinstance(max_length, int):
        errors.extend(f"{key}: default {reason}" for reason, _ in rules.text_problems(default, max_length, variables))
    for flag in ("allow_empty", "admin_visible"):
        if flag in raw and not isinstance(raw[flag], bool):
            errors.append(f"{key}: {flag} must be true or false")
    if "sort" in raw and (not isinstance(raw["sort"], int) or isinstance(raw["sort"], bool)):
        errors.append(f"{key}: sort must be an integer")
    if errors:
        return None, errors
    return SeedText(key=key, page=page, slot_label=slot, default_text=default.strip(), max_length=max_length,
                    variables=tuple(variables), allow_empty=bool(raw.get("allow_empty", False)),
                    admin_visible=bool(raw.get("admin_visible", True)), sort_order=int(raw.get("sort", 0))), []


def load_seed(path: Path = SEED_PATH) -> list[SeedText]:
    """读取并校验种子；有任何错误抛 ValueError（逐条列出）。"""
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    errors = [f"unknown table [{name}]" for name in data if name != "texts"]
    items: list[SeedText] = []
    for key, raw in (data.get("texts") or {}).items():
        if not isinstance(raw, dict):
            errors.append(f"{key}: must be a table")
            continue
        item, problems = _check(key, raw)
        errors.extend(problems)
        if item is not None:
            items.append(item)
    if errors:
        raise ValueError("invalid text seed:\n  " + "\n  ".join(errors))
    return items


def sync_texts(db: Session, seed: list[SeedText] | None = None) -> dict[str, int]:
    """把种子写进 ui_texts；不提交（命令行与测试负责提交）。返回各类计数。"""
    seed = load_seed() if seed is None else seed
    existing = {row.key: row for row in db.execute(select(UiText)).scalars()}
    counts = {"created": 0, "updated": 0, "deleted": 0, "unchanged": 0, "reset": 0}
    for item in seed:
        row = existing.pop(item.key, None)
        variables = json.dumps(list(item.variables), ensure_ascii=False)
        if row is None:
            db.add(UiText(key=item.key, page=item.page, slot_label=item.slot_label, text=None,
                          default_text=item.default_text, max_length=item.max_length, variables_json=variables,
                          allow_empty=item.allow_empty, admin_visible=item.admin_visible, sort_order=item.sort_order,
                          version=1))
            _revision(db, TARGET_TEXT, item.key, None, item.default_text, None, SOURCE_MIGRATION, old_absent=True)
            counts["created"] += 1
            continue
        before = effective_text(row)
        meta = (row.page, row.slot_label, row.default_text, row.max_length, row.variables_json, row.allow_empty,
                row.admin_visible, row.sort_order)
        row.page, row.slot_label, row.default_text = item.page, item.slot_label, item.default_text
        row.max_length, row.variables_json, row.allow_empty = item.max_length, variables, item.allow_empty
        row.admin_visible, row.sort_order = item.admin_visible, item.sort_order
        invalid = row.text is not None and (
            not item.admin_visible
            or (row.text == "" and not item.allow_empty)
            or (row.text != "" and bool(rules.text_problems(row.text, item.max_length, item.variables))))
        if invalid:
            log_event(log, ev.CONTENT_SEED_SYNCED, logging.WARNING, outcome="success", reason="custom_text_reset", key=item.key)
            row.text = None  # 隐藏键不保留自定义文案；新上限或变量不容纳旧文案时回到默认
            counts["reset"] += 1
        changed_meta = meta != (row.page, row.slot_label, row.default_text, row.max_length, row.variables_json,
                                row.allow_empty, row.admin_visible, row.sort_order)
        after = effective_text(row)
        if changed_meta or before != after:
            row.version += 1
            counts["updated"] += 1
        else:
            counts["unchanged"] += 1
        if before != after:
            _revision(db, TARGET_TEXT, item.key, before, after, None, SOURCE_MIGRATION)
    for key, row in existing.items():  # 代码不再使用的键
        _revision(db, TARGET_TEXT, key, effective_text(row), None, None, SOURCE_MIGRATION, new_absent=True)
        db.delete(row)
        counts["deleted"] += 1
    db.flush()
    log_event(log, ev.CONTENT_SEED_SYNCED, outcome="success", **counts)
    return counts
