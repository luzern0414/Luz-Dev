#!/usr/bin/env python3
"""Luz Dev research knowledge base: keep what Luz-Crawl found so later projects reuse it.

knowledge/
  sources.json   every page read in research (normalized URL, tier, kind, modules, evidence, check date)
  facts.json     reusable statements distilled from sources (law / platform / feature / practice / pitfall)
  queries.json   searches already run (so fresh ones are not repeated)
  index.html     generated human view (Luz Design shell)

Freshness: a source or fact older than its kind's review period is "stale" and is re-read by the next
research run; fresh ones are reused instead of crawling again (references/research.md §5).
Only public information belongs here — never merchant data, credentials or personal information.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from luz_shell import chip, favicon_for_tokens, fill, inline, load_tokens, shell_css, tokens_css  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE = ROOT / "knowledge"
TEMPLATE = ROOT / "assets" / "reference" / "knowledge.template.html"
KINDS = {
    "law": ("法律法规", 365),
    "platform": ("平台文档", 90),
    "opensource": ("开源实现", 180),
    "feature": ("功能做法", 365),
    "practice": ("工程实践", 365),
    "pitfall": ("踩坑记录", 365),
}
TIER_TO_KIND = {"official": "platform", "standard": "law", "opensource": "opensource", "community": "practice"}
TRACKING_PARAMS = re.compile(r"^(utm_|spm|from|ops_request_misc|request_id|biz_id|share|scene)", re.IGNORECASE)
SECRET = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\bsk-[A-Za-z0-9_-]{20,}\b")


class KnowledgeError(Exception):
    pass


def now() -> datetime:
    return datetime.now().astimezone()


def parse_time(value: str) -> datetime:
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def normalize_url(url: str) -> str:
    """Canonical form for de-duplication: lowercase host, no fragment, no tracking parameters, no trailing slash."""
    parsed = urlparse(url.strip())
    query = urlencode([(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if not TRACKING_PARAMS.match(k)])
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", query, ""))


def _load(root: Path, name: str, key: str) -> dict[str, Any]:
    path = root / name
    if not path.is_file():
        return {"schema": f"luz-dev-knowledge-{key}/1", key: []}
    return json.loads(path.read_text(encoding="utf-8"))


def _save(root: Path, name: str, data: dict[str, Any]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    if SECRET.search(text):
        raise KnowledgeError("知识库内容疑似包含密钥，已拒绝写入")
    temporary = root / f".{name}.tmp"
    temporary.write_bytes(text.encode("utf-8"))
    temporary.replace(root / name)


def default_root() -> Path:
    """knowledge/ in the skill; LUZ_DEV_KNOWLEDGE overrides it (tests, isolated runs)."""
    return Path(os.environ["LUZ_DEV_KNOWLEDGE"]) if os.environ.get("LUZ_DEV_KNOWLEDGE") else KNOWLEDGE


class Knowledge:
    def __init__(self, root: Path | None = None):
        root = root or default_root()
        self.root = root
        self.sources = _load(root, "sources.json", "sources")
        self.facts = _load(root, "facts.json", "facts")
        self.queries = _load(root, "queries.json", "queries")

    def save(self) -> None:
        _save(self.root, "sources.json", self.sources)
        _save(self.root, "facts.json", self.facts)
        _save(self.root, "queries.json", self.queries)

    # ------------------------------------------------------------ sources
    def source_by_url(self, url: str) -> dict[str, Any] | None:
        key = normalize_url(url)
        return next((s for s in self.sources["sources"] if s["url"] == key), None)

    def add_source(self, *, url: str, title: str, tier: str, modules: Iterable[str], checked_at: str,
                   evidence: str = "", excerpt: str = "", query: str = "", kind: str | None = None) -> tuple[dict[str, Any], bool]:
        key = normalize_url(url)
        found = self.source_by_url(key)
        if found is None:
            numbers = [int(s["id"].split("-")[-1]) for s in self.sources["sources"]]
            found = {"id": f"KS-{max(numbers, default=0) + 1:04d}", "url": key, "title": title or key, "tier": tier,
                     "kind": kind or TIER_TO_KIND.get(tier, "practice"), "modules": [], "firstReadAt": checked_at,
                     "checkedAt": checked_at, "evidence": [], "excerpt": excerpt[:300], "queries": []}
            self.sources["sources"].append(found)
            created = True
        else:
            created = False
            if parse_time(checked_at) > parse_time(found["checkedAt"]):
                found["checkedAt"] = checked_at
            if title and (not found["title"] or found["title"] == found["url"]):
                found["title"] = title
            if excerpt and not found.get("excerpt"):
                found["excerpt"] = excerpt[:300]
        for module in modules:
            if module and module not in found["modules"]:
                found["modules"].append(module)
        if evidence and evidence not in found["evidence"]:
            found["evidence"].append(evidence)
        if query and query not in found["queries"]:
            found["queries"].append(query)
        return found, created

    def is_fresh(self, item: Mapping[str, Any], moment: datetime | None = None) -> bool:
        days = item.get("reviewAfterDays") or KINDS.get(item.get("kind", "practice"), ("", 365))[1]
        return parse_time(item["checkedAt"]) + timedelta(days=days) > (moment or now())

    def sources_for(self, modules: Iterable[str], moment: datetime | None = None, fresh_only: bool = True) -> list[dict[str, Any]]:
        wanted = set(modules)
        return [s for s in self.sources["sources"]
                if wanted.intersection(s.get("modules", [])) and (not fresh_only or self.is_fresh(s, moment))]

    # ------------------------------------------------------------ queries
    def record_query(self, platform: str, query: str, module: str, outcome: str, ran_at: str) -> None:
        rows = self.queries["queries"]
        row = next((r for r in rows if r["platform"] == platform and r["query"] == query), None)
        if row is None:
            row = {"platform": platform, "query": query, "module": module, "runs": 0}
            rows.append(row)
        row.update({"lastRunAt": ran_at, "outcome": outcome})
        row["runs"] += 1

    def query_is_fresh(self, platform: str, query: str, moment: datetime | None = None, days: int = 90) -> bool:
        row = next((r for r in self.queries["queries"] if r["platform"] == platform and r["query"] == query), None)
        return bool(row and row.get("outcome") == "ok" and parse_time(row["lastRunAt"]) + timedelta(days=days) > (moment or now()))

    # ------------------------------------------------------------ facts
    def add_fact(self, *, kind: str, topic: str, statement: str, sources: list[str], modules: list[str],
                 checked_at: str, used_by: list[str] | None = None) -> dict[str, Any]:
        if kind not in KINDS:
            raise KnowledgeError(f"kind 必须是 {sorted(KINDS)}")
        if not statement.strip() or not topic.strip():
            raise KnowledgeError("事实需要主题与内容")
        source_ids = []
        for ref in sources:
            source = next((s for s in self.sources["sources"] if s["id"] == ref), None) or self.source_by_url(ref)
            if source is None:
                raise KnowledgeError(f"来源不在知识库中：{ref}（先通过调研导入或 add-source 登记）")
            source_ids.append(source["id"])
        if not source_ids:
            raise KnowledgeError("事实必须至少有一个来源")
        existing = next((f for f in self.facts["facts"] if f["topic"] == topic and f["statement"] == statement), None)
        if existing:
            existing["checkedAt"] = checked_at
            for ref in source_ids:
                if ref not in existing["sources"]:
                    existing["sources"].append(ref)
            return existing
        numbers = [int(f["id"].split("-")[-1]) for f in self.facts["facts"]]
        fact = {"id": f"KF-{max(numbers, default=0) + 1:04d}", "kind": kind, "topic": topic, "statement": statement,
                "modules": modules, "sources": source_ids, "checkedAt": checked_at, "status": "active", "usedBy": used_by or []}
        self.facts["facts"].append(fact)
        return fact

    def facts_for(self, modules: Iterable[str], moment: datetime | None = None) -> list[dict[str, Any]]:
        wanted = set(modules)
        return [f for f in self.facts["facts"] if f.get("status") == "active" and wanted.intersection(f.get("modules", []))]

    def search(self, text: str) -> dict[str, list[dict[str, Any]]]:
        needle = text.lower()
        return {
            "facts": [f for f in self.facts["facts"] if needle in (f["topic"] + f["statement"] + " ".join(f["modules"])).lower()],
            "sources": [s for s in self.sources["sources"] if needle in (s["title"] + s["url"] + " ".join(s["modules"]) + s.get("excerpt", "")).lower()],
        }

    def stale(self, moment: datetime | None = None) -> dict[str, list[dict[str, Any]]]:
        return {"facts": [f for f in self.facts["facts"] if f.get("status") == "active" and not self.is_fresh(f, moment)],
                "sources": [s for s in self.sources["sources"] if not self.is_fresh(s, moment)]}

    def validate(self) -> list[str]:
        errors = []
        ids = {s["id"] for s in self.sources["sources"]}
        urls = [s["url"] for s in self.sources["sources"]]
        if len(urls) != len(set(urls)):
            errors.append("sources.json 有重复 URL")
        for s in self.sources["sources"]:
            if s.get("kind") not in KINDS:
                errors.append(f"{s['id']} kind 无效")
            if not str(s.get("url", "")).startswith(("http://", "https://")):
                errors.append(f"{s['id']} URL 无效")
        for f in self.facts["facts"]:
            if f.get("kind") not in KINDS:
                errors.append(f"{f['id']} kind 无效")
            missing = [ref for ref in f.get("sources", []) if ref not in ids]
            if missing or not f.get("sources"):
                errors.append(f"{f['id']} 来源缺失：{missing or '无来源'}")
        return errors


# ---------------------------------------------------------------- harvesting


def harvest_plan(knowledge: Knowledge, plan: Mapping[str, Any]) -> dict[str, int]:
    """Copy every read source of a plan into the knowledge base, and log its searches."""
    added = updated = 0
    checked = plan.get("updatedAt") or now().isoformat(timespec="seconds")
    for source in plan.get("research", {}).get("sources", []):
        if not source.get("read") or source.get("origin") == "knowledge" or not str(source.get("url", "")).startswith("http"):
            continue
        _, created = knowledge.add_source(url=source["url"], title=source.get("title", ""), tier=source.get("tier", "community"),
                                          modules=source.get("modules", []), checked_at=source.get("checkedAt") or checked,
                                          evidence=source.get("evidence", ""), excerpt=source.get("excerpt", ""), query=source.get("query", ""))
        added += created
        updated += not created
    for row in plan.get("research", {}).get("doneQueries", []):
        knowledge.record_query(row["platform"], row["query"], row.get("reason", ":").split(":", 1)[0], "ok", checked)
    for row in plan.get("research", {}).get("retryQueries", []):
        knowledge.record_query(row["platform"], row["query"], row.get("reason", ":").split(":", 1)[0], "cooled", checked)
    return {"added": added, "updated": updated}


# ---------------------------------------------------------------- page


def render(knowledge: Knowledge, design_root: Path | None = None, moment: datetime | None = None) -> str:
    tokens, token_source = load_tokens(design_root)
    template = TEMPLATE.read_text(encoding="utf-8")
    sources = {s["id"]: s for s in knowledge.sources["sources"]}
    fact_rows = []
    for fact in sorted(knowledge.facts["facts"], key=lambda f: (f["kind"], f["topic"], f["id"])):
        fresh = knowledge.is_fresh(fact, moment)
        refs = " ".join(f'<a href="{html.escape(sources[r]["url"], quote=True)}" rel="noreferrer">原文 {i}</a>' for i, r in enumerate(fact["sources"], 1) if r in sources)
        fact_rows.append(
            f'<tr data-kind="{html.escape(fact["kind"])}" data-search="{html.escape((fact["topic"] + fact["statement"] + " ".join(fact["modules"])).lower(), quote=True)}">'
            f"<td>{chip(KINDS[fact['kind']][0], 'info')}</td><td>{inline(fact['topic'])}</td>"
            f"<td>{inline(fact['statement'])}<div class=\"excerpt\">{refs}</div></td><td>{html.escape('、'.join(fact['modules']))}</td>"
            f"<td>{chip('有效' if fresh else '待复核', 'success' if fresh else 'warning')}</td></tr>"
        )
    source_rows = []
    for source in sorted(knowledge.sources["sources"], key=lambda s: (s["kind"], s["title"])):
        fresh = knowledge.is_fresh(source, moment)
        source_rows.append(
            f'<tr data-kind="{html.escape(source["kind"])}" data-search="{html.escape((source["title"] + " ".join(source["modules"]) + source.get("excerpt", "")).lower(), quote=True)}">'
            f"<td data-quoted><a href=\"{html.escape(source['url'], quote=True)}\" rel=\"noreferrer\">{html.escape(source['title'])}</a>"
            f"<div class=\"excerpt\">{html.escape(source.get('excerpt', '')[:120])}</div></td><td>{chip(KINDS[source['kind']][0], 'plain')}</td>"
            f"<td>{html.escape('、'.join(source['modules']) or '—')}</td>"
            f"<td>{chip('有效' if fresh else '待复核', 'success' if fresh else 'warning')}</td></tr>"
        )
    stale = knowledge.stale(moment)
    kpis = [("事实", len(knowledge.facts["facts"]), "可直接复用的结论"), ("来源", len(knowledge.sources["sources"]), "已阅读的原文"),
            ("检索记录", len(knowledge.queries["queries"]), "已执行的搜索"), ("待复核", len(stale["facts"]) + len(stale["sources"]), "超过复核周期")]
    values = {
        "{{TOKENS_CSS}}": tokens_css(tokens), "{{FAVICON}}": favicon_for_tokens("knowledge", tokens), "{{SHELL_CSS}}": shell_css(), "{{TOKEN_SOURCE}}": html.escape(token_source, quote=True),
        "{{KPIS}}": "".join(f'<div class="kpi"><span class="kpi-label">{a}</span><strong class="kpi-value">{b}</strong><span class="kpi-foot">{c}</span></div>' for a, b, c in kpis),
        "{{KIND_OPTIONS}}": "".join(f'<option value="{k}">{v[0]}</option>' for k, v in KINDS.items()),
        "{{FACT_ROWS}}": "".join(fact_rows) or '<tr><td colspan="5">暂无</td></tr>',
        "{{SOURCE_ROWS}}": "".join(source_rows) or '<tr><td colspan="4">暂无</td></tr>',
    }
    return fill(template, values)


def build(knowledge: Knowledge, design_root: Path | None = None) -> Path:
    output = knowledge.root / "index.html"
    output.write_bytes(render(knowledge, design_root).encode("utf-8"))
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=None, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)
    harvest = sub.add_parser("harvest", help="把方案中已阅读的来源与检索记录写入知识库")
    harvest.add_argument("plan_json", type=Path, nargs="+")
    fact = sub.add_parser("fact", help="记录一条可复用的事实（必须引用知识库中的来源）")
    fact.add_argument("--kind", required=True, choices=sorted(KINDS))
    fact.add_argument("--topic", required=True)
    fact.add_argument("--statement", required=True)
    fact.add_argument("--source", action="append", required=True, help="知识库来源 ID（KS-0001）或 URL")
    fact.add_argument("--module", action="append", default=[])
    fact.add_argument("--used-by", action="append", default=[], help="引用它的规则或文档，例如 PAY-009")
    search = sub.add_parser("search", help="按关键词查找事实与来源")
    search.add_argument("text")
    modules = sub.add_parser("for-modules", help="列出某些模块的有效事实与来源")
    modules.add_argument("modules", nargs="+")
    sub.add_parser("stale", help="列出超过复核周期的事实与来源")
    sub.add_parser("build", help="生成 knowledge/index.html")
    sub.add_parser("check", help="校验知识库")
    args = parser.parse_args(argv)
    kb = Knowledge(args.root)
    try:
        if args.command == "harvest":
            result = {}
            for path in args.plan_json:
                result[str(path)] = harvest_plan(kb, json.loads(path.read_text(encoding="utf-8")))
            kb.save()
            build(kb)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "fact":
            item = kb.add_fact(kind=args.kind, topic=args.topic, statement=args.statement, sources=args.source,
                               modules=[m for raw in args.module for m in re.split(r"[,，]", raw) if m],
                               checked_at=now().isoformat(timespec="seconds"), used_by=args.used_by)
            kb.save()
            build(kb)
            print(json.dumps(item, ensure_ascii=False, indent=2))
        elif args.command == "search":
            print(json.dumps(kb.search(args.text), ensure_ascii=False, indent=2))
        elif args.command == "for-modules":
            print(json.dumps({"facts": kb.facts_for(args.modules), "sources": kb.sources_for(args.modules)}, ensure_ascii=False, indent=2))
        elif args.command == "stale":
            stale = kb.stale()
            print(json.dumps({"facts": [f["id"] + " " + f["topic"] for f in stale["facts"]],
                              "sources": [s["id"] + " " + s["title"] for s in stale["sources"]]}, ensure_ascii=False, indent=2))
        elif args.command == "build":
            print(build(kb))
        else:
            errors = kb.validate()
            print(json.dumps({"ok": not errors, "errors": errors, "facts": len(kb.facts["facts"]), "sources": len(kb.sources["sources"])}, ensure_ascii=False))
            return 0 if not errors else 1
        return 0
    except (KnowledgeError, OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
