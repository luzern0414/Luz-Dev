#!/usr/bin/env python3
"""Validate references/function-catalog.json and render it as a Luz Design reading page (references/function-catalog.html)."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import secrets
import sys
from pathlib import Path
from typing import Any

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from luz_shell import chip, favicon_for_tokens, fill, inline, load_tokens, shell_css, tokens_css  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = ROOT / "references" / "function-catalog.json"
DEFAULT_TEMPLATE = ROOT / "assets" / "reference" / "function-catalog.template.html"
DEFAULT_OUTPUT = ROOT / "references" / "function-catalog.html"
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


STATUS_NAMES = {"implemented": "已实现", "mixed": "部分实现", "design": "已设计", "planned": "规划", "deprecated": "已废弃"}
STATUS_TONES = {"implemented": "success", "mixed": "info", "design": "warning", "planned": "warning", "deprecated": "danger"}


def _status(value: str) -> str:
    return chip(STATUS_NAMES.get(value, value), STATUS_TONES.get(value, "info"))


def render_result_policy(policy: dict[str, Any]) -> str:
    rows = "".join(
        f'<tr><td>{inline(rule["label"])}</td><td>{inline(rule["condition"])}</td><td>{inline(rule["surface"])}</td></tr>'
        for rule in policy.get("rules", [])
    )
    parts = [
        f'<p>{inline(policy.get("principle", ""))}</p>',
        '<div class="table-wrap"><table><thead><tr><th scope="col">落点</th><th scope="col">条件</th><th scope="col">页面落点</th></tr></thead>'
        f"<tbody>{rows}</tbody></table></div>",
    ]
    payment = policy.get("payment")
    if isinstance(payment, dict):
        items = [
            ("结果页", f'<code>{html.escape(payment.get("route", ""))}</code>'),
            ("标题", inline(" / ".join(payment.get(k, "") for k in ("processingTitle", "successTitle", "failureTitle", "unknownTitle")))),
            ("确认中提示", inline(payment.get("unknownCopy", ""))),
            ("轮询间隔", html.escape(" / ".join(str(v) for v in payment.get("pollIntervalsSeconds", []))) + " 秒"),
            ("终态", inline(" / ".join(payment.get("terminalStates", [])))),
            ("事实来源", inline(payment.get("authority", ""))),
        ]
        parts.append("<h3>支付结果</h3><dl class=\"kv\">" + "".join(f"<dt>{a}</dt><dd>{b}</dd>" for a, b in items) + "</dl>")
    return "".join(parts)


def render_function(module: dict[str, Any], function: dict[str, Any]) -> str:
    rows = "".join(f'<tr><th scope="row">{inline(row["label"])}</th><td>{inline(row["value"])}</td></tr>' for row in function["rows"])
    search = " ".join([function["id"], function["name"], module["name"], *(row["value"] for row in function["rows"])]).lower()
    return (
        f'<details class="rule fn" id="{html.escape(function["id"], quote=True)}" data-module="{html.escape(module["id"], quote=True)}" '
        f'data-search="{html.escape(search, quote=True)}"><summary><code>{html.escape(function["id"])}</code>{_status(function["status"])}'
        f'<span class="rule-title">{inline(function["name"])}</span></summary><div><div class="table-wrap"><table class="plain">'
        f'<thead><tr><th scope="col">逻辑项</th><th scope="col">当前基线</th></tr></thead><tbody>{rows}</tbody></table></div></div></details>'
    )


def render_module(module: dict[str, Any]) -> str:
    refs = "、".join(f"<code>{html.escape(ref)}</code>" for ref in module.get("sourceRefs", []))
    meta = (
        f'<dl class="kv"><dt>后端模块</dt><dd><code>{html.escape(module["backendModule"])}</code></dd>'
        f"<dt>状态</dt><dd>{_status(module['status'])}</dd><dt>对应文档</dt><dd>{refs}</dd></dl>"
    )
    functions = "".join(render_function(module, function) for function in module["functions"])
    return (
        f'<section class="module" id="m-{html.escape(module["id"], quote=True)}"><h2>{inline(module["name"])}</h2>'
        f'<p>{inline(module["description"])}</p>{meta}{functions}</section>'
    )


def render(data: dict[str, Any], template: str, design_root: Path | None = None) -> str:
    """The catalog as a Luz Design reading page: one section per module, one folding table per function."""
    tokens, token_source = load_tokens(design_root)
    options = "".join(
        f'<option value="{html.escape(m["id"], quote=True)}">{html.escape(m["name"])}</option>' for m in data["modules"]
    )
    values = {
        "{{TOKENS_CSS}}": tokens_css(tokens),
        "{{SHELL_CSS}}": shell_css(),
        "{{TOKEN_SOURCE}}": html.escape(token_source, quote=True),
        "{{FAVICON}}": favicon_for_tokens("catalog", tokens),
        "{{TITLE}}": html.escape(data["title"]),
        "{{MODULE_OPTIONS}}": options,
        "{{FUNCTION_COUNT}}": str(sum(len(m["functions"]) for m in data["modules"])),
        "{{RESULT_POLICY}}": render_result_policy(data.get("resultPolicy", {})),
        "{{MODULES}}": "".join(render_module(module) for module in data["modules"]),
    }
    return fill(template, values)


def write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(5)}.tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def build(data_path: Path, template_path: Path, output_path: Path, design_root: Path | None = None) -> dict[str, str | int]:
    try:
        data = json.loads(data_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取目录数据: {exc}") from exc
    errors = validate_catalog(data)
    if errors:
        raise ValueError("功能目录校验失败:\n" + "\n".join(f"- {error}" for error in errors))
    page = render(data, template_path.read_text(encoding="utf-8"), design_root)
    write_atomic(output_path, page)
    return {"output": str(output_path.resolve()), "bytes": output_path.stat().st_size}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="构建 Luz Dev 功能逻辑参考页（阅读页）。")
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--design-root", type=Path)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        payload = build(args.data, args.template, args.output, args.design_root)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
