from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "references" / "function-catalog.json"
TEMPLATE_PATH = ROOT / "assets" / "reference" / "function-catalog.template.html"
BUILD_SCRIPT = ROOT / "scripts" / "build_function_catalog.py"
OUTPUT_PATH = ROOT / "references" / "function-catalog.html"
REQUIRED_MODULES = {
    "reservation",
    "order",
    "payment",
    "inventory",
    "membership",
    "coupon",
    "delivery",
    "lottery",
    "result-flow",
}


def run_builder(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        [sys.executable, "-X", "utf8", str(BUILD_SCRIPT), *arguments],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"builder failed ({result.returncode})\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


class CatalogDataTests(unittest.TestCase):
    def test_initial_catalog_has_required_modules_and_concrete_logic(self) -> None:
        from scripts.build_function_catalog import validate_catalog

        data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
        self.assertEqual(validate_catalog(data), [])
        module_ids = {module["id"] for module in data["modules"]}
        self.assertTrue(REQUIRED_MODULES <= module_ids)
        function_count = sum(len(module["functions"]) for module in data["modules"])
        self.assertGreaterEqual(function_count, 24)
        for module in data["modules"]:
            for function in module["functions"]:
                self.assertGreaterEqual(len(function["rows"]), 5)
                self.assertTrue(any("测试" in row["label"] or "验证" in row["label"] for row in function["rows"]))
                self.assertTrue(any("失败" in row["label"] or "状态" in row["label"] for row in function["rows"]))

    def test_result_policy_defines_return_surfaces_and_payment_confirmation(self) -> None:
        data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
        self.assertEqual(data["schemaVersion"], "1.1")
        policy = data["resultPolicy"]
        rule_ids = {rule["id"] for rule in policy["rules"]}
        self.assertTrue(
            {"inline", "form-error", "result", "processing", "partial", "failure"}
            <= rule_ids
        )
        self.assertGreaterEqual(len(policy["rules"]), 6)
        payment = policy["payment"]
        self.assertIn("确认", payment["processingTitle"])
        self.assertIn("请勿重复支付", payment["unknownCopy"])
        self.assertIn("服务端", payment["authority"])
        intervals = payment["pollIntervalsSeconds"]
        self.assertTrue(intervals and intervals == sorted(intervals) and all(value > 0 for value in intervals))
        result_module = next(module for module in data["modules"] if module["id"] == "result-flow")
        self.assertGreaterEqual(len(result_module["functions"]), 6)

    def test_duplicate_function_ids_are_rejected(self) -> None:
        from scripts.build_function_catalog import validate_catalog

        data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
        duplicate = json.loads(json.dumps(data, ensure_ascii=False))
        duplicate["modules"][1]["functions"][0]["id"] = duplicate["modules"][0]["functions"][0]["id"]
        errors = validate_catalog(duplicate)
        self.assertTrue(any("重复" in error for error in errors), errors)


class CatalogBuildTests(unittest.TestCase):
    def test_builds_a_self_contained_reading_page(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "catalog.html"
            result = run_builder("--data", str(DATA_PATH), "--template", str(TEMPLATE_PATH), "--output", str(output))
            payload = json.loads(result.stdout)
            self.assertEqual(Path(payload["output"]), output.resolve())
            html = output.read_text(encoding="utf-8")
            data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
            for control_id in ("search-input", "module-filter", "count", "result-policy"):
                self.assertIn(f'id="{control_id}"', html)
            # A Luz Design reading page: the title first, no application shell, every function rendered statically.
            self.assertRegex(html, r'<main class="doc">\s*<h1>')
            for shell in ("<aside", "<nav", "<header", "<footer"):
                self.assertNotIn(shell, html)
            for module in data["modules"]:
                self.assertIn(f'id="m-{module["id"]}"', html)
                for function in module["functions"]:
                    self.assertIn(f'id="{function["id"]}"', html)
            self.assertNotIn(".innerHTML", html)
            self.assertNotIn("<script src=", html)
            # The only <link> is the inline SVG tab icon; nothing is fetched from outside.
            self.assertEqual(html.count("<link rel="), 1)
            self.assertIn('<link rel="icon" href="data:image/svg+xml,', html)
            self.assertIn('aria-live="polite"', html)

    def test_rendering_escapes_markup_in_catalog_text(self) -> None:
        from scripts.build_function_catalog import render

        data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
        data["modules"][0]["functions"][0]["rows"][0]["value"] = "</td><script>alert(1)</script>"
        page = render(data, TEMPLATE_PATH.read_text(encoding="utf-8"))
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)

    def test_builder_rejects_invalid_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            invalid = Path(temporary) / "invalid.json"
            invalid.write_text('{"schemaVersion":"1.0","modules":[]}', encoding="utf-8")
            result = run_builder(
                "--data",
                str(invalid),
                "--template",
                str(TEMPLATE_PATH),
                "--output",
                str(Path(temporary) / "catalog.html"),
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("校验", result.stderr)


class PackageCatalogIntegrationTests(unittest.TestCase):
    def test_generated_html_is_current(self) -> None:
        from scripts.build_function_catalog import render

        data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
        expected = render(data, TEMPLATE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(OUTPUT_PATH.read_text(encoding="utf-8"), expected, "run scripts/build_function_catalog.py")


if __name__ == "__main__":
    unittest.main()
