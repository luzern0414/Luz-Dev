#!/usr/bin/env python3
"""Consume a Luz System session and keep the shipped frontend bound to it.

A Luz System session folder (``D:\\Codes\\skill输出\\Luz-System\\NNN-项目``) holds hash-approved artifacts:
``99-delivery/MANIFEST.json``, ``99-delivery/APPROVALS.json``, the system architecture,
system design and an interactive prototype whose elements carry stable IDs
(P- pages, A- actions, D- entities, I- integrations, FL- flows, S- states …).

Commands
  inspect  verify approvals (status + SHA-256 of each approved artifact) and extract every stable ID
  bind     create or extend design-bindings.json: one row per action / entity / integration to fill in
  check    verify bindings are complete and the frontend source implements every approved action
           without prototype fixtures (references/design-integration.md)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

ID_ATTRIBUTES = {
    "data-function-id": ("functions", re.compile(r"^F-\d{3,}$")),
    "data-page-id": ("pages", re.compile(r"^P-\d{3,}$")),
    "data-navigation-id": ("navigation", re.compile(r"^N-\d{3,}$")),
    "data-component-id": ("components", re.compile(r"^C-\d{3,}$")),
    "data-action-id": ("actions", re.compile(r"^A-\d{3,}$")),
    "data-entity-id": ("entities", re.compile(r"^D-\d{3,}$")),
    "data-flow-id": ("flows", re.compile(r"^FL-\d{3,}$")),
    "data-state-id": ("states", re.compile(r"^S-\d{3,}$")),
    "data-integration-id": ("integrations", re.compile(r"^I-\d{3,}$")),
    "data-role-id": ("roles", re.compile(r"^R-\d{3,}$")),
}
DEV_STAGES = ("module_scope", "architecture", "system_design", "prototype")
SOURCE_SUFFIXES = {".vue", ".ts", ".tsx", ".js", ".jsx", ".wxml", ".wxs", ".html", ".dart", ".kt", ".swift"}
SKIP_DIRS = {"node_modules", "dist", "build", ".git", "miniprogram_npm", "unpackage", "coverage", "__tests__", "tests", "test", "mock", "mocks", "fixtures"}
FIXTURE_PATTERNS = [re.compile(p) for p in (r"data-fixture-id", r"\bDEMO-\d{3,}\b", r"from ['\"][^'\"]*mock[^'\"]*['\"]", r"require\(['\"][^'\"]*mock")]


class DesignError(Exception):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


class _IdCollector(HTMLParser):
    """Collect elements carrying stable IDs with their visible text and data-* context."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[dict[str, Any]] = []
        self.found: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = {key: (value or "").strip() for key, value in attrs if key.startswith("data-")}
        entry = {"tag": tag, "data": data, "text": []}
        if tag not in {"br", "img", "input", "meta", "link", "hr", "path"}:
            self.stack.append(entry)
        for attribute in data:
            if attribute in ID_ATTRIBUTES:
                self.found.append(entry)
                break

    def handle_endtag(self, tag: str) -> None:
        while self.stack:
            entry = self.stack.pop()
            if entry["tag"] == tag:
                break

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if text:
            for entry in self.stack:
                entry["text"].append(text)


def extract_ids(html_text: str, source: str) -> dict[str, dict[str, dict[str, Any]]]:
    parser = _IdCollector()
    parser.feed(html_text)
    result: dict[str, dict[str, dict[str, Any]]] = {kind: {} for kind, _ in ID_ATTRIBUTES.values()}
    for entry in parser.found:
        for attribute, value in entry["data"].items():
            if attribute not in ID_ATTRIBUTES or not value:
                continue
            kind, pattern = ID_ATTRIBUTES[attribute]
            if not pattern.fullmatch(value):
                continue
            item = result[kind].setdefault(value, {"label": "", "context": {}, "sources": []})
            label = " ".join(entry["text"])[:80]
            if label and not item["label"]:
                item["label"] = label
            for key, other in entry["data"].items():
                if key != attribute and other:
                    item["context"].setdefault(key, [])
                    if other not in item["context"][key]:
                        item["context"][key].append(other)
            if source not in item["sources"]:
                item["sources"].append(source)
    return result


def merge_ids(target: dict, extra: dict) -> None:
    for kind, items in extra.items():
        bucket = target.setdefault(kind, {})
        for key, item in items.items():
            current = bucket.setdefault(key, {"label": "", "context": {}, "sources": []})
            current["label"] = current["label"] or item["label"]
            for context_key, values in item["context"].items():
                for value in values:
                    if value not in current["context"].setdefault(context_key, []):
                        current["context"][context_key].append(value)
            for source in item["sources"]:
                if source not in current["sources"]:
                    current["sources"].append(source)


def inspect_session(session: Path) -> dict[str, Any]:
    session = session.resolve()
    # Luz System keeps every working file under raw/ and publishes one HTML at the session root;
    # earlier sessions kept 99-delivery/ at the root. Paths in the manifest are relative to the working folder.
    work = session / "raw" if (session / "raw" / "99-delivery" / "MANIFEST.json").is_file() else session
    manifest_path = work / "99-delivery" / "MANIFEST.json"
    if not manifest_path.is_file():
        raise DesignError(f"不是 Luz System 会话目录（缺少 raw/99-delivery/MANIFEST.json）: {session}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("approval_mode") == "authorization":
        # A platform demo signed once before generation (Luz System SKILL.md 调用协议 6) is for sales only.
        raise DesignError("这是平台按一次授权生成的演示会话（approval_mode authorization），只用于销售演示，不转入开发；"
                          f"正式开发请新建逐阶段人工批准的 Luz System 会话: {session}")
    approvals_path = work / manifest.get("approvals_file", "99-delivery/APPROVALS.json")
    approvals = json.loads(approvals_path.read_text(encoding="utf-8")) if approvals_path.is_file() else {}
    stages: dict[str, dict[str, Any]] = {}
    for name, item in approvals.items():
        if not isinstance(item, dict) or "status" not in item:
            continue
        artifact = work / item["artifact_path"] if item.get("artifact_path") else None
        state = {"status": item.get("status"), "artifact": item.get("artifact_path"), "approved_by": item.get("approved_by"),
                 "approved_at": item.get("approved_at"), "hash_ok": None}
        if item.get("status") == "approved":
            if artifact is None or not artifact.is_file():
                state["hash_ok"] = False
                state["problem"] = "已批准的文件不存在"
            else:
                state["hash_ok"] = sha256_file(artifact) == item.get("artifact_sha256")
                if not state["hash_ok"]:
                    state["problem"] = "文件在批准后被修改，批准失效"
            for key in ("browser_acceptance_path", "browser_acceptance_sha256"):
                if key in item:
                    state[key] = item[key]
            acceptance = item.get("browser_acceptance_path")
            if acceptance:
                accepted = work / acceptance
                if not accepted.is_file() or sha256_file(accepted) != item.get("browser_acceptance_sha256"):
                    state["hash_ok"] = False
                    state["problem"] = "浏览器验收截图缺失或已变化"
        stages[name] = state
    ids: dict[str, dict] = {kind: {} for kind, _ in ID_ATTRIBUTES.values()}
    artifacts = []
    for stage in DEV_STAGES:
        path = manifest.get("stages", {}).get(stage, {}).get("path")
        if path and (work / path).is_file() and path.endswith(".html"):
            merge_ids(ids, extract_ids((work / path).read_text(encoding="utf-8"), path))
            artifacts.append(path)
    handoff = work / "HANDOFF.json"
    published = session / f"{session.name}.html"
    return {
        "session": str(session),
        "sessionId": manifest.get("session_id"),
        "title": manifest.get("title") or manifest.get("system_name"),
        "manifestStatus": manifest.get("status"),
        "selectedModules": approvals.get("module_scope", {}).get("selected_modules", []),
        "stages": stages,
        "artifacts": artifacts,
        "handoff": str(handoff) if handoff.is_file() else None,
        "published": str(published) if published.is_file() else None,
        "counts": {kind: len(items) for kind, items in ids.items()},
        "ids": ids,
    }


def readiness_errors(report: dict[str, Any], required: list[str]) -> list[str]:
    errors = []
    for stage in required:
        state = report["stages"].get(stage)
        if state is None:
            errors.append(f"设计阶段 {stage} 不存在")
        elif state["status"] != "approved":
            errors.append(f"设计阶段 {stage} 尚未批准（{state['status']}）")
        elif not state["hash_ok"]:
            errors.append(f"设计阶段 {stage}：{state.get('problem', '哈希校验失败')}")
    if "prototype" in required and not report["counts"].get("actions"):
        errors.append("原型中没有可绑定的操作（data-action-id）")
    return errors


def build_bindings(report: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    """Create or extend the binding table; rows already filled in are kept."""
    bindings = existing or {"schema": "luz-dev-design-bindings/1", "session": report["session"], "sessionId": report["sessionId"]}
    bindings["designHashes"] = {name: state.get("status") for name, state in report["stages"].items()}

    def merge(kind: str, factory) -> None:
        rows = {row["id"]: row for row in bindings.get(kind, [])}
        for key, item in sorted(report["ids"].get(kind, {}).items()):
            if key not in rows:
                rows[key] = factory(key, item)
            else:
                rows[key]["label"] = item["label"] or rows[key].get("label", "")
        for key, row in rows.items():
            if key not in report["ids"].get(kind, {}):
                row["removedFromDesign"] = True
        bindings[kind] = [rows[key] for key in sorted(rows)]

    merge("actions", lambda key, item: {
        "id": key, "label": item["label"], "pages": item["context"].get("data-page-id", []),
        "states": item["context"].get("data-state-kind", []),
        "kind": "", "api": "", "notes": "",
    })
    merge("entities", lambda key, item: {"id": key, "label": item["label"], "table": "", "notes": ""})
    merge("integrations", lambda key, item: {"id": key, "label": item["label"], "adapter": "", "notes": ""})
    merge("pages", lambda key, item: {"id": key, "label": item["label"], "route": "", "notes": ""})
    return bindings


def scan_frontend(frontend: Path) -> dict[str, Any]:
    ids: dict[str, set[str]] = {}
    fixtures: list[str] = []
    for path in frontend.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in SOURCE_SUFFIXES:
            continue
        if any(part in SKIP_DIRS or part.endswith((".spec", ".test")) for part in path.relative_to(frontend).parts):
            continue
        if re.search(r"\.(spec|test)\.[jt]sx?$", path.name):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        relative = str(path.relative_to(frontend))
        for match in re.finditer(r"\b(A|P|C|N)-\d{3,}\b", text):
            ids.setdefault(match.group(0), set()).add(relative)
        for pattern in FIXTURE_PATTERNS:
            if pattern.search(text):
                fixtures.append(f"{relative}: {pattern.pattern}")
                break
    return {"ids": {key: sorted(value) for key, value in ids.items()}, "fixtures": fixtures}


def check_bindings(bindings: dict[str, Any], frontend: Path | None) -> tuple[list[str], dict[str, Any]]:
    errors: list[str] = []
    for row in bindings.get("actions", []):
        if row.get("removedFromDesign"):
            continue
        kind = row.get("kind", "")
        if kind not in {"api", "navigate", "local", "platform", "n/a"}:
            errors.append(f"{row['id']} {row.get('label', '')}：kind 需为 api / navigate / local / platform / n/a")
        elif kind == "api" and not re.match(r"^(GET|POST|PUT|PATCH|DELETE) /", row.get("api", "")):
            errors.append(f"{row['id']} {row.get('label', '')}：api 需写成“方法 路径”，例如 POST /api/v1/app/orders")
        elif kind in {"platform", "n/a"} and not row.get("notes"):
            errors.append(f"{row['id']}：{kind} 需要在 notes 说明")
    for row in bindings.get("entities", []):
        if not row.get("removedFromDesign") and not (row.get("table") or row.get("notes")):
            errors.append(f"{row['id']} {row.get('label', '')}：未映射到数据表")
    for row in bindings.get("integrations", []):
        if not row.get("removedFromDesign") and not (row.get("adapter") or row.get("notes")):
            errors.append(f"{row['id']} {row.get('label', '')}：未映射到后端适配器")
    coverage: dict[str, Any] = {}
    if frontend is not None:
        scan = scan_frontend(frontend)
        active = [row for row in bindings.get("actions", []) if not row.get("removedFromDesign") and row.get("kind") != "n/a"]
        missing = [row["id"] for row in active if row["id"] not in scan["ids"]]
        pages = [row["id"] for row in bindings.get("pages", []) if not row.get("removedFromDesign")]
        missing_pages = [page for page in pages if page not in scan["ids"]]
        for action in missing:
            errors.append(f"前端未实现操作 {action}（源码中找不到 {action}；用 data-action-id 或常量标注）")
        for page in missing_pages:
            errors.append(f"前端缺少页面 {page}")
        for fixture in scan["fixtures"]:
            errors.append(f"正式代码仍引用原型模拟数据或 mock：{fixture}")
        unknown = sorted(key for key in scan["ids"] if key.startswith("A-") and key not in {row["id"] for row in bindings.get("actions", [])})
        for key in unknown:
            errors.append(f"前端出现设计中不存在的操作 {key}；新增交互需先回到 Luz System")
        coverage = {"actions": f"{len(active) - len(missing)}/{len(active)}", "pages": f"{len(pages) - len(missing_pages)}/{len(pages)}"}
    return errors, coverage


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    inspect = sub.add_parser("inspect")
    inspect.add_argument("session", type=Path)
    inspect.add_argument("--require", default="", help="必须已批准且哈希一致的阶段，逗号分隔，如 architecture,system_design,prototype")
    inspect.add_argument("--out", type=Path)
    bind = sub.add_parser("bind")
    bind.add_argument("session", type=Path)
    bind.add_argument("--out", type=Path, required=True, help="design-bindings.json 路径（通常放在项目 packages/contracts/）")
    check = sub.add_parser("check")
    check.add_argument("bindings", type=Path)
    check.add_argument("--frontend", type=Path, action="append", help="前端源码目录，可多次（小程序、后台）")
    args = parser.parse_args(argv)
    try:
        if args.command == "inspect":
            report = inspect_session(args.session)
            required = [item for item in args.require.split(",") if item]
            errors = readiness_errors(report, required)
            if args.out:
                write_json(args.out, report)
            summary = {key: report[key] for key in ("sessionId", "title", "manifestStatus", "selectedModules", "counts")}
            summary["stages"] = {k: {"status": v["status"], "hash_ok": v["hash_ok"], **({"problem": v["problem"]} if v.get("problem") else {})}
                                 for k, v in report["stages"].items()}
            print(json.dumps({"ok": not errors, "errors": errors, **summary}, ensure_ascii=False, indent=2))
            return 0 if not errors else 1
        if args.command == "bind":
            report = inspect_session(args.session)
            errors = readiness_errors(report, ["architecture", "system_design", "prototype"])
            if errors:
                print(json.dumps({"ok": False, "errors": errors}, ensure_ascii=False, indent=2))
                return 1
            existing = json.loads(args.out.read_text(encoding="utf-8")) if args.out.is_file() else None
            bindings = build_bindings(report, existing)
            write_json(args.out, bindings)
            print(json.dumps({"ok": True, "out": str(args.out), "actions": len(bindings["actions"]),
                              "entities": len(bindings["entities"]), "integrations": len(bindings["integrations"])}, ensure_ascii=False))
            return 0
        bindings = json.loads(args.bindings.read_text(encoding="utf-8"))
        all_errors: list[str] = []
        coverage: dict[str, Any] = {}
        for frontend in args.frontend or [None]:
            errors, cov = check_bindings(bindings, frontend)
            all_errors.extend(e for e in errors if e not in all_errors)
            if frontend is not None:
                coverage[str(frontend)] = cov
        if args.frontend and len(args.frontend) > 1:
            # 一个操作只需在任一前端实现（小程序或后台）
            found = set()
            for frontend in args.frontend:
                found |= set(scan_frontend(frontend)["ids"])
            all_errors = [e for e in all_errors if not any(f"操作 {key}" in e or f"页面 {key}" in e for key in found)]
        print(json.dumps({"ok": not all_errors, "errors": all_errors, "coverage": coverage}, ensure_ascii=False, indent=2))
        return 0 if not all_errors else 1
    except (DesignError, OSError, json.JSONDecodeError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
