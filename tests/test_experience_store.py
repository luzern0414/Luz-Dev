"""Experience store: keyed upsert, replacement instead of duplicates, caps and checks."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "record_experience.py"
sys.path.insert(0, str(ROOT / "scripts"))

import record_experience as rx  # noqa: E402


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        [sys.executable, "-X", "utf8", str(SCRIPT), *args],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    if check and result.returncode != 0:
        raise AssertionError(f"failed ({result.returncode})\n{result.stdout}\n{result.stderr}")
    return result


def note(key: str, text: str, kind: str = "preference", uses: int = 1, day: str = "2026-01-01") -> dict:
    return {"key": key, "kind": kind, "text": text, "uses": uses, "first_seen": day, "last_confirmed": day}


class NoteUpsertTests(unittest.TestCase):
    def test_same_key_replaces_and_keeps_one_previous(self) -> None:
        doc = rx.new_notes_doc()
        action, _, _ = rx.upsert_note(doc, "preference", "table-header", "表头左对齐")
        self.assertEqual(action, "added")
        action, entry, _ = rx.upsert_note(doc, "preference", "table-header", "表头居中")
        self.assertEqual(action, "replaced")
        action, entry, _ = rx.upsert_note(doc, "preference", "table-header", "表头与数据都居中", on="2026-10-01")
        self.assertEqual(len(doc["notes"]), 1)
        self.assertEqual(entry["text"], "表头与数据都居中")
        self.assertEqual(entry["previous"], {"text": "表头居中", "replaced_at": "2026-10-01"})
        self.assertEqual((entry["uses"], entry["last_confirmed"]), (3, "2026-10-01"))

    def test_same_text_only_confirms(self) -> None:
        doc = rx.new_notes_doc()
        rx.upsert_note(doc, "fact", "crawl-batch", "调研每批 15 个检索，间隔 6 秒")
        action, entry, _ = rx.upsert_note(doc, "fact", "crawl-batch", "调研每批15个检索，间隔6秒。")
        self.assertEqual(action, "confirmed")
        self.assertNotIn("previous", entry)
        self.assertEqual(entry["uses"], 2)

    def test_same_text_under_new_key_confirms_the_existing_note(self) -> None:
        doc = rx.new_notes_doc()
        rx.upsert_note(doc, "lesson", "callback-verify", "支付成功只认验签后的回调或主动查询")
        action, entry, _ = rx.upsert_note(doc, "lesson", "pay-success", "支付成功只认验签后的回调或主动查询。")
        self.assertEqual((action, entry["key"], entry["uses"]), ("confirmed", "callback-verify", 2))
        self.assertEqual(len(doc["notes"]), 1)

    def test_private_data_is_rejected(self) -> None:
        for text in ("联系 13812345678 确认", r"方案在 C:\Users\me\plan.html", "token: abc", "邮件 dev@example.com",
                     "微信号：abc_12345"):
            with self.assertRaises(ValueError, msg=text):
                rx.upsert_note(rx.new_notes_doc(), "preference", "private", text)

    def test_invalid_key_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            rx.upsert_note(rx.new_notes_doc(), "preference", "Has Space", "x")


class NoteCapTests(unittest.TestCase):
    def test_prune_drops_fewest_uses_then_oldest(self) -> None:
        cap = rx.NOTE_CAPS["preference"]
        doc = rx.new_notes_doc()
        doc["notes"] = [note(f"p{i}", f"偏好 {i}", uses=5, day="2026-10-01") for i in range(cap - 1)]
        doc["notes"] += [note("rare-old", "很少用且旧", day="2025-01-01"), note("rare-new", "很少用但新", day="2026-10-01"),
                         note("popular-old", "常用但旧", uses=50, day="2020-01-01")]
        self.assertTrue(any("exceed the cap" in error for error in rx.check_notes(doc)))
        removed = rx.prune_notes(doc)
        self.assertEqual([item["key"] for item in removed], ["rare-old", "rare-new"])
        self.assertEqual(len(doc["notes"]), cap)
        self.assertEqual(rx.check_notes(doc), [])
        self.assertEqual([n["key"] for n in rx.stale_notes(doc)], ["popular-old"])

    def test_check_flags_duplicate_keys_texts_and_old_shape(self) -> None:
        doc = rx.new_notes_doc()
        doc["notes"] = [note("a", "表头居中"), note("a", "数据居中"), note("b", "表头 居中。", "lesson")]
        errors = rx.check_notes(doc)
        self.assertTrue(any("duplicate note key a" in error for error in errors))
        self.assertTrue(any("repeats the statement of a" in error for error in errors))
        old = {"description": "x", "caps": rx.NOTE_CAPS, "entries": [{**note("c", "旧格式"), "created": "2026-01-01T00:00:00+08:00"}]}
        self.assertTrue(any("top-level fields" in error for error in rx.check_notes(old)))


class CliStoreTests(unittest.TestCase):
    def make_plan(self, base: Path) -> Path:
        plan = base / "01-测试任务技术方案.html"
        plan.write_text("<html><body>plan</body></html>", encoding="utf-8")
        return plan

    def test_note_cli_upserts_and_renders_index(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            experience = Path(temporary) / "experience"
            common = ("--experience-root", str(experience))
            run("note", "--kind", "preference", "--key", "plan-first", "--text", "先批准方案再开发", *common)
            payload = json.loads(
                run("note", "--kind", "preference", "--key", "plan-first", "--text", "方案批准并通过哈希门禁后才开发", *common).stdout
            )
            self.assertEqual(payload["action"], "replaced")
            self.assertEqual(payload["previous"], "先批准方案再开发")
            store = json.loads((experience / "notes.json").read_text(encoding="utf-8"))
            self.assertEqual(list(store), ["schema_version", "caps", "notes"])
            self.assertEqual(len(store["notes"]), 1)
            index = (experience / "INDEX.md").read_text(encoding="utf-8")
            self.assertIn("方案批准并通过哈希门禁后才开发", index)
            self.assertNotIn("先批准方案再开发 |", index)
            found = json.loads(run("search", "哈希", *common).stdout)
            self.assertEqual([item["key"] for item in found["notes"]], ["plan-first"])
            listed = json.loads(run("list", "--kind", "preference", *common).stdout)
            self.assertEqual([item["key"] for item in listed["notes"]], ["plan-first"])
            self.assertEqual(json.loads(run("check", *common).stdout)["ok"], True)

    def test_record_with_existing_key_updates_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            experience = base / "experience"
            plan = self.make_plan(base)
            common = ("--task-mode", "implementation", "--plan", str(plan), "--experience-root", str(experience))
            first = json.loads(
                run("record", "--title", "支付回调幂等", "--key", "payment-callback", "--summary", "按事件号去重。",
                    "--evidence", "test:a", "--status", "verified", *common).stdout
            )
            second = json.loads(
                run("record", "--title", "支付回调幂等与验签", "--key", "payment-callback", "--summary",
                    "先验签，再按事件号去重。", "--evidence", "test:b", *common).stdout
            )
            self.assertEqual(first["id"], second["id"])
            self.assertEqual(second["action"], "replaced")
            self.assertEqual(second["status"], "candidate")
            records = list((experience / "records").glob("EXP-*.md"))
            self.assertEqual(len(records), 1)
            fields, body = rx.read_frontmatter(records[0].read_text(encoding="utf-8"))
            self.assertEqual(fields["title"], "支付回调幂等与验签")
            self.assertEqual(fields["previous"], "按事件号去重。")
            self.assertEqual(fields["evidence"], ["test:a", "test:b"])
            self.assertIn("先验签，再按事件号去重。", body)
            self.assertNotIn("\n按事件号去重。", body)

    def test_record_without_key_dedupes_by_title(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            experience = base / "experience"
            plan = self.make_plan(base)
            common = ("--task-mode", "review", "--plan", str(plan), "--experience-root", str(experience))
            run("record", "--title", "库存防超卖", "--summary", "条件更新扣减。", *common)
            run("record", "--title", "库存防超卖", "--summary", "条件更新扣减并写流水。", *common)
            self.assertEqual(len(list((experience / "records").glob("EXP-*.md"))), 1)

    def test_check_fails_on_duplicate_record_keys_and_prune_enforces_record_cap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            experience = base / "experience"
            plan = self.make_plan(base)
            common = ("--task-mode", "analysis", "--plan", str(plan), "--experience-root", str(experience))
            run("record", "--title", "主题一", "--key", "topic-a", "--summary", "一", *common)
            run("record", "--title", "主题二", "--key", "topic-b", "--summary", "二", *common)
            second = next((experience / "records").glob("EXP-0002-*.md"))
            second.write_text(second.read_text(encoding="utf-8").replace("topic-b", "topic-a"), encoding="utf-8")
            result = run("check", "--experience-root", str(experience), check=False)
            self.assertEqual(result.returncode, 1)
            self.assertIn("topic-a", result.stdout)

        original = rx.RECORD_CAP
        rx.RECORD_CAP = 2
        try:
            with tempfile.TemporaryDirectory() as temporary:
                experience = Path(temporary) / "experience"
                rx.ensure_repository(experience)
                plan = self.make_plan(Path(temporary))
                for index in range(3):
                    args = rx.build_parser().parse_args(
                        ["record", "--title", f"主题{index}", "--key", f"topic-{index}", "--task-mode", "analysis",
                         "--plan", str(plan), "--summary", f"摘要{index}", "--experience-root", str(experience)]
                    )
                    payload = rx.record(args)
                self.assertEqual(len(payload["pruned"]), 1)
                self.assertEqual(len(rx.active_records(experience)), 2)
                errors, _ = rx.check_records(experience)
                self.assertEqual(errors, [])
        finally:
            rx.RECORD_CAP = original


class RepositoryStoreTests(unittest.TestCase):
    def test_committed_store_passes_check(self) -> None:
        errors, _ = rx.check_store(ROOT / "experience")
        self.assertEqual(errors, [])

    def test_committed_notes_file_matches_shared_spec(self) -> None:
        """experience/notes.json has the shape shared by Luz-System, Luz-Design, Luz-Dev and Luz-Writing."""
        path = ROOT / "experience" / "notes.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(list(doc), ["schema_version", "caps", "notes"])
        self.assertEqual(doc["schema_version"], "1.0")
        self.assertEqual(doc["caps"], {"preference": 60, "lesson": 120, "fact": 120})
        allowed = {"key", "kind", "text", "why", "uses", "first_seen", "last_confirmed", "previous"}
        for n in doc["notes"]:
            self.assertLessEqual({"key", "kind", "text", "uses", "first_seen", "last_confirmed"}, set(n), n.get("key"))
            self.assertLessEqual(set(n), allowed, n.get("key"))
            self.assertIn(n["kind"], ("preference", "lesson", "fact"))
            self.assertRegex(n["first_seen"], r"^\d{4}-\d{2}-\d{2}$")
            self.assertRegex(n["last_confirmed"], r"^\d{4}-\d{2}-\d{2}$")
            if "previous" in n:
                self.assertEqual(set(n["previous"]), {"text", "replaced_at"})
        self.assertEqual(len({n["key"] for n in doc["notes"]}), len(doc["notes"]))
        self.assertEqual(rx.check_notes(doc), [])


if __name__ == "__main__":
    unittest.main()
