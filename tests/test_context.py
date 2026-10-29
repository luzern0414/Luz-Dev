"""Rule cards: every card resolves to real rule text, carries the user's preferences, and says what to sediment."""

from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import context  # noqa: E402

SECTIONS = ["## 1 用户偏好", "## 2 相关教训与有效做法", "## 3 规则", "## 4 用什么做", "## 5 交付前必须通过", "## 6 收尾"]
KINDS = {"tech-plan", "backend-module", "frontend-integration", "launch-compliance", "review-diagnose", "agent-service"}


def rule_text(kind: str) -> str:
    text, _ = context.make_card(kind, [])
    return text.split("## 3 规则", 1)[1].split("## 4 用什么做", 1)[0]


class RuleCardTests(unittest.TestCase):
    def test_every_card_resolves_to_rule_sections(self) -> None:
        self.assertEqual(context.check_cards(), [])
        self.assertTrue(KINDS <= set(context.card_kinds()), context.card_kinds())

    def test_shared_core_is_byte_identical_to_luz_design(self) -> None:
        from luz_shell import resolve_design_root  # noqa: PLC0415 - --design-root / LUZ_DESIGN_ROOT / ../Luz-Design

        design = resolve_design_root() / "scripts" / "context.py"
        if not design.is_file():
            self.skipTest("the paired Luz-Design package is not available")

        def core(path: Path) -> bytes:
            data = path.read_bytes()
            start = data.index(b"# ---- shared rule-card core")
            end = data.index(b"# ---- end of shared rule-card core")
            return data[start:end]
        self.assertEqual(core(ROOT / "scripts" / "context.py"), core(design), "copy the shared core from Luz-Design byte for byte")

    def test_every_card_has_the_six_parts_and_real_rule_text(self) -> None:
        for kind in context.card_kinds():
            text, digest = context.make_card(kind, [])
            for heading in SECTIONS:
                self.assertIn(heading, text, f"{kind}: {heading}")
            self.assertRegex(digest, r"^[0-9a-f]{12}$")
            size = len(rule_text(kind).encode("utf-8"))
            # Big enough to carry the rules, small enough to be read whole (shorten a section with max_chars).
            self.assertGreater(size, 8_000, f"{kind}: rule text too short")
            self.assertLess(size, 36_000, f"{kind}: rule text too long; give a section max_chars")
            self.assertNotRegex(rule_text(kind), r"<(p|td|th|tr|li|ul|div|section|h[1-6])[\s>]", f"{kind}: HTML left in the card")

    def test_every_card_carries_the_engineering_rules(self) -> None:
        for kind in context.card_kinds():
            self.assertIn("### SKILL.md › Engineering rules", rule_text(kind), kind)
            self.assertIn("integer minor units", rule_text(kind), kind)

    def test_preferences_are_always_on_the_card(self) -> None:
        notes = context.load_json(context.NOTES_FILE, {"notes": []})["notes"]
        prefs = [n["key"] for n in notes if n["kind"] == "preference"]
        self.assertTrue(prefs)
        for kind in context.card_kinds():
            text, _ = context.make_card(kind, [])
            for key in prefs:
                self.assertIn(f"（{key}）", text, kind)

    def test_tech_plan_card_runs_the_checklist_instead_of_inlining_standards(self) -> None:
        text, _ = context.make_card("tech-plan", [])
        for phrase in ("tech_plan.py check NN", "tech_plan.py mark", "tech_plan.py submit NN", "tech_plan.py gate NN",
                       "tech_plan.py rule NN", "research.md（全文）", "Mandatory technical plan before execution"):
            self.assertIn(phrase, text)
        self.assertNotIn('"rules": [', text)  # standards.json reaches the plan as its checklist, not on the card
        self.assertNotRegex(text, r"PAY-0\d\d.{0,40}MUST")

    def test_cards_cut_the_rule_pages_they_name(self) -> None:
        launch = rule_text("launch-compliance")
        for phrase in ("### operations.md（全文）", "### compliance.md（全文）", "### legal-documents.md（全文）", "### dev-checklist.html › 10 上线与合规"):
            self.assertIn(phrase, launch)
        frontend = rule_text("frontend-integration")
        self.assertIn("design_intake.py", frontend)
        self.assertIn("### result-patterns.md（全文）", frontend)
        backend = rule_text("backend-module")
        self.assertIn("### patterns/README.md（全文）", backend)
        self.assertIn("### module-specs/README.md（全文）", backend)

    def test_markdown_section_ends_at_the_next_heading_of_the_same_level(self) -> None:
        engineering = context.section_text(ROOT / "SKILL.md", ["Engineering rules"])
        self.assertIn("integer minor units", engineering)
        self.assertNotIn("Platform defaults", engineering)
        security = context.section_text(ROOT / "references" / "operations.md", ["6. 安全加固"])
        self.assertNotIn("7. 上线检查表", security)

    def test_saved_card_records_its_hash_and_goes_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            card = context.save_card(Path(temp), "tech-plan")
            self.assertEqual(card, Path(temp) / "00-rules" / "tech-plan.md")
            self.assertRegex(card.read_text(encoding="utf-8").split("\n", 1)[0], r"^<!-- rules-sha256: [0-9a-f]{12} · \d{4}-\d{2}-\d{2} -->$")
            self.assertTrue(context.card_is_current(card))
            first, rest = card.read_text(encoding="utf-8").split("\n", 1)
            card.write_text(re.sub(r"[0-9a-f]{12}", "000000000000", first, count=1) + "\n" + rest, encoding="utf-8")
            self.assertFalse(context.card_is_current(card))

    def test_card_tells_what_to_sediment(self) -> None:
        text, _ = context.make_card("tech-plan", [])
        for phrase in ("本次沉淀", "preference", "lesson", "fact", "调研来源与范围", "同一 key", "record_experience.py note"):
            self.assertIn(phrase, text)

    def test_templates_say_they_are_skeletons(self) -> None:
        for rel in ("assets/tech-plan/template.html", "assets/project-template/PLAN.md", "assets/legal/privacy-policy.md",
                    "assets/deploy/LAUNCH-CHECKLIST.md"):
            head = (ROOT / rel).read_text(encoding="utf-8").splitlines()[:3]
            self.assertTrue(any("context.py" in line for line in head), rel)


class ExperienceStoreOverrideTests(unittest.TestCase):
    """LUZ_EXPERIENCE_STORE (tests and rehearsals only) takes note writes and feeds the rule card; the skill's own files stay untouched."""

    def test_override_store_takes_writes_and_feeds_the_card(self) -> None:
        import os
        import subprocess
        skill_root = Path(__file__).resolve().parents[1]
        experience = {p: p.read_bytes() for p in (skill_root / "experience").iterdir() if p.is_file()}
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "notes.json"
            env = {**os.environ, "LUZ_EXPERIENCE_STORE": str(store)}

            def run(*args: str) -> subprocess.CompletedProcess:
                return subprocess.run([sys.executable, "-X", "utf8", *args], cwd=skill_root, env=env, text=True,
                                      capture_output=True, encoding="utf-8", errors="replace", check=False)

            noted = run("scripts/record_experience.py", "note", "--kind", "preference", "--key", "override-probe", "--text", "探针偏好，只写测试库")
            self.assertEqual(noted.returncode, 0, noted.stderr)
            self.assertTrue(store.is_file())
            card = run("scripts/context.py", "tech-plan")
            self.assertEqual(card.returncode, 0, card.stderr)
            self.assertIn("探针偏好", card.stdout)
        self.assertEqual({p: p.read_bytes() for p in (skill_root / "experience").iterdir() if p.is_file()}, experience)


if __name__ == "__main__":
    unittest.main()
