"""Technical-plan engine: lifecycle gates, research ingest, checklist, rendering safety, standards."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Research imports write to the knowledge base; keep tests away from the real knowledge/.
os.environ["LUZ_DEV_KNOWLEDGE"] = tempfile.mkdtemp(prefix="luz-knowledge-")
sys.path.insert(0, str(ROOT / "scripts"))

import build_standards  # noqa: E402
import tech_plan  # noqa: E402


def run(output: Path, *argv: str) -> int:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return tech_plan.main(["--output-root", str(output), *argv])


def load(output: Path, number: str = "01") -> dict:
    _, data_path = tech_plan.find_plan(number, output)
    return json.loads(data_path.read_text(encoding="utf-8"))


def save(output: Path, plan: dict, number: str = "01") -> None:
    html_path, data_path = tech_plan.find_plan(number, output)
    tech_plan.save_plan(plan, html_path, data_path)


def complete(plan: dict) -> dict:
    """Fill every section and checklist item the way an agent would before submitting."""
    for section in plan["sections"]:
        section["blocks"] = [block for block in section["blocks"] if block["type"] != "todo"]
        if not tech_plan.SECTION_LIBRARY[section["id"]].get("generated") and not section["blocks"]:
            section["blocks"] = [{"type": "p", "text": f"{section['title']}的做法已确定 [S1]。"}]
    for kind in ("rules", "baselines"):
        for item in plan["checklist"][kind]:
            item["status"] = "planned"
            item["note"] = "方案中已写明做法。"
    plan["research"]["sources"] = [
        {"id": f"S{i}", "title": f"来源{i}", "url": f"https://pay.weixin.qq.com/doc/v3/merchant/{i}", "tier": "official",
         "read": True, "status": "captured", "modules": ["payment", "face", "refund"], "evidence": "x", "excerpt": ""}
        for i in range(1, 8)
    ] + [{"id": "S8", "title": "开源同类系统", "url": "https://github.com/example/mall", "tier": "opensource",
          "read": True, "status": "captured", "modules": ["system"], "evidence": "x", "excerpt": ""},
         {"id": "S9", "title": "OWASP Cheat Sheet", "url": "https://cheatsheetseries.owasp.org/cheatsheets/x.html", "tier": "standard",
          "read": True, "status": "captured", "modules": list(tech_plan.research_tracks(plan)), "evidence": "x", "excerpt": ""}]
    plan["research"]["collections"] = [{"path": "x", "title": "x"}]
    plan["design"] = plan.get("design") or {"skip": "测试夹具：设计接入在专门的测试中覆盖"}
    if not plan.get("customFeatures"):
        plan["customFeatures"] = [{"id": "CF-01", "name": "人脸入场", "description": "会员刷脸入场，可用会员码替代",
                                   "priority": "P0", "modules": ["face"], "source": "用户需求"}]
    return plan


class LifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.output = Path(self.temp.name)
        self.assertEqual(run(self.output, "new", "健身房下单小程序", "--mode", "implementation",
                             "--project-type", "miniprogram,admin,backend", "--modules", "payment,refund,coupon,order,face"), 0)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_new_plan_has_data_file_and_derived_checklist(self) -> None:
        plan = load(self.output)
        rules = {item["id"] for item in plan["checklist"]["rules"]}
        self.assertIn("PAY-005", rules)
        self.assertIn("FACE-002", rules)
        self.assertIn("MKT-002", rules)
        self.assertNotIn("AGENT-001", rules)
        baselines = {item["id"] for item in plan["checklist"]["baselines"]}
        self.assertIn("BL-MP-07", baselines)
        self.assertIn("BL-AD-03", baselines)
        self.assertEqual(plan["status"], "draft")
        html = (self.output / "01-健身房下单小程序" / "01-健身房下单小程序.html").read_text(encoding="utf-8")
        self.assertIn('id="security"', html)
        self.assertIn("回调先验签", html)  # 规范要求按标题展示
        self.assertNotIn("PAY-005", html)  # 内部编号只在 plan.json
        self.assertTrue(html.split("<body>", 1)[1].lstrip().startswith('<main class="doc">\n<h1>'))

    def test_draft_cannot_be_submitted_or_pass_gate(self) -> None:
        self.assertEqual(run(self.output, "submit", "01"), 1)
        self.assertEqual(run(self.output, "gate", "01"), 1)
        errors, _ = tech_plan.check_plan(load(self.output), "review")
        self.assertTrue(any("待补全" in error for error in errors))
        self.assertTrue(any("Luz-Crawl" in error for error in errors))
        self.assertTrue(any("PAY-005" in error for error in errors))

    def test_full_lifecycle_and_hash_guard(self) -> None:
        save(self.output, complete(load(self.output)))
        self.assertEqual(run(self.output, "approve", "01", "--note", "用户：可以"), 2)  # not in review yet
        self.assertEqual(run(self.output, "submit", "01"), 0)
        self.assertEqual(run(self.output, "gate", "01"), 1)
        self.assertEqual(run(self.output, "approve", "01", "--note", "用户在对话中回复：方案通过"), 0)
        self.assertEqual(run(self.output, "gate", "01"), 0)

        # Editing the approved content without a revision breaks the gate.
        plan = load(self.output)
        plan["sections"][0]["blocks"].append({"type": "p", "text": "悄悄扩大范围"})
        save(self.output, plan)
        self.assertEqual(run(self.output, "gate", "01"), 1)

        # A recorded revision moves the plan back to review.
        self.assertEqual(run(self.output, "revise", "01", "--note", "增加团课预约"), 0)
        self.assertEqual(load(self.output)["status"], "revising")
        self.assertEqual(run(self.output, "submit", "01"), 0)
        self.assertEqual(run(self.output, "approve", "01", "--note", "用户：修订通过"), 0)
        self.assertEqual(run(self.output, "gate", "01"), 0)

    def test_delivery_evidence_does_not_break_approval(self) -> None:
        save(self.output, complete(load(self.output)))
        run(self.output, "submit", "01")
        run(self.output, "approve", "01", "--note", "用户：通过")
        self.assertEqual(run(self.output, "check", "01", "--stage", "delivery"), 1)
        self.assertEqual(run(self.output, "mark", "01", "PAY-005", "--status", "done"), 2)  # evidence required
        ids = [item["id"] for item in load(self.output)["checklist"]["rules"]]
        ids += [item["id"] for item in load(self.output)["checklist"]["baselines"]]
        self.assertEqual(run(self.output, "mark", "01", ",".join(ids), "--status", "done", "--evidence", "pytest 101 passed"), 0)
        self.assertEqual(run(self.output, "gate", "01"), 0)
        self.assertEqual(run(self.output, "check", "01", "--stage", "delivery"), 0)

    def test_blocking_decision_must_be_decided_before_approval(self) -> None:
        save(self.output, complete(load(self.output)))
        run(self.output, "decide", "01", "D1", "--question", "人脸用哪种方式？", "--option", "门禁厂商", "--option", "云人员库", "--blocking")
        self.assertEqual(run(self.output, "submit", "01"), 0)
        self.assertEqual(run(self.output, "approve", "01", "--note", "用户：通过"), 1)
        # Recording the user's decision changes reviewed content, so it goes back through submit.
        run(self.output, "decide", "01", "D1", "--choice", "门禁厂商人员库")
        self.assertEqual(run(self.output, "approve", "01", "--note", "用户：通过，人脸用门禁厂商"), 0)

    def test_strict_research_skip_requires_user_consent(self) -> None:
        plan = complete(load(self.output))
        plan["research"]["sources"] = []
        plan["research"]["collections"] = []
        for section in plan["sections"]:
            for block in section["blocks"]:
                if block.get("type") == "p":
                    block["text"] = block["text"].replace(" [S1]", "")
        save(self.output, plan)
        run(self.output, "research", "01", "--skip", "离线环境")
        errors, _ = tech_plan.check_plan(load(self.output))
        self.assertTrue(any("用户明确同意" in error for error in errors))
        run(self.output, "research", "01", "--skip", "离线环境", "--user-consent")
        errors, _ = tech_plan.check_plan(load(self.output))
        self.assertFalse(any("调研" in error or "来源" in error for error in errors))

    def test_missing_source_reference_is_reported(self) -> None:
        plan = complete(load(self.output))
        plan["sections"][0]["blocks"] = [{"type": "p", "text": "结论 [S99]"}]
        errors, _ = tech_plan.check_plan(plan)
        self.assertIn("引用了不存在的来源 [S99]", errors)


class ArchetypeTests(unittest.TestCase):
    def test_archetype_adds_modules_queries_and_requires_open_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            self.assertEqual(run(base, "new", "健身房系统", "--mode", "implementation", "--project-type", "backend",
                                 "--archetype", "booking", "--modules", "face"), 0)
            plan = load(base)
            self.assertEqual(plan["archetypes"], ["booking"])
            for module in ("face", "reservation", "recharge", "order", "payment"):
                self.assertIn(module, plan["modules"])
            self.assertEqual(plan["modules"][0], "face")  # explicit modules first
            queries = tech_plan.build_research_queries(plan)
            self.assertTrue(any(q["reason"].startswith("system:") for q in queries))
            self.assertTrue(any(q["reason"].startswith("reservation:") for q in queries))
            complete(plan)
            plan["research"]["sources"] = [s for s in plan["research"]["sources"] if s["tier"] != "opensource"]
            errors, _ = tech_plan.check_plan(plan)
            self.assertTrue(any("开源参考" in error for error in errors))

    def test_unknown_archetype_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(run(Path(temporary), "new", "x", "--mode", "implementation", "--archetype", "spaceship"), 2)


class DesignAndLaunchTests(unittest.TestCase):
    def test_frontend_plan_needs_design_session_and_has_launch_section(self) -> None:
        sys.path.insert(0, str(ROOT / "tests"))
        from test_design_intake import make_session  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            run(base, "new", "健身房", "--mode", "implementation", "--project-type", "miniprogram,backend", "--modules", "deploy,legal,compliance")
            plan = load(base)
            self.assertIn("launch", [section["id"] for section in plan["sections"]])
            rules = {item["id"] for item in plan["checklist"]["rules"]}
            for rule in ("INT-001", "INT-002", "OPS-003", "LEGAL-001", "COMP-001", "LIC-001"):
                self.assertIn(rule, rules)
            plan = complete(plan)
            plan["design"] = {}
            errors, _ = tech_plan.check_plan(plan)
            self.assertTrue(any("Luz Design" in error for error in errors))

            session = make_session(base / "design")
            self.assertEqual(run(base, "design", "01", "--session", str(session)), 0)
            plan = load(base)
            self.assertEqual(plan["design"]["counts"]["actions"], 2)
            self.assertEqual(plan["design"]["errors"], [])
            html = (base / "01-健身房" / "01-健身房.html").read_text(encoding="utf-8")
            self.assertNotIn("哈希", html)  # 设计批准状态留在 plan.json，不写进页面
            _, data_path = tech_plan.find_plan("01", base)
            self.assertTrue((data_path.parent / "design-intake.json").is_file())

    def test_backend_only_plan_has_no_design_requirement(self) -> None:
        plan = tech_plan.new_plan_data(number="01", topic="t", mode="implementation", summary="s",
                                       project_types=["backend"], modules=[], output_root=Path("."))
        plan = complete(plan)
        plan.pop("design")
        errors, _ = tech_plan.check_plan(plan)
        self.assertFalse(any("Luz Design" in error for error in errors))


class FeatureTests(unittest.TestCase):
    def test_features_section_lists_base_and_custom(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            run(base, "new", "健身房管理系统", "--mode", "implementation", "--project-type", "miniprogram,admin,backend")
            plan = load(base)
            self.assertEqual([s["id"] for s in plan["sections"]][:2], ["overview", "features"])
            errors, _ = tech_plan.check_plan(plan)
            self.assertTrue(any("定制功能" in e for e in errors))
            self.assertEqual(run(base, "feature", "01", "--name", "人脸入场", "--desc", "会员刷脸入场，可用会员码替代",
                                 "--priority", "P0", "--module", "face"), 0)
            self.assertEqual(run(base, "feature", "01", "--name", "抽奖", "--desc", "消费后抽奖", "--module", "lottery"), 0)
            plan = load(base)
            self.assertEqual([f["id"] for f in plan["customFeatures"]], ["CF-01", "CF-02"])
            self.assertIn("face", plan["modules"])
            self.assertIn("FACE-002", {r["id"] for r in plan["checklist"]["rules"]})  # 模块带出相应规范
            html = (base / "01-健身房管理系统" / "01-健身房管理系统.html").read_text(encoding="utf-8")
            for text in ("基础功能", "定制功能", "微信登录与会话保持", "个人中心：资料查看与修改", "用户管理", "订单列表", "人脸入场"):
                self.assertIn(text, html)
            self.assertEqual(run(base, "feature", "01", "--remove", "CF-02"), 0)
            self.assertEqual(len(load(base)["customFeatures"]), 1)


class LayoutTests(unittest.TestCase):
    def test_project_folder_holds_html_and_raw(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            (base / "03-旧方案.html").write_text("<html></html>", encoding="utf-8")
            run(base, "new", "健身房管理系统", "--mode", "implementation", "--project-type", "backend")
            folder = base / "04-健身房管理系统"
            self.assertEqual(sorted(p.name for p in folder.iterdir()), ["04-健身房管理系统.html", "raw"])
            self.assertTrue((folder / "raw" / "plan.json").is_file())
            self.assertEqual(run(base, "research", "04", "--build"), 0)
            self.assertTrue((folder / "raw" / "research-plan.json").is_file())
            self.assertEqual(run(base, "check", str(folder / "04-健身房管理系统.html")), 1)  # resolves by HTML path

    def test_migrate_layout_moves_old_plans(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            run(base, "new", "迁移示例", "--mode", "analysis")
            folder = base / "01-迁移示例"
            # 构造旧结构：平铺 HTML + _data/
            (base / "_data" / "01-迁移示例").mkdir(parents=True)
            for item in (folder / "raw").iterdir():
                item.replace(base / "_data" / "01-迁移示例" / item.name)
            (folder / "01-迁移示例.html").replace(base / "01-迁移示例.html")
            (folder / "raw").rmdir()
            folder.rmdir()
            (base / "02-只有页面.html").write_text("<html></html>", encoding="utf-8")
            self.assertEqual(run(base, "migrate-layout"), 0)
            self.assertTrue((folder / "raw" / "plan.json").is_file())
            self.assertTrue((folder / "01-迁移示例.html").is_file())
            self.assertFalse((base / "_data").exists())
            self.assertTrue((base / "02-只有页面" / "02-只有页面.html").is_file())
            self.assertEqual(run(base, "adopt", "02", "--mode", "analysis"), 0)
            self.assertTrue((base / "02-只有页面" / "raw" / "legacy.html").is_file())
            self.assertTrue((base / "02-只有页面" / "raw" / "plan.json").is_file())


class ResearchRunnerTests(unittest.TestCase):
    def test_queries_are_interleaved_by_site(self) -> None:
        queries = [{"platform": p, "query": f"{p}{i}", "reason": "x:y"} for p in ("csdn", "bing") for i in range(3)]
        ordered = tech_plan.interleave_by_platform(queries)
        self.assertEqual([q["platform"] for q in ordered], ["csdn", "bing"] * 3)

    def test_cooled_searches_are_retried_and_done_ones_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            run(base, "new", "库存调研", "--mode", "implementation", "--project-type", "backend", "--modules", "inventory")
            plan = load(base)
            queries = tech_plan.build_research_queries(plan)
            done, cooled = queries[0], queries[1]
            collection = base / "crawl" / "001_x"
            (collection / "raw").mkdir(parents=True)
            index = {"title": "x", "results": [
                {**done, "status": "captured", "candidates": [{"url": "https://blog.csdn.net/a"}], "sources": []},
                {**cooled, "status": "failed", "limitations": ["Platform is cooling down; no browser or download request was sent."]},
            ]}
            (collection / "raw" / "collection.json").write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(run(base, "research", "01", "--ingest", str(collection)), 0)
            plan = load(base)
            self.assertEqual(len(plan["research"]["retryQueries"]), 1)
            remaining = [(q["platform"], q["query"]) for q in tech_plan.build_research_queries(plan)]
            self.assertNotIn((done["platform"], done["query"]), remaining)
            self.assertIn((cooled["platform"], cooled["query"]), remaining)

    def test_build_splits_searches_into_batches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            run(base, "new", "商城", "--mode", "implementation", "--project-type", "backend", "--archetype", "mall")
            self.assertEqual(run(base, "research", "01", "--build", "--batch", "10"), 0)
            _, data_path = tech_plan.find_plan("01", base)
            batches = sorted(data_path.parent.glob("research-plan*.json"))
            self.assertGreater(len(batches), 1)
            sizes = [len(json.loads(b.read_text(encoding="utf-8"))) for b in batches]
            self.assertTrue(all(size <= 10 for size in sizes))
            self.assertEqual(sum(sizes), len(tech_plan.build_research_queries(load(base))))


class ResearchIngestTests(unittest.TestCase):
    def make_collection(self, root: Path) -> Path:
        collection = root / "001_调研"
        official = collection / "raw" / "sources" / "links" / "001_官方"
        blog = collection / "raw" / "sources" / "csdn" / "002_博客"
        for folder, title, text in ((official, "支付成功回调通知", "回调需要在5秒内应答"), (blog, "回调幂等实践", "博客内容")):
            (folder / "raw").mkdir(parents=True)
            (folder / "raw" / "content.json").write_text(json.dumps({"title": title, "text": text}, ensure_ascii=False), encoding="utf-8")
        index = {
            "title": "调研",
            "results": [
                {"url": "https://pay.weixin.qq.com/doc/v3/merchant/4012791902.md", "status": "captured",
                 "resolved_url": "https://pay.weixin.qq.com/doc/v3/merchant/4012791902.md", "folder": str(official),
                 "url_check": {"requested": "https://pay.weixin.qq.com/doc/v3/merchant/4012791902.md", "valid_content": True, "target_confirmed": True}},
                {"platform": "csdn", "query": "支付 幂等", "reason": "payment:实践", "status": "partial",
                 "sources": [{"resolved_url": "https://blog.csdn.net/x/article/1", "status": "captured", "folder": str(blog),
                              "url_check": {"valid_content": True, "target_confirmed": True}},
                             {"resolved_url": "https://blog.csdn.net/x/article/2", "status": "failed",
                              "url_check": {"valid_content": False, "target_confirmed": False}}]},
            ],
        }
        (collection / "raw" / "collection.json").write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
        return collection

    def test_ingest_classifies_tiers_and_read_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            run(base, "new", "支付调研", "--mode", "implementation", "--project-type", "backend", "--modules", "payment")
            collection = self.make_collection(base / "crawl")
            self.assertEqual(run(base, "research", "01", "--ingest", str(collection)), 0)
            sources = {s["url"]: s for s in load(base)["research"]["sources"]}
            official = sources["https://pay.weixin.qq.com/doc/v3/merchant/4012791902.md"]
            self.assertEqual(official["tier"], "official")
            self.assertTrue(official["read"])
            self.assertIn("payment", official["modules"])
            self.assertEqual(official["title"], "支付成功回调通知")
            self.assertEqual(sources["https://blog.csdn.net/x/article/1"]["tier"], "community")
            self.assertEqual(sources["https://blog.csdn.net/x/article/1"]["modules"], ["payment"])
            self.assertFalse(sources["https://blog.csdn.net/x/article/2"]["read"])
            # Re-ingesting the same collection does not duplicate sources.
            run(base, "research", "01", "--ingest", str(collection))
            self.assertEqual(len(load(base)["research"]["sources"]), 3)

    def test_build_writes_links_and_queries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            run(base, "new", "支付调研", "--mode", "implementation", "--project-type", "backend", "--modules", "payment,refund")
            self.assertEqual(run(base, "research", "01", "--add-link", "payment|https://example.com/doc", "--build"), 0)
            _, data_path = tech_plan.find_plan("01", base)
            links = (data_path.parent / "research-links.txt").read_text(encoding="utf-8").split()
            self.assertIn("https://pay.weixin.qq.com/doc/v3/merchant/4012791903.md", links)
            self.assertIn("https://example.com/doc", links)
            queries = json.loads((data_path.parent / "research-plan.json").read_text(encoding="utf-8"))
            self.assertTrue(all(":" in q["reason"] for q in queries))
            self.assertLessEqual(len(queries), 60)

    def test_tier_classification_respects_paths(self) -> None:
        self.assertEqual(tech_plan.classify_tier("https://cloud.tencent.com/document/product/867"), "official")
        self.assertEqual(tech_plan.classify_tier("https://cloud.tencent.com/developer/article/1"), "community")
        self.assertEqual(tech_plan.classify_tier("https://www.cac.gov.cn/2025-03/21/c_1744174262156096.htm"), "standard")
        self.assertEqual(tech_plan.classify_tier("https://github.com/wechatpay-apiv3/x"), "opensource")


class RenderingSafetyTests(unittest.TestCase):
    def test_text_is_escaped_and_svg_is_sanitized(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            run(base, "new", "安全渲染", "--mode", "analysis")
            plan = load(base)
            plan["title"] = "<img src=x onerror=alert(1)>标题"
            plan["sections"][0]["blocks"] = [
                {"type": "p", "text": "<script>alert(1)</script> `code` **粗体** {{SECTIONS}}"},
                {"type": "svg", "svg": "<svg onload=alert(1)></svg>"},
                {"type": "svg", "svg": "<svg viewBox='0 0 10 10'><rect width='10' height='10'/></svg>", "caption": "图"},
            ]
            save(base, plan)
            html = (base / "01-安全渲染" / "01-安全渲染.html").read_text(encoding="utf-8")
            self.assertNotIn("<script", html)
            self.assertNotIn("<img src=x", html)
            self.assertIn("&lt;script&gt;", html)
            self.assertIn("<code>code</code>", html)
            self.assertIn("<strong>粗体</strong>", html)
            self.assertIn("图示含脚本或事件属性", html)
            self.assertIn("<rect width='10'", html)
            self.assertIn("{{SECTIONS}}", html)  # 正文中的字面量不会被再次展开

    def test_tokens_fall_back_to_snapshot_without_luz_design(self) -> None:
        tokens, source = tech_plan.load_tokens(Path(tempfile.gettempdir()) / "no-such-luz-design")
        self.assertIn("snapshot", source)
        self.assertEqual(tokens["action"], "#4A5BD6")


class StandardsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data = json.loads((ROOT / "references" / "standards.json").read_text(encoding="utf-8"))

    def test_canonical_standards_are_valid(self) -> None:
        self.assertEqual(build_standards.validate_standards(self.data), [])

    def test_payment_rules_cover_official_requirements(self) -> None:
        rules = {rule["id"]: rule for rule in self.data["rules"]}
        for rule_id in ("PAY-001", "PAY-005", "PAY-006", "PAY-007", "PAY-008", "PAY-009", "PAY-010", "PAY-011", "RFD-003", "RFD-005"):
            self.assertIn(rule_id, rules)
            self.assertEqual(rules[rule_id]["level"], "MUST")
        self.assertIn("WECHATPAY/SIGNTEST/", rules["PAY-006"]["rule"])
        self.assertIn("200 或 204", rules["PAY-009"]["rule"])

    def test_validator_rejects_broken_rules(self) -> None:
        broken = json.loads(json.dumps(self.data))
        broken["rules"][0]["level"] = "MAY"
        broken["rules"].append(dict(broken["rules"][1]))
        broken["baselines"][0]["rules"] = ["NOPE-001"]
        errors = build_standards.validate_standards(broken)
        self.assertTrue(any("level" in e for e in errors))
        self.assertTrue(any("duplicate" in e for e in errors))
        self.assertTrue(any("NOPE-001" in e for e in errors))

    def test_generated_page_matches_data(self) -> None:
        template = (ROOT / "assets" / "reference" / "standards.template.html").read_text(encoding="utf-8")
        html = build_standards.render(self.data, template)
        self.assertEqual(html.count('class="rule"'), len(self.data["rules"]))
        self.assertNotIn(".innerHTML", html)
        self.assertNotIn("<script src=", html)
        built = (ROOT / "references" / "standards.html").read_text(encoding="utf-8")
        self.assertEqual(built, html, "references/standards.html is stale; run scripts/build_standards.py")


if __name__ == "__main__":
    unittest.main()
