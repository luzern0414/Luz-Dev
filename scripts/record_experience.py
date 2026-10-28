#!/usr/bin/env python3
"""Record, promote, and deprecate Luz Dev experience without third-party packages."""

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
    updated_text = marker.sub(table, text, count=1)
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

    with exclusive_lock(experience_root):
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
        if args.status == "verified":
            fields, _ = read_frontmatter(content)
            fields["status"] = "verified"
            fields["updated"] = created
            content = write_frontmatter(content, fields, ("status", "updated"))
        destination.write_text(content, encoding="utf-8", newline="\n")
        index = rebuild_index(experience_root)
    return {"id": record_id, "status": args.status, "path": str(destination), "index": str(index)}


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
        record = find_record(experience_root, args.record_id)
        text = record.read_text(encoding="utf-8")
        fields, _ = read_frontmatter(text)
        fields["status"] = "deprecated"
        fields["updated"] = now()
        fields["superseded_by"] = args.superseded_by
        text = write_frontmatter(text, fields, ("status", "updated", "superseded_by"))
        replacement = f"- 原因：{reason}\n- 替代经验：{args.superseded_by or '无'}"
        text = replace_marker(
            text, "<!-- DEPRECATION_START -->", "<!-- DEPRECATION_END -->", replacement
        )
        record.write_text(text, encoding="utf-8", newline="\n")
        index = rebuild_index(experience_root)
    return {"id": args.record_id, "status": "deprecated", "path": str(record), "index": str(index)}


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
    add_common(record_parser)

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
        else:
            payload = deprecate(args)
    except (ValueError, OSError, TimeoutError) as exc:
        parser.error(str(exc))
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
