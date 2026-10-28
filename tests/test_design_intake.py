"""Luz System 会话接入：批准与哈希、ID 提取、绑定表、前端覆盖与原型数据残留检查。"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import design_intake  # noqa: E402

ARCHITECTURE = """<main data-artifact="system-architecture"><h1>健身房</h1>
<tr data-function-id="F-001"><td>购卡支付</td></tr>
<tr data-page-id="P-001" data-function-id="F-001"><td>会员卡列表</td></tr>
<tr data-page-id="P-002"><td>支付结果</td></tr>
<tr data-entity-id="D-001"><td>会员卡订单</td></tr>
<tr data-integration-id="I-001"><td>微信支付</td></tr>
<tr data-flow-id="FL-001"><td>购卡</td></tr></main>"""
PROTOTYPE = """<main data-artifact="interactive-prototype">
<div data-page-id="P-001" data-fixture-id="DEMO-001">年卡 ¥1999</div>
<button data-action-id="A-001" data-page-id="P-001">立即购买</button>
<button data-action-id="A-001" data-page-id="P-001" data-state-kind="加载">加载</button>
<button data-action-id="A-002" data-page-id="P-002">查看订单</button></main>"""


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_session(base: Path, approve: bool = True) -> Path:
    session = base / "001-健身房"
    (session / "01-system").mkdir(parents=True)
    (session / "03-prototype").mkdir()
    (session / "99-delivery").mkdir()
    (session / "01-system" / "03-system-architecture.html").write_text(ARCHITECTURE, encoding="utf-8")
    (session / "01-system" / "04-system-design.html").write_text(ARCHITECTURE, encoding="utf-8")
    (session / "03-prototype" / "interactive-prototype.html").write_text(PROTOTYPE, encoding="utf-8")
    (session / "03-prototype" / "acceptance.png").write_bytes(b"png")
    manifest = {"session_id": "SES-001", "title": "健身房", "status": "draft", "approvals_file": "99-delivery/APPROVALS.json",
                "stages": {"architecture": {"path": "01-system/03-system-architecture.html"},
                           "system_design": {"path": "01-system/04-system-design.html"},
                           "prototype": {"path": "03-prototype/interactive-prototype.html"}}}
    (session / "99-delivery" / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    approvals = {"schema_version": "2.0", "module_scope": {"status": "approved", "artifact_path": "01-system/03-system-architecture.html",
                                                           "artifact_sha256": sha(session / "01-system/03-system-architecture.html"),
                                                           "selected_modules": ["购卡支付"]}}
    for name, path in manifest["stages"].items():
        approvals[name] = {"status": "approved" if approve else "pending", "artifact_path": path["path"],
                           "artifact_sha256": sha(session / path["path"]) if approve else None, "approved_by": "用户"}
    approvals["prototype"].update({"browser_acceptance_path": "03-prototype/acceptance.png",
                                   "browser_acceptance_sha256": sha(session / "03-prototype/acceptance.png")})
    (session / "99-delivery" / "APPROVALS.json").write_text(json.dumps(approvals, ensure_ascii=False), encoding="utf-8")
    return session


def run(*argv: str) -> tuple[int, dict]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = design_intake.main(list(argv))
    text = out.getvalue()
    return code, (json.loads(text) if text.strip().startswith("{") else {})


class DesignIntakeTests(unittest.TestCase):
    def test_inspect_extracts_ids_and_verifies_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            session = make_session(Path(temporary))
            report = design_intake.inspect_session(session)
            self.assertEqual(report["counts"]["actions"], 2)
            self.assertEqual(report["ids"]["actions"]["A-001"]["label"], "立即购买")
            self.assertEqual(report["ids"]["actions"]["A-001"]["context"]["data-page-id"], ["P-001"])
            self.assertIn("加载", report["ids"]["actions"]["A-001"]["context"]["data-state-kind"])
            self.assertIn("I-001", report["ids"]["integrations"])
            self.assertTrue(all(state["hash_ok"] for state in report["stages"].values()))
            code, _ = run("inspect", str(session), "--require", "architecture,system_design,prototype")
            self.assertEqual(code, 0)

    def test_changed_artifact_invalidates_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            session = make_session(Path(temporary))
            (session / "03-prototype" / "interactive-prototype.html").write_text(PROTOTYPE + "<p>改动</p>", encoding="utf-8")
            code, result = run("inspect", str(session), "--require", "prototype")
            self.assertEqual(code, 1)
            self.assertTrue(any("批准失效" in error for error in result["errors"]))

    def test_unapproved_design_cannot_be_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            session = make_session(Path(temporary), approve=False)
            code, result = run("bind", str(session), "--out", str(Path(temporary) / "b.json"))
            self.assertEqual(code, 1)
            self.assertTrue(any("尚未批准" in error for error in result["errors"]))

    def test_bindings_and_frontend_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            session = make_session(base)
            out = base / "contracts" / "design-bindings.json"
            self.assertEqual(run("bind", str(session), "--out", str(out))[0], 0)
            bindings = json.loads(out.read_text(encoding="utf-8"))
            code, result = run("check", str(out))
            self.assertEqual(code, 1)  # 尚未填写
            self.assertTrue(any("A-001" in error for error in result["errors"]))

            for row in bindings["actions"]:
                row.update({"A-001": {"kind": "api", "api": "POST /api/v1/app/orders/{id}/pay"},
                            "A-002": {"kind": "navigate", "api": ""}}[row["id"]])
            bindings["entities"][0]["table"] = "orders"
            bindings["integrations"][0]["adapter"] = "wechat_pay.apiv3"
            for row in bindings["pages"]:
                row["route"] = "/pages/x"
            out.write_text(json.dumps(bindings, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(run("check", str(out))[0], 0)

            app = base / "miniprogram"
            (app / "pages" / "card").mkdir(parents=True)
            (app / "pages" / "card" / "index.wxml").write_text(
                '<view data-page-id="P-001"><button data-action-id="A-001">立即购买</button></view>', encoding="utf-8")
            code, result = run("check", str(out), "--frontend", str(app))
            self.assertEqual(code, 1)
            self.assertTrue(any("A-002" in e for e in result["errors"]))
            self.assertTrue(any("P-002" in e for e in result["errors"]))

            (app / "pages" / "result").mkdir()
            (app / "pages" / "result" / "index.wxml").write_text(
                '<view data-page-id="P-002"><button data-action-id="A-002">查看订单</button></view>', encoding="utf-8")
            code, result = run("check", str(out), "--frontend", str(app))
            self.assertEqual(code, 0, result)
            self.assertEqual(result["coverage"][str(app)]["actions"], "2/2")

            (app / "pages" / "card" / "data.js").write_text("import cards from './mock/cards'\n", encoding="utf-8")
            (app / "pages" / "card" / "extra.wxml").write_text('<button data-action-id="A-099">彩蛋</button>', encoding="utf-8")
            code, result = run("check", str(out), "--frontend", str(app))
            self.assertEqual(code, 1)
            self.assertTrue(any("mock" in e for e in result["errors"]))
            self.assertTrue(any("A-099" in e and "回到 Luz System" in e for e in result["errors"]))

    def test_rebinding_keeps_filled_rows_and_flags_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            session = make_session(base)
            report = design_intake.inspect_session(session)
            bindings = design_intake.build_bindings(report)
            bindings["actions"][0]["api"] = "POST /x"
            report["ids"]["actions"].pop("A-002")
            again = design_intake.build_bindings(report, bindings)
            rows = {row["id"]: row for row in again["actions"]}
            self.assertEqual(rows["A-001"]["api"], "POST /x")
            self.assertTrue(rows["A-002"]["removedFromDesign"])


if __name__ == "__main__":
    unittest.main()
