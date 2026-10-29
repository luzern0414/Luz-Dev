#!/usr/bin/env python3
"""Rule card: the rules, preferences, lessons, tools and checks for one kind of Luz Dev task, printed before any work starts.

Templates, scaffolds (assets/) and the plan generator are skeletons; the rule card is what decides how the plan,
the code, the integration and the launch are done, and which checks must pass. Every call of this skill starts here:

  python -X utf8 scripts/context.py list                        task kinds and what each covers
  python -X utf8 scripts/context.py <kind> [--topic 词 …]        print the rule card (read it fully before producing)
  python -X utf8 scripts/context.py <kind> --session <方案目录>   also save it to <方案目录>/raw/00-rules/<kind>.md
  python -X utf8 scripts/context.py check                       verify every card resolves to existing rule sections

`tech_plan.py new` saves the matching cards into the new plan's raw/00-rules/ by itself; `tech_plan.py submit`
refuses a plan whose cards are missing or out of date, or whose adoptedRules (采用的规则) list is empty.

Cards are defined in references/cards.json; rule text is extracted from the rule pages at run time, so it never drifts.
"""
from __future__ import annotations

# ---- shared rule-card core (identical in every Luz skill; keep in sync) --------------------------------------------
import argparse
import hashlib
import json
import os
import re
import sys
from datetime import date
from html.parser import HTMLParser
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
CARDS_FILE = SKILL_ROOT / "references" / "cards.json"
# LUZ_EXPERIENCE_STORE names another notes file for tests and rehearsals; the skill's own notes and catalog stay untouched.
NOTES_FILE = Path(os.environ.get("LUZ_EXPERIENCE_STORE") or SKILL_ROOT / "experience" / "notes.json")
HEADING = re.compile(r"h([1-6])$")
SKIP_TAGS = {"script", "style", "svg", "template", "noscript", "head"}
BLOCK_TAGS = {"p", "div", "section", "article", "header", "footer", "main", "figure", "figcaption", "blockquote", "pre", "dl", "dt", "dd", "details", "summary", "nav", "aside", "ul", "ol", "table", "thead", "tbody", "tfoot"}


class Node:
    __slots__ = ("tag", "attrs", "children", "text")

    def __init__(self, tag: str, attrs: dict | None = None, text: str = ""):
        self.tag, self.attrs, self.children, self.text = tag, attrs or {}, [], text


class Tree(HTMLParser):
    VOID = {"br", "hr", "img", "meta", "link", "input", "col", "source", "wbr", "area", "base"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("root")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, dict(attrs))
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        self.stack[-1].children.append(Node("#text", text=data))


def inline_text(node: Node) -> str:
    if node.tag == "#text":
        return node.text
    if node.tag in SKIP_TAGS:
        return ""
    if node.tag == "br":
        return "\n"
    inner = "".join(inline_text(c) for c in node.children)
    if node.tag in ("code", "kbd"):
        return f"`{inner.strip()}`"
    return inner


def clean(text: str) -> str:
    return re.sub(r"[ \t\r\f\v]+", " ", text).strip()


def is_navigation(node: Node) -> bool:
    """Page tables of contents and navigation are not rules; leave them out of cards."""
    classes = (node.attrs.get("class") or "").split()
    return node.tag == "nav" or any(c in ("toc", "page-toc", "contents") for c in classes) or node.attrs.get("role") == "navigation"


def render(node: Node, out: list[str], depth: int = 0) -> None:
    """Flatten an HTML subtree into compact Markdown lines."""
    for child in node.children:
        tag = child.tag
        if tag == "#text":
            text = clean(child.text)
            if text:
                out.append(text)
            continue
        if tag in SKIP_TAGS or is_navigation(child):
            continue
        m = HEADING.match(tag)
        if m:
            text = clean(inline_text(child))
            if text:
                out.append("")
                out.append("#" * min(6, int(m.group(1)) + 1) + " " + text)
            continue
        if tag == "li":
            text = clean(" ".join(clean(inline_text(c)) if c.tag not in ("ul", "ol") else "" for c in child.children))
            if text:
                out.append("  " * depth + "- " + text)
            for c in child.children:
                if c.tag in ("ul", "ol"):
                    render(c, out, depth + 1)
            continue
        if tag == "tr":
            cells = [clean(inline_text(c)).replace("|", "/") for c in child.children if c.tag in ("td", "th")]
            if any(cells):
                out.append("| " + " | ".join(cells) + " |")
            continue
        if tag == "p":
            text = clean(inline_text(child))
            if text:
                out.append(text)
            continue
        if tag == "pre":
            out.append("```")
            out.append(inline_text(child).strip("\n"))
            out.append("```")
            continue
        if tag in BLOCK_TAGS or tag in ("ul", "ol"):
            render(child, out, depth + (1 if tag in ("ul", "ol") and depth else 0))
            continue
        text = clean(inline_text(child))
        if text:
            out.append(text)


def flatten(node: Node) -> list[Node]:
    items: list[Node] = []

    def walk(n: Node) -> None:
        for c in n.children:
            items.append(c)
            if c.tag not in SKIP_TAGS and not HEADING.match(c.tag):
                walk(c)
    walk(node)
    return items


_PAGES: dict[Path, Node] = {}


def page_tree(path: Path) -> Node:
    if path not in _PAGES:
        parser = Tree()
        parser.feed(path.read_text(encoding="utf-8"))
        _PAGES[path] = parser.root
    return _PAGES[path]


def headings(path: Path) -> list[tuple[int, str, Node]]:
    """Every heading in document order with its level and text."""
    found: list[tuple[int, str, Node]] = []

    def walk(n: Node) -> None:
        for c in n.children:
            m = HEADING.match(c.tag)
            if m:
                found.append((int(m.group(1)), clean(inline_text(c)), c))
            elif c.tag not in SKIP_TAGS:
                walk(c)
    walk(page_tree(path))
    return found


def markdown_marks(lines: list[str]) -> list[tuple[int, int, str]]:
    """Headings of a Markdown file as (line, level, text); lines inside fenced code blocks are not headings."""
    marks, fence = [], None
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        f = re.match(r"^(`{3,}|~{3,})", stripped)
        if f:
            if fence is None:
                fence = f.group(1)[0]
            elif stripped.startswith(fence * 3):
                fence = None
            continue
        if fence is None and (m := re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)):
            marks.append((i, len(m.group(1)), m.group(2).strip()))
    return marks


def demote_markdown(body: str, section_level: int) -> str:
    """Nest a Markdown section under the card's ### label: its sub-headings start at ####, code blocks untouched."""
    out, fence = [], None
    for line in body.splitlines():
        stripped = line.lstrip()
        f = re.match(r"^(`{3,}|~{3,})", stripped)
        if f:
            fence = f.group(1)[0] if fence is None else (None if stripped.startswith(fence * 3) else fence)
        elif fence is None and (m := re.match(r"^(#{1,6})(\s+.*)$", line)):
            line = "#" * min(6, 3 + len(m.group(1)) - section_level) + m.group(2)
        out.append(line)
    return "\n".join(out)


def markdown_section(path: Path, heading_path: list[str]) -> str:
    """Markdown rule files: text under the heading path, up to the next heading of the same or higher level."""
    lines = path.read_text(encoding="utf-8").splitlines()
    marks = markdown_marks(lines)
    start, level = -1, 0
    for step in heading_path:
        for i, lv, text in marks:
            if i <= start:
                continue
            if level and lv <= level:
                raise KeyError(f"{path.name}: '{step}' not found under {' › '.join(heading_path)}")
            if text == step:
                start, level = i, lv
                break
        else:
            raise KeyError(f"{path.name}: heading '{step}' not found ({' › '.join(heading_path)})")
    end = next((i for i, lv, _ in marks if i > start and lv <= level), len(lines))
    body = demote_markdown("\n".join(lines[start + 1:end]), level)
    return re.sub(r"\n{3,}", "\n\n", body).strip()


def section_text(path: Path, heading_path: list[str]) -> str:
    """Text under the heading reached by heading_path (each step a descendant heading), up to the next heading of the same or higher level.
    An empty heading_path on a Markdown file means the whole file."""
    if path.suffix.lower() == ".md":
        if not heading_path:
            return demote_markdown(path.read_text(encoding="utf-8"), 0).strip()
        return markdown_section(path, heading_path)
    hs = headings(path)
    start, level = 0, 0
    for step in heading_path:
        for i in range(start + 1 if level else 0, len(hs)):
            lv, text, _ = hs[i]
            if level and lv <= level:
                raise KeyError(f"{path.name}: '{step}' not found under {' › '.join(heading_path)}")
            if text == step:
                start, level = i, lv
                break
        else:
            raise KeyError(f"{path.name}: heading '{step}' not found ({' › '.join(heading_path)})")
    target = hs[start][2]
    stop = {id(n) for lv, _, n in hs if lv <= level}

    def contains_stop(n: Node) -> bool:
        return any(id(d) in stop for d in flatten(n))

    # Document-order nodes after the target heading, up to the next heading of the same or higher level.
    # A container that also holds that next heading is not taken whole; its children come later in the order.
    order = flatten(page_tree(path))
    idx = next(i for i, n in enumerate(order) if n is target)
    seen: set[int] = set()
    top: list[Node] = []
    for n in order[idx + 1:]:
        if id(n) in stop:
            break
        if id(n) in seen or (n.tag == "#text" and not n.text.strip()):
            continue
        if contains_stop(n):
            continue
        top.append(n)
        seen.update(id(d) for d in flatten(n))
    section = Node("section")
    section.children = top
    lines: list[str] = []
    render(section, lines)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def resolve_page(spec: dict) -> Path:
    base = Path(spec["root"]) if spec.get("root") else SKILL_ROOT / "references"
    return base / spec["page"]


def card_sections(card: dict, cards: dict) -> list[dict]:
    return [*cards.get("always", []), *card.get("sections", [])]


def note_matches(note: dict, words: list[str]) -> bool:
    hay = (note.get("key", "") + " " + note.get("text", "") + " " + note.get("why", "")).lower()
    return any(w.lower() in hay for w in words if w)


def closing_lines(cards: dict, card: dict) -> list[str]:
    """Part 6 of the card. A skill may replace the wording (cards.json "closing", per card or top level) and the fact examples ("fact_examples")."""
    note = cards.get("note_command", "scripts/experience.py note --kind preference|lesson|fact --key <主题> --text <一句话>")
    # A card is often read inside another skill's session (a Luz-System session reads Luz-Design's visual cards).
    cross = (f"- 在别的技能的会话里用本卡时：本次沉淀写进会话所属技能的经验库（它的发布检查只认那里）；经验涉及本卡规则本身"
             f"（{cards.get('skill', SKILL_ROOT.name)} 的规范、检查脚本或模板）时，再在 {SKILL_ROOT} 用 {note} 写一份同 key 的经验。")
    custom = card.get("closing") or cards.get("closing")
    if custom:
        return [*custom, cross]
    facts = card.get("fact_examples") or cards.get("fact_examples") or "有效的做法与参数、调研来源与范围（哪些站点或资料有效、哪些无效、用的关键词）"
    return [
        "- 采用的规则：在 BRIEF 的“采用的规则”里逐条写出本次落实了上面哪些规则（规范页 › 小节或偏好 key → 怎么落实）；没有会话时在回复末尾用一句话说明依据的规则卡。",
        f"- 本次沉淀：每次调用至少写入或确认一条经验，命令 {note}；写进 BRIEF 的“本次沉淀”（key、类型、一句话）。要记的包括但不限于：",
        "  - preference：用户这次新说或纠正的要求；",
        "  - lesson：规则纠正、踩过的坑、检查没拦住的问题及正确做法；",
        f"  - fact：{facts}；",
        "  - 已有经验这次又用上了：用同一 key 再写一次即确认（次数加一），不新增重复条目；与旧经验冲突时用同一 key 改写，旧内容自动留作上一版。",
        "- 只记一般规律，不记一次性的任务细节；不写手机号、邮箱、个人路径、凭据和客户名称。",
        cross,
    ]


def make_card(kind: str, topic: list[str], full: bool = False) -> tuple[str, str]:
    cards = load_json(CARDS_FILE, {})
    card = cards["cards"][kind]
    skill = cards.get("skill", SKILL_ROOT.name)
    rule_parts: list[str] = []
    for spec in card_sections(card, cards):
        page = resolve_page(spec)
        label = spec.get("label") or f"{spec.get('title', page.stem)} › {' › '.join(spec['path'])}"
        if not page.is_file():
            rule_parts.append(f"### {label}\n\n（本机没有 {page}，请直接阅读：{spec.get('url', page)}）")
            continue
        body = section_text(page, spec["path"])
        limit = spec.get("max_chars")
        if limit and not full and len(body) > limit:
            body = body[:limit].rsplit("\n", 1)[0] + f"\n…（已截断，全文见 {page.name} › {' › '.join(spec['path'])}；加 --full 显示全部）"
        rule_parts.append(f"### {label}\n\n{body}")
    rules = "\n\n".join(rule_parts)
    digest = hashlib.sha256(rules.encode("utf-8")).hexdigest()[:12]
    notes = load_json(NOTES_FILE, {"notes": []}).get("notes", [])
    words = [*card.get("keywords", []), *topic]
    # Preferences are the user's standing instructions: all of them apply unless a card narrows them by keywords.
    narrow = card.get("preference_keywords")
    prefs = [n for n in notes if n.get("kind") == "preference" and (not narrow or note_matches(n, narrow + topic))]
    others = [n for n in notes if n.get("kind") in ("lesson", "fact") and note_matches(n, words)][:12]
    out = [
        f"# 规则卡：{card['title']}（{skill} · 规则 {digest}）",
        "",
        "动手前读完本卡。模板和脚本只是骨架；内容、结构、版式、字号、配色、对齐、图标、分页等一律以本卡和规范页为准，与模板冲突时按本卡改。",
        "本卡全文同时存成文件（路径见命令输出末尾，会话里在 raw/00-rules/）；终端输出被截断时，用文件读取工具分段读完整个文件，不要只看前一部分。",
        "",
        "## 1 用户偏好（必须遵守）",
        "",
        *([f"- {n['text']}（{n['key']}）" for n in prefs] or ["- 暂无"]),
        "",
        "## 2 相关教训与有效做法",
        "",
        *([f"- [{n['kind']}] {n['text']}（{n['key']}）" for n in others] or ["- 暂无匹配；做完把新发现记进经验库"]),
        "",
        "## 3 规则（摘自规范页原文）",
        "",
        rules,
        "",
        "## 4 用什么做",
        "",
        *[f"- {t}" for t in card.get("tools", [])],
        "",
        "## 5 交付前必须通过",
        "",
        *[f"- {c}" for c in card.get("checks", [])],
        "",
        "## 6 收尾（必做，发布与交付会检查）",
        "",
        *closing_lines(cards, card),
    ]
    return "\n".join(out).rstrip() + "\n", digest


def card_kinds() -> list[str]:
    return list(load_json(CARDS_FILE, {"cards": {}})["cards"])


def save_card(raw_dir: Path, kind: str, topic: list[str] | None = None) -> Path:
    """Write the rule card into <session>/raw/00-rules/<kind>.md with the hash of its rule text."""
    text, digest = make_card(kind, topic or [])
    target = Path(raw_dir) / "00-rules" / f"{kind}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"<!-- rules-sha256: {digest} · {date.today().isoformat()} -->\n" + text, encoding="utf-8")
    return target


def card_is_current(path: Path) -> bool:
    """True when a saved card still matches the rule pages it was cut from."""
    first = path.read_text(encoding="utf-8").split("\n", 1)[0]
    m = re.search(r"rules-sha256: ([0-9a-f]{12})", first)
    if not m or path.stem not in card_kinds():
        return False
    return make_card(path.stem, [])[1] == m.group(1)


def check_cards() -> list[str]:
    errors: list[str] = []
    cards = load_json(CARDS_FILE, None)
    if not cards:
        return [f"missing {CARDS_FILE}"]
    for kind, card in cards.get("cards", {}).items():
        for key in ("title", "summary", "sections", "tools", "checks"):
            if not card.get(key):
                errors.append(f"card {kind}: missing {key}")
        for spec in card_sections(card, cards):
            page = resolve_page(spec)
            if not page.is_file():
                if not spec.get("root"):
                    errors.append(f"card {kind}: page {page} not found")
                continue
            try:
                if not section_text(page, spec["path"]):
                    errors.append(f"card {kind}: empty section {page.name} › {' › '.join(spec['path'])}")
            except KeyError as exc:
                errors.append(f"card {kind}: {exc}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("kind", help="task kind (see `list`), or list / check")
    parser.add_argument("--topic", nargs="*", default=[], help="words of this task; pulls matching lessons and facts")
    parser.add_argument("--session", help="session folder; the card is also saved to raw/00-rules/<kind>.md")
    parser.add_argument("--full", action="store_true", help="do not shorten long sections")
    args = parser.parse_args(argv)
    cards = load_json(CARDS_FILE, {"cards": {}})
    if args.kind == "list":
        for kind, card in cards["cards"].items():
            print(f"{kind:<16} {card['title']} — {card.get('summary', '')}")
        return 0
    if args.kind == "check":
        errors = check_cards()
        for e in errors:
            print("ERROR", e)
        print("FAIL" if errors else "PASS", f"rule cards: {len(cards['cards'])}")
        return 1 if errors else 0
    if args.kind not in cards["cards"]:
        print(f"unknown kind '{args.kind}'. Kinds: {', '.join(cards['cards'])}", file=sys.stderr)
        return 2
    text, _ = make_card(args.kind, args.topic, args.full)
    if args.session:
        saved = save_card(Path(args.session) / "raw", args.kind, args.topic)
    else:
        import tempfile
        skill = load_json(CARDS_FILE, {}).get("skill", SKILL_ROOT.name)
        saved = Path(tempfile.gettempdir()) / "luz-rule-cards" / f"{skill}-{args.kind}.md"
        saved.parent.mkdir(parents=True, exist_ok=True)
        saved.write_text(text, encoding="utf-8")
    print(text)
    print(f"全文已保存：{saved}（约 {len(text.encode('utf-8')) // 1024} KB；输出被截断时用文件读取工具分段读完）")
    return 0
# ---- end of shared rule-card core ---------------------------------------------------------------------------------


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
