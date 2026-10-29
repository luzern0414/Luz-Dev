#!/usr/bin/env python3
"""Luz Dev technical plans: data-first plans, research evidence, review and approval gates.

Every plan is two files that always move together:

* ``<output>/NN-主题/NN-主题.html``   the reviewable page, rendered with Luz Design tokens
* ``<output>/NN-主题/raw/plan.json``   the canonical plan data the agent edits; research inputs,
  logs and design intake results also live in ``raw/``

Lifecycle: ``draft`` → ``in-review`` → ``approved`` (→ ``revising`` → ``in-review`` …).
Development may start only when ``gate`` passes: the plan is approved and its
content still matches the hash recorded at approval.

Every plan works from rule cards (``scripts/context.py``; saved to ``raw/00-rules/`` by ``new``) and records
``adoptedRules`` (采用的规则, ``rule``) and ``sediment`` (本次沉淀, ``sediment``: experience notes written or
confirmed in this call). ``submit`` and ``check --stage delivery`` refuse a plan without them.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import html
import json
import os
import re
import secrets
import subprocess
import sys
import time
import unicodedata
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Mapping
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from luz_shell import chip, favicon_for_tokens, fill, inline, load_tokens, render_table, resolve_design_root, shell_css, tokens_css  # noqa: E402
import context as rule_cards  # noqa: E402  - rule cards (references/cards.json), shared core of every Luz skill
import knowledge as kb  # noqa: E402
import module_specs as ms  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = Path(r"D:\Codes\skill输出\Luz-Dev")
DEFAULT_CRAWL_ROOT = Path(r"D:\Codes\Luz-Crawl")
DEFAULT_CRAWL_OUTPUT = Path(r"D:\Codes\skill输出\Luz-Crawl")
RAW_DIRNAME = "raw"
RULES_DIRNAME = "00-rules"  # raw/00-rules/<kind>.md: the rule cards this plan works from (scripts/context.py)
LEGACY_DATA_DIRNAME = "_data"  # layout before 2026-10-26 evening; migrate with `migrate-layout`
TEMPLATE = ROOT / "assets" / "tech-plan" / "template.html"
STANDARDS = ROOT / "references" / "standards.json"
STANDARD_MODULES = ROOT / "references" / "standard-modules.json"
RESEARCH_SOURCES = ROOT / "references" / "research-sources.json"
CATALOG = ROOT / "references" / "function-catalog.json"
# LUZ_EXPERIENCE_STORE names another notes file for tests and rehearsals; the skill's own notes and catalog stay untouched.
NOTES_FILE = Path(os.environ.get("LUZ_EXPERIENCE_STORE") or ROOT / "experience" / "notes.json")  # record_experience.py note writes here; 本次沉淀 keys are checked against it
SCHEMA = "luz-dev-plan/2"
CRAWL_BATCH = 30
LOGIN_GATED_PLATFORMS = {"zhihu", "xhs", "xiaohongshu", "douyin", "weibo"}  # often stop for a login or verification
SPECLESS_MODULES = {"deploy", "compliance", "license", "design-integration", "cache", "agent"}  # covered by other sections

PLAN_NAME = re.compile(r"^(\d+)-.+$")
UNSAFE_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
MULTIPLE_HYPHENS = re.compile(r"-{2,}")
SPACE = re.compile(r"\s+")
LEADING_SEQUENCE = re.compile(r"^\d+\s*[-_.、]\s*")
SOURCE_REF = re.compile(r"\[(S\d+)\]")
PLACEHOLDER_TEXT = ("待填写", "TODO", "TBD", "{{")

TASK_MODES = ("implementation", "review", "diagnosis", "analysis", "operations")
PROJECT_TYPES = ("miniprogram", "admin", "mobile", "backend", "agent")
STATUSES = ("draft", "in-review", "approved", "revising")
STATUS_LABELS = {
    "draft": ("草稿", "warning"),
    "in-review": ("待审核", "info"),
    "approved": ("已批准", "success"),
    "revising": ("修订中，需重新审批", "warning"),
}
CHECK_STATUSES = ("open", "planned", "done", "n/a")
CHECK_LABELS = {
    "open": ("未处理", "danger"),
    "planned": ("已纳入方案", "info"),
    "done": ("已实现并验证", "success"),
    "n/a": ("不适用", "neutral"),
}
TIER_LABELS = {
    "official": ("官方", "success"),
    "standard": ("法规/标准", "success"),
    "opensource": ("开源", "info"),
    "community": ("社区", "neutral"),
    "search": ("搜索候选", "warning"),
}
DECISION_STATUSES = ("open", "decided", "deferred")

# ---------------------------------------------------------------- sections

SECTION_LIBRARY: dict[str, dict[str, Any]] = {
    "overview": {
        "nav": "结论",
        "title": "任务、结论与范围",
        "todo": [
            "一句话结论：本方案推荐怎么做，用户能得到什么可验证的结果。",
            "范围与非目标：逐条列出做什么、明确不做什么。",
            "输入：项目简报、HANDOFF.json、现有代码与环境、已知约束。",
        ],
    },
    "research": {
        "nav": "调研",
        "title": "调研结论",
        "todo": ["关键结论：每条结论用 [S#] 引用来源；官方文档优先，社区内容只作参考。"],
    },
    "findings": {
        "nav": "发现",
        "title": "发现与结论",
        "todo": ["按严重程度列出发现；每条写明证据位置、影响和建议。"],
    },
    "solution": {
        "nav": "选型",
        "title": "技术选型与工程结论",
        "todo": ["推荐方案、选择理由、替代方案与未采用原因（表格）。"],
    },
    "architecture": {
        "nav": "架构",
        "title": "系统架构",
        "todo": ["部署单元、模块边界、同步调用链、异步副作用、信任边界；建议附架构图（svg 块）。"],
    },
    "design": {
        "nav": "前端",
        "title": "页面与前端接入",
        "todo": [
            "前端由 Luz System 负责：用 tech_plan.py design NN --session <会话目录> 登记已批准的设计会话；尚无设计时先用 $luz-system 完成设计与批准。",
            "接入方式：绑定表位置（packages/contracts/design-bindings.json）、前端工程与技术栈、令牌来源、结果页落点（references/design-integration.md、result-patterns.md）。",
        ],
    },
    "launch": {
        "nav": "上线",
        "title": "上线、运维与合规",
        "todo": [
            "资质与备案表：事项 | 是否适用及理由 | 负责方 | 进度 | 证据 | 对排期的影响（references/compliance.md）。",
            "协议清单：用户协议、隐私政策、隐私保护指引、专项同意（人脸、储值）、退款与活动规则；起草方式与商户确认人（references/legal-documents.md）。",
            "环境与部署：服务器、域名与 HTTPS、发布流程、监控告警、备份恢复、回滚（references/operations.md）。",
            "上线检查表与发布后观察计划（assets/deploy/LAUNCH-CHECKLIST.md）。",
        ],
    },
    "routes": {
        "nav": "接口",
        "title": "接口约定",
        "todo": ["接口表：方法 | 路由 | 身份/权限 | 输入输出 | 错误码与幂等。"],
        "specDriven": True,
    },
    "data": {
        "nav": "数据",
        "title": "数据库约定",
        "todo": ["实体与关键字段、唯一约束、并发控制、金额单位、迁移与回滚。"],
        "specDriven": True,
    },
    "modules": {
        "nav": "模块",
        "title": "业务模块总览",
        "todo": ["每个模块：职责、状态机（状态 | 触发 | 守卫 | 副作用）、成功与失败后的逻辑。"],
        "specDriven": True,
    },
    "features": {
        "nav": "功能",
        "title": "系统功能：基础功能 + 定制功能",
        "todo": [],
        "generated": True,
    },
    "security": {
        "nav": "安全",
        "title": "安全与规范",
        "todo": [],
        "generated": True,
    },
    "testing-delivery": {
        "nav": "验证",
        "title": "测试、发布与回滚",
        "todo": ["测试矩阵（正常、异常、重复、并发、越权、依赖故障）、发布步骤、回滚方式。"],
    },
    "reusable": {
        "nav": "复用",
        "title": "复用的实现",
        "todo": ["引用的 verified 经验与功能目录条目；本次预计新增的经验候选。"],
    },
    "decisions": {
        "nav": "待决",
        "title": "待决问题",
        "todo": [],
        "generated": True,
    },
    "revisions": {
        "nav": "审批",
        "title": "审批与修订记录",
        "todo": [],
        "generated": True,
    },
}

MODE_SECTIONS: dict[str, tuple[str, ...]] = {
    "implementation": (
        "overview", "features", "research", "solution", "architecture", "modules", "data", "routes",
        "design", "security", "launch", "testing-delivery", "reusable", "decisions", "revisions",
    ),
    "operations": (
        "overview", "research", "architecture", "security", "launch", "testing-delivery", "decisions", "revisions",
    ),
    "diagnosis": (
        "overview", "research", "findings", "solution", "testing-delivery", "reusable", "decisions", "revisions",
    ),
    "review": ("overview", "research", "findings", "security", "reusable", "decisions", "revisions"),
    "analysis": ("overview", "research", "findings", "solution", "reusable", "decisions", "revisions"),
}
FRONTEND_TYPES = {"miniprogram", "admin", "mobile"}
# Modes whose plan must be approved by the user before execution.
APPROVAL_MODES = {"implementation", "operations"}
# Modes whose research cannot be skipped without explicit user consent.
STRICT_RESEARCH_MODES = {"implementation"}

DEFAULT_SOLUTION_TABLE = {
    "type": "table",
    "columns": ["领域", "推荐方案", "选择理由与边界"],
    "rows": [
        ["后端框架", "FastAPI + Pydantic", "契约清晰、适合 OpenAPI 与异步 I/O；已有项目以项目技术栈为准。"],
        ["数据与缓存", "PostgreSQL + Redis", "PostgreSQL 为事实来源；Redis 只承担可丢失或可重建的数据。"],
        ["迁移与部署", "Alembic + 容器化部署", "迁移可审计，环境可复现。"],
    ],
}

# ---------------------------------------------------------------- helpers


class PlanError(Exception):
    """A plan command failed for a reason the user or agent must fix."""


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def load_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        if default is not None:
            return copy.deepcopy(default)
        raise PlanError(f"文件不存在: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def clean_topic(raw: str) -> str:
    topic = unicodedata.normalize("NFKC", raw).strip()
    if not topic:
        raise ValueError("主题不能为空")
    if len(topic) > 160:
        raise ValueError("主题不能超过 160 个字符")
    topic = SPACE.sub(" ", topic)
    topic = LEADING_SEQUENCE.sub("", topic)
    topic = UNSAFE_FILENAME.sub("-", topic)
    topic = SPACE.sub("-", topic)
    topic = MULTIPLE_HYPHENS.sub("-", topic).strip(" .-_")
    if not topic or topic in {".", ".."}:
        raise ValueError("主题清洗后为空")
    if len(topic) > 100:
        topic = topic[:100].rstrip(" .-_")
    return topic


def next_sequence(output_root: Path) -> int:
    """Next number across project folders and older flat plan files, so numbers never collide."""
    maximum = 0
    for item in output_root.iterdir():
        if item.is_dir() or item.suffix.lower() == ".html":
            match = PLAN_NAME.fullmatch(item.stem if item.is_file() else item.name)
            if match:
                maximum = max(maximum, int(match.group(1)))
    return maximum + 1


@contextmanager
def exclusive_lock(output_root: Path, timeout: float = 20.0) -> Iterator[None]:
    lock_path = output_root / ".create-tech-plan.lock"
    deadline = time.monotonic() + timeout
    descriptor: int | None = None
    while descriptor is None:
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(descriptor, f"{os.getpid()} {time.time():.3f}\n".encode("ascii"))
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TimeoutError("等待方案编号锁超时；请检查是否有未结束的生成进程")
            time.sleep(0.05)
    try:
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def split_list(values: list[str] | None) -> list[str]:
    result: list[str] = []
    for value in values or []:
        for part in re.split(r"[,，\s]+", value):
            part = part.strip()
            if part and part not in result:
                result.append(part)
    return result


def resolve_crawl_root(cli_value: str | Path | None = None) -> Path:
    if cli_value:
        return Path(cli_value).expanduser().resolve()
    if os.environ.get("LUZ_CRAWL_ROOT"):
        return Path(os.environ["LUZ_CRAWL_ROOT"]).expanduser().resolve()
    sibling = ROOT.parent / "Luz-Crawl"
    return sibling.resolve() if sibling.is_dir() else DEFAULT_CRAWL_ROOT


# ---------------------------------------------------------------- reference data


def load_standard_modules() -> list[dict[str, Any]]:
    """The backend standard-module catalog: plans reference these instead of copying their design."""
    if not STANDARD_MODULES.is_file():
        return []
    return json.loads(STANDARD_MODULES.read_text(encoding="utf-8")).get("modules", [])


def standard_entries(plan_modules: list[str]) -> list[dict[str, Any]]:
    wanted = set(plan_modules)
    return [m for m in load_standard_modules() if wanted.intersection(m.get("planModules", []))]


def sync_standard_modules(plan: dict[str, Any]) -> list[str]:
    """Add a standardModules entry for every catalog module the plan uses; existing entries keep their values."""
    chosen = plan.setdefault("standardModules", [])
    have = {item["id"] for item in chosen}
    added = []
    for entry in standard_entries(plan.get("modules", [])):
        if entry["id"] not in have:
            chosen.append({"id": entry["id"], "params": {}, "deviations": []})
            added.append(entry["id"])
    order = {m["id"]: i for i, m in enumerate(load_standard_modules())}
    chosen.sort(key=lambda item: order.get(item["id"], 999))
    return added


def standard_spec_ids(plan: Mapping[str, Any]) -> set[str]:
    """Module-spec ids replaced by a standard module (same id in both catalogs)."""
    return {item["id"] for item in plan.get("standardModules") or []}


def load_standards() -> dict[str, Any]:
    return load_json(STANDARDS, {"rules": [], "baselines": [], "domains": [], "modules": []})


def load_research_sources() -> dict[str, Any]:
    return load_json(RESEARCH_SOURCES, {"tiers": {}, "modules": {}, "topicQueries": []})


def rule_applies(rule: Mapping[str, Any], project_types: list[str], modules: list[str]) -> bool:
    platforms = set(rule.get("platforms") or ["all"])
    if "all" not in platforms and not platforms.intersection(project_types):
        return False
    rule_modules = set(rule.get("modules") or [])
    return not rule_modules or bool(rule_modules.intersection(modules))


def baseline_applies(item: Mapping[str, Any], project_types: list[str]) -> bool:
    return item.get("projectType") in project_types


def derive_checklist(plan: dict[str, Any]) -> None:
    """Insert applicable standards and baselines, preserving statuses already recorded."""
    if plan["mode"] not in APPROVAL_MODES:
        plan.setdefault("checklist", {"rules": [], "baselines": []})
        return
    standards = load_standards()
    project_types = plan.get("projectTypes", [])
    modules = plan.get("modules", [])
    checklist = plan.setdefault("checklist", {"rules": [], "baselines": []})
    existing_rules = {item["id"]: item for item in checklist.get("rules", [])}
    rules = []
    for rule in standards.get("rules", []):
        if rule_applies(rule, project_types, modules):
            entry = existing_rules.pop(rule["id"], {"id": rule["id"], "status": "open", "note": "", "evidence": []})
            rules.append(entry)
    # Keep manually tracked rules that no longer derive, so a decision is never silently dropped.
    for leftover in existing_rules.values():
        if leftover.get("status") != "open":
            leftover["detached"] = True
            rules.append(leftover)
    checklist["rules"] = rules

    existing_baselines = {item["id"]: item for item in checklist.get("baselines", [])}
    baselines = []
    for item in standards.get("baselines", []):
        if baseline_applies(item, project_types):
            baselines.append(
                existing_baselines.pop(item["id"], {"id": item["id"], "status": "open", "note": "", "evidence": []})
            )
    for leftover in existing_baselines.values():
        if leftover.get("status") != "open":
            leftover["detached"] = True
            baselines.append(leftover)
    checklist["baselines"] = baselines


def classify_tier(url: str, platform: str = "") -> str:
    sources = load_research_sources()
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host:
        return "community"
    path = parsed.path or "/"
    for tier in ("official", "standard", "opensource", "community"):
        for entry in sources.get("tiers", {}).get(tier, []):
            domain, _, prefix = entry.lower().partition("/")
            if (host == domain or host.endswith("." + domain)) and path.lower().startswith("/" + prefix):
                return tier
    if platform in {"github", "gitee"}:
        return "opensource"
    return "community"


# ---------------------------------------------------------------- plan files


def plan_paths(output_root: Path, stem: str) -> tuple[Path, Path]:
    """<output>/NN-主题/NN-主题.html and <output>/NN-主题/raw/plan.json"""
    folder = output_root / stem
    return folder / f"{stem}.html", folder / RAW_DIRNAME / "plan.json"


def html_for(data_path: Path) -> Path:
    folder = data_path.parent.parent
    return folder / f"{folder.name}.html"


def find_plan(reference: str, output_root: Path) -> tuple[Path, Path]:
    """Resolve '05', a project folder, its HTML, or its raw/plan.json to (html, json)."""
    candidate = Path(reference)
    if candidate.suffix.lower() == ".json" and candidate.is_file():
        return html_for(candidate), candidate
    if candidate.suffix.lower() == ".html":
        data_path = candidate.parent / RAW_DIRNAME / "plan.json"
        if not data_path.is_file():
            raise PlanError(f"{candidate.name} 没有 raw/plan.json；旧版方案请运行 migrate-layout 或 adopt")
        return candidate, data_path
    if candidate.is_dir() and (candidate / RAW_DIRNAME / "plan.json").is_file():
        return html_for(candidate / RAW_DIRNAME / "plan.json"), candidate / RAW_DIRNAME / "plan.json"
    if reference.isdigit():
        number = f"{int(reference):02d}"
        matches = sorted(output_root.glob(f"{number}-*/{RAW_DIRNAME}/plan.json")) if output_root.is_dir() else []
        if len(matches) == 1:
            return html_for(matches[0]), matches[0]
        if not matches:
            if (output_root / LEGACY_DATA_DIRNAME).is_dir() and list((output_root / LEGACY_DATA_DIRNAME).glob(f"{number}-*/plan.json")):
                raise PlanError(f"编号 {number} 是旧目录结构；先运行 tech_plan.py migrate-layout")
            if list(output_root.glob(f"{number}-*.html")):
                raise PlanError(f"编号 {number} 是旧版 HTML 方案（无数据）；运行 migrate-layout 后用 adopt 转换")
            raise PlanError(f"找不到编号 {number} 的方案")
        raise PlanError(f"编号 {number} 对应多个方案: {[m.parent.parent.name for m in matches]}")
    raise PlanError(f"无法识别方案引用: {reference}")


def scaffold_sections(mode: str) -> list[dict[str, Any]]:
    sections = []
    for section_id in MODE_SECTIONS[mode]:
        spec = SECTION_LIBRARY[section_id]
        blocks: list[dict[str, Any]] = [{"type": "todo", "text": text} for text in spec["todo"]]
        if section_id == "solution" and mode == "implementation":
            blocks.insert(0, copy.deepcopy(DEFAULT_SOLUTION_TABLE))
        sections.append({"id": section_id, "nav": spec["nav"], "title": spec["title"], "blocks": blocks})
    return sections


def expand_archetypes(archetypes: list[str], modules: list[str]) -> list[str]:
    """Validate archetype IDs and add their modules after the explicitly requested ones."""
    known = load_research_sources().get("systemArchetypes", {})
    unknown = [item for item in archetypes if item not in known]
    if unknown:
        raise ValueError(f"未知系统原型: {unknown}；可选 {sorted(known)}")
    result = list(modules)
    for archetype in archetypes:
        for module in known[archetype].get("modules", []):
            if module not in result:
                result.append(module)
    return result


def module_reading_list(modules: list[str]) -> dict[str, Any] | None:
    """Callout block naming the pattern documents for the plan's modules."""
    known = {item["id"]: item for item in load_standards().get("modules", [])}
    docs: dict[str, list[str]] = {}
    for module in modules:
        path = known.get(module, {}).get("pattern")
        if path:
            docs.setdefault(path, []).append(known[module].get("name", module))
    if not docs:
        return None
    items = [f"`{path}`（{'、'.join(names)}）" for path, names in docs.items()]
    return {"type": "callout", "tone": "info", "title": "先读业务逻辑模式：",
            "text": "；".join(items) + "。方案中的状态机、并发控制与补偿在这些默认做法上按项目调整，并写明差异。"}


def card_kinds_for(mode: str, project_types: list[str], chosen: list[str] | None = None) -> list[str]:
    """Rule cards a plan works from: tech-plan always, then the --card choices or the defaults of the mode and project types."""
    kinds = ["tech-plan"]
    if chosen:
        kinds += split_list(chosen)
    else:
        types = set(project_types)
        if mode == "implementation":
            if not types or types & ({"backend"} | FRONTEND_TYPES):
                kinds.append("backend-module")
            if types & FRONTEND_TYPES:
                kinds.append("frontend-integration")
            if "agent" in types:
                kinds += ["agent-service", "agent-runtime"]
            kinds.append("launch-compliance")
        elif mode == "operations":
            kinds += ["launch-compliance", "review-diagnose"]
        else:
            kinds.append("review-diagnose")
    kinds = list(dict.fromkeys(kinds))
    known = rule_cards.card_kinds()
    unknown = [kind for kind in kinds if kind not in known]
    if unknown:
        raise ValueError(f"未知规则卡: {unknown}；可选 {known}（python -X utf8 scripts/context.py list）")
    return kinds


def write_rule_cards(plan: Mapping[str, Any], raw_dir: Path) -> tuple[list[str], list[str]]:
    """Save the plan's rule cards to raw/00-rules/<kind>.md (rule text hash on the first line) for the agent to read."""
    saved: list[str] = []
    problems: list[str] = []
    for kind in plan.get("ruleCards") or []:
        try:
            saved.append(str(rule_cards.save_card(raw_dir, kind, list(plan.get("modules") or []))))
        except (KeyError, OSError, ValueError) as exc:
            problems.append(f"规则卡 {kind} 没有生成：{exc}；运行 python -X utf8 scripts/context.py check 修好 references/cards.json，"
                            f"再运行 python -X utf8 scripts/context.py {kind} --session <方案目录>")
    return saved, problems


def adopted_rules(plan: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Filled rows of 采用的规则 (plan.json → adoptedRules): the rule-card rule or preference key, and how this plan applies it."""
    rows = []
    for item in plan.get("adoptedRules") or []:
        if isinstance(item, Mapping):
            rule, how = str(item.get("rule", "")).strip(), str(item.get("how", "")).strip()
            if rule and how:
                rows.append((rule, how))
    return rows


def rule_card_problems(plan: Mapping[str, Any], data_path: Path) -> list[str]:
    """A plan works from current rule cards and records the rules it applied; submit and delivery refuse otherwise."""
    folder = data_path.parent.parent
    rules_dir = data_path.parent / RULES_DIRNAME
    number = plan.get("number") or "NN"

    def command(kind: str) -> str:
        return f'python -X utf8 scripts/context.py {kind} --session "{folder}"'

    errors: list[str] = []
    cards = sorted(rules_dir.glob("*.md")) if rules_dir.is_dir() else []
    wanted = list(plan.get("ruleCards") or [])
    present = {card.stem for card in cards}
    if not cards and not wanted:
        errors.append(f"缺少规则卡 {RAW_DIRNAME}/{RULES_DIRNAME}/：运行 {command('tech-plan')}（其他种类见 context.py list），读完再写方案")
    for kind in wanted:
        if kind not in present:
            errors.append(f"缺少规则卡 {RAW_DIRNAME}/{RULES_DIRNAME}/{kind}.md：运行 {command(kind)}，读完再写方案")
    known = set(rule_cards.card_kinds())
    for card in cards:
        if card.stem not in known:
            continue
        try:
            current = rule_cards.card_is_current(card)
        except (KeyError, OSError) as exc:
            errors.append(f"规则卡 {card.stem} 无法核对：{exc}；运行 python -X utf8 scripts/context.py check")
            continue
        if not current:
            errors.append(f"规则卡 {RAW_DIRNAME}/{RULES_DIRNAME}/{card.name} 已过期（规范页改过）：运行 {command(card.stem)} 重新生成，"
                          "读变化的规则并复核方案")
    if not adopted_rules(plan):
        errors.append(f"采用的规则为空：逐条写出本方案落实了规则卡上的哪些规则或偏好——python -X utf8 scripts/tech_plan.py rule {number} "
                      '--rule "<规则卡第 3 部分的小节标签 或 第 1 部分括号里的偏好 key>" --how "<本次怎么落实>"')
    return errors


def load_notes() -> dict[str, Mapping[str, Any]]:
    """experience/notes.json by key; an unreadable store counts as empty."""
    try:
        doc = json.loads(NOTES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(n.get("key")): n for n in doc.get("notes", []) if isinstance(n, Mapping)}


def sediment_keys(plan: Mapping[str, Any]) -> list[str]:
    """Keys listed in 本次沉淀 (plan.json → sediment)."""
    keys = []
    for item in plan.get("sediment") or []:
        key = str(item.get("key", "") if isinstance(item, Mapping) else item).strip().strip("`").strip()
        if key and key not in keys:
            keys.append(key)
    return keys


def sediment_problems(plan: Mapping[str, Any], stage: str = "review") -> list[str]:
    """Every call leaves experience behind (本次沉淀).

    Submit: at least one note key, each in experience/notes.json and written or confirmed on or after the plan's creation date.
    Delivery: the build and launch leave their own note too — at least one key confirmed on or after the approval date.
    """
    if os.environ.get("LUZ_EXPERIENCE_STORE"):
        print(f"注意：LUZ_EXPERIENCE_STORE 已设置，本次沉淀按 {NOTES_FILE} 核对，经验不会进入技能（只用于测试和演练，正式交付不要设置）", file=sys.stderr)
    number = plan.get("number") or "NN"
    record = (f'python -X utf8 scripts/record_experience.py note --kind preference|lesson|fact --key <主题> --text "<一句话原则>"，'
              f"再 python -X utf8 scripts/tech_plan.py sediment {number} --key <主题>")
    keys = sediment_keys(plan)
    if not keys:
        return [f"本次沉淀为空：每次调用至少写入或确认一条经验（用户新偏好、规则纠正与踩坑、有效做法、调研来源与范围、参数）——{record}"]
    notes = load_notes()
    since = str(plan.get("createdAt", ""))[:10]
    errors = []
    for key in keys:
        note = notes.get(key)
        if note is None:
            errors.append(f"本次沉淀的 key「{key}」不在 experience/notes.json：先用 python -X utf8 scripts/record_experience.py note --key {key} … 写入")
        elif since and str(note.get("last_confirmed", "")) < since:
            errors.append(f"本次沉淀的 key「{key}」最近确认于 {note.get('last_confirmed')}，早于本方案创建日 {since}："
                          f"用同一 key 再运行一次 record_experience.py note 确认或改写")
    approved = str((plan.get("approval") or {}).get("at", ""))[:10]
    if stage == "delivery" and not errors and approved:
        if not any(str(notes[key].get("last_confirmed", "")) >= approved for key in keys):
            errors.append(f"交付前还要沉淀开发与上线阶段的经验：至少一条在批准日 {approved} 当天或之后写入或确认的经验——{record}")
    return errors


def new_plan_data(
    *, number: str, topic: str, mode: str, summary: str, project_types: list[str], modules: list[str],
    output_root: Path, archetypes: list[str] | None = None, cards: list[str] | None = None,
) -> dict[str, Any]:
    created = now_iso()
    archetypes = list(archetypes or [])
    modules = expand_archetypes(archetypes, modules)
    plan: dict[str, Any] = {
        "schema": SCHEMA,
        "number": number,
        "topic": topic,
        "mode": mode,
        "summary": summary,
        "createdAt": created,
        "updatedAt": created,
        "outputRoot": str(output_root),
        "projectTypes": project_types,
        "archetypes": archetypes,
        "modules": modules,
        "status": "draft",
        "sections": scaffold_sections(mode),
        "research": {
            "required": True,
            "strict": mode in STRICT_RESEARCH_MODES,
            "links": [],
            "queries": [],
            "collections": [],
            "sources": [],
            "skip": None,
        },
        "checklist": {"rules": [], "baselines": []},
        "customFeatures": [],
        "standardModules": [],
        "moduleSpecs": [],
        "decisions": [],
        # Process records, kept out of the page and out of the approval hash (references/cards.json, experience/notes.json):
        # ruleCards — raw/00-rules/<kind>.md this plan works from; adoptedRules (采用的规则) — {rule, how};
        # sediment (本次沉淀) — {key, kind, text, recordedAt} of notes written or confirmed with record_experience.py note.
        "ruleCards": card_kinds_for(mode, project_types, cards),
        "adoptedRules": [],
        "sediment": [],
        "approval": None,
        "revisions": [{"at": created, "version": "0.1", "change": "由 Luz Dev 方案生成器创建草稿。", "basis": "用户请求"}],
    }
    if mode == "implementation":
        sync_standard_modules(plan)
        replaced = standard_spec_ids(plan)
        plan["moduleSpecs"] = [spec for spec in ms.specs_for(modules) if spec["id"] not in replaced]
    try:
        apply_knowledge(plan)
    except (OSError, ValueError):
        pass  # knowledge base unreadable: research will fetch everything
    if plan["moduleSpecs"] or plan["standardModules"]:
        for section in plan["sections"]:
            if SECTION_LIBRARY[section["id"]].get("specDriven"):
                section["blocks"] = [b for b in section["blocks"] if b.get("type") != "todo"]
    reading = module_reading_list(modules)
    if reading and not plan["moduleSpecs"]:
        for section in plan["sections"]:
            if section["id"] in ("modules", "findings"):
                section["blocks"].insert(0, reading)
                break
    derive_checklist(plan)
    return plan


def save_plan(plan: dict[str, Any], html_path: Path, data_path: Path, design_root: Path | None = None) -> None:
    plan["updatedAt"] = now_iso()
    atomic_write(data_path, json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
    atomic_write(html_path, render_plan(plan, design_root=design_root))


def create_plan(
    topic_input: str,
    mode: str,
    output_root: Path,
    summary: str,
    project_types: list[str] | None = None,
    modules: list[str] | None = None,
    design_root: Path | None = None,
    archetypes: list[str] | None = None,
    cards: list[str] | None = None,
) -> dict[str, Any]:
    if mode not in TASK_MODES:
        raise ValueError(f"未知任务类型: {mode}")
    topic = clean_topic(topic_input)
    project_types = split_list(project_types)
    unknown_types = [item for item in project_types if item not in PROJECT_TYPES]
    if unknown_types:
        raise ValueError(f"未知项目类型: {unknown_types}；可选 {list(PROJECT_TYPES)}")
    modules = split_list(modules)
    archetypes = split_list(archetypes)
    cards = card_kinds_for(mode, project_types, cards)  # unknown kinds fail before a folder is reserved
    output_root.expanduser().mkdir(parents=True, exist_ok=True)
    output_root = output_root.resolve()
    with exclusive_lock(output_root):
        while True:
            sequence = f"{next_sequence(output_root):02d}"
            stem = f"{sequence}-{topic}"
            html_path, data_path = plan_paths(output_root, stem)
            try:
                html_path.parent.mkdir()  # the project folder doubles as the number reservation
            except FileExistsError:
                continue
            data_path.parent.mkdir()
            plan = new_plan_data(
                number=sequence, topic=topic, mode=mode, summary=summary,
                project_types=project_types, modules=modules, output_root=output_root, archetypes=archetypes,
                cards=cards,
            )
            content = render_plan(plan, design_root=design_root)
            atomic_write(html_path, content)
            atomic_write(data_path, json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
            saved, problems = write_rule_cards(plan, data_path.parent)
            return {
                "sequence": sequence,
                "path": str(html_path),
                "folder": str(html_path.parent),
                "data": str(data_path),
                "bytes": html_path.stat().st_size,
                "status": plan["status"],
                "modules": plan["modules"],
                "researchTracks": list(research_tracks(plan)),
                "rules": len(plan["checklist"]["rules"]),
                "baselines": len(plan["checklist"]["baselines"]),
                "ruleCards": saved,
                **({"ruleCardErrors": problems} if problems else {}),
                "next": "先读完 ruleCards 里的每张规则卡再调研和写方案；采用的规则用 tech_plan.py rule 记录；"
                        "结束前用 record_experience.py note 写经验，再用 tech_plan.py sediment 记进本次沉淀",
            }


# ---------------------------------------------------------------- hashing & checks


def content_hash(plan: Mapping[str, Any]) -> str:
    """Hash the decisions a reviewer approved; delivery evidence does not change it."""
    checklist = plan.get("checklist", {})

    def normalize(items: list[Mapping[str, Any]]) -> list[list[str]]:
        result = []
        for item in items:
            status = "planned" if item.get("status") == "done" else str(item.get("status"))
            result.append([str(item.get("id")), status, str(item.get("note", ""))])
        return sorted(result)

    payload = {
        "topic": plan.get("topic"),
        "mode": plan.get("mode"),
        "summary": plan.get("summary"),
        "projectTypes": plan.get("projectTypes"),
        "modules": plan.get("modules"),
        "sections": plan.get("sections"),
        "sources": [[s.get("id"), s.get("url")] for s in plan.get("research", {}).get("sources", [])],
        "decisions": plan.get("decisions"),
        "customFeatures": plan.get("customFeatures"),
        "moduleSpecs": plan.get("moduleSpecs") or [],
        "standardModules": plan.get("standardModules") or [],
        "rules": normalize(checklist.get("rules", [])),
        "baselines": normalize(checklist.get("baselines", [])),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def iter_texts(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from iter_texts(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from iter_texts(item)


def research_requirements(plan: Mapping[str, Any]) -> dict[str, Any]:
    sources = load_research_sources()
    thresholds = sources.get("thresholds", {})
    mode_threshold = thresholds.get(plan.get("mode"), thresholds.get("default", {}))
    official_modules = [
        module for module in plan.get("modules", [])
        if sources.get("modules", {}).get(module, {}).get("officialRequired")
    ]
    tracks = research_tracks(plan)
    return {
        "tracks": list(tracks),
        "officialTracks": [t for t, spec in tracks.items() if spec.get("officialRequired")],
        "minRead": int(mode_threshold.get("minRead", 2)),
        "minOfficial": int(mode_threshold.get("minOfficial", 0)),
        "minOpenSource": int(mode_threshold.get("minOpenSource", 0)),
        "officialModules": official_modules,
    }


def research_summary(plan: Mapping[str, Any]) -> dict[str, Any]:
    sources = plan.get("research", {}).get("sources", [])
    read = [s for s in sources if s.get("read")]
    official = [s for s in read if s.get("tier") in {"official", "standard"}]
    opensource = [s for s in read if s.get("tier") == "opensource"]
    covered = sorted({m for s in read for m in s.get("modules", [])})
    return {"total": len(sources), "read": len(read), "official": len(official), "opensource": len(opensource), "modules": covered}


def frontend_problems(plan: Mapping[str, Any], gate: bool) -> tuple[list[str], list[str]]:
    """The frontend address the user gives after approving the plan.

    The order is: report → user approval → frontend address → development. Review and approval only
    remind; the development gate refuses until a Luz System session or frontend directory is registered.
    """
    errors: list[str] = []
    warnings: list[str] = []
    if plan.get("mode") != "implementation" or not FRONTEND_TYPES.intersection(plan.get("projectTypes", [])):
        return errors, warnings
    design = plan.get("design") or {}
    if not design:
        message = ("包含前端但还没有前端地址：用户批准方案后，请用户给出 Luz System 设计会话目录或前端工程目录，"
                   "再运行 tech_plan.py design NN --session <会话目录> [--frontend <前端目录>]（纯后端任务用 --skip 原因）")
        (errors if gate else warnings).append(message)
    elif design.get("skip"):
        if not str(design["skip"]).strip():
            errors.append("跳过 Luz System 接入必须写明原因")
    else:
        if design.get("errors"):
            (errors if gate else warnings).append("Luz System 设计尚未全部批准：" + "；".join(design["errors"]))
        missing = [f["path"] for f in design.get("frontend", []) if not f.get("exists")]
        if missing:
            (errors if gate else warnings).append("前端目录不存在：" + "、".join(missing))
    return errors, warnings


def check_plan(plan: Mapping[str, Any], stage: str = "review") -> tuple[list[str], list[str]]:
    """Return (errors, warnings) for a stage: review | approve | development | delivery."""
    errors: list[str] = []
    warnings: list[str] = []
    mode = plan.get("mode")
    standards = load_standards()
    rule_index = {rule["id"]: rule for rule in standards.get("rules", [])}

    if stage in {"development", "delivery"}:
        if mode in APPROVAL_MODES:
            if plan.get("status") != "approved" or not plan.get("approval"):
                errors.append(f"方案状态为「{STATUS_LABELS.get(plan.get('status'), (plan.get('status'),))[0]}」，用户批准前不得开始开发或执行")
            elif plan["approval"].get("contentHash") != content_hash(plan):
                errors.append("方案在批准后被修改；请用 revise 记录修订并重新提交审批")
        if stage == "development":
            errors += frontend_problems(plan, gate=True)[0]
            return errors, warnings

    # Content completeness.
    expected = list(MODE_SECTIONS.get(mode, ()))
    present = [section.get("id") for section in plan.get("sections", [])]
    for section_id in expected:
        if section_id not in present:
            errors.append(f"缺少章节: {section_id}")
    for section in plan.get("sections", []):
        spec = SECTION_LIBRARY.get(section.get("id"), {})
        blocks = section.get("blocks", [])
        todos = [block for block in blocks if block.get("type") == "todo"]
        if todos:
            errors.append(f"章节「{section.get('title')}」还有 {len(todos)} 个待补全项")
        generated = spec.get("generated") or (spec.get("specDriven") and plan.get("moduleSpecs"))
        if not generated and not [block for block in blocks if block.get("type") != "todo"]:
            errors.append(f"章节「{section.get('title')}」没有内容")
        for text in iter_texts([block for block in blocks if block.get("type") != "todo"]):
            for marker in PLACEHOLDER_TEXT:
                if marker in text:
                    errors.append(f"章节「{section.get('title')}」含占位文字「{marker}」")
                    break

    # Module detailed design.
    specs = plan.get("moduleSpecs") or []
    standard = plan.get("standardModules") or []
    if mode == "implementation" and "backend" in (plan.get("projectTypes") or []) and not specs and not standard:
        errors.append("后端实施方案既没有采用标准模块也没有模块详细设计；运行 standard NN --sync 与 spec NN --sync")
    catalog = {m["id"]: m for m in load_standard_modules()}
    for item in standard:
        if item.get("id") not in catalog:
            errors.append(f"标准模块 {item.get('id')} 不在 references/standard-modules.json")
    errors += ms.validate_specs(specs, with_library=True)
    standard_tables = {t.get("name") for spec in ms.load_library() if spec.get("id") in standard_spec_ids(plan)
                       for t in spec.get("tables", [])}  # 标准模块的表由标准库定义
    for spec_id, table_name in ms.unknown_write_tables(specs, standard_tables):
        warnings.append(f"模块 {spec_id} 写入的表 {table_name} 属于未纳入的模块，确认是否需要或删去这条写入")
    if specs or standard:
        covered = {m for spec in specs for m in spec.get("modules", [])}
        covered |= {m for item in standard for m in catalog.get(item.get("id"), {}).get("planModules", [])}
        missing = [m for m in plan.get("modules", []) if m not in covered and m not in SPECLESS_MODULES]
        if missing:
            warnings.append(f"这些模块没有详细设计：{'、'.join(missing)}")

    # Source references.
    source_ids = {source.get("id") for source in plan.get("research", {}).get("sources", [])}
    for text in iter_texts(plan.get("sections", [])):
        for ref in SOURCE_REF.findall(text):
            if ref not in source_ids:
                errors.append(f"引用了不存在的来源 [{ref}]")

    # Research.
    research = plan.get("research", {})
    skip = research.get("skip")
    summary = research_summary(plan)
    if skip:
        if not str(skip.get("reason", "")).strip():
            errors.append("跳过调研必须写明原因")
        if research.get("strict") and not skip.get("userConsent"):
            errors.append("实施类方案跳过调研需要用户明确同意（--user-consent）")
    else:
        need = research_requirements(plan)
        if need["minRead"] and not research.get("collections") and summary["read"] == 0:
            errors.append("尚未执行 Luz-Crawl 调研；运行 research --run 或 --ingest")
        if summary["read"] < need["minRead"]:
            errors.append(f"已阅读来源 {summary['read']} 个，少于要求的 {need['minRead']} 个")
        if summary["official"] < need["minOfficial"]:
            errors.append(f"官方或法规来源 {summary['official']} 个，少于要求的 {need['minOfficial']} 个")
        if summary["opensource"] < need["minOpenSource"]:
            errors.append(f"同类系统开源参考 {summary['opensource']} 个，少于要求的 {need['minOpenSource']} 个（GitHub / Gitee 实现）")
        # 标准模块已带官方来源（MODULE.md §0），方案只需调研定制模块
        by_standard = {m for item in plan.get("standardModules") or [] for e in load_standard_modules()
                       if e["id"] == item.get("id") for m in e.get("planModules", [])}
        uncovered = [m for m in plan.get("modules", []) if m not in summary["modules"] and m not in by_standard]
        if uncovered and need["minRead"]:
            warnings.append(f"这些模块还没有对应的已读来源：{'、'.join(uncovered)}")
        for module in need["officialModules"]:
            if not any(
                s.get("read") and s.get("tier") in {"official", "standard"} and module in s.get("modules", [])
                for s in research.get("sources", [])
            ):
                errors.append(f"模块 {module} 需要至少一个已阅读的官方来源")
        track_names = {t: s.get("name", t) for t, s in load_research_sources().get("baselineTracks", {}).items()}
        for track in need["tracks"]:
            read_for = [s for s in research.get("sources", []) if s.get("read") and track in s.get("modules", [])]
            if not read_for:
                errors.append(f"必查主题「{track_names.get(track, track)}」（{track}）还没有已读来源；运行 research --run")
            elif track in need["officialTracks"] and not any(s.get("tier") in {"official", "standard"} for s in read_for):
                errors.append(f"必查主题「{track_names.get(track, track)}」（{track}）需要至少一个已阅读的官方或标准来源")
    for source in research.get("sources", []):
        if source.get("read") and not source.get("url"):
            errors.append(f"来源 {source.get('id')} 缺少 URL")

    # Checklist.
    checklist = plan.get("checklist", {})
    for item in checklist.get("rules", []):
        rule = rule_index.get(item.get("id"))
        if rule is None and not item.get("detached"):
            errors.append(f"核对表引用了不存在的规则 {item.get('id')}")
            continue
        level = rule.get("level", "MUST") if rule else "MUST"
        status = item.get("status")
        if status not in CHECK_STATUSES:
            errors.append(f"{item.get('id')} 状态无效: {status}")
        elif status == "open":
            (errors if level == "MUST" else warnings).append(f"{level} 规则 {item.get('id')} 未处理")
        elif status in {"planned", "n/a"} and not str(item.get("note", "")).strip():
            errors.append(f"{item.get('id')} 标记为「{CHECK_LABELS[status][0]}」但没有写明做法或原因")
        if stage == "delivery" and status == "planned" and level == "MUST":
            errors.append(f"MUST 规则 {item.get('id')} 尚未实现并验证")
        if status == "done" and not item.get("evidence"):
            errors.append(f"{item.get('id')} 标记为已实现但没有证据")
    baseline_index = {item["id"]: item for item in standards.get("baselines", [])}
    for item in checklist.get("baselines", []):
        spec = baseline_index.get(item.get("id"), {})
        status = item.get("status")
        required = spec.get("requirement") == "required"
        if status == "open":
            (errors if required else warnings).append(f"基础功能 {item.get('id')} 未处理")
        elif status in {"planned", "n/a"} and not str(item.get("note", "")).strip():
            errors.append(f"基础功能 {item.get('id')} 缺少说明")
        if stage == "delivery" and status == "planned" and required:
            errors.append(f"基础功能 {item.get('id')} 尚未实现并验证")
        if status == "done" and not item.get("evidence"):
            errors.append(f"基础功能 {item.get('id')} 标记为已实现但没有证据")

    # Custom features: what this project needs beyond the baseline.
    if mode == "implementation":
        custom = plan.get("customFeatures", [])
        if not custom and not str(plan.get("customFeaturesNone", "")).strip():
            errors.append("未列出定制功能：用 tech_plan.py feature NN --name … 记录基础功能之外的需求；确实没有时用 --none 说明")
        for feature in custom:
            if not str(feature.get("name", "")).strip() or not str(feature.get("description", "")).strip():
                errors.append(f"定制功能 {feature.get('id')} 缺少名称或说明")

    frontend_errors, frontend_warnings = frontend_problems(plan, gate=stage == "delivery")
    errors += frontend_errors
    warnings += frontend_warnings

    # Decisions.
    for decision in plan.get("decisions", []):
        if decision.get("status") not in DECISION_STATUSES:
            errors.append(f"待决问题 {decision.get('id')} 状态无效")
        if stage in {"approve", "delivery"} and decision.get("blocking") and decision.get("status") == "open":
            errors.append(f"阻断问题 {decision.get('id')} 尚未由用户决定")
        if decision.get("status") == "decided" and not str(decision.get("choice", "")).strip():
            errors.append(f"待决问题 {decision.get('id')} 已决定但没有记录选择")
    return errors, warnings


# ---------------------------------------------------------------- research


def research_tracks(plan: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Cross-cutting research every plan of this mode covers whatever its modules (Web and admin security, deployment, …)."""
    if plan.get("research", {}).get("tracks") is False:
        return {}
    mode = plan.get("mode")
    return {track_id: track for track_id, track in load_research_sources().get("baselineTracks", {}).items()
            if mode in track.get("modes", [])}


def research_topics(plan: Mapping[str, Any]) -> list[str]:
    """Module tags a research source may carry: plan modules, the system archetype layer and the baseline tracks."""
    return list(plan.get("modules", [])) + ["system"] + [t for t in research_tracks(plan) if t not in plan.get("modules", [])]


def build_research_queries(plan: dict[str, Any]) -> list[dict[str, str]]:
    sources = load_research_sources()
    topic = plan["topic"]
    queries: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(platform: str, query: str, module: str, purpose: str) -> None:
        key = (platform, query)
        if key not in seen:
            seen.add(key)
            queries.append({"platform": platform, "query": query, "reason": f"{module}:{purpose}"})

    done = {(item["platform"], item["query"]) for item in plan.get("research", {}).get("doneQueries", [])}
    seen.update(done)  # searches that already returned results are not repeated
    for item in plan.get("research", {}).get("queries", []):
        add(item["platform"], item["query"], *(item.get("reason", "topic:补充").split(":", 1) + ["补充"])[:2])
    for template in sources.get("topicQueries", []) if plan.get("research", {}).get("topicQueries", True) else []:
        add(template["platform"], template["query"].replace("{topic}", topic), "topic", template.get("purpose", "方案"))
    for archetype in plan.get("archetypes", []):
        for template in sources.get("systemArchetypes", {}).get(archetype, {}).get("queries", []):
            add(template["platform"], template["query"].replace("{topic}", topic), "system", template.get("purpose", "同类系统"))
    for module in plan.get("modules", []):
        for template in sources.get("modules", {}).get(module, {}).get("queries", []):
            add(template["platform"], template["query"].replace("{topic}", topic), module, template.get("purpose", "资料"))
    for track_id, track in research_tracks(plan).items():
        for template in track.get("queries", []):
            add(template["platform"], template["query"].replace("{topic}", topic), track_id, template.get("purpose", "资料"))
    return interleave_by_platform(queries)


def interleave_by_platform(queries: list[dict[str, str]]) -> list[dict[str, str]]:
    """Round-robin across sites so consecutive tasks do not drain one site's crawl budget."""
    buckets: dict[str, list[dict[str, str]]] = {}
    for query in queries:
        buckets.setdefault(query["platform"], []).append(query)
    ordered: list[dict[str, str]] = []
    while any(buckets.values()):
        for bucket in buckets.values():
            if bucket:
                ordered.append(bucket.pop(0))
    return ordered


def build_official_links(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Official pages to open directly (no search): module officialUrls plus plan-specific links."""
    sources = load_research_sources()
    links: dict[str, dict[str, Any]] = {}

    def add(url: str, title: str, modules: list[str]) -> None:
        entry = links.setdefault(url, {"url": url, "title": title, "modules": []})
        entry["title"] = entry["title"] or title
        for module in modules:
            if module not in entry["modules"]:
                entry["modules"].append(module)

    for module in plan.get("modules", []):
        for item in sources.get("modules", {}).get(module, {}).get("officialUrls", []):
            url, title = (item, "") if isinstance(item, str) else (item["url"], item.get("title", ""))
            add(url, title, [module])
    for track_id, track in research_tracks(plan).items():
        for item in track.get("officialUrls", []):
            url, title = (item, "") if isinstance(item, str) else (item["url"], item.get("title", ""))
            add(url, title, [track_id])
    for archetype in plan.get("archetypes", []):
        for item in sources.get("systemArchetypes", {}).get(archetype, {}).get("referenceRepos", []):
            add(item["url"], item.get("title", ""), ["system"])
    for item in plan.get("research", {}).get("links", []):
        add(item["url"], item.get("title", ""), item.get("modules", []))
    return list(links.values())


def knowledge_modules(plan: Mapping[str, Any]) -> list[str]:
    modules = list(plan.get("modules", []))
    if plan.get("archetypes"):
        modules.append("system")
    return modules + [t for t in research_tracks(plan) if t not in modules]


def apply_knowledge(plan: dict[str, Any], base: "kb.Knowledge | None" = None) -> dict[str, int]:
    """Reuse fresh sources and facts from knowledge/ before crawling (references/research.md §6)."""
    base = base or kb.Knowledge()
    research = plan.setdefault("research", {})
    sources = research.setdefault("sources", [])
    by_url = {kb.normalize_url(s["url"]): s for s in sources if s.get("url")}
    reused = 0
    for item in base.sources_for(knowledge_modules(plan)):
        if item["url"] in by_url:
            continue
        numbers = [int(s["id"][1:]) for s in sources if re.fullmatch(r"S\d+", str(s.get("id", "")))]
        entry = {"id": f"S{max(numbers, default=0) + 1}", "title": item["title"], "url": item["url"], "platform": "knowledge",
                 "query": "", "tier": item["tier"], "read": True, "status": "knowledge", "origin": "knowledge",
                 "knowledgeId": item["id"], "checkedAt": item["checkedAt"],
                 "modules": [m for m in item["modules"] if m in knowledge_modules(plan)],
                 "evidence": (item.get("evidence") or [""])[0], "excerpt": item.get("excerpt", "")}
        sources.append(entry)
        by_url[item["url"]] = entry
        reused += 1
    # Official pages a module or track lists that knowledge/ already holds fresh are not crawled again (write_crawl_plan):
    # attach them here under the modules and tracks that list them, so coverage counts them.
    for link in build_official_links(plan):
        item = base.source_by_url(link["url"])
        if not item or not base.is_fresh(item):
            continue
        entry = by_url.get(item["url"])
        if entry is None:
            numbers = [int(s["id"][1:]) for s in sources if re.fullmatch(r"S\d+", str(s.get("id", "")))]
            entry = {"id": f"S{max(numbers, default=0) + 1}", "title": item["title"], "url": item["url"], "platform": "knowledge",
                     "query": "", "tier": item["tier"], "read": True, "status": "knowledge", "origin": "knowledge",
                     "knowledgeId": item["id"], "checkedAt": item["checkedAt"], "modules": [],
                     "evidence": (item.get("evidence") or [""])[0], "excerpt": item.get("excerpt", "")}
            sources.append(entry)
            by_url[item["url"]] = entry
            reused += 1
        for module in link["modules"]:
            if module not in entry.setdefault("modules", []):
                entry["modules"].append(module)
    facts = base.facts_for(knowledge_modules(plan))
    research["knowledgeFacts"] = [f["id"] for f in facts]
    return {"reusedSources": reused, "facts": len(facts)}


def harvest_to_knowledge(plan: Mapping[str, Any]) -> dict[str, int]:
    base = kb.Knowledge()
    result = kb.harvest_plan(base, plan)
    base.save()
    kb.build(base)
    return result


def focus_topics(plan: Mapping[str, Any], focus: list[str] | None) -> set[str] | None:
    """--topics web-security,payment → those ids; the word "tracks" stands for every baseline track of the plan."""
    if not focus:
        return None
    topics: set[str] = set()
    for item in focus:
        topics.update(research_tracks(plan) if item == "tracks" else [item])
    unknown = topics - set(research_topics(plan)) - {"topic"}
    if unknown:
        raise PlanError(f"--topics 中这些不是本方案的模块或必查主题：{sorted(unknown)}")
    return topics


def write_crawl_plan(plan: dict[str, Any], data_path: Path, focus: list[str] | None = None) -> dict[str, Path]:
    """Write the Luz-Crawl inputs: research-links.txt (direct reads) and research-plan.json (searches)."""
    written: dict[str, Path] = {}
    topics = focus_topics(plan, focus)
    base = kb.Knowledge()
    already_read = {kb.normalize_url(s["url"]) for s in plan.get("research", {}).get("sources", []) if s.get("read") and s.get("url")}
    # Pages read before (this plan or a fresh knowledge entry) are not fetched again; the per-site crawl budget is small.
    links = [item for item in build_official_links(plan)
             if kb.normalize_url(item["url"]) not in already_read
             and not ((found := base.source_by_url(item["url"])) and base.is_fresh(found))
             and (topics is None or topics & set(item["modules"]))]
    if links:
        path = data_path.parent / "research-links.txt"
        atomic_write(path, "\n".join(item["url"] for item in links) + "\n")
        written["links"] = path
    queries = [q for q in build_research_queries(plan) if not base.query_is_fresh(q["platform"], q["query"])
               and (topics is None or q["reason"].split(":", 1)[0] in topics)]
    for stale in data_path.parent.glob("research-plan*.json"):
        stale.unlink()
    # Luz-Crawl accepts at most 60 tasks per run; smaller batches also spread the per-site budget.
    # Sites that stop for a login pause the whole Luz-Crawl batch, so they run last in batches of their own.
    open_queries = [q for q in queries if q["platform"] not in LOGIN_GATED_PLATFORMS]
    gated = [q for q in queries if q["platform"] in LOGIN_GATED_PLATFORMS]
    batches = [open_queries[i:i + CRAWL_BATCH] for i in range(0, len(open_queries), CRAWL_BATCH)]
    batches += [gated[i:i + CRAWL_BATCH] for i in range(0, len(gated), CRAWL_BATCH)]
    for number, batch in enumerate(batches, 1):
        suffix = "" if number == 1 else f"-{number}"
        path = data_path.parent / f"research-plan{suffix}.json"
        atomic_write(path, json.dumps(batch, ensure_ascii=False, indent=2) + "\n")
        written["queries" if number == 1 else f"queries-{number:02d}"] = path
    if not written:
        raise PlanError("没有可执行的调研：请为方案添加模块、research.links 或 research.queries")
    return written


EMPTY_PAGE_MARKERS = ("There is currently no text in this page", "页面不存在", "404 Not Found", "Page not found")


def landed_elsewhere(requested: str, resolved: str, excerpt: str) -> bool:
    """A direct read that ended on the site's home page, or on an empty or missing page, is not a read of that document."""
    asked, got = urlparse(requested), urlparse(resolved)
    if asked.path.strip("/") and not got.path.strip("/"):
        return True
    return any(marker in (excerpt or "") for marker in EMPTY_PAGE_MARKERS)


def _module_from_reason(reason: str, known: list[str]) -> list[str]:
    head = (reason or "").split(":", 1)[0].strip()
    return [head] if head in known else []


def read_source_content(folder: str) -> tuple[str, str]:
    """Return (title, excerpt) from a Luz-Crawl source folder's raw/content.json."""
    if not folder:
        return "", ""
    path = Path(folder) / "raw" / "content.json"
    try:
        content = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "", ""
    text = content.get("text") or (content.get("post") or {}).get("text") or ""
    return str(content.get("title") or ""), clean_excerpt(str(text))


EXCERPT_NOISE = re.compile(
    r"更新时间：\S+|以 Markdown 格式查看|复制给 LLM|问 AI|安装 Skill|^\s*>|\s\|\s|#+\s"
)


def clean_excerpt(text: str, limit: int = 160) -> str:
    """First meaningful words of a page, without documentation-site chrome."""
    cleaned = SPACE.sub(" ", EXCERPT_NOISE.sub(" ", text)).strip(" |>")
    return cleaned[:limit] + ("…" if len(cleaned) > limit else "")


def short_time(value: str) -> str:
    """2026-10-26T15:47:24+08:00 → 2026-10-26 15:47"""
    match = re.match(r"(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})", str(value))
    return f"{match.group(1)} {match.group(2)}" if match else str(value)


def ingest_collection(plan: dict[str, Any], collection_dir: Path) -> dict[str, int]:
    """Read a Luz-Crawl collection bundle and merge its sources into the plan."""
    collection_dir = collection_dir.resolve()
    index_path = collection_dir / "raw" / "collection.json"
    if not index_path.is_file():
        raise PlanError(f"不是 Luz-Crawl 合集目录（缺少 raw/collection.json）: {collection_dir}")
    index = load_json(index_path)
    research = plan.setdefault("research", {})
    sources = research.setdefault("sources", [])
    by_url = {kb.normalize_url(source["url"]) if str(source.get("url", "")).startswith("http") else source.get("url"): source
              for source in sources}
    known_modules = research_topics(plan)
    added = read_count = 0

    def next_id() -> str:
        numbers = [int(s["id"][1:]) for s in sources if re.fullmatch(r"S\d+", str(s.get("id", "")))]
        return f"S{max(numbers, default=0) + 1}"

    def merge(entry: dict[str, Any]) -> None:
        nonlocal added, read_count
        url = entry.get("url") or ""
        if not url:
            return
        url = entry["url"] = kb.normalize_url(url) if url.startswith("http") else url
        current = by_url.get(url)
        if current is None:
            entry["id"] = next_id()
            sources.append(entry)
            by_url[url] = entry
            added += 1
        else:
            current["read"] = current.get("read") or entry.get("read")
            for module in entry.get("modules", []):
                if module not in current.setdefault("modules", []):
                    current["modules"].append(module)
            if entry.get("read") and not current.get("excerpt"):
                current["excerpt"] = entry.get("excerpt", "")
        if entry.get("read"):
            read_count += 1

    official_links = {item["url"]: item for item in build_official_links(plan)}
    results = index.get("results")
    if isinstance(results, list):
        for result in results:
            if "query" not in result and (result.get("resolved_url") or result.get("url")):
                # Direct-read task from a links file: the result itself is the source.
                url = result.get("resolved_url") or result.get("url") or ""
                check = result.get("url_check") or {}
                requested = check.get("requested") or result.get("url") or url
                status = str(result.get("status", ""))
                folder = result.get("folder") or ""
                title, excerpt = read_source_content(folder)
                link = official_links.get(requested) or official_links.get(url) or {}
                if link.get("title") and (not title or title == (urlparse(url).hostname or "")):
                    title = link["title"]  # .md and .txt pages only report their host as title
                merge({
                    "title": title or Path(folder).name.split("_", 1)[-1] or url,
                    "url": url,
                    "platform": "official-link",
                    "query": "",
                    "tier": classify_tier(url),
                    "read": status in {"captured", "complete", "partial"} and bool(check.get("valid_content"))
                            and not landed_elsewhere(requested, url, excerpt),
                    "status": status if not landed_elsewhere(requested, url, excerpt) else "no-content",
                    "modules": list(link.get("modules", [])),
                    "evidence": folder,
                    "excerpt": excerpt,
                })
                continue
            modules = _module_from_reason(str(result.get("reason", "")), known_modules)
            key = {"platform": result.get("platform", ""), "query": result.get("query", ""), "reason": result.get("reason", "")}
            limitations = " ".join(str(item) for item in result.get("limitations", []) or [])
            if "cooling down" in limitations or result.get("status") == "failed":
                retry = research.setdefault("retryQueries", [])
                if key not in retry:
                    retry.append(key)
            elif result.get("candidates") or result.get("sources"):
                done_list = research.setdefault("doneQueries", [])
                if key not in done_list:
                    done_list.append(key)
                research["retryQueries"] = [item for item in research.get("retryQueries", []) if item != key]
            reads = result.get("reads") or result.get("read_results") or result.get("sources") or []
            for read in reads if isinstance(reads, list) else []:
                if not isinstance(read, dict):
                    continue
                url = read.get("resolved_url") or read.get("url") or ""
                status = str(read.get("status", ""))
                check = read.get("url_check") or {}
                is_read = status in {"complete", "captured", "partial"} and bool(
                    check.get("valid_content", status != "partial")
                ) and check.get("target_confirmed", True) is not False
                folder = read.get("folder") or read.get("source_folder") or ""
                title, excerpt = read_source_content(folder)
                merge({
                    "title": read.get("title") or title or Path(folder).name.split("_", 1)[-1] or url,
                    "url": url,
                    "platform": result.get("platform", ""),
                    "query": result.get("query", ""),
                    "tier": classify_tier(url, result.get("platform", "")),
                    "read": is_read,
                    "status": status,
                    "modules": list(modules),
                    "evidence": folder,
                    "excerpt": str(read.get("excerpt") or read.get("summary") or excerpt)[:400],
                })
    # Schema 3 and hand-merged collections list verified sources directly.
    for source in index.get("sources", []) if isinstance(index.get("sources"), list) else []:
        url = source.get("resolved_url") or source.get("url") or ""
        status = str(source.get("status", ""))
        merge({
            "title": source.get("title") or url,
            "url": url,
            "platform": source.get("platform", ""),
            "query": "",
            "tier": classify_tier(url, source.get("platform", "")),
            "read": status in {"captured", "complete"},
            "status": status,
            "modules": [],
            "evidence": str(collection_dir / source.get("source_folder", "")),
            "excerpt": "",
        })
    references = collection_dir / "raw" / "references.json"
    if references.is_file():
        data = load_json(references)
        items = data.get("references", data) if isinstance(data, dict) else data
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            url = item.get("resolved_url") or item.get("url") or item.get("requested_url") or ""
            status = str(item.get("status", ""))
            if not url or "bing.com/search" in url or "google.com/search" in url:
                continue
            modules = _module_from_reason(str(item.get("reason", "")), known_modules)
            merge({
                "title": item.get("title") or url,
                "url": url,
                "platform": item.get("platform", ""),
                "query": item.get("query", ""),
                "tier": classify_tier(url, item.get("platform", "")),
                "read": status in {"complete", "captured"} or bool(item.get("target_confirmed")),
                "status": status,
                "modules": modules,
                "evidence": item.get("folder") or item.get("source_folder") or "",
                "excerpt": "",
            })
    record = {
        "path": str(collection_dir),
        "title": index.get("title", collection_dir.name),
        "createdAt": index.get("created_at", ""),
        "ingestedAt": now_iso(),
        "added": added,
    }
    collections = research.setdefault("collections", [])
    collections[:] = [c for c in collections if c.get("path") != record["path"]] + [record]
    research["skip"] = None
    return {"added": added, "read": read_count, "total": len(sources)}


def crawl_task_folder(plan: dict[str, Any], crawl_root: Path, log: Path) -> Path:
    """One Luz-Crawl task folder per plan: created once with `begin --new`, stored in research.crawlFolder, reused after.

    Luz-Crawl otherwise writes into whatever task another session has active (Luz-Crawl SKILL.md: one folder per request).
    """
    research = plan.setdefault("research", {})
    stored = research.get("crawlFolder")
    if stored and Path(stored).is_dir():
        return Path(stored)
    script = crawl_root / "scripts" / "luz-crawl.mjs"
    title = f"Luz-Dev {plan['number']} {plan['topic']}"[:60]
    command = ["node", str(script), "begin", title, "--new"]
    completed = subprocess.run(command, cwd=crawl_root, capture_output=True, text=True, encoding="utf-8", errors="replace")
    with log.open("a", encoding="utf-8") as handle:
        handle.write("$ " + " ".join(command) + f"\nexit={completed.returncode}\n{completed.stdout[-2000:]}\n")
    try:
        folder = Path(json.loads(completed.stdout[completed.stdout.index("{"):])["folder"])
    except (KeyError, ValueError) as exc:
        raise PlanError(f"Luz-Crawl begin 没有返回任务目录；查看日志: {log}") from exc
    research["crawlFolder"] = str(folder)
    return folder


def _collections_under(root: Path) -> set[Path]:
    """Collection folders (raw/collection.json) in a task folder's raw/, the task folder itself, or the legacy flat root."""
    found: set[Path] = set()
    for base in (root, root / "raw"):
        if base.is_dir():
            found.update(item for item in base.iterdir() if item.is_dir() and (item / "raw" / "collection.json").is_file())
    return found


def _crawl(crawl_root: Path, name: str, args: list[str], log: Path, folder: Path | None = None) -> Path | None:
    script = crawl_root / "scripts" / "luz-crawl.mjs"
    output_root = folder or DEFAULT_CRAWL_OUTPUT
    before = _collections_under(output_root)
    command = ["node", str(script), "collect", name, *args, "--name", name] + (["--out", str(folder)] if folder else [])
    completed = subprocess.run(command, cwd=crawl_root, capture_output=True, text=True, encoding="utf-8", errors="replace")
    with log.open("a", encoding="utf-8") as handle:
        handle.write("$ " + " ".join(command) + f"\nexit={completed.returncode}\n")
        handle.write(completed.stdout[-4000:] + "\n--- stderr ---\n" + completed.stderr[-4000:] + "\n")
    # Other sessions may create collections at the same time: only accept folders carrying our name.
    expected = UNSAFE_FILENAME.sub(" ", name)[:24].strip()
    created = sorted(
        (item for item in _collections_under(output_root) - before
         if item.name.split("_", 1)[-1].startswith(expected)),
        key=lambda item: item.stat().st_mtime,
    )
    return created[-1] if created else None


def run_crawl(
    plan: dict[str, Any], data_path: Path, crawl_root: Path, read: int, only: str | None = None, pause_minutes: float = 0,
    focus: list[str] | None = None,
) -> list[Path]:
    """Run Luz-Crawl: official pages first (direct reads), then searches. Returns created collections."""
    script = crawl_root / "scripts" / "luz-crawl.mjs"
    if not script.is_file():
        raise PlanError(f"找不到 Luz-Crawl: {script}；用 --crawl-root 或 LUZ_CRAWL_ROOT 指定")
    inputs = write_crawl_plan(plan, data_path, focus)
    log = data_path.parent / "research-run.log"
    folder = crawl_task_folder(plan, crawl_root, log)
    base = f"Luz-Dev {plan['number']} {plan['topic']}"[:48]
    collections: list[Path] = []
    if only:
        inputs = {key: value for key, value in inputs.items() if key.startswith(only)}
    if "links" in inputs:
        created = _crawl(crawl_root, f"{base} 官方原文", ["--file", str(inputs["links"])], log, folder)
        if created:
            collections.append(created)
            ingest_collection(plan, created)
    html_path = html_for(data_path)
    for position, key in enumerate(sorted(k for k in inputs if k.startswith("queries"))):
        if position and pause_minutes:
            time.sleep(pause_minutes * 60)  # let per-site crawl budgets recover between batches
        label = "检索" if key == "queries" else "检索" + key.removeprefix("queries")
        created = _crawl(crawl_root, f"{base} {label}", ["--file", str(inputs[key]), "--read", str(read)], log, folder)
        if created:
            collections.append(created)
            # Import each batch as soon as it lands, so a later failure keeps the earlier evidence.
            ingest_collection(plan, created)
            harvest_to_knowledge(plan)
            save_plan(plan, html_path, data_path)
    if not collections:
        raise PlanError(f"Luz-Crawl 没有生成合集；查看日志: {log}")
    return collections


# ---------------------------------------------------------------- rendering
# The page is a Luz Design reading page: one h1, sections of content, nothing about the page itself.
# Sources, approval state, revisions and open decisions stay in raw/plan.json and in the conversation.

UNSAFE_SVG = re.compile(r"<\s*script|<\s*foreignObject|\son[a-z]+\s*=|javascript:", re.IGNORECASE)
HIDDEN_SECTIONS = {"decisions", "revisions"}
SECTION_TITLES = {  # visible headings; titles stored in older plans may contain process wording
    "research": "调研结论",
    "design": "页面与前端接入",
    "security": "安全与规范",
    "reusable": "复用的实现",
}


def text(value: Any) -> str:
    """Inline formatting for the page; [S#] source markers stay in plan.json only."""
    return inline(SOURCE_REF.sub("", str(value))).strip()


def tag(label: str, tone: str = "plain") -> str:
    return f'<span class="tag tag-{tone}">{html.escape(label)}</span>'


def table(columns: list[Any], rows: list[list[str]], dense: bool = False, raw: bool = False) -> str:
    head = "".join(f'<th scope="col">{text(c)}</th>' for c in columns)
    body = "".join("<tr>" + "".join(f"<td>{cell if raw else text(cell)}</td>" for cell in row) + "</tr>" for row in rows)
    cls = ' class="dense"' if dense else ""
    return f'<div class="table-wrap"><table{cls}><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def render_block(block: Mapping[str, Any]) -> str:
    kind = block.get("type")
    if kind == "p":
        return f"<p>{text(block.get('text', ''))}</p>"
    if kind == "h3":
        return f"<h3>{text(block.get('text', ''))}</h3>"
    if kind == "list":
        tag_name = "ol" if block.get("ordered") else "ul"
        return f"<{tag_name}>" + "".join(f"<li>{text(item)}</li>" for item in block.get("items", [])) + f"</{tag_name}>"
    if kind == "table":
        return table(block.get("columns", []), block.get("rows", []), dense=bool(block.get("dense")))
    if kind == "callout":
        title = f"<strong>{text(block['title'])}</strong>" if block.get("title") else ""
        return f'<p class="lead">{title}{text(block.get("text", ""))}</p>'
    if kind == "cards":
        cards = "".join(f'<div class="card"><h3>{text(i.get("title", ""))}</h3><p>{text(i.get("text", ""))}</p></div>'
                        for i in block.get("items", []))
        return f'<div class="grid">{cards}</div>'
    if kind == "kv":
        rows = "".join(f"<dt>{text(i.get('label', ''))}</dt><dd>{text(i.get('value', ''))}</dd>" for i in block.get("items", []))
        return f'<dl class="kv">{rows}</dl>'
    if kind == "code":
        return f'<pre><code>{html.escape(str(block.get("text", "")))}</code></pre>'
    if kind == "svg":
        svg = str(block.get("svg", ""))
        if UNSAFE_SVG.search(svg) or not svg.lstrip().startswith("<svg"):
            return '<p class="draft">图示含脚本或事件属性，未渲染。</p>'
        caption = f"<figcaption>{text(block['caption'])}</figcaption>" if block.get("caption") else ""
        return f"<figure>{svg}{caption}</figure>"
    if kind == "todo":
        return f'<p class="draft">{text(block.get("text", ""))}</p>'
    return ""


TOOL_FACT = re.compile(r"Luz[- ]?(Crawl|Dev|Design|Writing)|LuzCrawl", re.IGNORECASE)  # how we research is not content


def render_research(plan: Mapping[str, Any]) -> str:
    """Conclusions reused from knowledge/ are content; source lists, counts and tiers are not shown."""
    fact_ids = plan.get("research", {}).get("knowledgeFacts", [])
    if not fact_ids:
        return ""
    facts = [f for f in kb.Knowledge().facts["facts"] if f["id"] in fact_ids]
    if not facts:
        return ""
    rows = [[f["topic"], f["statement"]] for f in facts if not TOOL_FACT.search(f["topic"] + f["statement"])]
    return "<h3>已核实的平台与法规要求</h3>" + table(["主题", "要求"], rows) if rows else ""


STATE_TAGS = {"done": ("已实现", "good"), "n/a": ("不适用", "plain"), "open": ("待定", "warn")}


def render_checklist(plan: Mapping[str, Any]) -> str:
    """One table grouped by domain; a status column appears only when some rule is not simply planned."""
    standards = load_standards()
    index = {rule["id"]: rule for rule in standards.get("rules", [])}
    domains = {domain["id"]: domain["name"] for domain in standards.get("domains", [])}
    groups: dict[str, list[tuple[str, str, str]]] = {}
    for item in plan.get("checklist", {}).get("rules", []):
        rule = index.get(item.get("id"))
        if rule is None:
            continue
        groups.setdefault(rule.get("domain", "other"), []).append(
            (text(rule.get("title", "")), str(item.get("status", "open")), text(item.get("note", "")) or "—"))
    if not groups:
        return ""
    with_state = any(status != "planned" for rows in groups.values() for _t, status, _n in rows)
    columns = ["领域", "要求", "做法"] + (["状态"] if with_state else [])
    body = []
    for domain_id, rows in groups.items():
        for i, (title, status, note) in enumerate(rows):
            cells = [f'<td rowspan="{len(rows)}">{html.escape(domains.get(domain_id, domain_id))}</td>'] if i == 0 else []
            cells += [f'<td data-label="要求">{title}</td>', f'<td data-label="做法">{note}</td>']
            if with_state:
                label, tone = STATE_TAGS.get(status, ("纳入", "good"))
                cells.append(f'<td class="c" data-label="状态">{tag(label, tone)}</td>')
            body.append("<tr>" + "".join(cells) + "</tr>")
    head = "".join(f'<th scope="col">{c}</th>' for c in columns)
    return ("<h3>规范逐条落实</h3>"
            f'<div class="table-wrap"><table class="dense grouped"><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table></div>')


def render_features(plan: Mapping[str, Any]) -> str:
    standards = load_standards()
    index = {item["id"]: item for item in standards.get("baselines", [])}
    type_names = {"miniprogram": "小程序", "admin": "管理后台", "mobile": "移动 App", "backend": "后端服务", "agent": "智能体"}
    groups: dict[str, list[list[str]]] = {}
    for item in plan.get("checklist", {}).get("baselines", []):
        spec = index.get(item.get("id"))
        if spec is None:
            continue
        status = str(item.get("status", "open"))
        label, tone = STATE_TAGS.get(status, ("纳入", "good"))
        groups.setdefault(spec.get("projectType"), []).append(
            [text(spec.get("feature", "")), text(item.get("note", "")) or text(spec.get("acceptance", "")), status, tag(label, tone)]
        )
    parts = []
    if groups:
        parts.append("<h3>基础功能</h3>")
        for project_type, rows in groups.items():
            if len(groups) > 1:
                parts.append(f"<p><strong>{html.escape(type_names.get(project_type, str(project_type)))}</strong></p>")
            if any(row[2] != "planned" for row in rows):
                parts.append(table(["功能", "内容与验收", "状态"], [[r[0], r[1], r[3]] for r in rows], raw=True))
            else:
                parts.append(table(["功能", "内容与验收"], [r[:2] for r in rows], raw=True))
    custom = plan.get("customFeatures", [])
    if custom:
        parts.append("<h3>定制功能</h3>")
        names = {m["id"]: m.get("name", m["id"]) for m in standards.get("modules", [])}
        rows = [[text(f.get("name", "")), text(f.get("description", "")), tag(str(f.get("priority", "P1")), "strong"),
                 html.escape("、".join(names.get(m, m) for m in f.get("modules", [])) or "—")] for f in custom]
        parts.append(table(["功能", "内容", "优先级", "涉及模块"], rows, raw=True))
    return "".join(parts)


def section_parts(plan: Mapping[str, Any], section: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    """(anchor, title, body) for one stored section; the modules section expands to one part per module."""
    section_id = str(section.get("id"))
    if section_id in HIDDEN_SECTIONS:
        return []
    specs = plan.get("moduleSpecs") or []
    standard = plan.get("standardModules") or []
    designed = bool(specs or standard)
    body = "".join(render_block(block) for block in section.get("blocks", []))
    if section_id == "features":
        body = render_features(plan) + body
    elif section_id == "research":
        body += render_research(plan)
    elif section_id == "security":
        body += render_checklist(plan)
    elif section_id == "data" and designed:
        body += ms.render_data_overview(specs)
    elif section_id == "routes" and designed:
        body = ms.render_api_overview(specs) + body
    elif section_id == "modules" and designed:
        overview = render_standard_modules(plan)
        if specs:
            overview += ("<h3>定制模块</h3>" if standard else "") + ms.render_module_overview(specs)
        body = overview + body
    title = SECTION_TITLES.get(section_id, section.get("title", ""))
    if section_id == "modules" and designed:
        title = SECTION_LIBRARY["modules"]["title"]
    elif section_id in ("data", "routes") and designed:
        title = SECTION_LIBRARY[section_id]["title"]
    parts = [(section_id, str(title), body)] if body.strip() else []
    if section_id == "modules":
        for spec in specs:
            name, spec_body = ms.render_spec(spec)
            parts.append((f"module-{spec['id']}", name, spec_body))
    return parts


def render_standard_modules(plan: Mapping[str, Any]) -> str:
    """Standard modules: what each covers (once, from the catalog), this project's values, and deviations.

    The full design of a standard module lives in backend/<dir>/MODULE.md; the plan does not repeat it.
    """
    chosen = plan.get("standardModules") or []
    if not chosen:
        return ""
    catalog = {m["id"]: m for m in load_standard_modules()}
    rows = []
    for item in chosen:
        entry = catalog.get(item.get("id"))
        if entry is None:
            continue
        values = item.get("params") or {}
        settings = "；".join(f"{meaning}：{values.get(key, default)}" for key, default, meaning in entry.get("params", []))
        extra = [f"{key}：{value}" for key, value in values.items() if key not in {p[0] for p in entry.get("params", [])}]
        settings = "；".join([settings, *extra]) if extra else settings
        deviations = "；".join(text(d) for d in item.get("deviations") or []) or "无"
        rows.append([text(entry["name"]), text(entry["covers"]), text(settings) or "—", deviations])
    return "<h3>采用的标准模块</h3>" + table(["模块", "内容", "本项目取值", "与标准的差异"], rows, raw=True)


H3 = re.compile(r"<h3>")
TABLE_HTML = re.compile(r"<table([^>]*)>(.*?)</table>", re.DOTALL)
TAG_HTML = re.compile(r"<[^>]+>")
SHORT_CELL = 10  # visible characters; short columns (status, numbers, codes, yes/no) are centred


def finish_tables(markup: str) -> str:
    """Centre short columns and label every cell, so a table reads as cards on a phone."""
    def one(match: re.Match[str]) -> str:
        attrs, inner = match.group(1), match.group(2)
        if "grouped" in attrs:  # rowspan shifts columns; the grouped table only gets labels for its fixed cells
            return match.group(0)
        heads = [html.unescape(TAG_HTML.sub("", h)).strip() for h in re.findall(r"<th[^>]*>(.*?)</th>", inner, re.DOTALL)]
        rows = re.findall(r"<tr>(.*?)</tr>", inner.split("</thead>", 1)[-1], re.DOTALL)
        cells = [re.findall(r"<td>(.*?)</td>", row, re.DOTALL) for row in rows]
        width = max((len(r) for r in cells), default=0)
        short = [bool(cells) and all(len(html.unescape(TAG_HTML.sub("", r[c])).strip()) <= SHORT_CELL for r in cells if c < len(r))
                 for c in range(width)]
        short = [flag and c > 0 for c, flag in enumerate(short)]  # the first column names the object

        def cell(c: int, value: str) -> str:
            label = f' data-label="{html.escape(heads[c], quote=True)}"' if c < len(heads) and heads[c] else ""
            cls = ' class="c"' if c < len(short) and short[c] else ""
            return f"<td{cls}{label}>{value}</td>"
        body = "".join("<tr>" + "".join(cell(c, v) for c, v in enumerate(r)) + "</tr>" for r in cells)
        head = inner.split("<tbody>", 1)[0]
        return f"<table{attrs}>{head}<tbody>{body}</tbody></table>"
    return TABLE_HTML.sub(one, markup)


def number_parts(parts: list[tuple[str, str, str]]) -> str:
    """Numbered h2 / h3 headings and a contents list, so the document reads like a design specification."""
    sections, contents = [], []
    for index, (anchor, title, body) in enumerate(parts, start=1):
        counter = iter(range(1, 1000))
        body = H3.sub(lambda _m: f"<h3>{index}.{next(counter)} ", body)
        anchor = html.escape(anchor, quote=True)
        contents.append(f'<li><a href="#{anchor}">{index} {html.escape(title)}</a></li>')
        sections.append(f'<section id="{anchor}"><h2>{index} {html.escape(title)}</h2>{body}</section>')
    # Luz Design: no contents by default; past 8 sections a light list on the right of wide screens,
    # after the body in document order, section names only, hidden on narrow screens.
    toc = f'<div class="toc"><ol>{"".join(contents)}</ol></div>' if len(parts) > 8 else ""
    return finish_tables("".join(sections)) + toc


def ordered_sections(plan: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    order = {sid: i for i, sid in enumerate(MODE_SECTIONS.get(plan.get("mode"), ()))}
    sections = list(plan.get("sections", []))
    return sorted(sections, key=lambda s: order.get(s.get("id"), len(order)))


def render_section(plan: Mapping[str, Any], section: Mapping[str, Any]) -> str:
    return number_parts(section_parts(plan, section))


def render_plan(plan: Mapping[str, Any], design_root: Path | None = None) -> str:
    # Authoring notes in the template (<!-- … -->, e.g. "the template is a skeleton; read the rule card") stay out of the page.
    template = re.sub(r"<!--.*?-->\n?", "", TEMPLATE.read_text(encoding="utf-8"), flags=re.S)
    tokens, token_source = load_tokens(design_root)
    values = {
        "{{TOKENS_CSS}}": tokens_css(tokens),
        "{{FAVICON}}": favicon_for_tokens("plan", tokens),
        "{{SHELL_CSS}}": shell_css(),
        "{{TOKEN_SOURCE}}": html.escape(token_source, quote=True),
        "{{PLAN_NUMBER}}": html.escape(str(plan.get("number", "")), quote=True),
        "{{TITLE}}": html.escape(str(plan.get("title") or plan.get("topic", "")), quote=True),
        "{{TASK_MODE}}": html.escape(str(plan.get("mode", "")), quote=True),
        "{{STATUS}}": html.escape(str(plan.get("status", "draft")), quote=True),
        "{{SECTIONS}}": number_parts([part for section in ordered_sections(plan) for part in section_parts(plan, section)]),
    }
    return fill(template, values)


def check_page(html_path: Path, design_root: Path | None = None) -> tuple[list[str], list[str]] | None:
    """Run Luz Design's document checker on the page (read-only use of the sibling skill)."""
    script = (design_root or resolve_design_root()) / "scripts" / "check_document.py"
    if not script.is_file():
        return None
    completed = subprocess.run([sys.executable, "-X", "utf8", str(script), str(html_path)],
                               capture_output=True, text=True, encoding="utf-8", errors="replace")
    errors = [line.strip()[6:].strip() for line in completed.stdout.splitlines() if line.strip().startswith("ERROR")]
    warnings = [line.strip()[5:].strip() for line in completed.stdout.splitlines() if line.strip().startswith("WARN")]
    return errors, warnings


# ---------------------------------------------------------------- commands


def _load(reference: str, output_root: Path) -> tuple[dict[str, Any], Path, Path]:
    html_path, data_path = find_plan(reference, output_root)
    plan = load_json(data_path)
    if plan.get("schema") != SCHEMA:
        raise PlanError(f"不支持的方案数据版本: {plan.get('schema')}")
    return plan, html_path, data_path


def add_revision(plan: dict[str, Any], change: str, basis: str) -> None:
    revisions = plan.setdefault("revisions", [])
    last = revisions[-1]["version"] if revisions else "0.0"
    major, _, minor = str(last).partition(".")
    try:
        version = f"{int(major)}.{int(minor or 0) + 1}"
    except ValueError:
        version = f"{len(revisions) + 1}.0"
    revisions.append({"at": now_iso(), "version": version, "change": change, "basis": basis})


def print_result(payload: Mapping[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def announce_rule_cards(payload: Mapping[str, Any]) -> None:
    """Point the agent at the rule cards it must read before producing anything (stderr; stdout stays JSON)."""
    print("下一步：先读完这些规则卡（用户偏好、教训、规范原文、工具、检查），再调研和写方案：", file=sys.stderr)
    for path in payload.get("ruleCards", []):
        print(f"  {path}", file=sys.stderr)
    for problem in payload.get("ruleCardErrors", []):
        print(f"  错误：{problem}", file=sys.stderr)
    print("采用的规则：tech_plan.py rule NN --rule … --how …；本次沉淀：record_experience.py note … 后 tech_plan.py sediment NN --key …",
          file=sys.stderr)


def command_new(args: argparse.Namespace) -> int:
    payload = create_plan(
        args.topic, args.mode, args.output_root, args.summary.strip(),
        project_types=args.project_type, modules=args.modules, design_root=args.design_root,
        archetypes=args.archetype, cards=args.card,
    )
    print_result(payload)
    announce_rule_cards(payload)
    return 0


def command_render(args: argparse.Namespace) -> int:
    plan, html_path, data_path = _load(args.plan, args.output_root)
    if args.sync:
        derive_checklist(plan)
    save_plan(plan, html_path, data_path, args.design_root)
    print_result({"path": str(html_path), "status": plan["status"], "rules": len(plan["checklist"]["rules"])})
    return 0


def process_problems(plan: Mapping[str, Any], data_path: Path, stage: str) -> tuple[list[str], list[str]]:
    """Rule cards, 采用的规则 and 本次沉淀 for a stage: blocking at review (submit) and delivery, reminders otherwise.

    Plans created before rule cards existed (no adoptedRules field) only get reminders at delivery.
    """
    cards = rule_card_problems(plan, data_path)
    if stage == "review" or (stage == "delivery" and "adoptedRules" in plan):
        return cards + sediment_problems(plan, stage), []
    if stage == "delivery":
        return [], cards + sediment_problems(plan, stage)
    return [], cards


def command_check(args: argparse.Namespace) -> int:
    plan, html_path, data_path = _load(args.plan, args.output_root)
    errors, warnings = check_plan(plan, args.stage)
    extra_errors, extra_warnings = process_problems(plan, data_path, args.stage)
    errors += extra_errors
    warnings += extra_warnings
    print_result({"plan": str(html_path), "stage": args.stage, "status": plan.get("status"), "ok": not errors, "errors": errors, "warnings": warnings})
    return 0 if not errors else 1


def command_gate(args: argparse.Namespace) -> int:
    args.stage = "development"
    return command_check(args)


def command_submit(args: argparse.Namespace) -> int:
    plan, html_path, data_path = _load(args.plan, args.output_root)
    errors, warnings = check_plan(plan, "review")
    errors += process_problems(plan, data_path, "review")[0]  # rule cards current, 采用的规则 and 本次沉淀 filled
    save_plan(plan, html_path, data_path, args.design_root)
    page = check_page(html_path, args.design_root)
    if page is None:
        warnings.append("未找到 Luz Design 的 check_document.py，页面未经 Luz Design 规范检查")
    else:
        errors += [f"页面不符合 Luz Design 规范：{e}" for e in page[0]]
        warnings += [f"Luz Design 提示：{w}" for w in page[1]]
    if errors:
        print_result({"ok": False, "errors": errors, "warnings": warnings})
        return 1
    if plan["status"] == "approved":
        raise PlanError("方案已批准；如需修改请先 revise")
    plan["status"] = "in-review"
    add_revision(plan, args.note or "补全方案并提交用户审核。", "Luz Dev")
    save_plan(plan, html_path, data_path, args.design_root)
    print_result({"ok": True, "status": plan["status"], "path": str(html_path), "warnings": warnings})
    return 0


def command_approve(args: argparse.Namespace) -> int:
    plan, html_path, data_path = _load(args.plan, args.output_root)
    if plan["status"] != "in-review":
        raise PlanError("只有「待审核」的方案可以批准；先运行 submit")
    if not args.note.strip():
        raise PlanError("批准必须记录用户的原话或确认依据（--note）")
    errors, warnings = check_plan(plan, "approve")
    if errors:
        print_result({"ok": False, "errors": errors, "warnings": warnings})
        return 1
    plan["status"] = "approved"
    add_revision(plan, "用户批准方案。", args.note.strip())
    plan["approval"] = {"by": args.by, "at": now_iso(), "note": args.note.strip(), "contentHash": content_hash(plan)}
    save_plan(plan, html_path, data_path, args.design_root)
    print_result({"ok": True, "status": "approved", "contentHash": plan["approval"]["contentHash"], "path": str(html_path)})
    return 0


def command_revise(args: argparse.Namespace) -> int:
    plan, html_path, data_path = _load(args.plan, args.output_root)
    if plan["status"] == "approved":
        plan["status"] = "revising"
    add_revision(plan, args.note, args.basis)
    save_plan(plan, html_path, data_path, args.design_root)
    print_result({"ok": True, "status": plan["status"], "path": str(html_path)})
    return 0


def command_mark(args: argparse.Namespace) -> int:
    plan, html_path, data_path = _load(args.plan, args.output_root)
    if args.status not in CHECK_STATUSES:
        raise PlanError(f"状态必须是 {CHECK_STATUSES}")
    targets = split_list(args.ids)
    changed = []
    for kind in ("rules", "baselines"):
        for item in plan.get("checklist", {}).get(kind, []):
            if item["id"] in targets:
                if args.status == "done" and not (args.evidence or item.get("evidence")):
                    raise PlanError(f"{item['id']} 标记为已实现时必须提供 --evidence")
                item["status"] = args.status
                if args.note is not None:
                    item["note"] = args.note
                if args.evidence:
                    item.setdefault("evidence", []).append(args.evidence)
                changed.append(item["id"])
    missing = sorted(set(targets) - set(changed))
    if missing:
        raise PlanError(f"核对表中没有这些条目: {missing}")
    save_plan(plan, html_path, data_path, args.design_root)
    print_result({"ok": True, "changed": changed})
    return 0


def command_decide(args: argparse.Namespace) -> int:
    plan, html_path, data_path = _load(args.plan, args.output_root)
    decisions = plan.setdefault("decisions", [])
    decision = next((d for d in decisions if d.get("id") == args.id), None)
    if decision is None:
        if not args.question:
            raise PlanError(f"没有待决问题 {args.id}；新增时需要 --question")
        decision = {"id": args.id, "question": args.question, "options": args.option or [],
                    "recommendation": args.recommendation or "", "blocking": args.blocking, "status": "open", "choice": ""}
        decisions.append(decision)
    else:
        for field in ("question", "recommendation"):
            if getattr(args, field):
                decision[field] = getattr(args, field)
        if args.option:
            decision["options"] = args.option
        if args.blocking:
            decision["blocking"] = True
    if args.choice:
        decision["status"] = "decided"
        decision["choice"] = args.choice
    if args.defer:
        decision["status"] = "deferred"
        decision["choice"] = args.defer
    save_plan(plan, html_path, data_path, args.design_root)
    print_result({"ok": True, "decision": decision})
    return 0


def command_research(args: argparse.Namespace) -> int:
    global CRAWL_BATCH
    CRAWL_BATCH = max(1, min(args.batch, 60))
    plan, html_path, data_path = _load(args.plan, args.output_root)
    result: dict[str, Any] = {}
    if args.add_query:
        for raw in args.add_query:
            platform, _, rest = raw.partition("=")
            module, _, query = rest.partition("|") if "|" in rest else ("topic", "", rest)
            if not platform or not query:
                raise PlanError("--add-query 格式：platform=module|query，例如 bing=payment|微信支付 退款 site:pay.weixin.qq.com")
            plan["research"].setdefault("queries", []).append({"platform": platform, "query": query, "reason": f"{module}:补充"})
    if args.skip:
        plan["research"]["skip"] = {"reason": args.skip, "userConsent": bool(args.user_consent), "at": now_iso()}
        result["skipped"] = True
    if args.no_topic:
        plan["research"]["topicQueries"] = False  # 主题本身不是可检索的业务名称时（如技能维护）
    if args.no_tracks:
        plan["research"]["tracks"] = False  # 不涉及上线系统的方案（如技能维护）不做安全与部署必查主题
    for raw in args.add_link or []:
        module, _, url = raw.partition("|") if "|" in raw else ("", "", raw)
        if not url.startswith(("http://", "https://")):
            raise PlanError("--add-link 格式：module|https://… 或 https://…")
        plan["research"].setdefault("links", []).append({"url": url, "modules": split_list([module]) if module else []})
    if args.build or args.run:
        result["knowledge"] = apply_knowledge(plan)
        result["crawlInputs"] = {key: str(value) for key, value in write_crawl_plan(plan, data_path, split_list(args.topics)).items()}
        result["links"] = len(build_official_links(plan))
        result["queries"] = len(build_research_queries(plan))
    collections = [Path(item) for item in args.ingest or []]
    if args.run:
        collections.extend(run_crawl(plan, data_path, resolve_crawl_root(args.crawl_root), args.read, args.only, args.pause,
                                       split_list(args.topics)))
    for collection in collections:
        result.setdefault("ingested", []).append({"collection": str(collection), **ingest_collection(plan, collection)})
    if args.source_url:
        sources = plan["research"].setdefault("sources", [])
        numbers = [int(s["id"][1:]) for s in sources if re.fullmatch(r"S\d+", str(s.get("id", "")))]
        entry = {
            "id": f"S{max(numbers, default=0) + 1}", "title": args.source_title or args.source_url, "url": args.source_url,
            "platform": "manual", "query": "", "tier": args.source_tier or classify_tier(args.source_url),
            "read": True, "status": "captured", "modules": split_list(args.source_module),
            "evidence": args.source_evidence or "", "excerpt": (args.source_excerpt or "")[:400],
        }
        if not entry["evidence"]:
            raise PlanError("手动登记来源必须提供 --source-evidence（Luz-Crawl capture 目录或截图位置）")
        sources.append(entry)
        result["source"] = entry["id"]
    if args.tag:
        for raw in args.tag:
            source_id, _, module_list = raw.partition("=")
            source = next((s for s in plan["research"]["sources"] if s.get("id") == source_id), None)
            if source is None:
                raise PlanError(f"没有来源 {source_id}")
            for module in split_list([module_list]):
                if module not in source.setdefault("modules", []):
                    source["modules"].append(module)
    if collections or args.source_url:
        result["harvested"] = harvest_to_knowledge(plan)
    save_plan(plan, html_path, data_path, args.design_root)
    result["summary"] = research_summary(plan)
    result["retryQueries"] = len(plan["research"].get("retryQueries", []))
    print_result(result)
    return 0


def command_adopt(args: argparse.Namespace) -> int:
    """Convert an HTML-only plan (already moved into its folder by migrate-layout) into a data-first plan."""
    number = f"{int(args.number):02d}"
    output_root = args.output_root.resolve()
    folders = [f for f in sorted(output_root.glob(f"{number}-*")) if f.is_dir()]
    flat = sorted(output_root.glob(f"{number}-*.html"))
    if flat and not folders:
        raise PlanError("先运行 tech_plan.py migrate-layout，把旧方案移入项目文件夹")
    if len(folders) != 1:
        raise PlanError(f"编号 {number} 需要恰好一个项目文件夹，找到 {len(folders)} 个")
    stem = folders[0].name
    html_path, data_path = plan_paths(output_root, stem)
    if data_path.exists():
        raise PlanError(f"{stem} 已经是数据化方案")
    project_types = split_list(args.project_type)
    unknown = [item for item in project_types if item not in PROJECT_TYPES]
    if unknown:
        raise PlanError(f"未知项目类型: {unknown}")
    plan = new_plan_data(
        number=number, topic=stem.split("-", 1)[1], mode=args.mode, summary=args.summary,
        project_types=project_types, modules=split_list(args.modules), output_root=output_root,
        archetypes=split_list(args.archetype), cards=args.card,
    )
    data_path.parent.mkdir(parents=True, exist_ok=True)
    if html_path.is_file():
        os.replace(html_path, data_path.parent / "legacy.html")
    add_revision(plan, f"由旧版 HTML 方案迁移为数据化方案；原页面保存为 {RAW_DIRNAME}/legacy.html。", "tech_plan.py adopt")
    save_plan(plan, html_path, data_path, args.design_root)
    saved, problems = write_rule_cards(plan, data_path.parent)
    payload = {"ok": True, "path": str(html_path), "data": str(data_path), "rules": len(plan["checklist"]["rules"]), "ruleCards": saved,
               **({"ruleCardErrors": problems} if problems else {})}
    print_result(payload)
    announce_rule_cards(payload)
    return 0


def command_migrate_layout(args: argparse.Namespace) -> int:
    """Move plans from the old flat layout (NN-主题.html + _data/NN-主题/) into NN-主题/{NN-主题.html, raw/}."""
    output_root = args.output_root.resolve()
    moved = []
    legacy_root = output_root / LEGACY_DATA_DIRNAME
    for html_file in sorted(output_root.glob("*.html")):
        if not PLAN_NAME.fullmatch(html_file.stem):
            continue
        stem = html_file.stem
        folder = output_root / stem
        (folder / RAW_DIRNAME).mkdir(parents=True, exist_ok=True)
        legacy_data = legacy_root / stem
        if legacy_data.is_dir():
            for item in legacy_data.iterdir():
                target = folder / RAW_DIRNAME / item.name
                if target.exists():
                    raise PlanError(f"目标已存在，停止迁移: {target}")
                os.replace(item, target)
            legacy_data.rmdir()
        os.replace(html_file, folder / f"{stem}.html")
        data_path = folder / RAW_DIRNAME / "plan.json"
        if data_path.is_file():
            plan = load_json(data_path)
            plan["outputRoot"] = str(output_root)
            save_plan(plan, folder / f"{stem}.html", data_path, args.design_root)
        moved.append(stem)
    if legacy_root.is_dir() and not any(legacy_root.iterdir()):
        legacy_root.rmdir()
    print_result({"ok": True, "moved": moved})
    return 0


def command_feature(args: argparse.Namespace) -> int:
    """Add, update or remove a custom feature (needs beyond the base features)."""
    plan, html_path, data_path = _load(args.plan, args.output_root)
    features = plan.setdefault("customFeatures", [])
    if args.none:
        plan["customFeaturesNone"] = args.none
    elif args.remove:
        before = len(features)
        features[:] = [f for f in features if f.get("id") != args.remove]
        if len(features) == before:
            raise PlanError(f"没有定制功能 {args.remove}")
    else:
        if not args.name or not args.desc:
            raise PlanError("新增定制功能需要 --name 与 --desc")
        existing = next((f for f in features if f.get("name") == args.name), None)
        if existing is None:
            numbers = [int(f["id"][3:]) for f in features if str(f.get("id", "")).startswith("CF-")]
            existing = {"id": f"CF-{max(numbers, default=0) + 1:02d}", "name": args.name}
            features.append(existing)
        existing.update({"description": args.desc, "priority": args.priority, "modules": split_list(args.module),
                         "source": args.source})
        plan.pop("customFeaturesNone", None)
        for module in existing["modules"]:
            if module not in plan["modules"]:
                plan["modules"].append(module)
        derive_checklist(plan)
        if plan.get("moduleSpecs"):
            sync_specs(plan)
    save_plan(plan, html_path, data_path, args.design_root)
    print_result({"ok": True, "customFeatures": features})
    return 0


def sync_specs(plan: dict[str, Any]) -> list[str]:
    """Copy library specs for plan modules that no spec covers yet; existing (adapted) specs are kept."""
    specs = plan.setdefault("moduleSpecs", [])
    covered = {m for spec in specs for m in spec.get("modules", [])}
    have = {spec.get("id") for spec in specs}
    added = []
    replaced = standard_spec_ids(plan)
    for spec in ms.specs_for(plan.get("modules", [])):
        if spec["id"] in replaced:
            continue
        if spec["id"] not in have and not covered.issuperset(spec.get("modules", [])):
            specs.append(spec)
            added.append(spec["id"])
    library_order = {spec["id"]: spec.get("order", 999) for spec in ms.load_library()}
    specs.sort(key=lambda spec: (spec.get("order", library_order.get(spec.get("id"), 999)), str(spec.get("id"))))
    return added


def command_spec(args: argparse.Namespace) -> int:
    """Module detailed design: copy from the library, replace with an adapted file, or remove."""
    plan, html_path, data_path = _load(args.plan, args.output_root)
    specs = plan.setdefault("moduleSpecs", [])
    result: dict[str, Any] = {"ok": True}
    if args.remove:
        before = len(specs)
        specs[:] = [spec for spec in specs if spec.get("id") != args.remove]
        if len(specs) == before:
            raise PlanError(f"没有模块详细设计 {args.remove}")
        result["removed"] = args.remove
    elif args.file:
        spec = json.loads(Path(args.file).read_text(encoding="utf-8"))
        problems = ms.validate_spec(spec)
        if problems:
            raise PlanError("；".join(problems))
        specs[:] = [item for item in specs if item.get("id") != spec["id"]] + [spec]
        sync_specs(plan)  # re-sort only; nothing missing gets added twice
        result["replaced"] = spec["id"]
    elif args.sync:
        result["added"] = sync_specs(plan)
    if args.remove or args.file or args.sync:
        for section in plan["sections"]:
            if SECTION_LIBRARY.get(section["id"], {}).get("specDriven") and specs:
                section["blocks"] = [b for b in section["blocks"] if b.get("type") != "todo"]
        save_plan(plan, html_path, data_path, args.design_root)
    result["modules"] = [{"id": s.get("id"), "name": s.get("name"), "apis": len(s.get("apis", [])), "tables": len(s.get("tables", []))} for s in specs]
    result["problems"] = ms.validate_specs(specs, with_library=True)
    print_result(result)
    return 0


def command_standard(args: argparse.Namespace) -> int:
    """Standard modules the plan reuses: sync from the catalog, set a parameter, record a deviation, remove."""
    plan, html_path, data_path = _load(args.plan, args.output_root)
    chosen = plan.setdefault("standardModules", [])
    catalog = {m["id"]: m for m in load_standard_modules()}
    result: dict[str, Any] = {"ok": True}
    if args.sync:
        result["added"] = sync_standard_modules(plan)
        replaced = standard_spec_ids(plan)
        dropped = [s["id"] for s in plan.get("moduleSpecs", []) if s.get("id") in replaced]
        plan["moduleSpecs"] = [s for s in plan.get("moduleSpecs", []) if s.get("id") not in replaced]
        result["specsReplaced"] = dropped
    if args.module:
        if args.module not in catalog:
            raise PlanError(f"标准模块 {args.module} 不存在；可选：{'、'.join(catalog)}")
        item = next((m for m in chosen if m["id"] == args.module), None)
        if item is None:
            item = {"id": args.module, "params": {}, "deviations": []}
            chosen.append(item)
        if args.remove:
            chosen[:] = [m for m in chosen if m["id"] != args.module]
        for pair in args.set or []:
            key, sep, value = pair.partition("=")
            if not sep or not key.strip():
                raise PlanError(f"--set 需要 KEY=VALUE：{pair}")
            item["params"][key.strip()] = value.strip()
        for deviation in args.deviation or []:
            if deviation.strip():
                item["deviations"].append(deviation.strip())
    save_plan(plan, html_path, data_path, args.design_root)
    result["standardModules"] = [{"id": m["id"], "params": m.get("params"), "deviations": m.get("deviations")} for m in chosen]
    print_result(result)
    return 0


def command_design(args: argparse.Namespace) -> int:
    """Register the frontend address the user gave after approving the plan (references/design-integration.md).

    --session: the Luz System session (system design + prototype, hash-approved); --frontend: the frontend
    source directories (mini-program, admin) when they already exist. Either or both; --skip for backend-only work.
    """
    plan, html_path, data_path = _load(args.plan, args.output_root)
    if args.skip:
        plan["design"] = {"skip": args.skip, "checkedAt": now_iso()}
    else:
        if not args.session and not args.frontend:
            raise PlanError("请给出前端地址：--session <Luz System 会话目录> 和/或 --frontend <前端工程目录>")
        design: dict[str, Any] = {"checkedAt": now_iso(), "errors": []}
        if args.session:
            import design_intake  # noqa: PLC0415 - sibling script

            report = design_intake.inspect_session(Path(args.session))
            design.update({
                "session": report["session"], "sessionId": report["sessionId"], "title": report["title"],
                "stages": {k: {key: v.get(key) for key in ("status", "hash_ok", "approved_by", "problem")} for k, v in report["stages"].items()},
                "counts": report["counts"],
                "errors": design_intake.readiness_errors(report, ["architecture", "system_design", "prototype"]),
            })
            design_intake.write_json(data_path.parent / "design-intake.json", report)
        design["frontend"] = [{"path": str(Path(f)), "exists": Path(f).is_dir()} for f in (args.frontend or [])]
        plan["design"] = design
    save_plan(plan, html_path, data_path, args.design_root)
    print_result({"ok": not plan["design"].get("errors"), "design": {k: v for k, v in plan["design"].items() if k != "stages"}})
    return 0


def command_rule(args: argparse.Namespace) -> int:
    """采用的规则: which rule-card rules (section labels or preference keys) this plan applies, and how (plan.json → adoptedRules)."""
    plan, html_path, data_path = _load(args.plan, args.output_root)
    rows = plan.setdefault("adoptedRules", [])
    result: dict[str, Any] = {"ok": True}
    if args.remove:
        before = len(rows)
        rows[:] = [row for row in rows if str(row.get("rule", "")).strip() != args.remove.strip()]
        if len(rows) == before:
            raise PlanError(f"采用的规则里没有「{args.remove}」")
    elif args.rule is not None or args.how is not None:
        rule, how = (args.rule or "").strip(), (args.how or "").strip()
        if not rule or not how:
            raise PlanError("记录采用的规则需要同时给 --rule（规则卡上的小节标签或偏好 key）与 --how（本次怎么落实）")
        row = next((r for r in rows if str(r.get("rule", "")).strip() == rule), None)
        if row is None:
            rows.append({"rule": rule, "how": how})
        else:
            row["how"] = how
        cards = "\n".join(p.read_text(encoding="utf-8") for p in sorted((data_path.parent / RULES_DIRNAME).glob("*.md")))
        if rule not in cards:
            result["warning"] = (f"规则卡里找不到「{rule}」：照抄规则卡第 3 部分的小节标签（例如 SKILL.md › Engineering rules）"
                                 "或第 1 部分括号里的偏好 key，方便复核")
    if args.remove or args.rule is not None or args.how is not None:
        save_plan(plan, html_path, data_path, args.design_root)
    result["adoptedRules"] = rows
    result["ruleCards"] = [str(p) for p in sorted((data_path.parent / RULES_DIRNAME).glob("*.md"))]
    print_result(result)
    return 0


def command_sediment(args: argparse.Namespace) -> int:
    """本次沉淀: note keys this call wrote or confirmed with record_experience.py note (plan.json → sediment)."""
    plan, html_path, data_path = _load(args.plan, args.output_root)
    rows = plan.setdefault("sediment", [])
    changed = False
    if args.remove:
        before = len(rows)
        rows[:] = [row for row in rows if str(row.get("key", "")).strip() != args.remove.strip()]
        if len(rows) == before:
            raise PlanError(f"本次沉淀里没有「{args.remove}」")
        changed = True
    notes = load_notes()
    for key in split_list(args.key):
        note = notes.get(key)
        if note is None:
            raise PlanError(f"experience/notes.json 里没有 key「{key}」：先运行 python -X utf8 scripts/record_experience.py note "
                            f'--kind preference|lesson|fact --key {key} --text "<一句话原则>"')
        entry = {"key": key, "kind": note.get("kind", ""), "text": note.get("text", ""), "recordedAt": now_iso()[:10]}
        rows[:] = [row for row in rows if str(row.get("key", "")).strip() != key] + [entry]
        changed = True
    if changed:
        save_plan(plan, html_path, data_path, args.design_root)
    print_result({"ok": True, "sediment": rows, "problems": sediment_problems(plan, "delivery" if plan.get("approval") else "review")})
    return 0


def command_list(args: argparse.Namespace) -> int:
    items = []
    for data_path in sorted(args.output_root.glob(f"*/{RAW_DIRNAME}/plan.json")) if args.output_root.is_dir() else []:
        plan = load_json(data_path)
        items.append({"number": plan.get("number"), "topic": plan.get("topic"), "mode": plan.get("mode"), "status": plan.get("status")})
    print_result({"plans": items})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Luz Dev 技术方案：创建、调研、核对、审核与开发门禁。")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT, help=argparse.SUPPRESS)
    parser.add_argument("--design-root", type=Path, default=None, help="Luz Design 包；默认 LUZ_DESIGN_ROOT 或 ../Luz-Design")
    sub = parser.add_subparsers(dest="command", required=True)

    new = sub.add_parser("new", help="创建编号方案草稿")
    new.add_argument("topic")
    new.add_argument("--mode", required=True, choices=TASK_MODES)
    new.add_argument("--project-type", action="append", help=f"可多次或逗号分隔：{','.join(PROJECT_TYPES)}")
    new.add_argument("--modules", action="append", help="业务模块，逗号分隔，例如 payment,coupon,order,face")
    new.add_argument("--archetype", action="append", help="系统原型：mall,food,booking,membership-store,saas-admin；自动加入该类系统的模块与同类系统调研")
    new.add_argument("--summary", default="本方案由 Luz Dev 在任务执行前生成，用于记录调研、选型、架构、契约、安全、测试与交付决策。")
    new.add_argument("--card", action="append", help="规则卡种类，可重复或逗号分隔（context.py list）；替换按模式和项目类型推出的卡，tech-plan 总会写入")
    new.set_defaults(func=command_new)

    def with_plan(name: str, help_text: str) -> argparse.ArgumentParser:
        item = sub.add_parser(name, help=help_text)
        item.add_argument("plan", help="方案编号（如 05）、HTML 路径或 plan.json 路径")
        return item

    render = with_plan("render", "根据 plan.json 重新渲染 HTML")
    render.add_argument("--sync", action="store_true", help="按当前 projectTypes/modules 重新派生核对表")
    render.set_defaults(func=command_render)

    check = with_plan("check", "检查方案是否满足某阶段条件")
    check.add_argument("--stage", choices=("review", "approve", "development", "delivery"), default="review")
    check.set_defaults(func=command_check)

    gate = with_plan("gate", "开发门禁：方案已批准且内容未变时通过")
    gate.set_defaults(func=command_gate)

    submit = with_plan("submit", "补全后提交用户审核")
    submit.add_argument("--note", default="")
    submit.set_defaults(func=command_submit)

    approve = with_plan("approve", "记录用户批准（只能在用户明确批准后运行）")
    approve.add_argument("--note", required=True, help="用户原话或确认依据")
    approve.add_argument("--by", default="用户")
    approve.set_defaults(func=command_approve)

    revise = with_plan("revise", "追加修订；已批准方案转为修订中")
    revise.add_argument("--note", required=True)
    revise.add_argument("--basis", default="用户反馈")
    revise.set_defaults(func=command_revise)

    mark = with_plan("mark", "更新核对表条目状态")
    mark.add_argument("ids", nargs="+", help="规则或基础功能编号，可逗号分隔")
    mark.add_argument("--status", required=True, choices=CHECK_STATUSES)
    mark.add_argument("--note")
    mark.add_argument("--evidence")
    mark.set_defaults(func=command_mark)

    decide = with_plan("decide", "新增或决定待决问题")
    decide.add_argument("id")
    decide.add_argument("--question")
    decide.add_argument("--option", action="append")
    decide.add_argument("--recommendation")
    decide.add_argument("--blocking", action="store_true")
    decide.add_argument("--choice", help="用户的决定")
    decide.add_argument("--defer", help="延后原因")
    decide.set_defaults(func=command_decide)

    research = with_plan("research", "生成调研计划、调用 Luz-Crawl、导入来源")
    research.add_argument("--build", action="store_true", help="只生成 Luz-Crawl 计划文件")
    research.add_argument("--run", action="store_true", help="调用 Luz-Crawl collect 并导入结果")
    research.add_argument("--ingest", action="append", help="导入已有 Luz-Crawl 合集目录")
    research.add_argument("--read", type=int, default=2, help="每个查询打开的结果数（1-20）")
    research.add_argument("--topics", action="append", help="只调研这些模块或必查主题（逗号分隔；tracks 表示全部必查主题）")
    research.add_argument("--only", choices=("links", "queries"), help="只执行官方原文直读或只执行检索")
    research.add_argument("--batch", type=int, default=30, help="每批检索任务数（Luz-Crawl 单次最多 60）")
    research.add_argument("--pause", type=float, default=0, help="批次之间暂停的分钟数，让站点预算恢复")
    research.add_argument("--crawl-root", type=Path)
    research.add_argument("--add-query", action="append", help="platform=module|query")
    research.add_argument("--add-link", action="append", help="module|https://… 直接打开的官方原文")
    research.add_argument("--no-topic", action="store_true", help="不使用方案标题生成通用检索（标题不是业务名称时）")
    research.add_argument("--skip", help="跳过调研的原因")
    research.add_argument("--user-consent", action="store_true", help="用户明确同意跳过调研")
    research.add_argument("--source-url")
    research.add_argument("--source-title")
    research.add_argument("--source-tier", choices=tuple(TIER_LABELS))
    research.add_argument("--source-module", action="append")
    research.add_argument("--source-evidence")
    research.add_argument("--source-excerpt")
    research.add_argument("--no-tracks", action="store_true", help="不做必查主题（安全、部署、备份…）；只用于不涉及上线系统的方案")
    research.add_argument("--tag", action="append", help="S3=payment,refund：为来源补充模块标签")
    research.set_defaults(func=command_research)

    feature = with_plan("feature", "记录定制功能（基础功能之外的需求）")
    feature.add_argument("--name")
    feature.add_argument("--desc")
    feature.add_argument("--priority", default="P1", choices=("P0", "P1", "P2"))
    feature.add_argument("--module", action="append", help="涉及模块，可逗号分隔；会加入方案模块并更新核对表")
    feature.add_argument("--source", default="用户需求", help="来源，例如 用户需求、Luz System F-003")
    feature.add_argument("--remove", help="删除指定编号，如 CF-02")
    feature.add_argument("--none", help="本项目没有定制功能的说明")
    feature.set_defaults(func=command_feature)

    spec = with_plan("spec", "模块详细设计：从模块库复制、替换为调整后的文件或删除")
    spec_group = spec.add_mutually_exclusive_group()
    spec_group.add_argument("--sync", action="store_true", help="为尚无详细设计的模块复制 references/module-specs/ 中的设计")
    spec_group.add_argument("--file", help="用调整后的 JSON 文件新增或替换同 id 的模块设计")
    spec_group.add_argument("--remove", help="删除指定 id 的模块设计")
    spec.set_defaults(func=command_spec)

    standard = with_plan("standard", "采用的标准模块：同步、设置本项目取值、记录与标准的差异")
    standard.add_argument("--sync", action="store_true", help="按方案模块加入标准模块，并去掉被标准模块取代的详细设计")
    standard.add_argument("--module", help="标准模块编号，见 references/standard-modules.json")
    standard.add_argument("--set", action="append", help="KEY=VALUE，本项目取值")
    standard.add_argument("--deviation", action="append", help="与标准模块的差异（写进方案）")
    standard.add_argument("--remove", action="store_true", help="不采用该标准模块")
    standard.set_defaults(func=command_standard)

    design = with_plan("design", "方案批准后登记前端地址：Luz System 设计会话（核验批准与哈希）和/或前端工程目录")
    design.add_argument("--session", help=r"Luz System 会话目录，例如 D:\Codes\skill输出\Luz-System\001-项目")
    design.add_argument("--frontend", action="append", help="前端工程目录（小程序、后台），可多次")
    design.add_argument("--skip", help="不接入前端的原因（例如纯后端任务）")
    design.set_defaults(func=command_design)

    rule = with_plan("rule", "采用的规则：记录本方案落实了规则卡上的哪条规则或偏好、怎么落实（不加参数时列出）")
    rule.add_argument("--rule", help="规则卡第 3 部分的小节标签（如 SKILL.md › Engineering rules）或第 1 部分括号里的偏好 key")
    rule.add_argument("--how", help="本次怎么落实")
    rule.add_argument("--remove", help="删除这条规则的记录")
    rule.set_defaults(func=command_rule)

    sediment = with_plan("sediment", "本次沉淀：登记本次用 record_experience.py note 写入或确认的经验 key（不加参数时列出并核对）")
    sediment.add_argument("--key", action="append", help="经验 key，可重复或逗号分隔；必须已在 experience/notes.json")
    sediment.add_argument("--remove", help="删除这条 key 的登记")
    sediment.set_defaults(func=command_sediment)

    adopt = sub.add_parser("adopt", help="把旧版 HTML 方案迁移为数据化方案")
    adopt.add_argument("number")
    adopt.add_argument("--mode", required=True, choices=TASK_MODES)
    adopt.add_argument("--project-type", action="append")
    adopt.add_argument("--modules", action="append")
    adopt.add_argument("--archetype", action="append")
    adopt.add_argument("--summary", default="由旧版方案迁移。")
    adopt.add_argument("--card", action="append", help="规则卡种类（context.py list）；默认按模式和项目类型")
    adopt.set_defaults(func=command_adopt)

    migrate = sub.add_parser("migrate-layout", help="把旧的平铺结构迁移为 NN-主题/{NN-主题.html, raw/}")
    migrate.set_defaults(func=command_migrate_layout)

    listing = sub.add_parser("list", help="列出数据化方案")
    listing.set_defaults(func=command_list)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (PlanError, ValueError, OSError, RuntimeError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
