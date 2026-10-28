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
from urllib.parse import quote

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


# Browser-tab icons: one flat line drawing per page kind on a rounded tint square (32x32, rx 8),
# so no generated Luz page shows a blank tab. At most three shapes, no text, legible at 16px.
FAVICON_DRAWINGS = {
    # 技术方案：蓝图文档（折角 + 两条线）
    "plan": ("M9 5.5h9.5l5 5v16H9z", "M18.5 5.5v5h5", "M13 16.5h7M13 21h5"),
    # 规范：盾牌 + 对勾
    "standards": ("M16 5l9 3.5v7c0 5.5-3.8 9.3-9 11.5-5.2-2.2-9-6-9-11.5v-7z", "M12 16l3 3 5.5-6"),
    # 知识库：摊开的书
    "knowledge": ("M16 10c-3-2-6.5-2.3-10-1.3v15c3.5-1 7-.7 10 1.3 3-2 6.5-2.3 10-1.3v-15c-3.5-1-7-.7-10 1.3z", "M16 10v15"),
    # 功能目录：四宫格
    "catalog": ("M7 7h7v7H7zM18 7h7v7h-7zM7 18h7v7H7zM18 18h7v7h-7z",),
}


def favicon_svg(kind: str, color: str, tint: str) -> str:
    """Raw SVG for a page kind; colour and tint must be #RRGGBB."""
    if kind not in FAVICON_DRAWINGS:
        raise ValueError(f"未知图标类型: {kind}")
    for value in (color, tint):
        if not HEX.fullmatch(value):
            raise ValueError(f"图标颜色必须是 #RRGGBB: {value}")
    paths = "".join(f"<path d='{d}'/>" for d in FAVICON_DRAWINGS[kind])
    return (
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
        f"<rect width='32' height='32' rx='8' fill='{tint}'/>"
        f"<g fill='none' stroke='{color}' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'>{paths}</g>"
        "</svg>"
    )


def favicon_link(kind: str, color: str, tint: str) -> str:
    """Inline <link rel="icon"> carrying a URL-encoded SVG data URI (no external request)."""
    encoded = quote(favicon_svg(kind, color, tint), safe=" '=/:,.-")
    return f'<link rel="icon" href="data:image/svg+xml,{encoded}">'


def favicon_for_tokens(kind: str, tokens: Mapping[str, str]) -> str:
    """Icon in the page theme: action colour on action tint, else the bundled snapshot colours."""
    color, tint = str(tokens.get("action", "")), str(tokens.get("action-tint", ""))
    if not (HEX.fullmatch(color) and HEX.fullmatch(tint)):
        snapshot = json.loads(TOKEN_SNAPSHOT.read_text(encoding="utf-8"))["tokens"]
        color, tint = snapshot["action"], snapshot["action-tint"]
    return favicon_link(kind, color, tint)


def fill(template: str, values: Mapping[str, str]) -> str:
    """Replace {{TOKENS}} in one pass so injected text is never re-expanded."""
    found = set(re.findall(r"\{\{[A-Z0-9_]+\}\}", template))
    missing = sorted(found - set(values))
    if missing:
        raise RuntimeError(f"模板仍有未解析占位符: {', '.join(missing)}")
    return re.sub(r"\{\{[A-Z0-9_]+\}\}", lambda match: values[match.group(0)], template)
