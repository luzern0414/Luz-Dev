"""调研知识库：去重、复核周期、事实必须有来源、方案沉淀、下一个项目复用并跳过重复爬取。"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import knowledge as kb  # noqa: E402
import tech_plan  # noqa: E402

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
PAY_DOC = "https://pay.weixin.qq.com/doc/v3/merchant/4012791902.md"


def run(output: Path, *argv: str) -> int:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return tech_plan.main(["--output-root", str(output), *argv])


class KnowledgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "knowledge"
        self.previous = os.environ.get("LUZ_DEV_KNOWLEDGE")
        os.environ["LUZ_DEV_KNOWLEDGE"] = str(self.root)

    def tearDown(self) -> None:
        if self.previous is None:
            os.environ.pop("LUZ_DEV_KNOWLEDGE", None)
        else:
            os.environ["LUZ_DEV_KNOWLEDGE"] = self.previous
        self.temp.cleanup()

    def test_normalize_and_dedupe(self) -> None:
        self.assertEqual(kb.normalize_url("HTTPS://Blog.CSDN.net/a/article/1/?utm_source=x&id=2#top"),
                         "https://blog.csdn.net/a/article/1?id=2")
        base = kb.Knowledge()
        first, created = base.add_source(url=PAY_DOC, title="回调", tier="official", modules=["payment"], checked_at=NOW.isoformat())
        again, created_again = base.add_source(url=PAY_DOC + "#x", title="", tier="official", modules=["refund"],
                                               checked_at=(NOW + timedelta(days=1)).isoformat())
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(first["modules"], ["payment", "refund"])
        self.assertEqual(first["kind"], "platform")

    def test_freshness_by_kind(self) -> None:
        base = kb.Knowledge()
        doc, _ = base.add_source(url=PAY_DOC, title="回调", tier="official", modules=["payment"], checked_at=NOW.isoformat())
        law, _ = base.add_source(url="https://www.gov.cn/zhengce/x.htm", title="办法", tier="standard", modules=["face"], checked_at=NOW.isoformat())
        later = NOW + timedelta(days=120)
        self.assertFalse(base.is_fresh(doc, later))  # 平台文档 90 天复核
        self.assertTrue(base.is_fresh(law, later))   # 法规 365 天
        self.assertEqual([s["id"] for s in base.sources_for(["payment", "face"], later)], [law["id"]])

    def test_facts_need_known_sources(self) -> None:
        base = kb.Knowledge()
        with self.assertRaises(kb.KnowledgeError):
            base.add_fact(kind="platform", topic="回调", statement="5 秒内应答", sources=[PAY_DOC], modules=["payment"], checked_at=NOW.isoformat())
        source, _ = base.add_source(url=PAY_DOC, title="回调", tier="official", modules=["payment"], checked_at=NOW.isoformat())
        fact = base.add_fact(kind="platform", topic="回调", statement="5 秒内应答", sources=[PAY_DOC], modules=["payment"], checked_at=NOW.isoformat())
        self.assertEqual(fact["sources"], [source["id"]])
        self.assertEqual(base.validate(), [])
        base.save()
        self.assertIn("回调", (kb.build(kb.Knowledge())).read_text(encoding="utf-8"))

    def test_second_project_reuses_knowledge_and_skips_crawl(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            # 第一个项目：读到官方原文并完成一个检索，沉淀进知识库
            run(out, "new", "商城一期", "--mode", "implementation", "--project-type", "backend", "--modules", "payment")
            _, data_path = tech_plan.find_plan("01", out)
            plan = json.loads(data_path.read_text(encoding="utf-8"))
            plan["research"]["sources"].append({"id": "S1", "title": "支付成功回调通知", "url": PAY_DOC, "tier": "official", "read": True,
                                                "modules": ["payment"], "evidence": "D:/crawl/x", "excerpt": "5 秒内应答", "query": ""})
            first_query = tech_plan.build_research_queries(plan)[0]
            plan["research"]["doneQueries"] = [first_query]
            data_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
            harvested = tech_plan.harvest_to_knowledge(plan)
            self.assertEqual(harvested["added"], 1)
            base = kb.Knowledge()
            base.add_fact(kind="platform", topic="支付回调应答", statement="验签后 5 秒内应答 200/204", sources=[PAY_DOC],
                          modules=["payment"], checked_at=datetime.now().astimezone().isoformat())
            base.save()

            # 第二个项目：新建即复用知识库来源与事实；爬取输入中不再包含它们
            run(out, "new", "健身房", "--mode", "implementation", "--project-type", "backend", "--modules", "payment")
            _, data2 = tech_plan.find_plan("02", out)
            plan2 = json.loads(data2.read_text(encoding="utf-8"))
            reused = [s for s in plan2["research"]["sources"] if s.get("origin") == "knowledge"]
            self.assertEqual([s["url"] for s in reused], [PAY_DOC])
            self.assertEqual(len(plan2["research"]["knowledgeFacts"]), 1)
            self.assertEqual(run(out, "research", "02", "--build"), 0)
            links = (data2.parent / "research-links.txt").read_text(encoding="utf-8")
            self.assertNotIn("4012791902", links)
            queries = json.loads((data2.parent / "research-plan.json").read_text(encoding="utf-8"))
            self.assertNotIn((first_query["platform"], first_query["query"]), [(q["platform"], q["query"]) for q in queries])
            html = (out / "02-健身房" / "02-健身房.html").read_text(encoding="utf-8")
            self.assertIn("已核实的平台与法规要求", html)
            self.assertIn("验签后 5 秒内应答", html)


if __name__ == "__main__":
    unittest.main()
