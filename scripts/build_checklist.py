#!/usr/bin/env python3
"""Validate references/dev-checklist.json and render references/dev-checklist.html (Luz Design reading page)."""

from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from luz_shell import favicon_for_tokens, inline, load_tokens, resolve_design_root, shell_css, tokens_css  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "references" / "dev-checklist.json"
OUTPUT = ROOT / "references" / "dev-checklist.html"
ITEM_ID = re.compile(r"^[A-Z]{2,3}-\d{2}$")


def validate(data: Any) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    for stage in data.get("stages", []):
        if not stage.get("id") or not stage.get("name"):
            errors.append("stage needs id and name")
        for item in stage.get("items", []):
            if len(item) != 5 or not all(str(v).strip() for v in item):
                errors.append(f"{stage.get('id')}: item must be [id, 检查项, 标准, 验证, 底座] — {item}")
                continue
            if not ITEM_ID.fullmatch(item[0]):
                errors.append(f"bad id {item[0]}")
            if item[0] in seen:
                errors.append(f"duplicate id {item[0]}")
            seen.add(item[0])
    return errors


def render(data: dict[str, Any], design_root: Path | None = None) -> str:
    tokens, source = load_tokens(design_root)
    sections = []
    for index, stage in enumerate(data["stages"], start=1):
        rows = "".join(
            "<tr>" + "".join(f'<td{" class=\"c\"" if i == 0 else ""}>{inline(v)}</td>' for i, v in enumerate(item)) + "</tr>"
            for item in stage["items"]
        )
        head = "".join(f'<th scope="col">{c}</th>' for c in ("编号", "检查项", "标准", "验证方式", "底座"))
        sections.append(f'<section id="{html.escape(stage["id"])}"><h2>{index} {html.escape(stage["name"])}</h2>'
                        f'<div class="table-wrap"><table class="dense plain"><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table></div></section>')
    toc = "".join(f'<li><a href="#{html.escape(s["id"])}">{i} {html.escape(s["name"])}</a></li>' for i, s in enumerate(data["stages"], 1))
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="luz-design-tokens" content="{html.escape(source, quote=True)}">
<title>{html.escape(data["title"])}</title>
{favicon_for_tokens("standards", tokens)}
<style>
:root{{color-scheme:light;{tokens_css(tokens)}}}
{shell_css()}</style>
</head>
<body>
<main class="doc">
<h1>{html.escape(data["title"])}</h1>
{"".join(sections)}<div class="toc"><ol>{toc}</ol></div>
</main>
</body>
</html>
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--design-root", type=Path)
    parser.add_argument("--check", action="store_true", help="只校验，不生成")
    args = parser.parse_args(argv)
    data = json.loads(DATA.read_text(encoding="utf-8"))
    errors = validate(data)
    if errors:
        print("\n".join(errors))
        return 1
    if args.check:
        print(f"OK: {sum(len(s['items']) for s in data['stages'])} items")
        return 0
    OUTPUT.write_text(render(data, args.design_root), encoding="utf-8", newline="\n")
    checker = resolve_design_root(args.design_root) / "scripts" / "check_document.py"
    if checker.is_file():
        result = subprocess.run([sys.executable, "-X", "utf8", str(checker), str(OUTPUT)], capture_output=True, text=True, encoding="utf-8")
        print(result.stdout.strip().splitlines()[0] if result.stdout.strip() else result.stderr.strip())
        if result.returncode:
            print(result.stdout)
            return 1
    print(str(OUTPUT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
