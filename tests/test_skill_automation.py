from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Research imports write to the knowledge base; keep tests away from the real knowledge/.
os.environ["LUZ_DEV_KNOWLEDGE"] = tempfile.mkdtemp(prefix="luz-knowledge-")
PLAN_SCRIPT = ROOT / "scripts" / "create_tech_plan.py"
EXPERIENCE_SCRIPT = ROOT / "scripts" / "record_experience.py"


def run_cli(script: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        [sys.executable, "-X", "utf8", str(script), *args],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"command failed ({result.returncode})\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


class TechPlanGeneratorTests(unittest.TestCase):
    REQUIRED_SECTIONS = {
        "overview",
        "solution",
        "architecture",
        "routes",
        "data",
        "modules",
        "security",
        "testing-delivery",
        "reusable",
    }

    def test_creates_numbered_plan_and_required_sections(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            result = run_cli(
                PLAN_SCRIPT,
                "花店小程序后端技术方案",
                "--mode",
                "implementation",
                "--output-root",
                str(output),
            )
            payload = json.loads(result.stdout)
            plan = Path(payload["path"])
            self.assertEqual(payload["sequence"], "01")
            self.assertEqual(plan.name, "01-花店小程序后端技术方案.html")
            html = plan.read_text(encoding="utf-8")
            for section in self.REQUIRED_SECTIONS:
                self.assertIn(f'id="{section}"', html)
            self.assertIn("花店小程序后端技术方案", html)
            self.assertIn("implementation", html)
            self.assertNotIn('id="revisions"', html)  # 审批与修订只在 plan.json

    def test_rendered_plan_uses_luz_admin_layout_contract(self) -> None:
        template = (ROOT / "assets" / "tech-plan" / "template.html").read_text(encoding="utf-8")
        self.assertIn("{{SECTIONS}}", template)
        self.assertIn("{{SHELL_CSS}}", template)
        with tempfile.TemporaryDirectory() as temporary:
            result = run_cli(PLAN_SCRIPT, "布局契约", "--mode", "analysis", "--output-root", temporary)
            html = Path(json.loads(result.stdout)["path"]).read_text(encoding="utf-8")
        # Luz Design reading page: centered container, no application shell, type scale 14/16/20/24/32
        self.assertIn(".doc{width:min(1120px,100%);margin:0 auto", html)
        for shell in ("<nav", "<aside", "<footer", "sidebar"):
            self.assertNotIn(shell, html)
        import re as _re
        sizes = {int(x) for x in _re.findall(r"font-size:(\d+)px", html)}
        self.assertTrue(sizes <= {14, 16, 20, 24, 32}, sizes)
        self.assertIn("--action:#4A5BD6", html)

    def test_allocates_sequential_numbers_concurrently(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)

            def create(index: int) -> str:
                result = run_cli(
                    PLAN_SCRIPT,
                    f"并发方案{index}",
                    "--mode",
                    "analysis",
                    "--output-root",
                    str(output),
                )
                return json.loads(result.stdout)["path"]

            with ThreadPoolExecutor(max_workers=5) as executor:
                paths = list(executor.map(create, range(5)))

            names = sorted(Path(item).name for item in paths)
            sequences = [name.split("-", 1)[0] for name in names]
            self.assertEqual(sequences, ["01", "02", "03", "04", "05"])
            self.assertEqual(len(set(paths)), 5)
            self.assertTrue(all(name.endswith(".html") for name in names))

    def test_sanitizes_unsafe_topic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            result = run_cli(
                PLAN_SCRIPT,
                "../花店:支付/回调",
                "--mode",
                "diagnosis",
                "--output-root",
                str(output),
            )
            plan = Path(json.loads(result.stdout)["path"])
            self.assertEqual(plan.parent.parent.resolve(), output.resolve())
            self.assertEqual(plan.parent.name, plan.stem)
            self.assertTrue((plan.parent / "raw" / "plan.json").is_file())
            self.assertNotIn("..", plan.name)
            self.assertNotIn(":", plan.name)
            self.assertNotIn("/", plan.name)

    def test_rejects_blank_topic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = run_cli(
                PLAN_SCRIPT,
                "   ",
                "--mode",
                "review",
                "--output-root",
                temporary,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("主题", result.stderr)


class ExperienceLifecycleTests(unittest.TestCase):
    def make_plan(self, temporary: Path) -> Path:
        plan = temporary / "01-测试任务技术方案.html"
        plan.write_text("<html><body>test plan</body></html>", encoding="utf-8")
        return plan

    def test_records_candidate_and_updates_index(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            experience = base / "experience"
            plan = self.make_plan(base)
            result = run_cli(
                EXPERIENCE_SCRIPT,
                "record",
                "--title",
                "支付回调幂等处理",
                "--task-mode",
                "implementation",
                "--plan",
                str(plan),
                "--summary",
                "使用持久化事件标识过滤重复回调。",
                "--tag",
                "payment",
                "--tag",
                "idempotency",
                "--experience-root",
                str(experience),
            )
            payload = json.loads(result.stdout)
            record = Path(payload["path"])
            self.assertTrue(record.exists())
            self.assertTrue(record.name.startswith("EXP-0001-"))
            body = record.read_text(encoding="utf-8")
            self.assertIn("status: candidate", body)
            self.assertIn("支付回调幂等处理", body)
            self.assertIn("payment", body)
            index = (experience / "INDEX.md").read_text(encoding="utf-8")
            self.assertIn("EXP-0001", index)
            self.assertIn(record.name, index)

    def test_promotion_requires_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            experience = base / "experience"
            plan = self.make_plan(base)
            run_cli(
                EXPERIENCE_SCRIPT,
                "record",
                "--title",
                "优惠券核销",
                "--task-mode",
                "implementation",
                "--plan",
                str(plan),
                "--summary",
                "测试条件更新与并发。",
                "--experience-root",
                str(experience),
            )
            result = run_cli(
                EXPERIENCE_SCRIPT,
                "promote",
                "EXP-0001",
                "--experience-root",
                str(experience),
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("证据", result.stderr)

    def test_promotes_and_deprecates_with_audit_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            experience = base / "experience"
            plan = self.make_plan(base)
            run_cli(
                EXPERIENCE_SCRIPT,
                "record",
                "--title",
                "抽奖概率校验",
                "--task-mode",
                "analysis",
                "--plan",
                str(plan),
                "--summary",
                "校验奖项概率边界和随机源。",
                "--experience-root",
                str(experience),
            )
            promoted = run_cli(
                EXPERIENCE_SCRIPT,
                "promote",
                "EXP-0001",
                "--evidence",
                "test:test_draw_probability_bounds",
                "--experience-root",
                str(experience),
            )
            self.assertEqual(json.loads(promoted.stdout)["status"], "verified")
            body = next((experience / "records").glob("EXP-0001-*.md")).read_text(encoding="utf-8")
            self.assertIn("status: verified", body)
            self.assertIn("test:test_draw_probability_bounds", body)

            missing_reason = run_cli(
                EXPERIENCE_SCRIPT,
                "deprecate",
                "EXP-0001",
                "--experience-root",
                str(experience),
                check=False,
            )
            self.assertNotEqual(missing_reason.returncode, 0)
            self.assertIn("原因", missing_reason.stderr)

            deprecated = run_cli(
                EXPERIENCE_SCRIPT,
                "deprecate",
                "EXP-0001",
                "--reason",
                "平台随机接口已升级",
                "--experience-root",
                str(experience),
            )
            self.assertEqual(json.loads(deprecated.stdout)["status"], "deprecated")
            body = next((experience / "records").glob("EXP-0001-*.md")).read_text(encoding="utf-8")
            self.assertIn("status: deprecated", body)
            self.assertIn("平台随机接口已升级", body)


class PackageValidationTests(unittest.TestCase):
    def test_validator_rejects_verified_record_without_evidence(self) -> None:
        from scripts.validate_skill_package import validate_experience_root

        with tempfile.TemporaryDirectory() as temporary:
            experience = Path(temporary) / "experience"
            for relative in (
                "records",
                "playbooks",
                "reusable/backend",
                "reusable/frontend",
                "reusable/scripts",
            ):
                (experience / relative).mkdir(parents=True, exist_ok=True)
            (experience / "README.md").write_text("# Experience\n", encoding="utf-8")
            (experience / "reusable" / "README.md").write_text("# Reusable\n", encoding="utf-8")
            (experience / "INDEX.md").write_text(
                "<!-- EXPERIENCE_ROWS_START -->\n<!-- EXPERIENCE_ROWS_END -->\n",
                encoding="utf-8",
            )
            (experience / "records" / "EXP-0001-invalid.md").write_text(
                '---\nid: "EXP-0001"\ntitle: "Invalid"\nstatus: verified\n'
                'created: "2026-10-26"\nupdated: "2026-10-26"\n'
                'task_mode: "implementation"\nplan: "01-plan.html"\n'
                "tags: []\nevidence: []\nreusable_assets: []\n---\n",
                encoding="utf-8",
            )
            errors = validate_experience_root(experience)
            self.assertTrue(any("证据" in error and "EXP-0001" in error for error in errors))

    def test_validator_rejects_missing_reusable_asset(self) -> None:
        from scripts.validate_skill_package import validate_experience_root

        with tempfile.TemporaryDirectory() as temporary:
            experience = Path(temporary) / "experience"
            for relative in (
                "records",
                "playbooks",
                "reusable/backend",
                "reusable/frontend",
                "reusable/scripts",
            ):
                (experience / relative).mkdir(parents=True, exist_ok=True)
            (experience / "README.md").write_text("# Experience\n", encoding="utf-8")
            (experience / "reusable" / "README.md").write_text("# Reusable\n", encoding="utf-8")
            (experience / "INDEX.md").write_text(
                "<!-- EXPERIENCE_ROWS_START -->\n<!-- EXPERIENCE_ROWS_END -->\n",
                encoding="utf-8",
            )
            record = experience / "records" / "EXP-0001-missing-asset.md"
            record.write_text(
                '---\nid: "EXP-0001"\ntitle: "Missing asset"\nstatus: verified\n'
                'created: "2026-10-26"\nupdated: "2026-10-26"\n'
                'task_mode: "implementation"\nplan: "01-plan.html"\n'
                'tags: []\nevidence: ["test:pass"]\n'
                'reusable_assets: ["missing-asset.py"]\n---\n',
                encoding="utf-8",
            )
            errors = validate_experience_root(experience)
            self.assertTrue(any("missing-asset.py" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
