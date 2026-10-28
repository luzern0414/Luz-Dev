#!/usr/bin/env python3
"""Build and validate the standalone Luz Dev function logic catalog."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = ROOT / "references" / "function-catalog.json"
DEFAULT_TEMPLATE = ROOT / "assets" / "reference" / "function-catalog.template.html"
DEFAULT_OUTPUT = ROOT / "references" / "function-catalog.html"
PLACEHOLDER = "{{CATALOG_DATA_JSON}}"
ID_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*$")
MODULE_STATUSES = {"implemented", "design", "planned", "mixed", "deprecated"}
FUNCTION_STATUSES = {"implemented", "design", "planned", "deprecated"}


def validate_catalog(data: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["catalog root must be an object"]
    for field in ("schemaVersion", "updatedAt", "title", "description", "modules"):
        if field not in data:
            errors.append(f"missing catalog field: {field}")
    if data.get("schemaVersion") != "1.1":
        errors.append("schemaVersion must be 1.1")
    for field in ("updatedAt", "title", "description"):
        if not isinstance(data.get(field), str) or not data.get(field, "").strip():
            errors.append(f"{field} must be a non-empty string")
    policy = data.get("resultPolicy")
    if not isinstance(policy, dict):
        errors.append("resultPolicy must be an object")
    else:
        for field in ("principle", "rules", "payment"):
            if field not in policy:
                errors.append(f"resultPolicy missing {field}")
        if not isinstance(policy.get("principle"), str) or not policy.get("principle", "").strip():
            errors.append("resultPolicy.principle must be a non-empty string")
        rules = policy.get("rules")
        if not isinstance(rules, list) or len(rules) < 6:
            errors.append("resultPolicy.rules must contain at least six decisions")
        else:
            rule_ids: set[str] = set()
            for index, rule in enumerate(rules):
                at = f"resultPolicy.rules[{index}]"
                if not isinstance(rule, dict):
                    errors.append(f"{at} must be an object")
                    continue
                for field in ("id", "label", "condition", "surface"):
                    if not isinstance(rule.get(field), str) or not rule.get(field, "").strip():
                        errors.append(f"{at}.{field} must be a non-empty string")
                rule_id = rule.get("id", "")
                if rule_id in rule_ids:
                    errors.append(f"duplicate resultPolicy rule id: {rule_id}")
                rule_ids.add(rule_id)
        payment = policy.get("payment")
        if not isinstance(payment, dict):
            errors.append("resultPolicy.payment must be an object")
        else:
            for field in (
                "route",
                "processingTitle",
                "successTitle",
                "failureTitle",
                "unknownTitle",
                "unknownCopy",
                "authority",
            ):
                if not isinstance(payment.get(field), str) or not payment.get(field, "").strip():
                    errors.append(f"resultPolicy.payment.{field} must be a non-empty string")
            if "服务端" not in str(payment.get("authority", "")):
                errors.append("resultPolicy.payment.authority must identify the server as authority")
            intervals = payment.get("pollIntervalsSeconds")
            if (
                not isinstance(intervals, list)
                or not intervals
                or any(not isinstance(value, int) or value <= 0 for value in intervals)
                or intervals != sorted(intervals)
            ):
                errors.append("resultPolicy.payment.pollIntervalsSeconds must be sorted positive integers")
            terminal_states = payment.get("terminalStates")
            if not isinstance(terminal_states, list) or not terminal_states:
                errors.append("resultPolicy.payment.terminalStates must be a non-empty list")

    modules = data.get("modules")
    if not isinstance(modules, list) or not modules:
        return errors + ["modules must be a non-empty list"]

    module_ids: set[str] = set()
    function_ids: set[str] = set()
    for module_index, module in enumerate(modules):
        at = f"modules[{module_index}]"
        if not isinstance(module, dict):
            errors.append(f"{at} must be an object")
            continue
        for field in ("id", "name", "backendModule", "status", "description", "functions"):
            if field not in module:
                errors.append(f"{at} missing {field}")
        module_id = module.get("id", "")
        if not isinstance(module_id, str) or ID_PATTERN.fullmatch(module_id) is None:
            errors.append(f"{at}.id is invalid: {module_id!r}")
        elif module_id in module_ids:
            errors.append(f"重复 module id: {module_id}")
        else:
            module_ids.add(module_id)
        if module.get("status") not in MODULE_STATUSES:
            errors.append(f"{at}.status is invalid: {module.get('status')!r}")
        for field in ("name", "backendModule", "description"):
            if not isinstance(module.get(field), str) or not module.get(field, "").strip():
                errors.append(f"{at}.{field} must be a non-empty string")
        source_refs = module.get("sourceRefs", [])
        if not isinstance(source_refs, list) or not source_refs:
            errors.append(f"{at}.sourceRefs must contain at least one reference")
        functions = module.get("functions")
        if not isinstance(functions, list) or not functions:
            errors.append(f"{at}.functions must be a non-empty list")
            continue
        for function_index, function in enumerate(functions):
            function_at = f"{at}.functions[{function_index}]"
            if not isinstance(function, dict):
                errors.append(f"{function_at} must be an object")
                continue
            for field in ("id", "name", "status", "rows"):
                if field not in function:
                    errors.append(f"{function_at} missing {field}")
            function_id = function.get("id", "")
            if not isinstance(function_id, str) or ID_PATTERN.fullmatch(function_id) is None:
                errors.append(f"{function_at}.id is invalid: {function_id!r}")
            elif function_id in function_ids:
                errors.append(f"重复 function id: {function_id}")
            else:
                function_ids.add(function_id)
            if function.get("status") not in FUNCTION_STATUSES:
                errors.append(f"{function_at}.status is invalid: {function.get('status')!r}")
            if not isinstance(function.get("name"), str) or not function.get("name", "").strip():
                errors.append(f"{function_at}.name must be a non-empty string")
            rows = function.get("rows")
            if not isinstance(rows, list) or not rows:
                errors.append(f"{function_at}.rows must be a non-empty list")
                continue
            labels: set[str] = set()
            for row_index, row in enumerate(rows):
                row_at = f"{function_at}.rows[{row_index}]"
                if not isinstance(row, dict):
                    errors.append(f"{row_at} must be an object")
                    continue
                for field in ("label", "value"):
                    if not isinstance(row.get(field), str) or not row.get(field, "").strip():
                        errors.append(f"{row_at}.{field} must be a non-empty string")
                label = row.get("label", "")
                if label in labels:
                    errors.append(f"{row_at} duplicate label: {label}")
                labels.add(label)
            if not any("验证" in label or "测试" in label for label in labels):
                errors.append(f"{function_at} needs a verification/test row")
            if not any("状态" in label or "失败" in label for label in labels):
                errors.append(f"{function_at} needs a state/failure row")
    return errors


def safe_json_for_script(data: Any) -> str:
    encoded = json.dumps(data, ensure_ascii=False, indent=2)
    return encoded.replace("</", "<\\/").replace("<!--", "<\\u0021--")


def render_catalog(template: str, data: Any) -> str:
    if template.count(PLACEHOLDER) != 1:
        raise ValueError("catalog template must contain exactly one data placeholder")
    return template.replace(PLACEHOLDER, safe_json_for_script(data))


def extract_embedded_catalog(html: str) -> Any:
    match = re.search(
        r'<script id="catalog-data" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    if match is None:
        raise ValueError("generated HTML is missing catalog-data script")
    return json.loads(match.group(1))


def write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(5)}.tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def build(data_path: Path, template_path: Path, output_path: Path) -> dict[str, str | int]:
    try:
        data = json.loads(data_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取目录数据: {exc}") from exc
    errors = validate_catalog(data)
    if errors:
        raise ValueError("功能目录校验失败:\n" + "\n".join(f"- {error}" for error in errors))
    template = template_path.read_text(encoding="utf-8")
    html = render_catalog(template, data)
    write_atomic(output_path, html)
    return {"output": str(output_path.resolve()), "bytes": output_path.stat().st_size}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="构建 Luz Dev 功能逻辑参考 HTML。")
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        payload = build(args.data, args.template, args.output)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
