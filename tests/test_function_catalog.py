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
    def test_builds_self_contained_html_with_embedded_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "catalog.html"
            result = run_builder("--data", str(DATA_PATH), "--template", str(TEMPLATE_PATH), "--output", str(output))
            payload = json.loads(result.stdout)
            self.assertEqual(Path(payload["output"]), output.resolve())
            html = output.read_text(encoding="utf-8")
            for control_id in (
                "search-input",
                "module-filter",
                "edit-toggle",
                "add-module",
                "import-data",
                "export-json",
                "export-html",
                "reset-local",
                "result-decision-board",
            ):
                self.assertIn(f'id="{control_id}"', html)
            self.assertIn('id="catalog-data"', html)
            self.assertIn("textContent", html)
            self.assertNotIn(".innerHTML", html)
            self.assertNotIn("<script src=", html)
            self.assertNotIn("<link rel=", html)
            self.assertIn('aria-live="polite"', html)
            self.assertIn("prefers-reduced-motion", html)

    def test_generated_script_avoids_reserved_function_parameter(self) -> None:
        html = OUTPUT_PATH.read_text(encoding="utf-8")
        self.assertNotIn("renderFunction(module,moduleIndex,function,", html)
        self.assertIn("renderFunction(module,moduleIndex,fn,functionIndex)", html)

    def test_build_escapes_script_close_sequences(self) -> None:
        from scripts.build_function_catalog import render_catalog

        template = '<script id="catalog-data" type="application/json">{{CATALOG_DATA_JSON}}</script><main></main>'
        malicious = {"value": "</script><script>alert(1)</script>"}
        html = render_catalog(template, malicious)
        self.assertNotIn("</script><script>alert(1)</script>", html)
        self.assertIn("<\\/script>", html)
        embedded = html.split('type="application/json">', 1)[1].split("</script>", 1)[0]
        self.assertEqual(json.loads(embedded), malicious)

    def test_template_uses_admin_shell_without_double_sidebar_offset(self) -> None:
        template = TEMPLATE_PATH.read_text(encoding="utf-8")
        self.assertIn(".app{display:grid;grid-template-columns:240px minmax(0,1fr)", template)
        self.assertIn("@media(max-width:1439px)", template)
        self.assertIn(".page-head>div:first-child{min-width:0}", template)
        self.assertIn("--sidebar-bg:#FFFFFF", template)
        self.assertNotIn(".workspace{margin-left:", template)

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
    def test_generated_html_matches_canonical_json(self) -> None:
        from scripts.build_function_catalog import extract_embedded_catalog

        canonical = json.loads(DATA_PATH.read_text(encoding="utf-8"))
        embedded = extract_embedded_catalog(OUTPUT_PATH.read_text(encoding="utf-8"))
        self.assertEqual(embedded, canonical)


if __name__ == "__main__":
    unittest.main()
