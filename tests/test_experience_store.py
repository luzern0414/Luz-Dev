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


class NoteUpsertTests(unittest.TestCase):
    def test_same_key_replaces_and_keeps_one_previous(self) -> None:
        entries: list[dict] = []
        action, _ = rx.upsert_note(entries, kind="preference", key="table-header", text="表头左对齐")
        self.assertEqual(action, "created")
        action, entry = rx.upsert_note(entries, kind="preference", key="table-header", text="表头居中")
        self.assertEqual(action, "replaced")
        action, entry = rx.upsert_note(entries, kind="preference", key="table-header", text="表头与数据都居中")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entry["text"], "表头与数据都居中")
        self.assertEqual(entry["previous"]["text"], "表头居中")
        self.assertEqual(entry["uses"], 3)

    def test_same_text_only_confirms(self) -> None:
        entries: list[dict] = []
        rx.upsert_note(entries, kind="fact", key="crawl-batch", text="调研每批 15 个检索，间隔 6 秒")
        action, entry = rx.upsert_note(entries, kind="fact", key="crawl-batch", text="调研每批15个检索，间隔6秒。")
        self.assertEqual(action, "confirmed")
        self.assertIsNone(entry["previous"])
        self.assertEqual(entry["uses"], 2)

    def test_same_text_under_new_key_is_rejected(self) -> None:
        entries: list[dict] = []
        rx.upsert_note(entries, kind="lesson", key="callback-verify", text="支付成功只认验签后的回调或主动查询")
        with self.assertRaises(ValueError):
            rx.upsert_note(entries, kind="lesson", key="pay-success", text="支付成功只认验签后的回调或主动查询。")

    def test_private_data_is_rejected(self) -> None:
        for text in ("联系 13812345678 确认", "方案在 C:\\Users\\me\\plan.html", "token: abc"):
            with self.assertRaises(ValueError, msg=text):
                rx.upsert_note([], kind="preference", key="private", text=text)

    def test_invalid_key_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            rx.upsert_note([], kind="preference", key="Has Space", text="x")


class NoteCapTests(unittest.TestCase):
    def test_prune_drops_stale_then_least_used(self) -> None:
        cap = rx.NOTE_CAPS["preference"]
        entries = [
            {"kind": "preference", "key": f"p{i}", "text": f"偏好 {i}", "uses": 5, "last_confirmed": rx.now()}
            for i in range(cap)
        ]
        entries.append({"kind": "preference", "key": "stale", "text": "很久没确认", "uses": 50, "last_confirmed": "2020-01-01T00:00:00+08:00"})
        entries.append({"kind": "preference", "key": "rare", "text": "很少用", "uses": 1, "last_confirmed": rx.now()})
        errors, _ = rx.check_notes(entries)
        self.assertTrue(any("超过上限" in error for error in errors))
        removed = rx.prune_notes(entries)
        self.assertEqual([item["key"] for item in removed], ["stale", "rare"])
        self.assertEqual(len(entries), cap)
        errors, _ = rx.check_notes(entries)
        self.assertEqual(errors, [])

    def test_check_flags_duplicate_keys_and_texts(self) -> None:
        entries = [
            {"kind": "preference", "key": "a", "text": "表头居中", "uses": 1, "last_confirmed": rx.now()},
            {"kind": "preference", "key": "a", "text": "数据居中", "uses": 1, "last_confirmed": rx.now()},
            {"kind": "lesson", "key": "b", "text": "表头 居中。", "uses": 1, "last_confirmed": rx.now()},
        ]
        errors, _ = rx.check_notes(entries)
        self.assertTrue(any("key 重复" in error for error in errors))
        self.assertTrue(any("内容重复" in error for error in errors))


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
            self.assertEqual(len(store["entries"]), 1)
            index = (experience / "INDEX.md").read_text(encoding="utf-8")
            self.assertIn("方案批准并通过哈希门禁后才开发", index)
            self.assertNotIn("先批准方案再开发 |", index)
            found = json.loads(run("search", "哈希", *common).stdout)
            self.assertEqual([item["key"] for item in found["notes"]], ["plan-first"])
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


if __name__ == "__main__":
    unittest.main()
