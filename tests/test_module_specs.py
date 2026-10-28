"""模块详细设计：模块库校验、新方案自动复制、逐模块编号渲染、表与写入一致性、spec 命令。"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("LUZ_DEV_KNOWLEDGE", tempfile.mkdtemp(prefix="luz-knowledge-"))
sys.path.insert(0, str(ROOT / "scripts"))

import module_specs as ms  # noqa: E402
import tech_plan  # noqa: E402


def run(output: Path, *argv: str) -> int:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return tech_plan.main(["--output-root", str(output), *argv])


class LibraryTests(unittest.TestCase):
    def test_library_is_valid_and_complete(self) -> None:
        library = ms.load_library()
        self.assertEqual(ms.validate_specs(library), [])
        ids = {spec["id"] for spec in library}
        for required in ("login", "order", "payment", "refund", "coupon", "foundation"):
            self.assertIn(required, ids)
        for spec in library:
            for table in spec["tables"]:
                for field in table["fields"]:
                    self.assertNotIn(field["name"], {"id", "created_at", "updated_at"}, f"{table['name']} 重复列出公共列")

    def test_write_to_undefined_table_and_duplicates_are_errors(self) -> None:
        order = copy.deepcopy(next(s for s in ms.load_library() if s["id"] == "order"))
        order["writes"].append({"when": "x", "table": "ghost_table", "content": "x"})
        self.assertTrue(any("ghost_table" in e for e in ms.validate_specs([order], with_library=True)))
        twin = copy.deepcopy(order)
        twin["id"] = "order2"
        self.assertTrue(any("同时定义" in e for e in ms.validate_specs([order, twin])))

    def test_field_type_and_placeholder_are_checked(self) -> None:
        spec = copy.deepcopy(next(s for s in ms.load_library() if s["id"] == "coupon"))
        spec["tables"][0]["fields"][0]["type"] = "STRING"
        spec["summary"] = "待填写"
        problems = ms.validate_spec(spec)
        self.assertTrue(any("类型无效" in p for p in problems))
        self.assertTrue(any("未完成" in p for p in problems))


class PlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.out = Path(self.temp.name)
        self.assertEqual(run(self.out, "new", "门店小程序", "--mode", "implementation", "--project-type", "backend",
                             "--modules", "login,order,payment,refund"), 0)
        self.html, self.data = tech_plan.find_plan("01", self.out)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def plan(self) -> dict:
        return json.loads(self.data.read_text(encoding="utf-8"))

    def test_new_plan_copies_specs_and_renders_numbered_modules(self) -> None:
        plan = self.plan()
        self.assertEqual([s["id"] for s in plan["moduleSpecs"]], ["login", "order", "payment", "refund"])
        page = self.html.read_text(encoding="utf-8")
        self.assertIn('<section id="module-payment"><h2>', page)
        self.assertRegex(page, r"<h3>\d+\.1 功能说明</h3>")
        self.assertIn("数据表总览", page)
        self.assertIn("/api/v1/app/orders/preview", page)
        self.assertEqual(page.count("<h1>"), 1)

    def test_spec_commands_and_hash(self) -> None:
        before = tech_plan.content_hash(self.plan())
        self.assertEqual(run(self.out, "spec", "01", "--remove", "refund"), 0)
        self.assertNotIn("refund", [s["id"] for s in self.plan()["moduleSpecs"]])
        self.assertNotEqual(before, tech_plan.content_hash(self.plan()))
        self.assertEqual(run(self.out, "spec", "01", "--sync"), 0)
        self.assertIn("refund", [s["id"] for s in self.plan()["moduleSpecs"]])
        adapted = next(s for s in self.plan()["moduleSpecs"] if s["id"] == "order")
        adapted["summary"] = "门店小程序的订单。"
        path = self.out / "order.json"
        path.write_text(json.dumps(adapted, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(run(self.out, "spec", "01", "--file", str(path)), 0)
        specs = self.plan()["moduleSpecs"]
        self.assertEqual(next(s for s in specs if s["id"] == "order")["summary"], "门店小程序的订单。")
        self.assertEqual([s["id"] for s in specs], ["login", "order", "payment", "refund"])

    def test_backend_plan_without_specs_fails_review(self) -> None:
        plan = self.plan()
        plan["moduleSpecs"] = []
        errors, _ = tech_plan.check_plan(plan, "review")
        self.assertTrue(any("模块详细设计" in e for e in errors))

    def test_tables_of_missing_modules_are_warnings(self) -> None:
        _, warnings = tech_plan.check_plan(self.plan(), "review")
        self.assertTrue(any("wallet_ledger" in w for w in warnings))


if __name__ == "__main__":
    unittest.main()
