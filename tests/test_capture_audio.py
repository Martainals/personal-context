from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "personal-context" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import personal_context as pc  # noqa: E402
import personal_context_bootstrap as onboarding  # noqa: E402
import transcript_markdown  # noqa: E402


class TranscriptMarkdownTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="personal-context-markdown-")
        self.base = Path(self.temp.name)
        self.audio_hash = hashlib.sha256(b"synthetic audio").hexdigest()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_complete_markdown_orders_segments_and_formats_long_unicode_timeline(self) -> None:
        transcript = {
            "event": {"title": "跨时区 复盘", "type": "recording"},
            "segments": [
                {
                    "start_ms": 3_723_000,
                    "end_ms": 3_726_500,
                    "speaker": "人物乙",
                    "text": "最后一段\n仍然完整。",
                },
                {
                    "start_ms": 0,
                    "end_ms": 1_500,
                    "speaker": "人物甲",
                    "text": "第一段，包含 Unicode：你好。",
                },
            ],
            "processing": {"contract": "transcript.v1", "provider": "synthetic"},
        }

        rendered = transcript_markdown.render_transcript_markdown(
            transcript, source_audio_sha256=self.audio_hash
        )

        self.assertEqual(rendered.segment_count, 2)
        self.assertEqual(rendered.duration_ms, 3_726_500)
        self.assertIn("# 跨时区 复盘", rendered.text)
        self.assertIn("状态：完整转写", rendered.text)
        self.assertIn("时长：01:02:06", rendered.text)
        first = rendered.text.index("00:00:00 · 人物甲")
        last = rendered.text.index("01:02:03 · 人物乙")
        self.assertLess(first, last)
        self.assertIn("第一段，包含 Unicode：你好。", rendered.text)
        self.assertIn("最后一段\n仍然完整。", rendered.text)
        self.assertIn("personal-context:generated", rendered.text)

    def test_generated_markdown_updates_but_manual_edits_are_never_overwritten(self) -> None:
        target = self.base / "录音转写.md"
        first = transcript_markdown.render_transcript_markdown(
            {
                "event": {"title": "第一次"},
                "segments": [
                    {"start_ms": 0, "end_ms": 1000, "speaker": "S01", "text": "原文"}
                ],
            },
            source_audio_sha256=self.audio_hash,
        )
        transcript_markdown.publish_markdown(target, first)
        second = transcript_markdown.render_transcript_markdown(
            {
                "event": {"title": "第二次"},
                "segments": [
                    {"start_ms": 0, "end_ms": 1200, "speaker": "S01", "text": "更新原文"}
                ],
            },
            source_audio_sha256=self.audio_hash,
        )
        transcript_markdown.publish_markdown(target, second)
        self.assertIn("更新原文", target.read_text(encoding="utf-8"))

        manually_edited = target.read_text(encoding="utf-8").replace("更新原文", "人工修改")
        target.write_text(manually_edited, encoding="utf-8")
        before = target.read_bytes()
        with self.assertRaises(transcript_markdown.ManualEditError):
            transcript_markdown.publish_markdown(target, first)
        self.assertEqual(target.read_bytes(), before)


class CaptureAudioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="personal-context-capture-")
        self.base = Path(self.temp.name)
        self.root = self.base / "隔离 Vault"
        self.config = self.base / "隔离 配置"
        self.audio = self.base / "用户录音.m4a"
        self.audio.write_bytes(b"synthetic audio bytes only")
        pc.init_vault(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _provider_document(self) -> dict[str, Any]:
        return {
            "event": {
                "title": "合成录音",
                "type": "recording",
                "observed_at": "2026-08-16T08:00:00Z",
            },
            "segments": [
                {"start_ms": 0, "end_ms": 1500, "speaker": "S01", "text": "机密第一段。"},
                {
                    "start_ms": 3_723_000,
                    "end_ms": 3_725_000,
                    "speaker": "S02",
                    "text": "机密最后一段。",
                },
            ],
            "entities": [],
            "statements": [],
            "decisions": [],
            "actions": [],
            "claims": [],
            "relationships": [],
            "candidate_memories": [],
            "processing": {
                "contract": "transcript.v1",
                "provider": "qwen-mlx",
                "source_audio_sha256": hashlib.sha256(self.audio.read_bytes()).hexdigest(),
            },
        }

    def _fake_transcribe(self, root: Path, **kwargs: Any) -> dict[str, Any]:
        self.assertEqual(root, self.root.resolve())
        output = Path(kwargs["output"])
        output.relative_to((self.config / "capture-jobs").resolve())
        self.assertNotIn((self.root / "inbox").resolve(), output.parents)
        if sys.platform != "win32":
            self.assertEqual(output.parent.stat().st_mode & 0o777, 0o700)
        output.write_text(
            json.dumps(self._provider_document(), ensure_ascii=False), encoding="utf-8"
        )
        return {
            "status": "transcribed",
            "provider": "qwen-mlx",
            "mode": "strict-local",
            "segments": 2,
            "cache": {"enabled": True, "hits": 4, "computed": 1},
            "text_exposed_to_agent": False,
        }

    def _assert_no_inbox_or_job_leak(self) -> None:
        self.assertEqual(list((self.root / "inbox").iterdir()), [])
        if self.config.exists():
            self.assertFalse(any(self.config.rglob("*.json")))
            jobs = self.config / "capture-jobs"
            if jobs.exists():
                self.assertEqual(list(jobs.iterdir()), [])

    def test_capture_audio_cli_delivers_only_markdown_and_imports_claim_evidence(self) -> None:
        outputs: list[dict[str, Any]] = []

        def load_private_transcript(path: Path) -> dict[str, Any]:
            if sys.platform != "win32":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            return transcript_markdown.load_transcript(path)

        with mock.patch.object(
            onboarding, "transcribe_audio", side_effect=self._fake_transcribe
        ), mock.patch.object(
            pc, "load_transcript", side_effect=load_private_transcript
        ), mock.patch.object(pc, "print_json", side_effect=lambda value, **_: outputs.append(value)):
            exit_code = pc.main(
                [
                    "capture-audio",
                    "--root",
                    str(self.root),
                    "--config-dir",
                    str(self.config),
                    "--agent-host",
                    "codex",
                    "--audio",
                    str(self.audio),
                    "--provider",
                    "qwen-mlx",
                    "--language",
                    "Chinese",
                    "--speaker-count",
                    "2",
                ]
            )

        self.assertEqual(exit_code, 0)
        result = outputs[0]
        stdout = json.dumps(result, ensure_ascii=False)
        inbox_files = sorted(path.name for path in (self.root / "inbox").iterdir())
        self.assertEqual(inbox_files, ["用户录音-录音转写.md"])
        self.assertFalse(any((self.root / "inbox").glob("*.json")))
        self.assertFalse(any(self.config.rglob("*.json")))
        markdown_path = Path(result["markdown"]["path"])
        self.assertEqual(markdown_path.parent, (self.root / "inbox").resolve())
        if sys.platform != "win32":
            self.assertEqual(markdown_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(result["counts"]["events"], 1)
        self.assertEqual(result["counts"]["segments"], 2)
        self.assertEqual(result["cache"], {"enabled": True, "hits": 4, "computed": 1})
        self.assertNotIn("机密第一段", stdout)
        self.assertTrue(self.audio.is_file())
        blobs = [path for path in (self.root / "blobs").rglob("*") if path.is_file()]
        self.assertEqual(len(blobs), 1)
        self.assertEqual(blobs[0].read_bytes(), self.audio.read_bytes())
        with pc.connect(self.root, readonly=True) as connection:
            source = connection.execute(
                "SELECT id, original_name FROM sources WHERE id=?", (result["source_id"],)
            ).fetchone()
            self.assertEqual(source["original_name"], self.audio.name)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM segments").fetchone()[0], 2)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM statements WHERE kind='Claim'").fetchone()[0],
                2,
            )
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM memories").fetchone()[0], 0)

    def test_repeating_the_same_audio_returns_existing_delivery_without_provider(self) -> None:
        results = []
        with mock.patch.object(
            onboarding, "transcribe_audio", side_effect=self._fake_transcribe
        ) as provider:
            for _ in range(2):
                results.append(
                    pc.capture_audio(
                        self.root,
                        config_dir=self.config,
                        provider="qwen-mlx",
                        agent_host="codex",
                        audio=self.audio,
                        language="Chinese",
                        title=None,
                        observed_at=None,
                        speaker_count=2,
                    )
                )

        self.assertEqual(provider.call_count, 1)
        self.assertEqual(results[0]["status"], "captured")
        self.assertEqual(results[1]["status"], "already_delivered")
        self.assertEqual(results[0]["source_id"], results[1]["source_id"])
        self.assertEqual(results[0]["event_id"], results[1]["event_id"])
        self.assertEqual(results[1]["cache"], {"enabled": True, "status": "not_run"})
        with pc.connect(self.root, readonly=True) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM segments").fetchone()[0], 2)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM statements WHERE kind='Claim'").fetchone()[0],
                2,
            )
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM memories").fetchone()[0], 0)
        self.assertEqual(
            sorted(path.name for path in (self.root / "inbox").iterdir()),
            ["用户录音-录音转写.md"],
        )

    def test_explicit_rerun_reuses_one_delivery_and_rejects_metadata_drift(self) -> None:
        with mock.patch.object(
            onboarding, "transcribe_audio", side_effect=self._fake_transcribe
        ) as provider:
            first = pc.capture_audio(
                self.root,
                config_dir=self.config,
                provider="qwen-mlx",
                agent_host="codex",
                audio=self.audio,
                language="Chinese",
                title=None,
                observed_at="2026-08-16T08:00:00Z",
                speaker_count=2,
            )
            rerun = pc.capture_audio(
                self.root,
                config_dir=self.config,
                provider="qwen-mlx",
                agent_host="codex",
                audio=self.audio,
                language="Chinese",
                title=None,
                observed_at="2026-08-16T08:00:00Z",
                speaker_count=2,
                rerun=True,
            )
            with self.assertRaises(pc.ContextError):
                pc.capture_audio(
                    self.root,
                    config_dir=self.config,
                    provider="qwen-mlx",
                    agent_host="codex",
                    audio=self.audio,
                    language="Chinese",
                    title="另一个标题",
                    observed_at="2026-08-16T08:00:00Z",
                    speaker_count=2,
                    rerun=True,
                )

        self.assertEqual(provider.call_count, 2)
        self.assertEqual(first["event_id"], rerun["event_id"])
        self.assertEqual(self._db_count("events"), 1)
        self.assertEqual(self._db_count("segments"), 2)
        self.assertEqual(
            sorted(path.name for path in (self.root / "inbox").iterdir()),
            ["用户录音-录音转写.md"],
        )

    def test_rerun_with_changed_segments_is_refused_by_schema_one_immutability(self) -> None:
        with mock.patch.object(
            onboarding, "transcribe_audio", side_effect=self._fake_transcribe
        ):
            first = pc.capture_audio(
                self.root,
                config_dir=self.config,
                provider="qwen-mlx",
                agent_host="codex",
                audio=self.audio,
                language="Chinese",
                title=None,
                observed_at="2026-08-16T08:00:00Z",
                speaker_count=2,
            )
        target = Path(first["markdown"]["path"])
        before = target.read_bytes()

        def changed_transcribe(root: Path, **kwargs: Any) -> dict[str, Any]:
            result = self._fake_transcribe(root, **kwargs)
            output = Path(kwargs["output"])
            document = json.loads(output.read_text(encoding="utf-8"))
            document["segments"][0]["speaker"] = "S02"
            output.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
            return result

        with mock.patch.object(
            onboarding, "transcribe_audio", side_effect=changed_transcribe
        ):
            with self.assertRaises(pc.ContextError):
                pc.capture_audio(
                    self.root,
                    config_dir=self.config,
                    provider="qwen-mlx",
                    agent_host="codex",
                    audio=self.audio,
                    language="Chinese",
                    title=None,
                    observed_at="2026-08-16T08:00:00Z",
                    speaker_count=2,
                    rerun=True,
                )

        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(self._db_count("events"), 1)
        self.assertEqual(self._db_count("segments"), 2)
        self.assertFalse(any(self.config.rglob("*.json")))

    def test_provider_failure_leaves_no_inbox_or_private_job_transcript(self) -> None:
        with mock.patch.object(
            onboarding,
            "transcribe_audio",
            side_effect=onboarding.BootstrapError("synthetic provider failure"),
        ):
            with self.assertRaises(onboarding.BootstrapError):
                pc.capture_audio(
                    self.root,
                    config_dir=self.config,
                    provider="qwen-mlx",
                    agent_host="codex",
                    audio=self.audio,
                    language="Chinese",
                    title=None,
                    observed_at=None,
                    speaker_count=2,
                )
        self._assert_no_inbox_or_job_leak()
        self.assertEqual(self._db_count("sources"), 0)

    def test_render_failure_leaves_no_inbox_or_private_job_transcript(self) -> None:
        def invalid_provider(_: Path, **kwargs: Any) -> dict[str, Any]:
            Path(kwargs["output"]).write_text(
                json.dumps(
                    {
                        "event": {"title": "损坏"},
                        "segments": [],
                        "processing": {
                            "contract": "transcript.v1",
                            "source_audio_sha256": hashlib.sha256(
                                self.audio.read_bytes()
                            ).hexdigest(),
                        },
                    }
                ),
                encoding="utf-8",
            )
            return {"provider": "qwen-mlx", "mode": "strict-local"}

        with mock.patch.object(onboarding, "transcribe_audio", side_effect=invalid_provider):
            with self.assertRaises(pc.ContextError):
                pc.capture_audio(
                    self.root,
                    config_dir=self.config,
                    provider="qwen-mlx",
                    agent_host="codex",
                    audio=self.audio,
                    language="Chinese",
                    title=None,
                    observed_at=None,
                    speaker_count=2,
                )
        self._assert_no_inbox_or_job_leak()
        self.assertEqual(self._db_count("sources"), 0)

    def test_import_failure_keeps_database_atomic_and_leaks_no_delivery_file(self) -> None:
        authoritative_import = pc.import_transcript

        def failing_import(
            root: Path, transcript_path: Path, *, source_id: str, dry_run: bool
        ) -> dict[str, Any]:
            return authoritative_import(
                root,
                transcript_path,
                source_id=source_id,
                dry_run=dry_run,
                fail_after=None if dry_run else "segments",
            )

        with mock.patch.object(
            onboarding, "transcribe_audio", side_effect=self._fake_transcribe
        ), mock.patch.object(
            pc, "import_transcript", side_effect=failing_import
        ):
            with self.assertRaises(pc.ContextError):
                pc.capture_audio(
                    self.root,
                    config_dir=self.config,
                    provider="qwen-mlx",
                    agent_host="codex",
                    audio=self.audio,
                    language="Chinese",
                    title=None,
                    observed_at="2026-08-16T08:00:00Z",
                    speaker_count=2,
                )
        self._assert_no_inbox_or_job_leak()
        self.assertEqual(self._db_count("sources"), 1)
        self.assertEqual(self._db_count("events"), 0)
        self.assertEqual(self._db_count("segments"), 0)
        self.assertEqual(self._db_count("memories"), 0)

    def test_capture_refuses_to_overwrite_a_manually_edited_delivery(self) -> None:
        with mock.patch.object(
            onboarding, "transcribe_audio", side_effect=self._fake_transcribe
        ) as provider:
            result = pc.capture_audio(
                self.root,
                config_dir=self.config,
                provider="qwen-mlx",
                agent_host="codex",
                audio=self.audio,
                language="Chinese",
                title=None,
                observed_at="2026-08-16T08:00:00Z",
                speaker_count=2,
            )
            target = Path(result["markdown"]["path"])
            target.write_text(
                target.read_text(encoding="utf-8").replace("机密第一段", "人工保留内容"),
                encoding="utf-8",
            )
            before = target.read_bytes()
            with self.assertRaises(pc.ContextError):
                pc.capture_audio(
                    self.root,
                    config_dir=self.config,
                    provider="qwen-mlx",
                    agent_host="codex",
                    audio=self.audio,
                    language="Chinese",
                    title=None,
                    observed_at="2026-08-16T08:00:00Z",
                    speaker_count=2,
                )
        self.assertEqual(provider.call_count, 1)
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(self._db_count("events"), 1)
        self.assertEqual(self._db_count("segments"), 2)

    def test_atomic_publish_failure_is_recoverable_without_partial_inbox_file(self) -> None:
        authoritative_publish = pc.publish_markdown

        def fail_final_publish(path: Path, rendered: Any) -> dict[str, Any]:
            if path.parent.resolve() == (self.root / "inbox").resolve():
                raise OSError("synthetic atomic publish failure")
            return authoritative_publish(path, rendered)

        with mock.patch.object(
            onboarding, "transcribe_audio", side_effect=self._fake_transcribe
        ), mock.patch.object(pc, "publish_markdown", side_effect=fail_final_publish):
            with self.assertRaises(OSError):
                pc.capture_audio(
                    self.root,
                    config_dir=self.config,
                    provider="qwen-mlx",
                    agent_host="codex",
                    audio=self.audio,
                    language="Chinese",
                    title=None,
                    observed_at="2026-08-16T08:00:00Z",
                    speaker_count=2,
                )
        self._assert_no_inbox_or_job_leak()
        self.assertEqual(self._db_count("events"), 1)
        self.assertEqual(self._db_count("segments"), 2)

        with mock.patch.object(
            onboarding, "transcribe_audio", side_effect=self._fake_transcribe
        ):
            recovered = pc.capture_audio(
                self.root,
                config_dir=self.config,
                provider="qwen-mlx",
                agent_host="codex",
                audio=self.audio,
                language="Chinese",
                title=None,
                observed_at="2026-08-16T08:00:00Z",
                speaker_count=2,
            )
        self.assertTrue(Path(recovered["markdown"]["path"]).is_file())
        self.assertEqual(self._db_count("events"), 1)
        self.assertEqual(self._db_count("segments"), 2)

    def _db_count(self, table: str) -> int:
        with pc.connect(self.root, readonly=True) as connection:
            return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


if __name__ == "__main__":
    unittest.main()
