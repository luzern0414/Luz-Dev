#!/usr/bin/env python3
"""Record, promote, and deprecate Luz Dev experience without third-party packages.

Two stores share one lock and one index:
- experience/records/EXP-*.md  long task records (candidate → verified → deprecated),
  de-duplicated by `key`: recording an existing key updates that record in place.
- experience/notes.json         short preference / lesson / fact entries, upserted by key,
  capped per kind and pruned by value (oldest confirmation, fewest uses first).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXPERIENCE_ROOT = ROOT / "experience"
RECORD_TEMPLATE = ROOT / "assets" / "experience" / "RECORD-TEMPLATE.md"
RECORD_NAME = re.compile(r"^EXP-(\d{4,})-.+\.md$")
PLAN_NAME = re.compile(r"^\d+-.+\.html$", re.IGNORECASE)
UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
SPACE = re.compile(r"\s+")
MULTIPLE_HYPHENS = re.compile(r"-{2,}")
TASK_MODES = ("implementation", "review", "diagnosis", "analysis", "operations")
STATUSES = ("candidate", "verified", "deprecated")
# Short entries learned on every call: keyed, replaced in place, bounded.
NOTES_FILE = "notes.json"
NOTE_KINDS = ("preference", "lesson", "fact")
NOTE_CAPS = {"preference": 60, "lesson": 120, "fact": 120}
RECORD_CAP = 120  # active (candidate + verified) EXP records
STALE_DAYS = 180
KEY_PATTERN = re.compile(r"^[0-9a-z一-鿿][0-9a-z一-鿿-]{0,79}$")
PRIVATE_PATTERNS = (
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "手机号"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[A-Za-z]{2,}"), "邮箱"),
    (re.compile(r"(?i)\bwxid_|微信号"), "微信号"),
    (re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/]|/Users/|/home/"), "本机路径"),
    (re.compile(r"(?i)(password|passwd|secret|api[_-]?key|token)\s*[:=]"), "凭证"),
)
NOTES_START = "<!-- NOTES_START -->"
NOTES_END = "<!-- NOTES_END -->"
NOTE_KIND_LABELS = {"preference": "用户偏好", "lesson": "教训", "fact": "有效信息"}
INDEX_HEADER = """# Luz Dev Experience Index

按主题、标签和状态检索经验。候选经验不能作为默认工程规则。

<!-- EXPERIENCE_ROWS_START -->
| ID | 主题 | 任务类型 | 状态 | 标签 | 证据数 | 更新时间 |
|---|---|---|---|---|---|---|
<!-- EXPERIENCE_ROWS_END -->
"""


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def clean_slug(raw: str) -> str:
    title = raw.strip()
    if not title:
        raise ValueError("经验主题不能为空")
    if len(title) > 160:
        raise ValueError("经验主题不能超过 160 个字符")
    title = SPACE.sub("-", title)
    title = UNSAFE.sub("-", title)
    title = MULTIPLE_HYPHENS.sub("-", title).strip(" .-_")
    if not title:
        raise ValueError("经验主题清洗后为空")
    return title[:100].rstrip(" .-_")


def metadata_value(value: Any) -> str:
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.+-]+", value):
        return value
    return json.dumps(value, ensure_ascii=False)


def json_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def read_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("经验记录缺少 front matter")
    try:
        end = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration as exc:
        raise ValueError("经验记录 front matter 未闭合") from exc
    fields: dict[str, Any] = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise ValueError(f"无法解析经验元数据: {line}")
        key, raw = line.split(":", 1)
        raw = raw.strip()
        try:
            fields[key.strip()] = json.loads(raw) if raw[:1] in {'"', "[", "{"} else raw
        except json.JSONDecodeError as exc:
            raise ValueError(f"无法解析字段 {key.strip()}: {raw}") from exc
    return fields, "\n".join(lines[end + 1 :])


def write_frontmatter(text: str, fields: dict[str, Any], keys: tuple[str, ...]) -> str:
    lines = text.splitlines()
    end = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    present: dict[str, int] = {}
    for index in range(1, end):
        if ":" in lines[index]:
            key = lines[index].split(":", 1)[0].strip()
            if key in keys:
                present[key] = index
    for key in keys:
        line = f"{key}: {metadata_value(fields[key])}"
        if key in present:
            lines[present[key]] = line
    for key in keys:
        if key not in present:
            lines.insert(end, f"{key}: {json_value(fields[key])}")
            end += 1
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def replace_marker(text: str, start: str, end: str, content: str) -> str:
    pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)
    replacement = f"{start}\n{content.strip()}\n{end}"
    if pattern.search(text) is None:
        raise ValueError(f"经验记录缺少标记: {start}")
    return pattern.sub(replacement, text, count=1)


@contextmanager
def exclusive_lock(experience_root: Path, timeout: float = 20.0) -> Iterator[None]:
    lock_path = experience_root / ".record-experience.lock"
    deadline = time.monotonic() + timeout
    descriptor: int | None = None
    while descriptor is None:
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(descriptor, f"{os.getpid()} {time.time():.3f}\n".encode("ascii"))
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TimeoutError("等待经验记录锁超时")
            time.sleep(0.05)
    try:
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def ensure_repository(experience_root: Path) -> None:
    for relative in ("records", "playbooks", "reusable", "reusable/backend", "reusable/frontend", "reusable/scripts"):
        (experience_root / relative).mkdir(parents=True, exist_ok=True)
    index = experience_root / "INDEX.md"
    if not index.exists():
        index.write_text(INDEX_HEADER, encoding="utf-8", newline="\n")


def next_id(experience_root: Path) -> str:
    maximum = 0
    for record in (experience_root / "records").glob("EXP-*.md"):
        match = RECORD_NAME.fullmatch(record.name)
        if match:
            maximum = max(maximum, int(match.group(1)))
    return f"EXP-{maximum + 1:04d}"


def load_records(experience_root: Path) -> list[tuple[Path, dict[str, Any], str]]:
    records: list[tuple[Path, dict[str, Any], str]] = []
    for record in sorted((experience_root / "records").glob("EXP-*.md")):
        if not RECORD_NAME.fullmatch(record.name):
            continue
        fields, body = read_frontmatter(record.read_text(encoding="utf-8"))
        records.append((record, fields, body))
    return records


def rebuild_index(experience_root: Path) -> Path:
    index = experience_root / "INDEX.md"
    text = index.read_text(encoding="utf-8") if index.exists() else INDEX_HEADER
    marker = re.compile(
        r"<!-- EXPERIENCE_ROWS_START -->.*?<!-- EXPERIENCE_ROWS_END -->", re.DOTALL
    )
    if marker.search(text) is None:
        raise ValueError("INDEX.md 缺少经验表格标记")
    rows: list[str] = []
    for record, fields, _ in load_records(experience_root):
        record_id = fields.get("id", "")
        if record_id not in record.name:
            raise ValueError(f"经验 ID 与文件名不一致: {record.name}")
        status = fields.get("status", "")
        if status not in STATUSES:
            raise ValueError(f"{record_id} 使用了非法状态: {status}")
        title = str(fields.get("title", "")).replace("|", "\\|")
        tags = ", ".join(str(item) for item in fields.get("tags", []))
        evidence = fields.get("evidence", [])
        updated = fields.get("updated", fields.get("created", ""))
        relative = record.relative_to(experience_root).as_posix()
        rows.append(
            f"| {record_id} | [{title}]({relative}) | {fields.get('task_mode', '')} | "
            f"{status} | {tags} | {len(evidence)} | {updated} |"
        )
    table = (
        "<!-- EXPERIENCE_ROWS_START -->\n"
        "| ID | 主题 | 任务类型 | 状态 | 标签 | 证据数 | 更新时间 |\n"
        "|---|---|---|---|---|---|---|\n"
        + ("\n".join(rows) + "\n" if rows else "")
        + "<!-- EXPERIENCE_ROWS_END -->"
    )
    updated_text = marker.sub(lambda _: table, text, count=1)
    notes_block = f"{NOTES_START}\n{render_notes_table(load_notes(experience_root))}{NOTES_END}"
    notes_marker = re.compile(re.escape(NOTES_START) + r".*?" + re.escape(NOTES_END), re.DOTALL)
    if notes_marker.search(updated_text):
        updated_text = notes_marker.sub(lambda _: notes_block, updated_text, count=1)
    else:
        updated_text = (
            updated_text.rstrip("\n")
            + "\n\n## 偏好、教训与有效信息\n\n"
            + "由 `record_experience.py note` 按 key 写入：同一 key 再写会替换原条目，不追加。\n\n"
            + notes_block
            + "\n"
        )
    temporary = index.with_name(f".{index.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    temporary.write_text(updated_text, encoding="utf-8", newline="\n")
    os.replace(temporary, index)
    return index


def find_record(experience_root: Path, record_id: str) -> Path:
    if not re.fullmatch(r"EXP-\d{4,}", record_id):
        raise ValueError("经验 ID 必须使用 EXP-NNNN 格式")
    matches = list((experience_root / "records").glob(f"{record_id}-*.md"))
    if len(matches) != 1:
        raise ValueError(f"找不到唯一的经验记录: {record_id}")
    return matches[0]


def markdown_list(values: list[str], empty: str) -> str:
    return "\n".join(f"- {value}" for value in values) if values else empty


# ---------------------------------------------------------------------------
# Keys, normalization, privacy
# ---------------------------------------------------------------------------


def normalize_text(text: str) -> str:
    """Comparable form used for near-duplicate detection."""
    return re.sub(r"[\W_]+", "", str(text).lower())


def derive_key(title: str) -> str:
    key = re.sub(r"[^0-9a-z一-鿿]+", "-", title.strip().lower()).strip("-")
    return key[:80].rstrip("-") or "untitled"


def validate_key(key: str) -> str:
    key = key.strip()
    if not KEY_PATTERN.fullmatch(key):
        raise ValueError("key 只能用小写字母、数字、中文和连字符，1–80 个字符，例如 plan-before-build")
    return key


def privacy_problems(text: str) -> list[str]:
    return [label for pattern, label in PRIVATE_PATTERNS if pattern.search(text)]


def parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.astimezone()


def is_stale(value: Any, reference: datetime | None = None) -> bool:
    confirmed = parse_time(value)
    if confirmed is None:
        return True
    reference = reference or datetime.now().astimezone()
    return (reference - confirmed).days >= STALE_DAYS


def value_order(uses: Any, confirmed: Any) -> tuple[int, int, str]:
    """Sort key: smallest = least valuable (stale first, then fewest uses, then oldest)."""
    try:
        count = int(uses)
    except (TypeError, ValueError):
        count = 0
    return (0 if is_stale(confirmed) else 1, count, str(confirmed or ""))


# ---------------------------------------------------------------------------
# Notes store: preference / lesson / fact
# ---------------------------------------------------------------------------


def notes_path(experience_root: Path) -> Path:
    return experience_root / NOTES_FILE


def load_notes(experience_root: Path) -> list[dict[str, Any]]:
    path = notes_path(experience_root)
    if not path.is_file():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    entries = payload.get("entries", []) if isinstance(payload, dict) else []
    if not isinstance(entries, list):
        raise ValueError(f"{NOTES_FILE} 的 entries 必须是数组")
    return entries


def save_notes(experience_root: Path, entries: list[dict[str, Any]]) -> Path:
    path = notes_path(experience_root)
    ordered = sorted(entries, key=lambda item: (NOTE_KINDS.index(item["kind"]) if item.get("kind") in NOTE_KINDS else 99, item.get("key", "")))
    payload = {
        "description": "Luz Dev 每次调用后沉淀的偏好、教训与有效信息。由 scripts/record_experience.py note 写入；同一 key 替换，不追加。",
        "caps": NOTE_CAPS,
        "entries": ordered,
    }
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    os.replace(temporary, path)
    return path


def upsert_note(
    entries: list[dict[str, Any]], *, kind: str, key: str, text: str, why: str = "", stamp: str | None = None
) -> tuple[str, dict[str, Any]]:
    """Insert or replace the entry with this key. Returns (action, entry)."""
    if kind not in NOTE_KINDS:
        raise ValueError(f"kind 必须是 {' / '.join(NOTE_KINDS)}")
    key = validate_key(key)
    text = SPACE.sub(" ", text).strip()
    why = SPACE.sub(" ", why).strip()
    if not text:
        raise ValueError("--text 不能为空")
    if len(text) > 400:
        raise ValueError("--text 不能超过 400 个字符；写通用原则，不写任务细节")
    problems = privacy_problems(f"{text} {why}")
    if problems:
        raise ValueError(f"条目包含个人或私有信息（{'、'.join(problems)}）；仓库是公开的，不能写入")
    stamp = stamp or now()
    normalized = normalize_text(text)
    for entry in entries:
        if entry.get("key") != key and normalize_text(entry.get("text", "")) == normalized:
            raise ValueError(f"与已有条目 {entry.get('key')} 内容相同；请用该 key 更新，而不是新增")
    existing = next((entry for entry in entries if entry.get("key") == key), None)
    if existing is None:
        entry = {
            "kind": kind,
            "key": key,
            "text": text,
            "why": why,
            "created": stamp,
            "last_confirmed": stamp,
            "uses": 1,
            "previous": None,
        }
        entries.append(entry)
        return "created", entry
    existing["uses"] = int(existing.get("uses", 0) or 0) + 1
    existing["last_confirmed"] = stamp
    if normalize_text(existing.get("text", "")) == normalized and existing.get("kind") == kind:
        if why:
            existing["why"] = why
        return "confirmed", existing
    existing["previous"] = {"kind": existing.get("kind"), "text": existing.get("text", ""), "replaced": stamp}
    existing["kind"] = kind
    existing["text"] = text
    existing["why"] = why or existing.get("why", "")
    return "replaced", existing


def prune_notes(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop the least valuable entries of each kind above its cap. Returns removed entries."""
    removed: list[dict[str, Any]] = []
    for kind, cap in NOTE_CAPS.items():
        of_kind = [entry for entry in entries if entry.get("kind") == kind]
        overflow = len(of_kind) - cap
        if overflow <= 0:
            continue
        victims = sorted(of_kind, key=lambda item: value_order(item.get("uses"), item.get("last_confirmed")))[:overflow]
        for victim in victims:
            entries.remove(victim)
            removed.append(victim)
    return removed


def check_notes(entries: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    seen_keys: dict[str, int] = {}
    seen_text: dict[str, str] = {}
    for entry in entries:
        key = str(entry.get("key", ""))
        kind = entry.get("kind")
        if kind not in NOTE_KINDS:
            errors.append(f"note {key}: 非法 kind {kind!r}")
        if not KEY_PATTERN.fullmatch(key):
            errors.append(f"note {key!r}: key 格式不合法")
        seen_keys[key] = seen_keys.get(key, 0) + 1
        text = str(entry.get("text", ""))
        if not text.strip():
            errors.append(f"note {key}: text 为空")
        normalized = normalize_text(text)
        if normalized and normalized in seen_text and seen_text[normalized] != key:
            errors.append(f"note {key}: 与 {seen_text[normalized]} 内容重复")
        seen_text.setdefault(normalized, key)
        problems = privacy_problems(f"{text} {entry.get('why', '')}")
        if problems:
            errors.append(f"note {key}: 含个人或私有信息（{'、'.join(problems)}）")
        if is_stale(entry.get("last_confirmed")):
            warnings.append(f"note {key}: 超过 {STALE_DAYS} 天未确认，超限时优先裁剪")
    for key, count in seen_keys.items():
        if count > 1:
            errors.append(f"note {key}: key 重复 {count} 次")
    for kind, cap in NOTE_CAPS.items():
        count = sum(1 for entry in entries if entry.get("kind") == kind)
        if count > cap:
            errors.append(f"{NOTE_KIND_LABELS[kind]}超过上限：{count} > {cap}（运行 prune）")
    return errors, warnings


def render_notes_table(entries: list[dict[str, Any]]) -> str:
    header = "| 类型 | key | 内容 | 使用次数 | 最近确认 |\n|---|---|---|---|---|\n"
    rows = []
    for entry in sorted(entries, key=lambda item: (NOTE_KINDS.index(item["kind"]) if item.get("kind") in NOTE_KINDS else 99, item.get("key", ""))):
        text = str(entry.get("text", "")).replace("|", "\\|")
        confirmed = str(entry.get("last_confirmed", ""))[:10]
        rows.append(
            f"| {NOTE_KIND_LABELS.get(entry.get('kind'), entry.get('kind'))} | {entry.get('key')} | {text} | "
            f"{entry.get('uses', 0)} | {confirmed} |"
        )
    return header + ("\n".join(rows) + "\n" if rows else "")


# ---------------------------------------------------------------------------
# Record keys, duplicates and caps
# ---------------------------------------------------------------------------


def record_key(fields: dict[str, Any]) -> str:
    return str(fields.get("key") or derive_key(str(fields.get("title", ""))))


def active_records(experience_root: Path) -> list[tuple[Path, dict[str, Any], str]]:
    return [item for item in load_records(experience_root) if item[1].get("status") != "deprecated"]


def check_records(experience_root: Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    by_key: dict[str, list[str]] = {}
    by_title: dict[str, list[str]] = {}
    active = active_records(experience_root)
    for record, fields, _ in active:
        record_id = str(fields.get("id", record.name))
        if not fields.get("key"):
            warnings.append(f"{record_id}: 缺少 key，按标题推导为 {record_key(fields)}")
        by_key.setdefault(record_key(fields), []).append(record_id)
        by_title.setdefault(normalize_text(str(fields.get("title", ""))), []).append(record_id)
    for key, ids in by_key.items():
        if len(ids) > 1:
            errors.append(f"经验 key {key} 重复：{', '.join(ids)}（保留一条，其余 deprecate 并指向它）")
    for ids in by_title.values():
        if len(ids) > 1 and len({record_key(f) for _, f, _ in active if str(f.get('id')) in ids}) > 1:
            errors.append(f"经验标题重复：{', '.join(ids)}")
    if len(active) > RECORD_CAP:
        errors.append(f"有效经验超过上限：{len(active)} > {RECORD_CAP}（运行 prune）")
    return errors, warnings


def check_store(experience_root: Path) -> tuple[list[str], list[str]]:
    note_errors, note_warnings = check_notes(load_notes(experience_root))
    record_errors, record_warnings = check_records(experience_root)
    return note_errors + record_errors, note_warnings + record_warnings


SECTION_SUMMARY = "摘要"
SECTION_DECISIONS = "决策与做法"
SECTION_FAILURES = "失败尝试与教训"
SECTION_REUSABLE = "可复用候选"


def replace_section(text: str, heading: str, content: str) -> str:
    pattern = re.compile(r"(^## " + re.escape(heading) + r"[ \t]*\n\n)(.*?)(?=\n\n## |\Z)", re.DOTALL | re.MULTILINE)
    if pattern.search(text) is None:
        raise ValueError(f"经验记录缺少章节: {heading}")
    return pattern.sub(lambda match: match.group(1) + content.strip(), text, count=1)


def read_section(text: str, heading: str) -> str:
    match = re.search(r"^## " + re.escape(heading) + r"[ \t]*\n\n(.*?)(?=\n\n## |\Z)", text, re.DOTALL | re.MULTILINE)
    return match.group(1).strip() if match else ""


def render_record(
    *,
    record_id: str,
    title: str,
    task_mode: str,
    plan: Path,
    summary: str,
    tags: list[str],
    decisions: list[str],
    failures: list[str],
    evidence: list[str],
    reusable_assets: list[str],
    reusable_notes: list[str],
    supersedes: str,
    superseded_by: str,
    created: str,
) -> str:
    template = RECORD_TEMPLATE.read_text(encoding="utf-8")
    values = {
        "{{ID}}": json_value(record_id),
        "{{TITLE_JSON}}": json_value(title),
        "{{STATUS}}": "candidate",
        "{{CREATED}}": json_value(created),
        "{{UPDATED}}": json_value(created),
        "{{TASK_MODE}}": json_value(task_mode),
        "{{PLAN_JSON}}": json_value(str(plan.resolve())),
        "{{TAGS_JSON}}": json_value(tags),
        "{{EVIDENCE_JSON}}": json_value(evidence),
        "{{ASSETS_JSON}}": json_value(reusable_assets),
        "{{SUPERSEDES_JSON}}": json_value(supersedes),
        "{{SUPERSEDED_BY_JSON}}": json_value(superseded_by),
        "{{TITLE}}": title,
        "{{SUMMARY}}": summary,
        "{{DECISIONS}}": markdown_list(decisions, "- 尚未补充；记录后按实际决策更新。"),
        "{{FAILURES}}": markdown_list(failures, "- 无，或尚未补充。"),
        "{{EVIDENCE_MARKDOWN}}": markdown_list(evidence, "- 尚无验证证据。"),
        "{{REUSABLE_NOTES}}": markdown_list(reusable_notes, "- 尚无复用候选。"),
    }
    result = template
    for token, value in values.items():
        result = result.replace(token, value)
    unresolved = sorted(set(re.findall(r"\{\{[A-Z0-9_]+\}\}", result)))
    if unresolved:
        raise ValueError(f"经验模板仍有占位符: {', '.join(unresolved)}")
    return result


def record(args: argparse.Namespace) -> dict[str, str]:
    experience_root = args.experience_root.expanduser().resolve()
    experience_root.mkdir(parents=True, exist_ok=True)
    ensure_repository(experience_root)
    title = args.title.strip()
    if not title:
        raise ValueError("经验主题不能为空")
    summary = args.summary.strip()
    if not summary:
        raise ValueError("摘要不能为空")
    plan = args.plan.expanduser().resolve()
    if not plan.is_file() or not PLAN_NAME.fullmatch(plan.name):
        raise ValueError("必须提供已生成的序号技术方案 HTML")
    tags = sorted(set(item.strip() for item in args.tag if item.strip()))
    evidence = [item.strip() for item in args.evidence if item.strip()]
    decisions = [item.strip() for item in args.decision if item.strip()]
    failures = [item.strip() for item in args.failure if item.strip()]
    reusable_notes = [item.strip() for item in args.reusable_note if item.strip()]
    reusable_assets = [item.strip() for item in args.reusable_asset if item.strip()]
    if args.status == "verified" and not evidence:
        raise ValueError("状态为 verified 时必须提供至少一项证据")

    key = validate_key(args.key) if args.key else derive_key(title)

    with exclusive_lock(experience_root):
        existing = next(
            (item for item in active_records(experience_root) if record_key(item[1]) == key), None
        )
        if existing is not None:
            return update_record(
                experience_root,
                existing,
                key=key,
                title=title,
                status=args.status,
                plan=plan,
                summary=summary,
                tags=tags,
                decisions=decisions,
                failures=failures,
                evidence=evidence,
                reusable_assets=reusable_assets,
                reusable_notes=reusable_notes,
            )
        record_id = next_id(experience_root)
        slug = clean_slug(title)
        destination = experience_root / "records" / f"{record_id}-{slug}.md"
        created = now()
        content = render_record(
            record_id=record_id,
            title=title,
            task_mode=args.task_mode,
            plan=plan,
            summary=summary,
            tags=tags,
            decisions=decisions,
            failures=failures,
            evidence=evidence,
            reusable_assets=reusable_assets,
            reusable_notes=reusable_notes,
            supersedes=args.supersedes,
            superseded_by="",
            created=created,
        )
        fields, _ = read_frontmatter(content)
        initial_status = "verified" if args.status == "verified" else "candidate"
        fields.update({"status": initial_status, "updated": created, "key": key, "uses": 1, "last_confirmed": created})
        content = write_frontmatter(content, fields, ("status", "updated", "key", "uses", "last_confirmed"))
        destination.write_text(content, encoding="utf-8", newline="\n")
        pruned = prune_records(experience_root)
        index = rebuild_index(experience_root)
    return {
        "id": record_id,
        "action": "created",
        "key": key,
        "status": initial_status,
        "path": str(destination),
        "index": str(index),
        "pruned": pruned,
    }


def update_record(
    experience_root: Path,
    existing: tuple[Path, dict[str, Any], str],
    *,
    key: str,
    title: str,
    status: str,
    plan: Path,
    summary: str,
    tags: list[str],
    decisions: list[str],
    failures: list[str],
    evidence: list[str],
    reusable_assets: list[str],
    reusable_notes: list[str],
) -> dict[str, Any]:
    """Replace the statement of an existing record on the same topic (caller holds the lock)."""
    path, fields, _ = existing
    text = path.read_text(encoding="utf-8")
    stamp = now()
    old_summary = read_section(text, SECTION_SUMMARY)
    changed = normalize_text(old_summary) != normalize_text(summary) or normalize_text(
        str(fields.get("title", ""))
    ) != normalize_text(title)
    merged_evidence = list(dict.fromkeys([str(item) for item in fields.get("evidence", [])] + evidence))
    merged_assets = list(dict.fromkeys([str(item) for item in fields.get("reusable_assets", [])] + reusable_assets))
    merged_tags = sorted(set(str(item) for item in fields.get("tags", [])) | set(tags))
    if status == "verified":
        new_status = "verified"
    elif changed:
        # A new statement has not been verified yet, even if the old one was.
        new_status = "candidate"
    else:
        new_status = str(fields.get("status", "candidate"))
    fields.update(
        {
            "title": title,
            "status": new_status,
            "updated": stamp,
            "last_confirmed": stamp,
            "plan": str(plan),
            "tags": merged_tags,
            "evidence": merged_evidence,
            "reusable_assets": merged_assets,
            "key": key,
            "uses": int(fields.get("uses", 1) or 1) + 1,
        }
    )
    keys = ("title", "status", "updated", "last_confirmed", "plan", "tags", "evidence", "reusable_assets", "key", "uses")
    if changed:
        fields["previous"] = old_summary
        keys += ("previous",)
    text = write_frontmatter(text, fields, keys)
    text = re.sub(r"^# .*$", lambda _: f"# {title}", text, count=1, flags=re.MULTILINE)
    text = replace_section(text, SECTION_SUMMARY, summary)
    if decisions:
        text = replace_section(text, SECTION_DECISIONS, markdown_list(decisions, ""))
    if failures:
        text = replace_section(text, SECTION_FAILURES, markdown_list(failures, ""))
    if reusable_notes:
        text = replace_section(text, SECTION_REUSABLE, markdown_list(reusable_notes, ""))
    text = replace_marker(
        text, "<!-- EVIDENCE_START -->", "<!-- EVIDENCE_END -->", markdown_list(merged_evidence, "- 尚无验证证据。")
    )
    path.write_text(text, encoding="utf-8", newline="\n")
    index = rebuild_index(experience_root)
    return {
        "id": str(fields.get("id")),
        "action": "replaced" if changed else "confirmed",
        "key": key,
        "status": new_status,
        "path": str(path),
        "index": str(index),
        "pruned": [],
    }


def deprecate_record(experience_root: Path, record_id: str, reason: str, superseded_by: str = "") -> Path:
    """Mark one record deprecated (caller holds the lock)."""
    record = find_record(experience_root, record_id)
    text = record.read_text(encoding="utf-8")
    fields, _ = read_frontmatter(text)
    fields["status"] = "deprecated"
    fields["updated"] = now()
    fields["superseded_by"] = superseded_by
    text = write_frontmatter(text, fields, ("status", "updated", "superseded_by"))
    replacement = f"- 原因：{reason}\n- 替代经验：{superseded_by or '无'}"
    text = replace_marker(text, "<!-- DEPRECATION_START -->", "<!-- DEPRECATION_END -->", replacement)
    record.write_text(text, encoding="utf-8", newline="\n")
    return record


def prune_records(experience_root: Path) -> list[str]:
    """Deprecate the least valuable active records above RECORD_CAP (candidates before verified)."""
    active = active_records(experience_root)
    overflow = len(active) - RECORD_CAP
    if overflow <= 0:
        return []
    ranked = sorted(
        active,
        key=lambda item: (
            item[1].get("status") == "verified",
            value_order(item[1].get("uses", 1), item[1].get("last_confirmed") or item[1].get("updated")),
        ),
    )
    pruned = []
    for _, fields, _ in ranked[:overflow]:
        record_id = str(fields.get("id"))
        deprecate_record(experience_root, record_id, f"有效经验超过上限 {RECORD_CAP}，按价值自动裁剪")
        pruned.append(record_id)
    return pruned


def promote(args: argparse.Namespace) -> dict[str, str]:
    experience_root = args.experience_root.expanduser().resolve()
    ensure_repository(experience_root)
    evidence = [item.strip() for item in args.evidence if item.strip()]
    if not evidence:
        raise ValueError("晋升需要至少一项可复核证据")
    assets = [item.strip() for item in args.reusable_asset if item.strip()]
    with exclusive_lock(experience_root):
        record = find_record(experience_root, args.record_id)
        text = record.read_text(encoding="utf-8")
        fields, _ = read_frontmatter(text)
        if fields.get("status") == "deprecated":
            raise ValueError("已废弃经验不能直接晋升")
        existing_evidence = [str(item) for item in fields.get("evidence", [])]
        merged_evidence = list(dict.fromkeys(existing_evidence + evidence))
        existing_assets = [str(item) for item in fields.get("reusable_assets", [])]
        merged_assets = list(dict.fromkeys(existing_assets + assets))
        fields["status"] = "verified"
        fields["updated"] = now()
        fields["evidence"] = merged_evidence
        fields["reusable_assets"] = merged_assets
        text = write_frontmatter(
            text, fields, ("status", "updated", "evidence", "reusable_assets")
        )
        text = replace_marker(
            text,
            "<!-- EVIDENCE_START -->",
            "<!-- EVIDENCE_END -->",
            markdown_list(merged_evidence, "- 尚无验证证据。"),
        )
        record.write_text(text, encoding="utf-8", newline="\n")
        index = rebuild_index(experience_root)
    return {"id": args.record_id, "status": "verified", "path": str(record), "index": str(index)}


def deprecate(args: argparse.Namespace) -> dict[str, str]:
    experience_root = args.experience_root.expanduser().resolve()
    ensure_repository(experience_root)
    reason = args.reason.strip()
    if not reason:
        raise ValueError("废弃必须填写原因")
    if args.superseded_by and not re.fullmatch(r"EXP-\d{4,}", args.superseded_by):
        raise ValueError("替代经验必须使用 EXP-NNNN 格式")
    with exclusive_lock(experience_root):
        record = deprecate_record(experience_root, args.record_id, reason, args.superseded_by)
        index = rebuild_index(experience_root)
    return {"id": args.record_id, "status": "deprecated", "path": str(record), "index": str(index)}


def note(args: argparse.Namespace) -> dict[str, Any]:
    experience_root = args.experience_root.expanduser().resolve()
    ensure_repository(experience_root)
    with exclusive_lock(experience_root):
        entries = load_notes(experience_root)
        action, entry = upsert_note(entries, kind=args.kind, key=args.key, text=args.text, why=args.why)
        removed = prune_notes(entries)
        save_notes(experience_root, entries)
        rebuild_index(experience_root)
    return {
        "action": action,
        "kind": entry["kind"],
        "key": entry["key"],
        "uses": entry["uses"],
        "previous": (entry.get("previous") or {}).get("text") if action == "replaced" else None,
        "pruned": [item["key"] for item in removed],
    }


def prune(args: argparse.Namespace) -> dict[str, Any]:
    experience_root = args.experience_root.expanduser().resolve()
    ensure_repository(experience_root)
    with exclusive_lock(experience_root):
        entries = load_notes(experience_root)
        removed = prune_notes(entries)
        if removed or notes_path(experience_root).is_file():
            save_notes(experience_root, entries)
        records = prune_records(experience_root)
        rebuild_index(experience_root)
    return {"pruned_notes": [item["key"] for item in removed], "deprecated_records": records}


def check(args: argparse.Namespace) -> dict[str, Any]:
    experience_root = args.experience_root.expanduser().resolve()
    errors, warnings = check_store(experience_root)
    counts = {kind: sum(1 for item in load_notes(experience_root) if item.get("kind") == kind) for kind in NOTE_KINDS}
    counts["records_active"] = len(active_records(experience_root)) if (experience_root / "records").is_dir() else 0
    return {"ok": not errors, "errors": errors, "warnings": warnings, "counts": counts}


def search(args: argparse.Namespace) -> dict[str, Any]:
    experience_root = args.experience_root.expanduser().resolve()
    terms = [normalize_text(term) for term in (args.query or []) if normalize_text(term)]

    def matches(*values: Any) -> bool:
        haystack = normalize_text(" ".join(str(value) for value in values))
        return all(term in haystack for term in terms)

    notes = [
        {key: entry.get(key) for key in ("kind", "key", "text", "why", "uses", "last_confirmed")}
        for entry in load_notes(experience_root)
        if (not args.kind or entry.get("kind") == args.kind) and matches(entry.get("key"), entry.get("text"), entry.get("why"))
    ]
    records = []
    if args.kind in (None, "record") and (experience_root / "records").is_dir():
        for path, fields, body in active_records(experience_root):
            if matches(fields.get("title"), record_key(fields), " ".join(map(str, fields.get("tags", []))), body):
                records.append(
                    {
                        "id": fields.get("id"),
                        "key": record_key(fields),
                        "title": fields.get("title"),
                        "status": fields.get("status"),
                        "path": path.relative_to(experience_root).as_posix(),
                    }
                )
    if args.kind == "record":
        notes = []
    return {"notes": notes, "records": records}


def add_common(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument(
        "--experience-root", type=Path, default=DEFAULT_EXPERIENCE_ROOT, help=argparse.SUPPRESS
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="记录、验证和废弃 Luz Dev 开发经验。")
    commands = parser.add_subparsers(dest="command", required=True)

    record_parser = commands.add_parser("record", help="创建经验记录")
    record_parser.add_argument("--title", required=True)
    record_parser.add_argument("--task-mode", required=True, choices=TASK_MODES)
    record_parser.add_argument("--plan", required=True, type=Path)
    record_parser.add_argument("--summary", required=True)
    record_parser.add_argument("--status", choices=STATUSES, default="candidate")
    record_parser.add_argument("--tag", action="append", default=[])
    record_parser.add_argument("--decision", action="append", default=[])
    record_parser.add_argument("--failure", action="append", default=[])
    record_parser.add_argument("--evidence", action="append", default=[])
    record_parser.add_argument("--reusable-note", action="append", default=[])
    record_parser.add_argument("--reusable-asset", action="append", default=[])
    record_parser.add_argument("--supersedes", default="")
    record_parser.add_argument(
        "--key", default="", help="主题 key；已存在的有效经验用同一 key 时原地更新，不新建 EXP 编号"
    )
    add_common(record_parser)

    note_parser = commands.add_parser("note", help="按 key 写入偏好 / 教训 / 有效信息（同 key 替换）")
    note_parser.add_argument("--kind", required=True, choices=NOTE_KINDS)
    note_parser.add_argument("--key", required=True)
    note_parser.add_argument("--text", required=True)
    note_parser.add_argument("--why", default="")
    add_common(note_parser)

    prune_parser = commands.add_parser("prune", help="按上限裁剪价值最低的条目")
    add_common(prune_parser)

    check_parser = commands.add_parser("check", help="检查重复 key、重复内容、隐私信息与上限")
    add_common(check_parser)

    search_parser = commands.add_parser("search", help="检索偏好、教训、有效信息与经验记录")
    search_parser.add_argument("query", nargs="*")
    search_parser.add_argument("--kind", choices=NOTE_KINDS + ("record",))
    add_common(search_parser)

    promote_parser = commands.add_parser("promote", help="将经验晋升为 verified")
    promote_parser.add_argument("record_id")
    promote_parser.add_argument("--evidence", action="append", default=[])
    promote_parser.add_argument("--reusable-asset", action="append", default=[])
    add_common(promote_parser)

    deprecate_parser = commands.add_parser("deprecate", help="废弃经验")
    deprecate_parser.add_argument("record_id")
    deprecate_parser.add_argument("--reason", default="")
    deprecate_parser.add_argument("--superseded-by", default="")
    add_common(deprecate_parser)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "record":
            payload = record(args)
        elif args.command == "promote":
            payload = promote(args)
        elif args.command == "deprecate":
            payload = deprecate(args)
        elif args.command == "note":
            payload = note(args)
        elif args.command == "prune":
            payload = prune(args)
        elif args.command == "check":
            payload = check(args)
        else:
            payload = search(args)
    except (ValueError, OSError, TimeoutError) as exc:
        parser.error(str(exc))
    print(json.dumps(payload, ensure_ascii=False, indent=2 if args.command in {"check", "search"} else None))
    return 0 if payload.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
