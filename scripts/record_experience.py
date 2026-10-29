#!/usr/bin/env python3
"""Record, promote, and deprecate Luz Dev experience without third-party packages.

Two stores share one lock and one index:
- experience/records/EXP-*.md  long task records (candidate → verified → deprecated),
  de-duplicated by `key`: recording an existing key updates that record in place.
- experience/notes.json         short preference / lesson / fact notes, upserted by key, capped per kind.

Notes (same file, fields and commands in Luz-System, Luz-Design, Luz-Dev and Luz-Writing):
  record_experience.py note --kind preference|lesson|fact --key K --text "…" [--why "…"]
  record_experience.py list [--kind K]      record_experience.py search <words> [--kind K]
  record_experience.py prune                record_experience.py check
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import time
import unicodedata
from contextlib import contextmanager
from datetime import date, datetime
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
NOTES_FILE = "notes.json"
RECORD_CAP = 120  # active (candidate + verified) EXP records
STALE_DAYS = 180
KEY_PATTERN = re.compile(r"^[0-9a-z一-鿿][0-9a-z一-鿿-]{0,79}$")  # EXP record keys
NOTES_START = "<!-- NOTES_START -->"
NOTES_END = "<!-- NOTES_END -->"
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
    notes_block = f"{NOTES_START}\n{render_notes_table(load_notes(experience_root / NOTES_FILE)['notes'])}{NOTES_END}"
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


# ---------------------------------------------------------------- notes store (identical in Luz-System, Luz-Design, Luz-Dev, Luz-Writing)
# experience/notes.json = {"schema_version": "1.0", "caps": {...}, "notes": [{key, kind, text, why?, uses, first_seen,
# last_confirmed, previous?}]}. A note on an existing key replaces its text (one `previous` kept); the same statement
# under another key confirms that note; over a cap the fewest uses, then the oldest confirmation, are dropped.
NOTES_SCHEMA_VERSION = "1.0"
NOTE_KINDS = {"preference": "偏好", "lesson": "教训", "fact": "有效信息"}
NOTE_CAPS = {"preference": 60, "lesson": 120, "fact": 120}
NOTE_FIELDS = ("key", "kind", "text", "why", "uses", "first_seen", "last_confirmed", "previous")
NOTE_REQUIRED = ("key", "kind", "text", "uses", "first_seen", "last_confirmed")
NOTE_STALE_DAYS = 180
NOTE_TEXT_MAX = 400
NOTE_KEY_RE = re.compile(r"[\w一-鿿][\w一-鿿-]{0,47}")
NOTE_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
NOTE_PRIVATE_RES = (
    (re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)"), "phone number"),
    (re.compile(r"[\w.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.(?!(?:png|jpe?g|gif|svg|webp)\b)[A-Za-z]{2,}\b"), "email address"),
    (re.compile(r"(?i)\bwxid_\w+|(?:微信号?|wechat(?:\s*id)?|weixin|vx)\s*[:：]\s*[A-Za-z][\w-]{5,}"), "WeChat ID"),
    (re.compile(r"(?i)\b[A-Z]:[\\/]+(?:Users|Documents and Settings)[\\/]|(?<![\w.])/(?:Users|home)/\w"), "user-home path"),
    (re.compile(r"(?i)\b(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)\s*[:=：]\s*\S+"
                r"|(?:密码|口令|密钥)\s*[:=：]\s*\S+|\b(?:sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{30,})"), "credential"),
)


def note_today() -> str:
    return date.today().isoformat()


def note_normalize(text: str) -> str:
    """Comparison form of a statement: NFKC, lower case, without whitespace, punctuation or symbols."""
    return "".join(c for c in unicodedata.normalize("NFKC", str(text or "")).lower()
                   if not c.isspace() and unicodedata.category(c)[0] not in "PS")


def note_private_hits(text: str) -> list[str]:
    return [label for pattern, label in NOTE_PRIVATE_RES if pattern.search(text or "")]


def new_notes_doc() -> dict:
    return {"schema_version": NOTES_SCHEMA_VERSION, "caps": dict(NOTE_CAPS), "notes": []}


def load_notes(path: Path) -> dict:
    if not path.is_file():
        return new_notes_doc()
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or not isinstance(doc.get("notes"), list):
        raise ValueError(f"{path.name} must be an object with a notes array")
    return doc


def save_notes(path: Path, doc: dict) -> None:
    """Write atomically, notes ordered by kind then key, fields in the shared order."""
    order = list(NOTE_KINDS)
    notes = sorted(doc["notes"], key=lambda n: (order.index(n["kind"]) if n.get("kind") in order else len(order), str(n.get("key", ""))))
    doc.update(schema_version=NOTES_SCHEMA_VERSION, caps=dict(NOTE_CAPS))
    doc["notes"] = [{**{f: n[f] for f in NOTE_FIELDS if n.get(f) not in (None, "")},
                     **{k: v for k, v in n.items() if k not in NOTE_FIELDS}} for n in notes]
    payload = {"schema_version": doc["schema_version"], "caps": doc["caps"], "notes": doc["notes"]}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def prune_notes(doc: dict, keep: dict | None = None) -> list[dict]:
    """Enforce the caps: drop the fewest uses first, then the oldest confirmation; never the note just written."""
    removed: list[dict] = []
    for kind, cap in NOTE_CAPS.items():
        group = [n for n in doc["notes"] if n.get("kind") == kind]
        overflow = len(group) - cap
        if overflow <= 0:
            continue
        ranked = sorted((n for n in group if n is not keep), key=lambda n: (int(n.get("uses") or 0), str(n.get("last_confirmed", ""))))
        removed += ranked[:overflow]
    gone = {id(n) for n in removed}
    doc["notes"] = [n for n in doc["notes"] if id(n) not in gone]
    return removed


def upsert_note(doc: dict, kind: str, key: str, text: str, why: str = "", on: str | None = None,
                privacy: bool = True) -> tuple[str, dict, list[dict]]:
    """Insert or replace one note by key. Returns (added|replaced|confirmed, note, pruned notes); raises ValueError."""
    if kind not in NOTE_KINDS:
        raise ValueError(f"--kind must be one of {', '.join(NOTE_KINDS)}")
    key = (key or "").strip()
    if not NOTE_KEY_RE.fullmatch(key):
        raise ValueError("--key must be a short slug: letters, digits, Chinese, - or _, at most 48 characters, no spaces")
    text, why = " ".join((text or "").split()), " ".join((why or "").split())
    if not text:
        raise ValueError("--text is empty")
    if len(text) > NOTE_TEXT_MAX:
        raise ValueError(f"--text is longer than {NOTE_TEXT_MAX} characters; record the general principle, not task details")
    hits = note_private_hits(f"{text} {why}") if privacy else []
    if hits:
        raise ValueError(f"notes are public; remove the {', '.join(hits)} and write the general principle")
    on = on or note_today()
    notes = doc["notes"]
    norm = note_normalize(text)
    entry = next((n for n in notes if n.get("key") == key), None)
    twin = next((n for n in notes if n is not entry and note_normalize(n.get("text", "")) == norm), None)
    if entry is None and twin is not None:
        entry, action = twin, "confirmed"
        if why and not entry.get("why"):
            entry["why"] = why
    elif entry is None:
        entry, action = {"key": key, "kind": kind, "text": text, "why": why, "uses": 0, "first_seen": on}, "added"
        notes.append(entry)
    elif twin is not None:
        raise ValueError(f"the same statement is stored under {twin['key']}; update that key instead")
    else:
        action = "confirmed"
        if note_normalize(entry.get("text", "")) != norm:
            entry["previous"] = {"text": entry.get("text", ""), "replaced_at": on}
            action = "replaced"
        entry.update(kind=kind, text=text)
        if why:
            entry["why"] = why
    entry["uses"] = int(entry.get("uses") or 0) + 1
    entry["last_confirmed"] = on
    return action, entry, prune_notes(doc, keep=entry)


def stale_notes(doc: dict, now: date | None = None) -> list[dict]:
    """Notes not confirmed for NOTE_STALE_DAYS days: listed by prune and check, first to go when a cap is hit."""
    now = now or date.today()
    out = []
    for n in doc["notes"]:
        try:
            age = (now - date.fromisoformat(str(n.get("last_confirmed")))).days
        except ValueError:
            age = NOTE_STALE_DAYS + 1
        if age > NOTE_STALE_DAYS:
            out.append(n)
    return out


def check_notes(doc: dict, privacy: bool = True) -> list[str]:
    """Shape, fields, duplicate keys, duplicate statements, caps and private data."""
    errors: list[str] = []
    if not isinstance(doc, dict):
        return ["notes.json must be a JSON object"]
    if sorted(doc) != ["caps", "notes", "schema_version"]:
        errors.append(f"notes.json top-level fields must be schema_version, caps, notes (found {', '.join(sorted(doc))})")
    if doc.get("schema_version") != NOTES_SCHEMA_VERSION:
        errors.append(f"notes.json schema_version must be {NOTES_SCHEMA_VERSION}")
    if doc.get("caps") != NOTE_CAPS:
        errors.append(f"notes.json caps must be {json.dumps(NOTE_CAPS)}")
    notes = doc.get("notes")
    if not isinstance(notes, list):
        return errors + ["notes.json notes must be an array"]
    keys: dict[str, int] = {}
    texts: dict[str, str] = {}
    for n in notes:
        key = str(n.get("key", "")) if isinstance(n, dict) else ""
        if not isinstance(n, dict):
            errors.append("note is not an object")
            continue
        for field in NOTE_REQUIRED:
            if n.get(field) in (None, ""):
                errors.append(f"note {key or '?'} missing {field}")
        extra = sorted(set(n) - set(NOTE_FIELDS))
        if extra:
            errors.append(f"note {key} has unknown fields {', '.join(extra)}")
        if n.get("kind") not in NOTE_KINDS:
            errors.append(f"note {key} kind must be one of {', '.join(NOTE_KINDS)}")
        if key and not NOTE_KEY_RE.fullmatch(key):
            errors.append(f"note key is not a short slug: {key}")
        if not isinstance(n.get("uses"), int) or isinstance(n.get("uses"), bool) or n.get("uses", 0) < 1:
            errors.append(f"note {key} uses must be a positive integer")
        for field in ("first_seen", "last_confirmed"):
            if n.get(field) and not NOTE_DATE_RE.fullmatch(str(n[field])):
                errors.append(f"note {key} {field} must be YYYY-MM-DD")
        previous = n.get("previous")
        if previous is not None and (not isinstance(previous, dict) or sorted(previous) != ["replaced_at", "text"]
                                     or not NOTE_DATE_RE.fullmatch(str(previous.get("replaced_at")))):
            errors.append(f"note {key} previous must be {{text, replaced_at: YYYY-MM-DD}}")
        if len(str(n.get("text", ""))) > NOTE_TEXT_MAX:
            errors.append(f"note {key} text is longer than {NOTE_TEXT_MAX} characters")
        keys[key] = keys.get(key, 0) + 1
        norm = note_normalize(n.get("text", ""))
        if norm and norm in texts and texts[norm] != key:
            errors.append(f"note {key} repeats the statement of {texts[norm]}; keep one key")
        texts.setdefault(norm, key)
        if privacy:
            hits = note_private_hits(f"{n.get('text', '')} {n.get('why', '')} {(previous or {}).get('text', '') if isinstance(previous, dict) else ''}")
            if hits:
                errors.append(f"note {key} contains a {', '.join(hits)}; notes are public")
    errors += [f"duplicate note key {k} ({c} entries); a key is replaced, never repeated" for k, c in keys.items() if c > 1]
    for kind, cap in NOTE_CAPS.items():
        count = sum(1 for n in notes if isinstance(n, dict) and n.get("kind") == kind)
        if count > cap:
            errors.append(f"{count} {kind} notes exceed the cap {cap}; run prune")
    return errors


def search_notes(doc: dict, words: list[str], kind: str | None = None) -> list[dict]:
    terms = [t for t in (note_normalize(w) for w in words or []) if t]
    return [n for n in doc["notes"] if (not kind or n.get("kind") == kind)
            and all(t in note_normalize(f"{n.get('key', '')} {n.get('text', '')} {n.get('why', '')}") for t in terms)]


def format_note(n: dict) -> str:
    why = f"（{n['why']}）" if n.get("why") else ""
    return f"[{NOTE_KINDS.get(n.get('kind'), n.get('kind'))}] {n.get('key')}：{n.get('text')}{why}  （{n.get('uses', 1)} 次，最近 {n.get('last_confirmed')}）"


def note_counts(doc: dict) -> str:
    return "，".join(f"{NOTE_KINDS[k]} {sum(1 for n in doc['notes'] if n.get('kind') == k)}/{cap}" for k, cap in NOTE_CAPS.items())
# ---------------------------------------------------------------- end of notes store


def notes_path(experience_root: Path) -> Path:
    """The notes store; LUZ_EXPERIENCE_STORE names another file for tests and rehearsals (INDEX.md keeps the skill's own notes)."""
    override = os.environ.get("LUZ_EXPERIENCE_STORE")
    return Path(override) if override else experience_root / NOTES_FILE


def check_notes_file(experience_root: Path) -> tuple[list[str], list[str]]:
    """Shared notes check on experience/notes.json (absent file = empty store); stale notes are warnings."""
    path = notes_path(experience_root)
    if not path.is_file():
        return [], []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return [f"{NOTES_FILE} is not valid JSON: {exc}"], []
    errors = check_notes(raw)
    warnings = [f"note {n.get('key')}: 超过 {NOTE_STALE_DAYS} 天未确认" for n in stale_notes(raw)] if not errors else []
    return errors, warnings


def render_notes_table(notes: list[dict[str, Any]]) -> str:
    header = "| 类型 | key | 内容 | 使用次数 | 最近确认 |\n|---|---|---|---|---|\n"
    order = list(NOTE_KINDS)
    rows = []
    for entry in sorted(notes, key=lambda item: (order.index(item["kind"]) if item.get("kind") in order else 99, item.get("key", ""))):
        text = str(entry.get("text", "")).replace("|", "\\|")
        rows.append(
            f"| {NOTE_KINDS.get(entry.get('kind'), entry.get('kind'))} | {entry.get('key')} | {text} | "
            f"{entry.get('uses', 0)} | {entry.get('last_confirmed', '')} |"
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
    note_errors, note_warnings = check_notes_file(experience_root)
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
        doc = load_notes(notes_path(experience_root))
        action, entry, removed = upsert_note(doc, args.kind, args.key, args.text, args.why)
        save_notes(notes_path(experience_root), doc)
        rebuild_index(experience_root)
    return {
        "action": action,
        "kind": entry["kind"],
        "key": entry["key"],
        "uses": entry["uses"],
        "previous": (entry.get("previous") or {}).get("text") if action == "replaced" else None,
        "pruned": [item["key"] for item in removed],
    }


def list_notes(args: argparse.Namespace) -> dict[str, Any]:
    experience_root = args.experience_root.expanduser().resolve()
    notes = [n for n in load_notes(notes_path(experience_root))["notes"] if not args.kind or n.get("kind") == args.kind]
    return {"notes": notes, "count": len(notes)}


def prune(args: argparse.Namespace) -> dict[str, Any]:
    experience_root = args.experience_root.expanduser().resolve()
    ensure_repository(experience_root)
    with exclusive_lock(experience_root):
        doc = load_notes(notes_path(experience_root))
        removed = prune_notes(doc)
        save_notes(notes_path(experience_root), doc)
        records = prune_records(experience_root)
        rebuild_index(experience_root)
    return {
        "pruned_notes": [item["key"] for item in removed],
        "stale_notes": [item["key"] for item in stale_notes(doc)],
        "deprecated_records": records,
    }


def check(args: argparse.Namespace) -> dict[str, Any]:
    experience_root = args.experience_root.expanduser().resolve()
    errors, warnings = check_store(experience_root)
    notes = load_notes(notes_path(experience_root))["notes"] if not errors else []
    counts: dict[str, Any] = {kind: sum(1 for item in notes if item.get("kind") == kind) for kind in NOTE_KINDS}
    counts["records_active"] = len(active_records(experience_root)) if (experience_root / "records").is_dir() else 0
    return {"ok": not errors, "errors": errors, "warnings": warnings, "counts": counts}


def search(args: argparse.Namespace) -> dict[str, Any]:
    experience_root = args.experience_root.expanduser().resolve()
    terms = [normalize_text(term) for term in (args.words or []) if normalize_text(term)]

    def matches(*values: Any) -> bool:
        haystack = normalize_text(" ".join(str(value) for value in values))
        return all(term in haystack for term in terms)

    note_kind = args.kind if args.kind in NOTE_KINDS else None
    notes = [] if args.kind == "record" else search_notes(load_notes(notes_path(experience_root)), args.words, note_kind)
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
    note_parser.add_argument("--kind", required=True, choices=list(NOTE_KINDS))
    note_parser.add_argument("--key", required=True, help="稳定的主题键，例如 plan-first 或 方案先批准")
    note_parser.add_argument("--text", required=True, help="一句话通用原则")
    note_parser.add_argument("--why", default="", help="依据或纠正来源")
    add_common(note_parser)

    list_parser = commands.add_parser("list", help="列出偏好、教训与有效信息")
    list_parser.add_argument("--kind", choices=list(NOTE_KINDS))
    add_common(list_parser)

    prune_parser = commands.add_parser("prune", help="按上限裁剪，列出 180 天未确认的条目，并废弃超限的经验记录")
    add_common(prune_parser)

    check_parser = commands.add_parser("check", help="检查结构、重复 key、重复内容、隐私信息与上限")
    add_common(check_parser)

    search_parser = commands.add_parser("search", help="检索偏好、教训、有效信息与经验记录，所有词都要命中")
    search_parser.add_argument("words", nargs="*")
    search_parser.add_argument("--kind", choices=list(NOTE_KINDS) + ["record"])
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
        elif args.command == "list":
            payload = list_notes(args)
        elif args.command == "prune":
            payload = prune(args)
        elif args.command == "check":
            payload = check(args)
        else:
            payload = search(args)
    except (ValueError, OSError, TimeoutError) as exc:
        parser.error(str(exc))
    print(json.dumps(payload, ensure_ascii=False, indent=2 if args.command in {"check", "search", "list"} else None))
    return 0 if payload.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
