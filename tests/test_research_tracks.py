"""必查调研主题：Web 与后台安全、HTTPS 加固、部署、主机、监控、备份、密钥、数据保护，不论方案选了哪些模块都要调研。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("LUZ_DEV_KNOWLEDGE", tempfile.mkdtemp(prefix="luz-knowledge-"))
sys.path.insert(0, str(ROOT / "scripts"))

import tech_plan  # noqa: E402


def plan_for(mode: str, modules: list[str] | None = None) -> dict:
    return {"mode": mode, "topic": "门店系统", "modules": modules or ["order"], "archetypes": [], "research": {}}


class TrackTests(unittest.TestCase):
    def test_tracks_follow_mode_not_modules(self) -> None:
        tracks = tech_plan.research_tracks(plan_for("implementation"))
        for track in ("web-security", "admin-security", "http-hardening", "container-deploy", "backup-dr", "secrets-supply-chain"):
            self.assertIn(track, tracks)
        review = tech_plan.research_tracks(plan_for("review"))
        self.assertIn("web-security", review)
        self.assertNotIn("backup-dr", review)
        self.assertEqual(tech_plan.research_tracks(plan_for("diagnosis")), {})

    def test_queries_and_links_include_tracks(self) -> None:
        plan = plan_for("implementation")
        reasons = {q["reason"].split(":", 1)[0] for q in tech_plan.build_research_queries(plan)}
        self.assertIn("web-security", reasons)
        self.assertIn("container-deploy", reasons)
        links = tech_plan.build_official_links(plan)
        owasp = next(link for link in links if link["url"] == "https://top10.owasp.org/2025")
        self.assertEqual(owasp["modules"], ["web-security"])
        self.assertIn("web-security", tech_plan.research_topics(plan))

    def test_uncovered_track_blocks_review(self) -> None:
        plan = plan_for("implementation")
        plan["research"]["sources"] = [
            {"id": "S1", "url": "https://csdn.net/a", "tier": "community", "read": True, "modules": ["web-security"]},
        ]
        errors = [e for e in tech_plan.check_plan(plan, "review")[0] if "必查主题" in e]
        self.assertTrue(any("web-security" in e and "官方" in e for e in errors))
        self.assertTrue(any("backup-dr" in e and "还没有已读来源" in e for e in errors))
        self.assertFalse(any("host-security" in e and "官方" in e for e in errors))  # 主机加固不强制官方来源

    def test_topics_filter_limits_crawl_inputs(self) -> None:
        plan = plan_for("implementation", ["order", "payment"])
        with tempfile.TemporaryDirectory() as temp:
            data_path = Path(temp) / "raw" / "plan.json"
            data_path.parent.mkdir()
            written = tech_plan.write_crawl_plan(plan, data_path, ["backup-dr"])
            links = (data_path.parent / "research-links.txt").read_text(encoding="utf-8")
            self.assertIn("postgresql.org", links)
            self.assertNotIn("pay.weixin.qq.com", links)
            import json
            reasons = {q["reason"].split(":", 1)[0] for key, path in written.items() if key.startswith("queries")
                       for q in json.loads(path.read_text(encoding="utf-8"))}
            self.assertEqual(reasons, {"backup-dr"})
            all_tracks = tech_plan.focus_topics(plan, ["tracks"])
            self.assertIn("web-security", all_tracks)
            with self.assertRaises(tech_plan.PlanError):
                tech_plan.focus_topics(plan, ["nonexistent"])

    def test_collections_found_inside_task_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            task = Path(temp) / "012_Luz-Dev 01 门店系统"
            nested = task / "raw" / "353_Luz-Dev 01 门店系统 检索"
            (nested / "raw").mkdir(parents=True)
            (nested / "raw" / "collection.json").write_text("{}", encoding="utf-8")
            (task / "raw" / "354_某页面.html").write_text("x", encoding="utf-8")
            self.assertEqual(tech_plan._collections_under(task), {nested})

    def test_stored_crawl_folder_is_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            plan = plan_for("implementation")
            plan["research"]["crawlFolder"] = temp
            self.assertEqual(tech_plan.crawl_task_folder(plan, Path(temp) / "no-crawl", Path(temp) / "log"), Path(temp))

    def test_fresh_knowledge_page_counts_for_the_track_that_lists_it(self) -> None:
        import knowledge as kb
        with tempfile.TemporaryDirectory() as temp:
            base = kb.Knowledge(Path(temp))
            base.add_source(url="https://top10.owasp.org/2025", title="OWASP Top 10", tier="standard", modules=["auth"],
                            checked_at=kb.now().isoformat(timespec="seconds"))
            plan = plan_for("implementation")
            tech_plan.apply_knowledge(plan, base)
            entry = next(s for s in plan["research"]["sources"] if s["url"] == kb.normalize_url("https://top10.owasp.org/2025"))
            self.assertIn("web-security", entry["modules"])
            self.assertTrue(entry["read"])

    def test_login_gated_sites_run_in_the_last_batch(self) -> None:
        import json
        plan = plan_for("implementation")
        with tempfile.TemporaryDirectory() as temp:
            data_path = Path(temp) / "raw" / "plan.json"
            data_path.parent.mkdir()
            written = tech_plan.write_crawl_plan(plan, data_path, ["tracks"])
            keys = sorted(k for k in written if k.startswith("queries"))
            batches = [json.loads(written[k].read_text(encoding="utf-8")) for k in keys]
            self.assertTrue(all(q["platform"] not in tech_plan.LOGIN_GATED_PLATFORMS for q in batches[0]))
            self.assertTrue(all(q["platform"] in tech_plan.LOGIN_GATED_PLATFORMS for q in batches[-1]))

    def test_home_page_redirect_or_empty_page_is_not_a_read(self) -> None:
        self.assertTrue(tech_plan.landed_elsewhere("https://www.gov.cn/xinwen/2021-08/20/a.htm", "https://www.gov.cn/", "首页"))
        self.assertTrue(tech_plan.landed_elsewhere("https://wiki.ubuntu.com/SSH", "https://wiki.ubuntu.com/SSH",
                                                   "There is currently no text in this page."))
        self.assertFalse(tech_plan.landed_elsewhere("https://top10.owasp.org/2025", "https://top10.owasp.org/2025/", "A01:2025"))

    def test_tracks_can_be_turned_off_for_non_system_topics(self) -> None:
        plan = plan_for("implementation")
        plan["research"]["tracks"] = False
        self.assertEqual(tech_plan.research_tracks(plan), {})


if __name__ == "__main__":
    unittest.main()
