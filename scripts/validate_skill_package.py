#!/usr/bin/env python3
"""Validate the Luz Dev package, automation, experience store, and local links."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DESIGN_ROOT = ROOT.parent / "Luz-Design"
DEV_SCHEMA_PATH = ROOT / "contracts" / "handoff.schema.json"
REQUIRED = [
    "SKILL.md",
    "scripts/module_specs.py",
    "references/module-specs/README.md",
    "tests/test_module_specs.py",
    "tests/test_research_tracks.py",
    "WORKFLOW.md",
    "MIGRATION.md",
    "agents/openai.yaml",
    "references/implementation-architecture.md",
    "references/miniprogram.md",
    "references/admin.md",
    "references/mobile-app.md",
    "references/backend.md",
    "references/agent.md",
    "references/quality.md",
    "references/delivery.md",
    "references/api-bindings.md",
    "references/index.md",
    "references/function-catalog.json",
    "references/function-catalog.html",
    "references/result-patterns.md",
    "assets/reference/function-catalog.template.html",
    "scripts/build_function_catalog.py",
    "tests/test_function_catalog.py",
    "contracts/HANDOFF.md",
    "contracts/IMPLEMENTATION-RECORD.md",
    "contracts/handoff.schema.json",
    "assets/project-template/IMPLEMENTATION.md",
    "assets/project-template/PLAN.md",
    "assets/project-template/QUALITY.md",
    "assets/tech-plan/template.html",
    "assets/tech-plan/tokens.snapshot.json",
    "assets/shared/luz-reading.css",
    "assets/reference/standards.template.html",
    "references/standards.json",
    "references/standards.html",
    "references/research.md",
    "references/research-sources.json",
    "references/patterns/README.md",
    "references/design-integration.md",
    "references/operations.md",
    "references/compliance.md",
    "references/legal-documents.md",
    "scripts/design_intake.py",
    "scripts/knowledge.py",
    "tests/test_knowledge.py",
    "knowledge/README.md",
    "knowledge/sources.json",
    "knowledge/facts.json",
    "knowledge/queries.json",
    "knowledge/index.html",
    "assets/reference/knowledge.template.html",
    "tests/test_design_intake.py",
    "assets/deploy/docker-compose.prod.yml",
    "assets/deploy/nginx.conf",
    "assets/deploy/api.env.example",
    "assets/deploy/backup.sh",
    "assets/deploy/LAUNCH-CHECKLIST.md",
    "assets/legal/privacy-policy.md",
    "assets/legal/user-terms.md",
    "assets/legal/face-consent.md",
    "assets/legal/prepaid-card-terms.md",
    "backend/27-legal/code/legal/__init__.py",
    "references/patterns/inventory.md",
    "references/patterns/order.md",
    "references/patterns/consistency.md",
    "references/patterns/pricing.md",
    "backend/00-framework/code/luzback/statemachine.py",
    "backend/07-inventory/code/inventory/reservation.py",
    "backend/08-order/code/order/jobs.py",
    "backend/10-after-sales/code/after_sales/__init__.py",
    "backend/13-reservation/code/booking/__init__.py",
    "backend/15-recharge/code/wallet/__init__.py",
    "backend/17-campaign/code/pricing/__init__.py",
    "scripts/tech_plan.py",
    "scripts/luz_shell.py",
    "scripts/build_standards.py",
    "tests/test_tech_plan.py",
    "backend/09-wechat-pay/code/wechat_pay/apiv3.py",
    "backend/09-wechat-pay/code/tests/test_apiv3.py",
    "assets/experience/RECORD-TEMPLATE.md",
    "assets/experience/PLAYBOOK-TEMPLATE.md",
    "backend/README.md",
    "backend/SPEC.md",
    "backend/MODULES.md",
    "backend/API.md",
    "backend/SECURITY.md",
    "backend/ROUTING.md",
    "backend/MICROSERVICES.md",
    "backend/CONTRACTS.md",
    "backend/MODULE-TEMPLATE.md",
    "backend/00-framework/code/luzback/__init__.py",
    "agent/README.md",
    "agent/backend/SPEC.md",
    "agent/shared/protocol.md",
    "experience/README.md",
    "experience/INDEX.md",
    "experience/reusable/README.md",
    "experience/playbooks/README.md",
    "experience/reusable/scripts/README.md",
    "scripts/validate_handoff.py",
    "scripts/create_tech_plan.py",
    "scripts/record_experience.py",
    "tests/test_skill_automation.py",
    "tests/test_validate_handoff.py",
]
REQUIRED_DIRECTORIES = [
    "experience/records",
    "experience/playbooks",
    "experience/reusable/backend",
    "experience/reusable/frontend",
    "experience/reusable/scripts",
]
EXPERIENCE_NAME = re.compile(r"^EXP-(\d{4,})-.+\.md$")
SECRET_PATTERN = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\bsk-[A-Za-z0-9_-]{20,}\b"
)
STATUSES = {"candidate", "verified", "deprecated"}
TASK_MODES = {"implementation", "review", "diagnosis", "analysis", "operations"}
TECH_PLAN_SECTIONS = {
    "overview",
    "research",
    "solution",
    "architecture",
    "design",
    "routes",
    "data",
    "modules",
    "features",
    "security",
    "launch",
    "testing-delivery",
    "reusable",
}
TECH_PLAN_TOKENS = {
    "{{TOKENS_CSS}}", "{{SHELL_CSS}}", "{{TOKEN_SOURCE}}", "{{PLAN_NUMBER}}", "{{TITLE}}", "{{TASK_MODE}}",
    "{{STATUS}}", "{{SECTIONS}}",
}


def parse_frontmatter(text: str) -> dict[str, Any]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("缺少 front matter")
    try:
        end = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration as exc:
        raise ValueError("front matter 未闭合") from exc
    fields: dict[str, Any] = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise ValueError(f"无法解析元数据: {line}")
        key, raw = line.split(":", 1)
        raw = raw.strip()
        try:
            fields[key.strip()] = json.loads(raw) if raw[:1] in {'"', "[", "{"} else raw
        except json.JSONDecodeError as exc:
            raise ValueError(f"无法解析字段 {key.strip()}") from exc
    return fields


def validate_tech_plan_template(path: Path) -> list[str]:
    """The template must carry every render token, and a rendered implementation draft every section."""
    errors: list[str] = []
    if not path.is_file():
        return [f"missing technical-plan template: {path}"]
    text = path.read_text(encoding="utf-8")
    found_tokens = set(re.findall(r"\{\{[A-Z0-9_]+\}\}", text))
    for token in sorted(TECH_PLAN_TOKENS - found_tokens):
        errors.append(f"technical-plan template missing token: {token}")
    for token in sorted(found_tokens - TECH_PLAN_TOKENS):
        errors.append(f"technical-plan template has unknown token: {token}")

    root_text = str(ROOT / "scripts")
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    try:
        import tech_plan
    except ImportError as exc:
        return errors + [f"cannot import tech_plan: {exc}"]
    plan = tech_plan.new_plan_data(
        number="00", topic="校验", mode="implementation", summary="校验", project_types=["miniprogram", "backend"],
        modules=["payment"], output_root=ROOT,
    )
    rendered = tech_plan.render_plan(plan)
    for section in sorted(TECH_PLAN_SECTIONS):
        if f'id="{section}"' not in rendered:
            errors.append(f"rendered implementation plan missing section: {section}")
    if '<main class="doc">' not in rendered or "<nav" in rendered:
        errors.append("rendered plan is not a Luz Design reading page")
    if not plan["checklist"]["rules"] or not plan["checklist"]["baselines"]:
        errors.append("implementation plan did not derive standards checklist")
    return errors


def validate_standards() -> list[str]:
    errors: list[str] = []
    data_path = ROOT / "references" / "standards.json"
    html_path = ROOT / "references" / "standards.html"
    template_path = ROOT / "assets" / "reference" / "standards.template.html"
    if not data_path.is_file() or not html_path.is_file() or not template_path.is_file():
        return errors
    root_text = str(ROOT / "scripts")
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    try:
        import build_standards
    except ImportError as exc:
        return [f"cannot import standards builder: {exc}"]
    try:
        data = json.loads(data_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"invalid standards.json: {exc}"]
    errors.extend(f"standards.json: {error}" for error in build_standards.validate_standards(data))
    if errors:
        return errors
    expected = build_standards.render(data, template_path.read_text(encoding="utf-8"))
    if html_path.read_text(encoding="utf-8") != expected:
        errors.append("references/standards.html is stale; run scripts/build_standards.py")
    for forbidden in ("<script src=", "<link rel=", ".innerHTML"):
        if forbidden in expected:
            errors.append(f"standards.html contains forbidden dependency or unsafe rendering: {forbidden}")
    rule_ids = {rule["id"] for rule in data["rules"]}
    for required in ("PAY-001", "PAY-005", "PAY-006", "PAY-009", "PAY-010", "RFD-002", "RFD-003", "FACE-001", "FACE-002", "AUTHZ-001", "MONEY-001"):
        if required not in rule_ids:
            errors.append(f"standards.json is missing core rule {required}")
    if SECRET_PATTERN.search(data_path.read_text(encoding="utf-8")):
        errors.append("standards.json contains possible secret material")
    return errors


def validate_module_specs() -> list[str]:
    sys.path.insert(0, str(ROOT / "scripts"))
    import module_specs  # noqa: PLC0415 - sibling script

    library = module_specs.load_library()
    if len(library) < 10:
        return [f"references/module-specs has only {len(library)} module specs"]
    return [f"module spec: {problem}" for problem in module_specs.validate_specs(library)]


def validate_knowledge() -> list[str]:
    root_text = str(ROOT / "scripts")
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    import knowledge  # noqa: PLC0415

    base = knowledge.Knowledge(ROOT / "knowledge")
    errors = [f"knowledge: {error}" for error in base.validate()]
    for name in ("sources.json", "facts.json", "queries.json"):
        path = ROOT / "knowledge" / name
        if path.is_file() and SECRET_PATTERN.search(path.read_text(encoding="utf-8")):
            errors.append(f"knowledge/{name} contains possible secret material")
    return errors


def validate_pages_with_luz_design(design_root: Path) -> list[str]:
    """Run Luz Design's document checker (read-only) on the HTML pages this package generates."""
    script = design_root / "scripts" / "check_document.py"
    if not script.is_file():
        return []
    import subprocess  # noqa: PLC0415

    errors = []
    for relative in ("references/standards.html", "knowledge/index.html"):
        page = ROOT / relative
        if not page.is_file():
            continue
        result = subprocess.run([sys.executable, "-X", "utf8", str(script), str(page)], capture_output=True,
                                text=True, encoding="utf-8", errors="replace")
        errors += [f"{relative} fails Luz Design check: {line.strip()[6:]}" for line in result.stdout.splitlines() if line.strip().startswith("ERROR")]
    return errors


def validate_research_sources() -> list[str]:
    path = ROOT / "references" / "research-sources.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"invalid research-sources.json: {exc}"]
    errors: list[str] = []
    for mode in TASK_MODES:
        if mode not in data.get("thresholds", {}):
            errors.append(f"research-sources.json missing threshold for {mode}")
    for tier in ("official", "standard", "opensource", "community"):
        if not data.get("tiers", {}).get(tier):
            errors.append(f"research-sources.json missing tier list: {tier}")
    standards_path = ROOT / "references" / "standards.json"
    modules = set()
    if standards_path.is_file():
        modules = {item["id"] for item in json.loads(standards_path.read_text(encoding="utf-8")).get("modules", [])}
    for module_id, module in data.get("modules", {}).items():
        if modules and module_id not in modules:
            errors.append(f"research-sources.json module {module_id} is not declared in standards.json")
        if module.get("officialRequired") and not (module.get("officialUrls") or module.get("queries")):
            errors.append(f"research-sources.json module {module_id} requires official sources but has no route")
        for query in module.get("queries", []):
            if not query.get("platform") or not query.get("query"):
                errors.append(f"research-sources.json module {module_id} has an incomplete query")
        for item in module.get("officialUrls", []):
            url = item if isinstance(item, str) else item.get("url", "")
            if not str(url).startswith("https://"):
                errors.append(f"research-sources.json module {module_id} official URL must be https: {url}")
    tracks = data.get("baselineTracks", {})
    for required in ("web-security", "admin-security", "container-deploy", "backup-dr"):
        if required not in tracks:
            errors.append(f"research-sources.json baselineTracks missing {required}")
    for track_id, track in tracks.items():
        if not track.get("name") or not track.get("queries"):
            errors.append(f"research-sources.json track {track_id} needs a name and queries")
        unknown_modes = [m for m in track.get("modes", []) if m not in TASK_MODES]
        if not track.get("modes") or unknown_modes:
            errors.append(f"research-sources.json track {track_id} has invalid modes: {unknown_modes or 'none'}")
        if track_id in modules:
            errors.append(f"research-sources.json track {track_id} clashes with a module id")
        if track.get("officialRequired") and not track.get("officialUrls"):
            errors.append(f"research-sources.json track {track_id} requires official sources but lists no officialUrls")
        for query in track.get("queries", []):
            if not query.get("platform") or not query.get("query"):
                errors.append(f"research-sources.json track {track_id} has an incomplete query")
        for item in track.get("officialUrls", []):
            url = item if isinstance(item, str) else item.get("url", "")
            if not str(url).startswith("https://"):
                errors.append(f"research-sources.json track {track_id} official URL must be https: {url}")
    return errors


def validate_experience_root(experience_root: Path) -> list[str]:
    errors: list[str] = []
    for relative in (
        "records",
        "playbooks",
        "reusable",
        "reusable/backend",
        "reusable/frontend",
        "reusable/scripts",
    ):
        if not (experience_root / relative).is_dir():
            errors.append(f"missing experience directory: {relative}")
    for relative in ("README.md", "INDEX.md", "reusable/README.md"):
        if not (experience_root / relative).is_file():
            errors.append(f"missing experience file: {relative}")

    index_path = experience_root / "INDEX.md"
    index_text = index_path.read_text(encoding="utf-8") if index_path.is_file() else ""
    if "<!-- EXPERIENCE_ROWS_START -->" not in index_text or "<!-- EXPERIENCE_ROWS_END -->" not in index_text:
        errors.append("experience/INDEX.md is missing table markers")

    for record in sorted((experience_root / "records").glob("*.md")):
        if record.name == "README.md":
            continue
        name_match = EXPERIENCE_NAME.fullmatch(record.name)
        if not name_match:
            errors.append(f"invalid experience filename: {record.name}")
            continue
        text = record.read_text(encoding="utf-8")
        try:
            fields = parse_frontmatter(text)
        except ValueError as exc:
            errors.append(f"{record.name}: {exc}")
            continue
        record_id = str(fields.get("id", ""))
        expected_id = f"EXP-{int(name_match.group(1)):04d}"
        if record_id != expected_id:
            errors.append(f"{record.name}: id {record_id!r} does not match {expected_id}")
        if record_id not in index_text:
            errors.append(f"{record.name}: ID absent from experience index")
        for field in (
            "title",
            "status",
            "created",
            "updated",
            "task_mode",
            "plan",
            "tags",
            "evidence",
            "reusable_assets",
        ):
            if field not in fields:
                errors.append(f"{record.name}: missing field {field}")
        if fields.get("status") not in STATUSES:
            errors.append(f"{record.name}: invalid status {fields.get('status')!r}")
        if fields.get("task_mode") not in TASK_MODES:
            errors.append(f"{record.name}: invalid task_mode {fields.get('task_mode')!r}")
        evidence = fields.get("evidence", [])
        if not isinstance(evidence, list):
            errors.append(f"{record.name}: evidence must be a list")
            evidence = []
        if fields.get("status") == "verified" and not evidence:
            errors.append(f"{record_id}: verified experience requires at least one evidence item（证据）")
        if fields.get("status") == "deprecated":
            deprecation = re.search(
                r"<!-- DEPRECATION_START -->(.*?)<!-- DEPRECATION_END -->", text, re.DOTALL
            )
            if deprecation is None or "原因：" not in deprecation.group(1):
                errors.append(f"{record.name}: deprecated experience requires a reason")
        if SECRET_PATTERN.search(text):
            errors.append(f"{record.name}: possible secret material")
        reusable_assets = fields.get("reusable_assets", [])
        if not isinstance(reusable_assets, list):
            errors.append(f"{record.name}: reusable_assets must be a list")
            reusable_assets = []
        for raw_asset in reusable_assets:
            asset = Path(str(raw_asset)).expanduser()
            candidates = [asset] if asset.is_absolute() else [ROOT / asset, experience_root / asset]
            if not any(candidate.resolve().exists() for candidate in candidates):
                errors.append(f"{record.name}: reusable asset does not exist: {raw_asset}")

    for playbook in sorted((experience_root / "playbooks").glob("*.md")):
        if playbook.name == "README.md":
            continue
        text = playbook.read_text(encoding="utf-8")
        if "状态：verified" not in text:
            errors.append(f"{playbook.name}: playbook must be verified")
        if not re.search(r"来源经验：EXP-\d{4,}", text):
            errors.append(f"{playbook.name}: playbook must cite a source experience")
        if SECRET_PATTERN.search(text):
            errors.append(f"{playbook.name}: possible secret material")

    # Keyed preference / lesson / fact entries and record de-duplication (record_experience.py check).
    scripts_dir = str(Path(__file__).resolve().parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import record_experience

    try:
        store_errors, _ = record_experience.check_store(experience_root)
    except ValueError as exc:
        store_errors = [str(exc)]
    errors.extend(f"experience store: {error}" for error in store_errors)
    return errors


DESIGN_REFERENCES = {
    "skill": "$LUZ_DESIGN_ROOT/SKILL.md",
    "integration": "references/design-integration.md",
}
STALE_DESIGN_PATTERN = re.compile(
    r"\." r"\./Luz-" r"Design/design|ROOT\.parent\s*/\s*[\"']Luz-" r"Design"
)


def resolve_design_root(
    cli_value: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    dev_root: Path = ROOT,
) -> Path:
    """Resolve Design using CLI, environment, then the paired sibling default."""
    environment = os.environ if environ is None else environ
    if cli_value is not None:
        candidate = Path(cli_value)
    elif environment.get("LUZ_DESIGN_ROOT"):
        candidate = Path(environment["LUZ_DESIGN_ROOT"])
    else:
        candidate = dev_root.parent / "Luz-Design"
    return candidate.expanduser().resolve()


def validate_design_target(design_root: Path, dev_schema_path: Path = DEV_SCHEMA_PATH) -> list[str]:
    """Validate the real Design target and its producer/consumer contract boundary."""
    errors: list[str] = []
    if not design_root.is_dir():
        return [f"paired Design target is not a directory: {design_root}"]

    skill_path = design_root / "SKILL.md"
    if not skill_path.is_file():
        errors.append(f"paired Design target is missing SKILL.md: {design_root}")
    else:
        skill_text = skill_path.read_text(encoding="utf-8")
        if re.search(r"(?m)^name:\s*[\"']?luz-design[\"']?\s*$", skill_text) is None:
            errors.append(f"paired Design target is not the luz-design skill: {design_root}")

    # Luz Design reorganises its internals between releases. Luz Dev depends only on a stable contract:
    # the luz-design skill and a theme file (any known location). Design sessions come from Luz System (design_intake.py).
    root_text = str(ROOT / "scripts")
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    from luz_shell import find_themes  # noqa: PLC0415

    if find_themes(design_root) is None:
        errors.append(f"paired Design target has no theme file (references/themes.json or design/shared/themes/themes.json): {design_root}")
    return errors


def validate_agent_manifest(path: Path = ROOT / "agents" / "openai.yaml") -> list[str]:
    if not path.is_file():
        return [f"missing Agent manifest: {path.relative_to(ROOT)}"]
    text = path.read_text(encoding="utf-8")
    match = re.search(r"(?m)^\s{2}default_prompt:\s*(.*?)\s*$", text)
    if match is None or not match.group(1).strip("\"'"):
        return ["agents/openai.yaml must define a non-empty default_prompt"]
    return []


def validate_reference_index(path: Path = ROOT / "references" / "index.md") -> list[str]:
    if not path.is_file():
        return [f"missing reference index: {path.relative_to(ROOT)}"]
    text = path.read_text(encoding="utf-8")
    errors = [
        f"references/index.md is missing Design mapping: {route}"
        for route in DESIGN_REFERENCES.values()
        if route not in text
    ]
    if "--design-root" not in text or "LUZ_DESIGN_ROOT" not in text:
        errors.append("references/index.md must document both Design resolver overrides")
    return errors


def validate_stale_design_references() -> list[str]:
    errors: list[str] = []
    ignored_parts = {".git", ".superpowers", "__pycache__", ".pytest_cache"}
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in ignored_parts for part in path.parts):
            continue
        if path == Path(__file__).resolve() or path.suffix.lower() not in {".md", ".py", ".yaml", ".yml"}:
            continue
        text = path.read_text(encoding="utf-8")
        for line_number, line in enumerate(text.splitlines(), start=1):
            if STALE_DESIGN_PATTERN.search(line):
                errors.append(
                    f"stale Design path in {path.relative_to(ROOT)}:{line_number}; use $LUZ_DESIGN_ROOT"
                )
    return errors


def validate_function_catalog() -> list[str]:
    errors: list[str] = []
    data_path = ROOT / "references" / "function-catalog.json"
    html_path = ROOT / "references" / "function-catalog.html"
    template_path = ROOT / "assets" / "reference" / "function-catalog.template.html"
    if not data_path.is_file() or not html_path.is_file() or not template_path.is_file():
        return errors

    root_text = str(ROOT)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    try:
        from scripts.build_function_catalog import extract_embedded_catalog, validate_catalog
    except ImportError as exc:
        return [f"cannot import function catalog builder: {exc}"]

    try:
        data = json.loads(data_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"invalid function-catalog.json: {exc}"]
    errors.extend(f"function-catalog.json: {error}" for error in validate_catalog(data))

    html = html_path.read_text(encoding="utf-8")
    try:
        embedded = extract_embedded_catalog(html)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"invalid function-catalog.html: {exc}")
    else:
        if embedded != data:
            errors.append("function-catalog.html embedded data differs from canonical JSON")

    for forbidden in ("<script src=", "<link rel=", ".innerHTML"):
        if forbidden in html:
            errors.append(f"function-catalog.html contains forbidden dependency or unsafe rendering: {forbidden}")
    for control_id in (
        "search-input",
        "module-filter",
        "edit-toggle",
        "add-module",
        "import-data",
        "export-json",
        "export-html",
        "reset-local",
    ):
        if f'id="{control_id}"' not in html:
            errors.append(f"function-catalog.html missing control: {control_id}")
    template = template_path.read_text(encoding="utf-8")
    if template.count("{{CATALOG_DATA_JSON}}") != 1:
        errors.append("function-catalog.template.html must contain one catalog data placeholder")
    module_ids = {module.get("id") for module in data.get("modules", []) if isinstance(module, dict)}
    if "result-flow" not in module_ids:
        errors.append("function-catalog.json is missing the result-flow module")
    if "resultPolicy" not in data:
        errors.append("function-catalog.json is missing resultPolicy")
    return errors


def validate_result_patterns() -> list[str]:
    path = ROOT / "references" / "result-patterns.md"
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    errors: list[str] = []
    for phrase in (
        "## 2. 页面落点决策矩阵",
        "不得显示为成功或失败",
        "支付结果确认中",
        "请勿重复支付",
        "replace 导航",
        "total = success + failed + skipped",
        "结果页必须可刷新、可深链",
    ):
        if phrase not in text:
            errors.append(f"result-patterns.md missing required rule: {phrase}")
    if SECRET_PATTERN.search(text):
        errors.append("result-patterns.md contains possible secret material")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--design-root", type=Path, help="Luz Design package; overrides LUZ_DESIGN_ROOT")
    args = parser.parse_args(argv)
    design_root = resolve_design_root(args.design_root)
    print(f"DESIGN target: {design_root}")
    errors: list[str] = []
    for relative in REQUIRED:
        if not (ROOT / relative).exists():
            errors.append(f"missing required file: {relative}")
    for relative in REQUIRED_DIRECTORIES:
        if not (ROOT / relative).is_dir():
            errors.append(f"missing required directory: {relative}")

    for forbidden in ("frontend", "MODULES-DESIGN.md", "agent/frontend"):
        if (ROOT / forbidden).exists():
            errors.append(f"development package contains obsolete design/mixed path: {forbidden}")

    skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    if not skill.startswith("---" + chr(10) + "name: luz-dev" + chr(10) + "description: Use when "):
        errors.append("SKILL.md must declare luz-dev and a Use when description")
    if len(skill) > 40_000:
        errors.append("SKILL.md is too large; move conditional detail into references")
    for required_phrase in (
        "Mandatory technical plan before execution",
        "scripts/create_tech_plan.py",
        "Experience and continuous learning",
        "scripts/record_experience.py",
        "references/function-catalog.json",
        "references/result-patterns.md",
        "references/standards.json",
        "references/research.md",
        "references/patterns/",
        "references/design-integration.md",
        "references/operations.md",
        "references/legal-documents.md",
        "tech_plan.py gate",
        "tech_plan.py approve",
        "knowledge/",
        "tech_plan.py feature",
        "references/module-specs/",
        "tech_plan.py spec",
        "One task, one folder",
        "check_document.py",
        "Luz-Crawl",
        "$luz-system",
        "luz-design",
    ):
        if required_phrase not in skill:
            errors.append(f"SKILL.md missing required workflow phrase: {required_phrase}")

    errors.extend(validate_tech_plan_template(ROOT / "assets" / "tech-plan" / "template.html"))
    errors.extend(validate_experience_root(ROOT / "experience"))

    record_template = (ROOT / "assets" / "experience" / "RECORD-TEMPLATE.md").read_text(encoding="utf-8")
    for marker in ("<!-- EVIDENCE_START -->", "<!-- EVIDENCE_END -->", "<!-- DEPRECATION_START -->", "<!-- DEPRECATION_END -->"):
        if marker not in record_template:
            errors.append(f"experience record template missing marker: {marker}")
    if SECRET_PATTERN.search(record_template):
        errors.append("experience record template contains possible secret material")

    dev_schema = json.loads(DEV_SCHEMA_PATH.read_text(encoding="utf-8"))
    if dev_schema.get("title") != "Luz design-to-development handoff":
        errors.append("handoff schema title is unexpected")

    errors.extend(validate_design_target(design_root, DEV_SCHEMA_PATH))
    errors.extend(validate_agent_manifest())
    errors.extend(validate_reference_index())
    errors.extend(validate_function_catalog())
    errors.extend(validate_result_patterns())
    errors.extend(validate_standards())
    errors.extend(validate_research_sources())
    errors.extend(validate_knowledge())
    errors.extend(validate_module_specs())
    errors.extend(validate_pages_with_luz_design(design_root))
    errors.extend(validate_stale_design_references())

    markdown_link = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
    markdown_files = sorted(
        path for path in ROOT.rglob("*.md") if ".git" not in path.parts and ".superpowers" not in path.parts
    )
    for markdown in markdown_files:
        text = markdown.read_text(encoding="utf-8")
        if SECRET_PATTERN.search(text):
            errors.append(f"possible secret material in {markdown.relative_to(ROOT)}")
        for raw_target in markdown_link.findall(text):
            target = raw_target.strip().split()[0].strip("<>")
            parsed = urlparse(target)
            if parsed.scheme in {"http", "https", "mailto"} or target.startswith("#"):
                continue
            clean = unquote(target.split("#", 1)[0])
            if clean and not (markdown.parent / clean).resolve().exists():
                errors.append(f"broken local link in {markdown.relative_to(ROOT)}: {target}")

    if errors:
        print(f"FAIL: {len(errors)} issue(s)")
        for error in errors:
            print("ERROR", error)
        return 1
    records = len(list((ROOT / "experience" / "records").glob("EXP-*.md")))
    print(
        f"PASS: Luz Dev package, automation, {records} experience record(s), "
        f"source boundary, {len(markdown_files)} Markdown files, and Design target {design_root} validated"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
