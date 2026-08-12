from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Optional


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "personal-context"
SCRIPTS = SKILL_ROOT / "scripts"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "合成 转录.json"
sys.path.insert(0, str(SCRIPTS))

import personal_context as pc  # noqa: E402


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PersonalContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="personal-context-")
        self.base = Path(self.temp.name)
        self.root = self.base / "中文 路径" / "合成资料库"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def init(self) -> None:
        result = pc.init_vault(self.root)
        self.assertEqual(result["schema_version"], 1)

    def import_fixture(self, *, dry_run: bool = False, source_id: Optional[str] = None) -> dict:
        return pc.import_transcript(self.root, FIXTURE, source_id=source_id, dry_run=dry_run)

    def db_count(self, table: str) -> int:
        with pc.connect(self.root, readonly=True) as connection:
            return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_initialize_empty_vault_and_doctor(self) -> None:
        self.init()
        self.assertTrue((self.root / "context.sqlite3").is_file())
        self.assertTrue(pc.doctor(self.root)["ok"])
        again = pc.init_vault(self.root)
        self.assertEqual(again["status"], "already_initialized")

    def test_unicode_space_path_and_duplicate_file_ingest(self) -> None:
        self.init()
        source = self.base / "资料 文件.txt"
        source.write_text("合成内容", encoding="utf-8")
        preview = pc.ingest(self.root, [source, source], observed_at=None, dry_run=True)
        self.assertTrue(preview["dry_run"])
        pc.ingest(self.root, [source], observed_at=None, dry_run=False)
        result = pc.ingest(self.root, [source], observed_at=None, dry_run=False)
        self.assertEqual(result["items"][0]["action"], "duplicate")
        self.assertEqual(self.db_count("sources"), 1)
        with pc.connect(self.root) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE sources SET original_name='changed' WHERE 1=1")

    def test_structured_transcript_claim_is_not_fact_and_pending_is_not_memory(self) -> None:
        self.init()
        preview = self.import_fixture(dry_run=True)
        self.assertEqual(preview["counts"]["segments"], 2)
        self.assertEqual(self.db_count("events"), 0)
        result = self.import_fixture()
        self.assertTrue(result["event_id"].startswith("evt_"))
        with pc.connect(self.root, readonly=True) as connection:
            default_kinds = [
                row[0]
                for row in connection.execute(
                    "SELECT kind FROM statements WHERE text IN (?, ?) ORDER BY text",
                    (
                        "我认为测试环境已经稳定，我们决定周五发布。",
                        "我会在周四准备发布检查表。",
                    ),
                )
            ]
            self.assertEqual(default_kinds, ["Claim", "Claim"])
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM statements WHERE kind='Fact'").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM candidate_memories WHERE review_status='pending'").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM memories").fetchone()[0], 0)
        self.import_fixture()
        self.assertEqual(self.db_count("events"), 1)
        self.assertEqual(self.db_count("segments"), 2)
        self.assertEqual(self.db_count("candidate_memories"), 2)

    def test_approve_and_reject_are_audited_and_do_not_overwrite(self) -> None:
        self.init()
        self.import_fixture()
        pending = pc.list_candidates(self.root, "pending")["candidates"]
        approved = pc.decide_candidate(
            self.root, pending[0]["id"], decision="approve", reviewer="测试用户", reason="已核对原文"
        )
        rejected = pc.decide_candidate(
            self.root, pending[1]["id"], decision="reject", reviewer="测试用户", reason="不适合长期保存"
        )
        self.assertTrue(approved["memory_id"].startswith("mem_"))
        self.assertIsNone(rejected["memory_id"])
        self.assertEqual(self.db_count("memories"), 1)
        self.assertEqual(self.db_count("reviews"), 2)
        with pc.connect(self.root) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE memories SET content='静默覆盖' WHERE id=?", (approved["memory_id"],))
        repeated = pc.decide_candidate(
            self.root, pending[0]["id"], decision="approve", reviewer="测试用户", reason=None
        )
        self.assertEqual(repeated["status"], "already_reviewed")
        with self.assertRaises(pc.ContextError):
            pc.decide_candidate(
                self.root, pending[0]["id"], decision="reject", reviewer="测试用户", reason="反向覆盖"
            )

    def test_retrieve_has_source_and_wiki_is_rebuildable(self) -> None:
        self.init()
        self.import_fixture()
        candidate = next(
            item for item in pc.list_candidates(self.root, "pending")["candidates"]
            if "周五发布" in item["content"]
        )
        pc.decide_candidate(self.root, candidate["id"], decision="approve", reviewer="测试用户", reason=None)
        results = pc.retrieve(self.root, "周五发布", limit=20)
        approved = [item for item in results["results"] if item["authority"] == "approved_memory"]
        self.assertTrue(approved)
        self.assertTrue(approved[0]["source"]["id"].startswith("src_"))
        self.assertEqual(len(approved[0]["source"]["content_hash"]), 64)
        preview = pc.compile_wiki(self.root, dry_run=True)
        self.assertFalse((self.root / "wiki" / "memories.md").exists())
        self.assertEqual(len(preview["files"]), 4)
        pc.compile_wiki(self.root, dry_run=False)
        for item in preview["files"]:
            self.assertEqual(file_hash(self.root / item["path"]), item["sha256"])
        memories_path = self.root / "wiki" / "memories.md"
        first = memories_path.read_text(encoding="utf-8")
        self.assertIn("灯塔项目计划周五发布", first)
        memories_path.unlink()
        pc.compile_wiki(self.root, dry_run=False)
        self.assertEqual(memories_path.read_text(encoding="utf-8"), first)

    def test_old_new_schema_compatibility_and_migration_dry_run(self) -> None:
        self.init()
        with pc.connect(self.root) as connection, connection:
            connection.execute("UPDATE schema_metadata SET value='0' WHERE key='schema_version'")
        self.assertEqual(pc.schema_state(self.root)["status"], "older")
        before = file_hash(self.root / "context.sqlite3")
        plan = pc.migrate(self.root, apply=False)
        after = file_hash(self.root / "context.sqlite3")
        self.assertTrue(plan["dry_run"])
        self.assertEqual(before, after)
        with self.assertRaises(pc.ContextError):
            pc.ingest(self.root, [FIXTURE], observed_at=None, dry_run=False)
        applied = pc.migrate(self.root, apply=True)
        self.assertEqual(applied["status"], "migrated")
        self.assertTrue(Path(applied["backup"]).is_file())
        with pc.connect(self.root) as connection, connection:
            connection.execute("UPDATE schema_metadata SET value='2' WHERE key='schema_version'")
        self.assertEqual(pc.schema_state(self.root)["status"], "newer")
        with self.assertRaises(pc.ContextError):
            pc.import_transcript(self.root, FIXTURE, source_id=None, dry_run=False)

    def test_audit_detects_orphan_record(self) -> None:
        self.init()
        path = self.root / "context.sqlite3"
        connection = sqlite3.connect(path)
        try:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute(
                "INSERT INTO segments(id, source_id, event_id, ordinal, text, content_hash, observed_at, created_at, schema_version) "
                "VALUES('seg_orphan', 'src_missing', 'evt_missing', 0, '孤立片段', 'bad', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 1)"
            )
            connection.commit()
        finally:
            connection.close()
        result = pc.audit(self.root)
        codes = {item["code"] for item in result["issues"]}
        self.assertIn("foreign_key", codes)
        self.assertIn("orphan_segment", codes)

    def test_failed_import_rolls_back_all_derived_records(self) -> None:
        self.init()
        source_id = pc.ingest(self.root, [FIXTURE], observed_at=None, dry_run=False)["items"][0]["source_id"]
        with self.assertRaises(pc.ContextError):
            pc.import_transcript(
                self.root, FIXTURE, source_id=source_id, dry_run=False, fail_after="segments"
            )
        for table in ("processing_runs", "events", "segments", "statements", "candidate_memories"):
            self.assertEqual(self.db_count(table), 0, table)
        self.assertEqual(self.db_count("sources"), 1)

    def test_end_to_end_synthetic_flow(self) -> None:
        self.init()
        imported = self.import_fixture()
        evidence = pc.review_event(self.root, imported["event_id"])
        self.assertEqual(len(evidence["candidate_memories"]), 2)
        candidate_id = next(
            item["id"] for item in evidence["candidate_memories"] if "灯塔项目" in item["content"]
        )
        pc.decide_candidate(self.root, candidate_id, decision="approve", reviewer="合成测试", reason="端到端验证")
        pc.compile_wiki(self.root, dry_run=False)
        result = pc.retrieve(self.root, "灯塔项目", limit=10)
        self.assertTrue(any(item["authority"] == "approved_memory" for item in result["results"]))
        self.assertTrue(pc.audit(self.root)["ok"])

    def test_cli_help_and_actionable_failure(self) -> None:
        entry = SCRIPTS / "context"
        help_result = subprocess.run([str(entry), "--help"], capture_output=True, text=True, check=False)
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("import-transcript", help_result.stdout)
        failure = subprocess.run(
            [str(entry), "doctor", "--root", str(self.root)], capture_output=True, text=True, check=False
        )
        self.assertEqual(failure.returncode, 0)
        payload = json.loads(failure.stdout)
        self.assertFalse(payload["ok"])


if __name__ == "__main__":
    unittest.main()
