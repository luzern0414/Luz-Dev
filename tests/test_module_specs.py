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
    """Standard modules are referenced, not copied; only custom modules get a detailed chapter."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.out = Path(self.temp.name)
        self.assertEqual(run(self.out, "new", "门店小程序", "--mode", "implementation", "--project-type", "backend",
                             "--modules", "login,order,payment,refund,face,iot"), 0)
        self.html, self.data = tech_plan.find_plan("01", self.out)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def plan(self) -> dict:
        return json.loads(self.data.read_text(encoding="utf-8"))

    def test_new_plan_references_standard_modules_and_details_custom_ones(self) -> None:
        plan = self.plan()
        self.assertEqual([m["id"] for m in plan["standardModules"]], ["login", "payment", "order", "refund"])
        self.assertEqual(sorted(s["id"] for s in plan["moduleSpecs"]), ["face", "iot"])
        page = self.html.read_text(encoding="utf-8")
        self.assertIn("采用的标准模块", page)
        self.assertIn("微信支付与对账", page)
        self.assertNotIn('<section id="module-payment">', page)  # 标准模块不再展开成章节
        self.assertIn('<section id="module-face"><h2>', page)
        self.assertRegex(page, r"<h3>\d+\.1 功能说明</h3>")
        self.assertIn("统一约定", page)
        self.assertNotIn("数据表总览", page)
        self.assertNotIn("方法与路径", page)
        self.assertEqual(page.count("<h1>"), 1)

    def test_standard_command_sets_values_and_deviations(self) -> None:
        before = tech_plan.content_hash(self.plan())
        self.assertEqual(run(self.out, "standard", "01", "--module", "order", "--set", "ORDER_PAY_TIMEOUT_MINUTES=10",
                             "--deviation", "不支持余额支付"), 0)
        item = next(m for m in self.plan()["standardModules"] if m["id"] == "order")
        self.assertEqual(item["params"], {"ORDER_PAY_TIMEOUT_MINUTES": "10"})
        self.assertNotEqual(before, tech_plan.content_hash(self.plan()))
        page = self.html.read_text(encoding="utf-8")
        self.assertIn("待支付超时：10", page)
        self.assertIn("不支持余额支付", page)
        self.assertEqual(run(self.out, "standard", "01", "--module", "nope"), 2)
        self.assertEqual(run(self.out, "standard", "01", "--module", "refund", "--remove"), 0)
        self.assertNotIn("refund", [m["id"] for m in self.plan()["standardModules"]])

    def test_spec_commands_keep_custom_modules_only(self) -> None:
        self.assertEqual(run(self.out, "spec", "01", "--remove", "iot"), 0)
        self.assertEqual(run(self.out, "spec", "01", "--sync"), 0)
        ids = sorted(s["id"] for s in self.plan()["moduleSpecs"])
        self.assertEqual(ids, ["face", "iot"])  # sync never brings back specs replaced by standard modules
        adapted = next(s for s in self.plan()["moduleSpecs"] if s["id"] == "face")
        adapted["summary"] = "门店人脸入场。"
        path = self.out / "face.json"
        path.write_text(json.dumps(adapted, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(run(self.out, "spec", "01", "--file", str(path)), 0)
        self.assertEqual(next(s for s in self.plan()["moduleSpecs"] if s["id"] == "face")["summary"], "门店人脸入场。")

    def test_standard_sync_replaces_copied_specs_of_older_plans(self) -> None:
        plan = self.plan()
        plan["standardModules"] = []
        plan["moduleSpecs"] = ms.specs_for(plan["modules"])  # 旧方案：全部复制
        self.data.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(run(self.out, "standard", "01", "--sync"), 0)
        plan = self.plan()
        self.assertEqual(sorted(s["id"] for s in plan["moduleSpecs"]), ["face", "iot"])
        self.assertEqual(len(plan["standardModules"]), 4)

    def test_backend_plan_without_modules_fails_review(self) -> None:
        plan = self.plan()
        plan["moduleSpecs"] = []
        plan["standardModules"] = []
        errors, _ = tech_plan.check_plan(plan, "review")
        self.assertTrue(any("标准模块" in e for e in errors))

    def test_tables_of_missing_modules_are_warnings(self) -> None:
        plan = self.plan()
        order = copy.deepcopy(next(s for s in ms.load_library() if s["id"] == "order"))
        order["writes"].append({"when": "余额支付", "table": "wallet_ledger", "content": "扣减流水"})
        plan["moduleSpecs"] = [order]
        _, warnings = tech_plan.check_plan(plan, "review")
        self.assertTrue(any("wallet_ledger" in w for w in warnings))


if __name__ == "__main__":
    unittest.main()
