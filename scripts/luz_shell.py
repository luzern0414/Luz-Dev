"""Shared Luz Design rendering helpers for Luz Dev generated pages.

Pages built here (technical plans, the standards reference) take their colour tokens
from the paired Luz Design package at build time, so a theme change in Luz Design is
picked up on the next render. The reading-page CSS lives in assets/shared/luz-reading.css.
"""

from __future__ import annotations

import html
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
SHELL_CSS = ROOT / "assets" / "shared" / "luz-reading.css"
TOKEN_SNAPSHOT = ROOT / "assets" / "tech-plan" / "tokens.snapshot.json"
THEME_ID = "ai-agent"
DESIGN_DIRNAME = "Luz-Design"  # sibling default used only after --design-root and LUZ_DESIGN_ROOT
# Luz Design has moved its theme file between releases; accept every known location.
THEME_CANDIDATES = ("references/themes.json", "design/shared/themes/themes.json", "themes.json")


def find_themes(design_root: Path) -> Path | None:
    for relative in THEME_CANDIDATES:
        candidate = design_root / relative
        if candidate.is_file():
            return candidate
    return None
HEX = re.compile(r"#[0-9A-Fa-f]{6}")
SOURCE_REF = re.compile(r"\[(S\d+)\]")
INLINE_CODE = re.compile(r"`([^`]+)`")
INLINE_BOLD = re.compile(r"\*\*([^*]+)\*\*")
INLINE_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")


def resolve_design_root(cli_value: str | Path | None = None, environ: Mapping[str, str] | None = None) -> Path:
    """Resolve Luz Design by CLI value, then LUZ_DESIGN_ROOT, then the sibling ../Luz-Design."""
    environment = os.environ if environ is None else environ
    if cli_value:
        candidate = Path(cli_value)
    elif environment.get("LUZ_DESIGN_ROOT"):
        candidate = Path(environment["LUZ_DESIGN_ROOT"])
    else:
        candidate = ROOT.parent / DESIGN_DIRNAME
    return candidate.expanduser().resolve()


def load_tokens(design_root: Path | None = None) -> tuple[dict[str, str], str]:
    """Return (tokens, provenance). Luz Design themes.json wins over the bundled snapshot."""
    snapshot = json.loads(TOKEN_SNAPSHOT.read_text(encoding="utf-8"))
    tokens = dict(snapshot["tokens"])
    root = design_root or resolve_design_root()
    themes_path = find_themes(root)
    if themes_path is None:
        return tokens, f"snapshot {snapshot.get('version', '')}（{root} 中未找到主题文件）"
    try:
        themes = json.loads(themes_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return tokens, f"snapshot {snapshot.get('version', '')}（{themes_path} 无法解析）"
    shared = themes.get("shared", {})
    theme = themes.get("themes", {}).get(THEME_ID, {})
    mapping = {
        "surface": shared.get("surface"), "ink": shared.get("ink"), "ink-2": shared.get("ink2"),
        "ink-3": shared.get("ink3"), "line": shared.get("line"),
        "success": shared.get("success"), "success-tint": shared.get("successTint"),
        "warning": shared.get("warning"), "warning-tint": shared.get("warningTint"),
        "danger": shared.get("danger"), "danger-tint": shared.get("dangerTint"),
        "info": shared.get("info"), "info-tint": shared.get("infoTint"),
        "canvas": theme.get("canvas"), "action": theme.get("action"),
        "action-press": theme.get("actionPress"), "action-tint": theme.get("actionTint"),
        "accent": theme.get("accent"), "accent-tint": theme.get("accentTint"),
    }
    for key, value in mapping.items():
        if isinstance(value, str) and HEX.fullmatch(value):
            tokens[key] = value
    return tokens, f"Luz Design {themes.get('version', '')} · {THEME_ID}"


def tokens_css(tokens: Mapping[str, str]) -> str:
    return ";".join(f"--{key}:{value}" for key, value in tokens.items())


def shell_css() -> str:
    return SHELL_CSS.read_text(encoding="utf-8")


def inline(text: Any) -> str:
    """Escape text, then allow `code`, **bold**, [label](https://…) and [S#] source references."""
    escaped = html.escape(str(text), quote=True)
    escaped = INLINE_LINK.sub(lambda m: f'<a href="{m.group(2)}" rel="noreferrer">{m.group(1)}</a>', escaped)
    escaped = INLINE_CODE.sub(lambda m: f"<code>{m.group(1)}</code>", escaped)
    escaped = INLINE_BOLD.sub(lambda m: f"<strong>{m.group(1)}</strong>", escaped)
    return SOURCE_REF.sub(lambda m: f'<a class="ref" href="#src-{m.group(1)}">[{m.group(1)}]</a>', escaped)


def chip(label: str, tone: str) -> str:
    """Status chip: text plus a dot, so state is never carried by colour alone."""
    return f'<span class="chip chip-{html.escape(tone)}">{html.escape(label)}</span>'


def render_table(columns: list[Any], rows: list[list[Any]], caption: str = "") -> str:
    head = "".join(f'<th scope="col">{inline(c)}</th>' for c in columns)
    body = "".join("<tr>" + "".join(f"<td>{inline(cell)}</td>" for cell in row) + "</tr>" for row in rows)
    cap = f"<caption>{inline(caption)}</caption>" if caption else ""
    return f'<div class="table-wrap"><table>{cap}<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def fill(template: str, values: Mapping[str, str]) -> str:
    """Replace {{TOKENS}} in one pass so injected text is never re-expanded."""
    found = set(re.findall(r"\{\{[A-Z0-9_]+\}\}", template))
    missing = sorted(found - set(values))
    if missing:
        raise RuntimeError(f"模板仍有未解析占位符: {', '.join(missing)}")
    return re.sub(r"\{\{[A-Z0-9_]+\}\}", lambda match: values[match.group(0)], template)
