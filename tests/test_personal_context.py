from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Optional
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "personal-context"
SCRIPTS = SKILL_ROOT / "scripts"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "合成 转录.json"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "providers"))

import personal_context as pc  # noqa: E402
import personal_context_bootstrap as onboarding  # noqa: E402
import qwen_mlx  # noqa: E402


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PersonalContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="personal-context-")
        self.base = Path(self.temp.name)
        self.root = self.base / "中文 路径" / "合成资料库"
        self.config_dir = self.base / "私有 配置"

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
        self.assertIn("bootstrap-status", help_result.stdout)
        self.assertIn("transcribe-audio", help_result.stdout)
        self.assertIn("capture-audio", help_result.stdout)
        failure = subprocess.run(
            [str(entry), "doctor", "--root", str(self.root)], capture_output=True, text=True, check=False
        )
        self.assertEqual(failure.returncode, 0)
        payload = json.loads(failure.stdout)
        self.assertFalse(payload["ok"])

    def test_bootstrap_plan_is_read_only_and_consent_digest_is_required(self) -> None:
        state = pc.schema_state(self.root)
        plan = onboarding.bootstrap_plan(
            self.root,
            config_dir=self.config_dir,
            mode="strict-local",
            provider="transcript-only",
            agent_host="test-agent",
            database_state=state,
        )
        self.assertTrue(plan["dry_run"])
        self.assertFalse(self.root.exists())
        self.assertFalse(self.config_dir.exists())
        with self.assertRaises(onboarding.BootstrapError):
            onboarding.record_consent(
                self.root,
                config_dir=self.config_dir,
                mode="strict-local",
                provider="transcript-only",
                agent_host="test-agent",
                accepted_digest="stale",
            )
        with self.assertRaises(onboarding.BootstrapError):
            onboarding.record_consent(
                self.root,
                config_dir=self.config_dir,
                mode="agent-assisted",
                provider="transcript-only",
                agent_host="test-agent",
                accepted_digest=plan["plan_digest"],
            )
        result = onboarding.record_consent(
            self.root,
            config_dir=self.config_dir,
            mode="strict-local",
            provider="transcript-only",
            agent_host="test-agent",
            accepted_digest=plan["plan_digest"],
        )
        receipt = Path(result["receipt"])
        self.assertTrue(receipt.is_file())
        if os.name != "nt":
            self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
        status = onboarding.bootstrap_status(
            self.root,
            config_dir=self.config_dir,
            provider="auto",
            agent_host="another-agent",
            database_state=pc.schema_state(self.root),
        )
        self.assertEqual(status["provider"], "transcript-only")
        self.assertEqual(status["status"], "needs_vault")

    def test_bootstrap_apply_creates_one_vault_and_is_idempotent(self) -> None:
        onboarding.record_consent(
            self.root,
            config_dir=self.config_dir,
            mode="strict-local",
            provider="transcript-only",
            agent_host="test-agent",
            accepted_digest=onboarding.consent_scope_digest(
                self.root, provider="transcript-only", mode="strict-local", agent_host="test-agent"
            ),
        )
        applied = onboarding.bootstrap_apply(
            self.root,
            config_dir=self.config_dir,
            provider="auto",
            agent_host="different-agent",
            database_state=pc.schema_state(self.root),
            init_vault=pc.init_vault,
        )
        self.assertEqual(applied["status"], "ready")
        self.assertTrue(pc.doctor(self.root)["ok"])
        repeated = onboarding.bootstrap_apply(
            self.root,
            config_dir=self.config_dir,
            provider="auto",
            agent_host="different-agent",
            database_state=pc.schema_state(self.root),
            init_vault=pc.init_vault,
        )
        self.assertEqual(repeated["database"]["status"], "already_initialized")
        status = onboarding.bootstrap_status(
            self.root,
            config_dir=self.config_dir,
            provider="auto",
            agent_host="different-agent",
            database_state=pc.schema_state(self.root),
        )
        self.assertEqual(status["status"], "ready")

    def test_agent_assisted_consent_is_scoped_to_named_host(self) -> None:
        self.init()
        onboarding.record_consent(
            self.root,
            config_dir=self.config_dir,
            mode="agent-assisted",
            provider="transcript-only",
            agent_host="codex",
            accepted_digest=onboarding.consent_scope_digest(
                self.root, provider="transcript-only", mode="agent-assisted", agent_host="codex"
            ),
        )
        same = onboarding.bootstrap_status(
            self.root,
            config_dir=self.config_dir,
            provider="auto",
            agent_host="codex",
            database_state=pc.schema_state(self.root),
        )
        changed = onboarding.bootstrap_status(
            self.root,
            config_dir=self.config_dir,
            provider="auto",
            agent_host="claude-code",
            database_state=pc.schema_state(self.root),
        )
        self.assertEqual(same["status"], "ready")
        self.assertEqual(changed["status"], "needs_consent")
        self.assertEqual(changed["consent"]["reason"], "agent_host_changed")

    def test_persistent_artifact_notice_invalidates_old_receipts(self) -> None:
        self.assertEqual(onboarding.NOTICE_VERSION, 2)
        qwen_plan = onboarding.bootstrap_plan(
            self.root,
            config_dir=self.config_dir,
            mode="strict-local",
            provider="qwen-mlx",
            agent_host="codex",
            database_state=pc.schema_state(self.root),
        )
        artifact_step = next(
            step
            for step in qwen_plan["steps"]
            if step["action"] == "enable-private-transcription-artifacts"
        )
        self.assertTrue(qwen_plan["requires_explicit_user_consent"])
        self.assertTrue(artifact_step["default_enabled"])
        self.assertEqual(artifact_step["artifact_contract"], 1)
        self.assertEqual(artifact_step["writes"], str(onboarding.artifacts_dir(self.config_dir) / onboarding.vault_scope_hash(self.root)))
        self.assertFalse(self.root.exists())
        self.assertFalse(self.config_dir.exists())
        plan = onboarding.bootstrap_plan(
            self.root,
            config_dir=self.config_dir,
            mode="strict-local",
            provider="transcript-only",
            agent_host="codex",
            database_state=pc.schema_state(self.root),
        )
        consent = onboarding.record_consent(
            self.root,
            config_dir=self.config_dir,
            mode="strict-local",
            provider="transcript-only",
            agent_host="codex",
            accepted_digest=plan["plan_digest"],
        )
        receipt_path = Path(consent["receipt"])
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["notice_version"] = 1
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        status = onboarding.bootstrap_status(
            self.root,
            config_dir=self.config_dir,
            provider="auto",
            agent_host="codex",
            database_state=pc.schema_state(self.root),
        )
        self.assertEqual(status["status"], "uninitialized")
        self.assertEqual(status["consent"]["reason"], "notice_changed")

    def test_provider_provenance_is_preserved_without_schema_change(self) -> None:
        self.init()
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        payload["processing"] = {
            "provider": "synthetic-local",
            "models": {"asr": {"revision": "abc123"}},
        }
        transcript = self.base / "provider transcript.json"
        transcript.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        pc.import_transcript(self.root, transcript, source_id=None, dry_run=False)
        with pc.connect(self.root, readonly=True) as connection:
            row = connection.execute("SELECT parameters_json FROM processing_runs").fetchone()
            parameters = json.loads(row[0])
            schema = pc.read_schema_version(connection)
        self.assertEqual(schema, 1)
        self.assertEqual(parameters["upstream"]["provider"], "synthetic-local")
        self.assertEqual(parameters["upstream"]["models"]["asr"]["revision"], "abc123")

    def test_transcribe_command_returns_metadata_not_transcript_text(self) -> None:
        audio = self.base / "private recording.m4a"
        audio.write_bytes(b"synthetic-audio")
        output = self.base / "private transcript.json"
        compatible = {
            "system": "Darwin",
            "machine": "arm64",
            "python": "3.9.0",
            "qwen_mlx_compatible": True,
            "qwen_mlx_reason": None,
        }
        with mock.patch.object(onboarding, "platform_probe", return_value=compatible):
            onboarding.record_consent(
                self.root,
                config_dir=self.config_dir,
                mode="strict-local",
                provider="qwen-mlx",
                agent_host="codex",
                accepted_digest=onboarding.consent_scope_digest(
                    self.root, provider="qwen-mlx", mode="strict-local", agent_host="codex"
                ),
            )

        commands: list[list[str]] = []

        def fake_run(
            command: list[str], *, env: Optional[dict[str, str]] = None
        ) -> subprocess.CompletedProcess[str]:
            del env
            commands.append(command)
            output.write_text(
                json.dumps(
                    {
                        "segments": [{"text": "高度敏感的正文"}],
                        "processing": {
                            "provider": "qwen-mlx",
                            "source_audio_sha256": file_hash(audio),
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    {"status": "transcribed", "cache": {"enabled": True, "hits": 5, "computed": 0}}
                ),
                stderr="",
            )

        ready = {"provider": "qwen-mlx", "compatible": True, "installed": True, "ready": True}
        with mock.patch.object(onboarding, "provider_status", return_value=ready), mock.patch.object(
            onboarding, "_run_checked", side_effect=fake_run
        ):
            result = onboarding.transcribe_audio(
                self.root,
                config_dir=self.config_dir,
                provider="auto",
                agent_host="unrelated-host",
                audio=audio,
                output=output,
                language="Chinese",
                title=None,
                observed_at=None,
                speaker_count=2,
                no_cache=False,
                refresh_stage="alignment",
            )
        rendered = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("高度敏感的正文", rendered)
        self.assertFalse(result["text_exposed_to_agent"])
        self.assertEqual(result["bytes"], output.stat().st_size)
        self.assertEqual(result["transcript"], str(output.resolve()))
        self.assertEqual(result["cache"], {"enabled": True, "hits": 5, "computed": 0})
        self.assertIn("--speaker-count", commands[0])
        self.assertEqual(commands[0][commands[0].index("--speaker-count") + 1], "2")
        self.assertIn("--artifacts-dir", commands[0])
        self.assertIn("--vault-scope", commands[0])
        self.assertIn("--refresh-stage", commands[0])
        self.assertEqual(commands[0][commands[0].index("--refresh-stage") + 1], "alignment")

    def test_agent_assisted_transcription_can_prepare_private_speaker_review(self) -> None:
        audio = self.base / "agent assisted recording.m4a"
        audio.write_bytes(b"synthetic-agent-assisted-audio")
        output = self.base / "agent assisted transcript.json"
        review_output = self.base / "agent assisted speaker review.json"
        compatible = {
            "system": "Darwin",
            "machine": "arm64",
            "python": "3.9.0",
            "qwen_mlx_compatible": True,
            "qwen_mlx_reason": None,
        }
        with mock.patch.object(onboarding, "platform_probe", return_value=compatible):
            onboarding.record_consent(
                self.root,
                config_dir=self.config_dir,
                mode="agent-assisted",
                provider="qwen-mlx",
                agent_host="codex",
                accepted_digest=onboarding.consent_scope_digest(
                    self.root,
                    provider="qwen-mlx",
                    mode="agent-assisted",
                    agent_host="codex",
                ),
            )

        commands: list[list[str]] = []

        def fake_run(
            command: list[str], *, env: Optional[dict[str, str]] = None
        ) -> subprocess.CompletedProcess[str]:
            del env
            commands.append(command)
            output.write_text(
                json.dumps(
                    {
                        "segments": [
                            {
                                "start_ms": 0,
                                "end_ms": 1000,
                                "speaker": "S01",
                                "text": "只应存在于私有审核输入。",
                            }
                        ],
                        "processing": {
                            "provider": "qwen-mlx",
                            "source_audio_sha256": file_hash(audio),
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            review_output.write_text(
                json.dumps({"contract": "semantic-speaker-review-input.v1"}),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    {
                        "status": "transcribed",
                        "cache": {"enabled": True, "hits": 5, "computed": 0},
                        "speaker_review": {
                            "prepared": True,
                            "applied": False,
                            "input_sha256": "a" * 64,
                            "accepted_operations": 0,
                        },
                    }
                ),
                stderr="",
            )

        ready = {"provider": "qwen-mlx", "compatible": True, "installed": True, "ready": True}
        with mock.patch.object(onboarding, "provider_status", return_value=ready), mock.patch.object(
            onboarding, "_run_checked", side_effect=fake_run
        ):
            result = onboarding.transcribe_audio(
                self.root,
                config_dir=self.config_dir,
                provider="auto",
                agent_host="codex",
                audio=audio,
                output=output,
                language="Chinese",
                title=None,
                observed_at=None,
                speaker_count=2,
                speaker_review_output=review_output,
            )

        rendered = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("只应存在于私有审核输入", rendered)
        self.assertTrue(result["text_exposed_to_agent"])
        self.assertTrue(result["speaker_review"]["prepared"])
        self.assertIn("--speaker-review-output", commands[0])
        self.assertEqual(
            commands[0][commands[0].index("--speaker-review-output") + 1],
            str(review_output.resolve()),
        )

    def test_strict_local_mode_refuses_semantic_speaker_review_files(self) -> None:
        audio = self.base / "strict local recording.m4a"
        audio.write_bytes(b"synthetic-strict-local-audio")
        output = self.base / "strict transcript.json"
        review_output = self.base / "forbidden review.json"
        compatible = {
            "system": "Darwin",
            "machine": "arm64",
            "python": "3.9.0",
            "qwen_mlx_compatible": True,
            "qwen_mlx_reason": None,
        }
        with mock.patch.object(onboarding, "platform_probe", return_value=compatible):
            onboarding.record_consent(
                self.root,
                config_dir=self.config_dir,
                mode="strict-local",
                provider="qwen-mlx",
                agent_host="codex",
                accepted_digest=onboarding.consent_scope_digest(
                    self.root,
                    provider="qwen-mlx",
                    mode="strict-local",
                    agent_host="codex",
                ),
            )

        with self.assertRaises(onboarding.BootstrapError):
            onboarding.transcribe_audio(
                self.root,
                config_dir=self.config_dir,
                provider="auto",
                agent_host="codex",
                audio=audio,
                output=output,
                language="Chinese",
                title=None,
                observed_at=None,
                speaker_review_output=review_output,
            )
        self.assertFalse(review_output.exists())

    def test_qwen_provider_profile_is_locked_and_lazy_loads_optional_dependencies(self) -> None:
        manifest = onboarding.load_manifest()
        self.assertEqual(manifest["provider"], "qwen-mlx")
        self.assertEqual(manifest["profile_version"], 4)
        self.assertEqual(manifest["runtime"]["packages"], ["mlx-audio[stt]==0.4.6"])
        self.assertEqual(manifest["artifacts"]["contract_version"], 1)
        self.assertEqual(manifest["artifacts"]["format"], "json+gzip")
        self.assertEqual(manifest["diarization"]["mode"], "high_accuracy_streaming")
        self.assertEqual(manifest["diarization"]["streaming"]["chunk_frames"], 340)
        self.assertEqual(manifest["diarization"]["streaming"]["right_context_frames"], 40)
        self.assertEqual(manifest["asr_recovery"]["subchunk_seconds"], 30)
        for model in manifest["models"].values():
            revision = model["revision"]
            self.assertEqual(len(revision), 40)
            self.assertTrue(all(character in "0123456789abcdef" for character in revision))
            self.assertTrue(model["license"])
        help_result = subprocess.run(
            [sys.executable, str(SCRIPTS / "providers" / "qwen_mlx.py"), "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("download", help_result.stdout)
        self.assertIn("transcribe", help_result.stdout)

    def test_release_versions_keep_schema_and_transcript_contract_stable(self) -> None:
        versions = pc.version_info(None)
        self.assertEqual(versions["skill_version"], "0.8.0")
        self.assertEqual(versions["schema_version"], 1)
        self.assertEqual(versions["provider_contract_version"], 1)
        self.assertEqual(versions["artifact_contract_version"], 1)
        self.assertEqual(versions["consent_notice_version"], 2)
        self.assertEqual(versions["qwen_mlx_profile_version"], 4)
        self.assertEqual(versions["qwen_mlx_3dspeaker_profile_version"], 1)

    def test_known_speaker_count_removes_fragmentary_false_channels(self) -> None:
        frames = (
            [[0.86, 0.04, 0.08, 0.02]] * 8
            + [[0.44, 0.95, 0.12, 0.02]] * 2
            + [[0.84, 0.05, 0.10, 0.02]] * 8
            + [[0.08, 0.06, 0.91, 0.02]] * 10
        )
        postprocessing = {
            "activity_threshold": 0.5,
            "reassignment_threshold": 0.2,
            "min_speech_seconds": 0.1,
            "max_silence_gap_seconds": 0.15,
            "max_weak_switch_seconds": 0.24,
            "max_weak_switch_margin": 0.18,
        }
        segments, selected = qwen_mlx._probabilities_to_diarization(
            frames,
            frame_seconds=0.08,
            expected_speakers=2,
            postprocessing=postprocessing,
        )
        self.assertEqual(selected, [0, 2])
        self.assertEqual({item["speaker"] for item in segments}, {0, 1})
        self.assertEqual(segments[0]["speaker"], 0)
        self.assertEqual(segments[-1]["speaker"], 1)

    def test_second_speaker_can_move_between_model_slots_across_long_audio(self) -> None:
        frames = (
            [[0.88, 0.04, 0.03, 0.05]] * 8
            + [[0.08, 0.04, 0.03, 0.92]] * 8
            + [[0.89, 0.04, 0.03, 0.04]] * 8
            + [[0.08, 0.93, 0.03, 0.05]] * 8
        )
        postprocessing = {
            "activity_threshold": 0.5,
            "reassignment_threshold": 0.2,
            "min_speech_seconds": 0.1,
            "max_silence_gap_seconds": 0.15,
            "max_weak_switch_seconds": 0.24,
            "max_weak_switch_margin": 0.18,
        }
        segments, selected = qwen_mlx._probabilities_to_diarization(
            frames,
            frame_seconds=0.1,
            expected_speakers=2,
            postprocessing=postprocessing,
            speaker_selection_window_seconds=1.6,
        )
        self.assertEqual(selected, [0, 3, 1])
        self.assertEqual([item["speaker"] for item in segments], [0, 1, 0, 1])

    def test_low_confidence_sentence_tail_is_not_split_into_another_speaker(self) -> None:
        words = [
            {"start": 0.0, "end": 0.6, "text": "不能再熬", "speaker": "S01", "speaker_margin": 0.72},
            {"start": 0.6, "end": 0.9, "text": "夜了", "speaker": "S02", "speaker_margin": 0.08},
            {"start": 0.9, "end": 1.1, "text": "我", "speaker": "S01", "speaker_margin": 0.61},
        ]
        smoothed = qwen_mlx._smooth_word_speakers(
            words,
            {
                "max_fragment_characters": 2,
                "max_fragment_seconds": 1.0,
                "max_fragment_gap_seconds": 0.2,
                "max_fragment_margin": 0.2,
            },
        )
        self.assertEqual([item["speaker"] for item in smoothed], ["S01", "S01", "S01"])

    def test_confident_short_backchannel_keeps_its_speaker(self) -> None:
        words = [
            {"start": 0.0, "end": 0.6, "text": "我们继续", "speaker": "S01", "speaker_margin": 0.72},
            {"start": 0.6, "end": 0.8, "text": "嗯", "speaker": "S02", "speaker_margin": 0.74},
            {"start": 0.8, "end": 1.2, "text": "说这个", "speaker": "S01", "speaker_margin": 0.66},
        ]
        smoothed = qwen_mlx._smooth_word_speakers(
            words,
            {
                "max_fragment_characters": 2,
                "max_fragment_seconds": 1.0,
                "max_fragment_gap_seconds": 0.2,
                "max_fragment_margin": 0.2,
            },
        )
        self.assertEqual([item["speaker"] for item in smoothed], ["S01", "S02", "S01"])

    def test_speaker_boundary_moves_back_to_the_nearest_word_pause(self) -> None:
        words = [
            {"start": 55.44, "end": 55.52, "text": "是", "speaker": "S02", "speaker_margin": 0.79},
            {"start": 55.60, "end": 55.60, "text": "不", "speaker": "S02", "speaker_margin": 0.79},
            {"start": 55.68, "end": 55.84, "text": "是", "speaker": "S02", "speaker_margin": 0.79},
            {"start": 56.16, "end": 56.24, "text": "我", "speaker": "S02", "speaker_margin": 0.79},
            {"start": 56.24, "end": 56.40, "text": "没", "speaker": "S02", "speaker_margin": 0.79},
            {"start": 56.40, "end": 56.56, "text": "感", "speaker": "S01", "speaker_margin": 0.92},
            {"start": 56.56, "end": 56.64, "text": "觉", "speaker": "S01", "speaker_margin": 0.92},
        ]
        realigned = qwen_mlx._realign_word_boundaries(
            words,
            {
                "boundary_pause_seconds": 0.2,
                "boundary_max_shift_seconds": 0.6,
                "boundary_max_shift_characters": 2,
                "boundary_join_gap_seconds": 0.08,
            },
        )
        self.assertEqual(
            [item["speaker"] for item in realigned],
            ["S02", "S02", "S02", "S01", "S01", "S01", "S01"],
        )

    def test_speaker_boundary_moves_forward_to_the_nearest_word_pause(self) -> None:
        words = [
            {"start": 0.0, "end": 0.6, "text": "不能再熬", "speaker": "S01", "speaker_margin": 0.8},
            {"start": 0.6, "end": 0.7, "text": "夜", "speaker": "S02", "speaker_margin": 0.7},
            {"start": 0.7, "end": 0.8, "text": "了", "speaker": "S02", "speaker_margin": 0.7},
            {"start": 1.1, "end": 1.2, "text": "我", "speaker": "S02", "speaker_margin": 0.7},
            {"start": 1.2, "end": 1.3, "text": "操", "speaker": "S02", "speaker_margin": 0.7},
        ]
        realigned = qwen_mlx._realign_word_boundaries(
            words,
            {
                "boundary_pause_seconds": 0.2,
                "boundary_max_shift_seconds": 0.6,
                "boundary_max_shift_characters": 2,
                "boundary_join_gap_seconds": 0.08,
            },
        )
        self.assertEqual(
            [item["speaker"] for item in realigned],
            ["S01", "S01", "S01", "S02", "S02"],
        )

    def test_transcript_assembly_module_preserves_030_provider_behavior(self) -> None:
        import transcript_assembly

        diarization = [
            {"start": 0.0, "end": 0.9, "speaker": 0, "confidence": 0.92, "margin": 0.71},
            {"start": 0.9, "end": 1.8, "speaker": 1, "confidence": 0.89, "margin": 0.66},
        ]
        words = [
            {"start": 0.0, "end": 0.4, "text": "我们"},
            {"start": 0.4, "end": 0.8, "text": "开始"},
            {"start": 1.0, "end": 1.3, "text": "好的"},
        ]
        settings = {
            "max_fragment_characters": 2,
            "max_fragment_seconds": 1.0,
            "max_fragment_gap_seconds": 0.2,
            "max_fragment_margin": 0.2,
            "max_same_speaker_gap_seconds": 1.2,
            "max_segment_characters": 280,
        }
        extracted = transcript_assembly.merge_words(words, diarization, settings)
        expected = [
            {"start_ms": 0, "end_ms": 800, "speaker": "S01", "text": "我们开始"},
            {"start_ms": 1000, "end_ms": 1300, "speaker": "S02", "text": "好的"},
        ]
        self.assertEqual(extracted, expected)

        fallbacks = [{"start": 2.0, "end": 2.6, "text": "补充"}]
        assembled = transcript_assembly.assemble_transcript_segments(
            words, fallbacks, diarization, settings
        )
        self.assertEqual(assembled[: len(expected)], expected)
        self.assertEqual(
            assembled[-1],
            {"start_ms": 2000, "end_ms": 2600, "speaker": "S02", "text": "补充"},
        )


if __name__ == "__main__":
    unittest.main()
