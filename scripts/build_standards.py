#!/usr/bin/env python3
"""Validate references/standards.json and build the human-readable standards page.

The JSON is canonical: agents read it, tech_plan.py derives plan checklists from it,
and this script renders references/standards.html with Luz Design tokens.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from luz_shell import chip, favicon_for_tokens, fill, inline, load_tokens, shell_css, tokens_css  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = ROOT / "references" / "standards.json"
DEFAULT_TEMPLATE = ROOT / "assets" / "reference" / "standards.template.html"
DEFAULT_OUTPUT = ROOT / "references" / "standards.html"
RULE_ID = re.compile(r"^[A-Z]+-\d{3}$")
BASELINE_ID = re.compile(r"^BL-[A-Z]{2}-\d{2}$")
LEVELS = {"MUST", "SHOULD"}
PROJECT_TYPES = {"miniprogram", "admin", "mobile", "backend", "agent"}
TYPE_NAMES = {"miniprogram": "小程序", "admin": "管理后台", "mobile": "移动 App", "backend": "后端服务", "agent": "智能体", "all": "全部"}
EXTERNAL_DOMAINS = {"pay", "face", "mp"}


def validate_standards(data: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["standards root must be an object"]
    for field in ("schemaVersion", "updatedAt", "title", "description", "domains", "modules", "rules", "baselines"):
        if field not in data:
            errors.append(f"missing field: {field}")
    domains = {item.get("id") for item in data.get("domains", [])}
    modules = {item.get("id") for item in data.get("modules", [])}
    for item in data.get("modules", []):
        pattern = item.get("pattern")
        if pattern and not (ROOT / pattern).is_file():
            errors.append(f"module {item.get('id')}: pattern file does not exist: {pattern}")
    platforms = set(data.get("platforms", {})) or PROJECT_TYPES | {"all"}
    rule_ids: set[str] = set()
    for index, rule in enumerate(data.get("rules", [])):
        at = f"rules[{index}] {rule.get('id', '?')}"
        rule_id = rule.get("id", "")
        if not RULE_ID.fullmatch(str(rule_id)):
            errors.append(f"{at}: id must look like PAY-001")
        if rule_id in rule_ids:
            errors.append(f"{at}: duplicate id")
        rule_ids.add(rule_id)
        for field in ("title", "rule", "why"):
            if not str(rule.get(field, "")).strip():
                errors.append(f"{at}: {field} is required")
        if rule.get("level") not in LEVELS:
            errors.append(f"{at}: level must be MUST or SHOULD")
        if rule.get("domain") not in domains:
            errors.append(f"{at}: unknown domain {rule.get('domain')}")
        for platform in rule.get("platforms", []):
            if platform not in platforms:
                errors.append(f"{at}: unknown platform {platform}")
        if not rule.get("platforms"):
            errors.append(f"{at}: platforms must not be empty")
        for module in rule.get("modules", []):
            if module not in modules:
                errors.append(f"{at}: unknown module {module}")
        if rule.get("level") == "MUST" and not rule.get("verify"):
            errors.append(f"{at}: MUST rule needs at least one verify item")
        for list_field in ("do", "dont", "verify", "sources"):
            if not isinstance(rule.get(list_field, []), list):
                errors.append(f"{at}: {list_field} must be a list")
        if rule.get("domain") in EXTERNAL_DOMAINS and rule.get("level") == "MUST":
            official = [s for s in rule.get("sources", []) if str(s.get("url", "")).startswith("http")]
            if not official:
                errors.append(f"{at}: external-platform MUST rule needs an http(s) source")
        for source in rule.get("sources", []):
            if not str(source.get("title", "")).strip() or not str(source.get("url", "")).strip():
                errors.append(f"{at}: every source needs title and url")
    baseline_ids: set[str] = set()
    for index, item in enumerate(data.get("baselines", [])):
        at = f"baselines[{index}] {item.get('id', '?')}"
        if not BASELINE_ID.fullmatch(str(item.get("id", ""))):
            errors.append(f"{at}: id must look like BL-MP-01")
        if item.get("id") in baseline_ids:
            errors.append(f"{at}: duplicate id")
        baseline_ids.add(item.get("id"))
        if item.get("projectType") not in PROJECT_TYPES:
            errors.append(f"{at}: unknown projectType")
        if item.get("requirement") not in {"required", "conditional"}:
            errors.append(f"{at}: requirement must be required or conditional")
        if item.get("requirement") == "conditional" and not str(item.get("condition", "")).strip():
            errors.append(f"{at}: conditional baseline needs a condition")
        for field in ("feature", "acceptance"):
            if not str(item.get(field, "")).strip():
                errors.append(f"{at}: {field} is required")
        for rule_id in item.get("rules", []):
            if rule_id not in rule_ids:
                errors.append(f"{at}: references unknown rule {rule_id}")
        for module in item.get("modules", []):
            if module not in modules:
                errors.append(f"{at}: unknown module {module}")
    return errors


def render_rule(rule: dict[str, Any]) -> str:
    level_tone = "danger" if rule["level"] == "MUST" else "neutral"
    platforms = "、".join(TYPE_NAMES.get(p, p) for p in rule.get("platforms", []))
    modules = "、".join(rule.get("modules", [])) or "通用"

    def items(values: list[str]) -> str:
        return "".join(f"<li>{inline(value)}</li>" for value in values) or "<li>—</li>"

    sources = "".join(
        f'<li><a href="{html.escape(s["url"], quote=True)}" rel="noreferrer">{inline(s["title"])}</a></li>'
        for s in rule.get("sources", [])
    ) or '<li class="muted">内部经验规则</li>'
    search = " ".join([rule["id"], rule["title"], rule["rule"], modules, platforms]).lower()
    return (
        f'<details class="rule" id="{html.escape(rule["id"])}" data-level="{rule["level"]}" '
        f'data-platforms="{html.escape(" ".join(rule.get("platforms", [])))}" data-search="{html.escape(search, quote=True)}">'
        f'<summary><code>{html.escape(rule["id"])}</code>{chip(rule["level"], level_tone)}<span class="rule-title">{inline(rule["title"])}</span>'
        f'<span class="rule-meta">{html.escape(platforms)} · {html.escape(modules)}</span></summary>'
        f'<div class="rule-body"><p>{inline(rule["rule"])}</p><p class="muted">原因：{inline(rule["why"])}</p>'
        f'<div class="grid"><div class="card"><h3>做法</h3><ul class="list">{items(rule.get("do", []))}</ul></div>'
        f'<div class="card"><h3>禁止</h3><ul class="list">{items(rule.get("dont", []))}</ul></div>'
        f'<div class="card"><h3>验证方式</h3><ul class="list">{items(rule.get("verify", []))}</ul></div>'
        f'<div class="card"><h3>依据</h3><ul class="list">{sources}</ul></div></div></div></details>'
    )


def rule_links(rule_ids: list[str]) -> str:
    return " ".join(f'<a href="#{html.escape(r)}"><code>{html.escape(r)}</code></a>' for r in rule_ids) or "—"


def render(data: dict[str, Any], template: str, design_root: Path | None = None) -> str:
    tokens, token_source = load_tokens(design_root)
    domains = data["domains"]
    rules = data["rules"]
    by_domain: dict[str, list[dict[str, Any]]] = {}
    for rule in rules:
        by_domain.setdefault(rule["domain"], []).append(rule)
    nav = ['<a href="#overview" class="nav-link active"><span class="nav-index">00</span><span class="nav-copy">概览</span></a>']
    sections = []
    for index, domain in enumerate([d for d in domains if d["id"] in by_domain], start=1):
        nav.append(
            f'<a href="#d-{html.escape(domain["id"])}" class="nav-link"><span class="nav-index">{index:02d}</span>'
            f'<span class="nav-copy">{html.escape(domain["name"])}</span></a>'
        )
        domain_rules = by_domain[domain["id"]]
        must = len([r for r in domain_rules if r["level"] == "MUST"])
        sections.append(
            f'<section class="section domain" id="d-{html.escape(domain["id"])}"><h2>{html.escape(domain["name"])}'
            f' <span class="muted">MUST {must} · SHOULD {len(domain_rules) - must}</span></h2>'
            + "".join(render_rule(rule) for rule in domain_rules) + "</section>"
        )
    nav.append(f'<a href="#baselines" class="nav-link"><span class="nav-index">{len(nav):02d}</span><span class="nav-copy">基础功能</span></a>')
    baseline_parts = []
    for project_type in ("miniprogram", "admin", "backend", "mobile", "agent"):
        items = [item for item in data["baselines"] if item["projectType"] == project_type]
        if not items:
            continue
        rows = "".join(
            f'<tr><td><code>{html.escape(item["id"])}</code></td><td>{inline(item["feature"])}</td>'
            f'<td>{chip("必备", "danger") if item["requirement"] == "required" else chip("按需", "neutral")}'
            f'<div class="excerpt">{inline(item.get("condition", ""))}</div></td><td>{inline(item["acceptance"])}</td>'
            f'<td>{rule_links(item.get("rules", []))}</td></tr>'
            for item in items
        )
        baseline_parts.append(
            f'<h3>{TYPE_NAMES[project_type]}</h3><div class="table-wrap"><table><thead><tr><th scope="col">编号</th><th scope="col">功能</th>'
            f'<th scope="col">要求</th><th scope="col">验收</th><th scope="col">相关规则</th></tr></thead><tbody>{rows}</tbody></table></div>'
        )
    must_total = len([r for r in rules if r["level"] == "MUST"])
    kpis = [
        ("规则", str(len(rules)), f"MUST {must_total} · SHOULD {len(rules) - must_total}"),
        ("领域", str(len(by_domain)), "安全、资金、支付、隐私等"),
        ("基础功能", str(len(data["baselines"])), "小程序、后台、后端、App、智能体"),
        ("更新", data["updatedAt"], "规范版本 " + data["schemaVersion"]),
    ]
    kpi_html = "".join(
        f'<div class="kpi"><span class="kpi-label">{html.escape(a)}</span><strong class="kpi-value">{html.escape(b)}</strong>'
        f'<span class="kpi-foot">{html.escape(c)}</span></div>' for a, b, c in kpis
    )
    values = {
        "{{TOKENS_CSS}}": tokens_css(tokens),
        "{{FAVICON}}": favicon_for_tokens("standards", tokens),
        "{{SHELL_CSS}}": shell_css(),
        "{{TOKEN_SOURCE}}": html.escape(token_source, quote=True),
        "{{TITLE}}": html.escape(data["title"]),
        "{{DESCRIPTION}}": inline(data["description"]),
        "{{UPDATED_AT}}": html.escape(data["updatedAt"]),
        "{{NAV}}": "".join(nav),
        "{{KPIS}}": f'<section class="kpis" aria-label="规范概况">{kpi_html}</section>',
        "{{SECTIONS}}": "".join(sections),
        "{{BASELINES}}": "".join(baseline_parts),
        "{{RULE_COUNT}}": str(len(rules)),
    }
    return fill(template, values)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--design-root", type=Path)
    parser.add_argument("--check", action="store_true", help="只校验 JSON，不生成页面")
    args = parser.parse_args(argv)
    data = json.loads(args.data.read_text(encoding="utf-8"))
    errors = validate_standards(data)
    if errors:
        for error in errors:
            print("ERROR", error)
        print(f"FAIL: {len(errors)} issue(s)")
        return 1
    if args.check:
        print(f"PASS: {len(data['rules'])} rules, {len(data['baselines'])} baselines")
        return 0
    output = render(data, args.template.read_text(encoding="utf-8"), args.design_root)
    args.output.write_text(output, encoding="utf-8", newline="\n")
    print(json.dumps({"output": str(args.output), "rules": len(data["rules"]), "baselines": len(data["baselines"]), "bytes": len(output.encode())}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
