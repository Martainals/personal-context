from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "personal-context" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import personal_context as pc  # noqa: E402
import transcript_markdown  # noqa: E402


class NoteDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="personal-context-note-")
        self.base = Path(self.temp.name)
        self.root = self.base / "隔离 Vault"
        self.audio = self.base / "2026-01-02_03_04_05.wav"
        self.audio.write_bytes(b"synthetic note audio")
        pc.init_vault(self.root)
        ingested = pc.ingest(
            self.root,
            [self.audio],
            observed_at="2026-01-01T19:04:05Z",
            dry_run=False,
        )
        self.source_id = ingested["items"][0]["source_id"]
        self.audio_hash = ingested["items"][0]["content_hash"]
        self.document = {
            "event": {
                "title": "合成产品原型复盘",
                "type": "conversation",
                "observed_at": "2026-01-01T19:04:05Z",
            },
            "segments": [
                {
                    "start_ms": 0,
                    "end_ms": 3000,
                    "speaker": "人物A",
                    "text": "团队准备先完成一个合成原型。",
                },
                {
                    "start_ms": 4000,
                    "end_ms": 7000,
                    "speaker": "人物B",
                    "text": "验证完成后再决定下一步。",
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
                "provider": "synthetic",
                "source_audio_sha256": self.audio_hash,
            },
        }
        transcript_json = self.base / "transcript.v1.json"
        transcript_json.write_text(
            json.dumps(self.document, ensure_ascii=False), encoding="utf-8"
        )
        imported = pc.import_transcript(
            self.root,
            transcript_json,
            source_id=self.source_id,
            dry_run=False,
        )
        self.event_id = imported["event_id"]
        rendered = transcript_markdown.render_transcript_markdown(
            self.document, source_audio_sha256=self.audio_hash
        )
        self.transcript = (
            self.root
            / "inbox"
            / "2026-01-02 03：04：05-合成产品原型复盘.md"
        )
        transcript_markdown.publish_markdown(self.transcript, rendered)
        self.draft = self.base / "note-draft.md"
        self.draft.write_text(
            "# 合成产品原型复盘\n\n"
            "## 内容概览\n\n团队讨论了先完成合成原型再验证的方向。\n\n"
            "## 可执行事项\n\n- 完成合成原型后再决定下一步。〔00:00:00–00:00:07〕\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_new_vault_creates_notes_and_legacy_absence_remains_healthy(self) -> None:
        self.assertTrue((self.root / "notes").is_dir())
        (self.root / "notes").rmdir()

        health = pc.doctor(self.root)

        self.assertTrue(health["ok"])
        note_check = next(item for item in health["checks"] if item["name"] == "notes")
        self.assertFalse(note_check["present"])
        self.assertTrue(note_check["created_on_first_note"])

    def test_check_only_is_read_only_and_returns_linked_audio(self) -> None:
        (self.root / "notes").rmdir()

        result = pc.publish_note(
            self.root,
            transcript=self.transcript,
            draft=None,
            check_only=True,
        )

        self.assertEqual(result["status"], "draft_required")
        self.assertFalse((self.root / "notes").exists())
        self.assertEqual(result["source_id"], self.source_id)
        self.assertEqual(result["event_id"], self.event_id)
        self.assertEqual(result["source_audio"]["sha256"], self.audio_hash)
        self.assertEqual(
            Path(result["source_audio"]["path"]).resolve(),
            (self.root / "blobs" / self.audio_hash[:2] / self.audio_hash).resolve(),
        )
        self.assertNotIn("团队准备", json.dumps(result, ensure_ascii=False))

    def test_publish_uses_matching_filename_and_repeat_creates_nothing(self) -> None:
        first = pc.publish_note(
            self.root,
            transcript=self.transcript,
            draft=self.draft,
        )
        note_path = Path(first["note"]["path"])
        before = note_path.read_bytes()

        second = pc.publish_note(
            self.root,
            transcript=self.transcript,
            draft=None,
        )

        self.assertEqual(first["status"], "published")
        self.assertEqual(second["status"], "already_delivered")
        self.assertEqual(note_path.parent.resolve(), (self.root / "notes").resolve())
        self.assertEqual(note_path.name, self.transcript.name)
        self.assertEqual(note_path.read_bytes(), before)
        self.assertIn("note-markdown-v1", note_path.read_text(encoding="utf-8"))
        self.assertEqual(list((self.root / "inbox").glob("*.json")), [])
        self.assertEqual(list((self.root / "notes").glob("*.json")), [])
        self.assertEqual(list((self.root / "notes").glob("*.wav")), [])
        self.assertFalse(first["long_term_memory_created"])

    def test_manual_note_edit_is_never_overwritten(self) -> None:
        published = pc.publish_note(
            self.root,
            transcript=self.transcript,
            draft=self.draft,
        )
        note_path = Path(published["note"]["path"])
        note_path.write_text(
            note_path.read_text(encoding="utf-8").replace("完成合成原型", "人工补充：完成合成原型"),
            encoding="utf-8",
        )
        before = note_path.read_bytes()

        with self.assertRaisesRegex(pc.ContextError, "edited after machine generation"):
            pc.publish_note(
                self.root,
                transcript=self.transcript,
                draft=self.draft,
                rerun=True,
            )

        self.assertEqual(note_path.read_bytes(), before)

    def test_explicit_rerun_updates_only_an_intact_note(self) -> None:
        first = pc.publish_note(
            self.root,
            transcript=self.transcript,
            draft=self.draft,
        )
        updated = self.base / "updated-note.md"
        updated.write_text(
            self.draft.read_text(encoding="utf-8").replace("团队讨论", "本次讨论"),
            encoding="utf-8",
        )

        second = pc.publish_note(
            self.root,
            transcript=self.transcript,
            draft=updated,
            rerun=True,
        )

        self.assertEqual(second["status"], "republished")
        self.assertEqual(first["note"]["path"], second["note"]["path"])
        self.assertIn("本次讨论", Path(second["note"]["path"]).read_text(encoding="utf-8"))

    def test_rejects_wrong_title_and_draft_inside_vault(self) -> None:
        wrong_title = self.base / "wrong-title.md"
        wrong_title.write_text("# 另一个标题\n\n内容。\n", encoding="utf-8")
        with self.assertRaisesRegex(pc.ContextError, "exactly match"):
            pc.publish_note(
                self.root,
                transcript=self.transcript,
                draft=wrong_title,
            )

        inside = self.root / "draft.md"
        inside.write_text(self.draft.read_text(encoding="utf-8"), encoding="utf-8")
        with self.assertRaisesRegex(pc.ContextError, "outside the Vault"):
            pc.publish_note(
                self.root,
                transcript=self.transcript,
                draft=inside,
            )

    def test_rejects_manually_edited_or_external_transcript(self) -> None:
        external = self.base / "external.md"
        external.write_bytes(self.transcript.read_bytes())
        with self.assertRaisesRegex(pc.ContextError, "directly inside"):
            pc.publish_note(
                self.root,
                transcript=external,
                draft=None,
                check_only=True,
            )

        self.transcript.write_text(
            self.transcript.read_text(encoding="utf-8").replace("合成原型", "人工修改的合成原型"),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(pc.ContextError, "edited after machine generation"):
            pc.publish_note(
                self.root,
                transcript=self.transcript,
                draft=None,
                check_only=True,
            )

    def test_cli_publishes_note_and_storage_status_counts_it(self) -> None:
        script = REPO_ROOT / "personal-context" / "scripts" / "personal_context.py"
        result = subprocess.run(
            [
                sys.executable,
                str(script),
                "publish-note",
                "--root",
                str(self.root),
                "--transcript",
                str(self.transcript),
                "--draft",
                str(self.draft),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "published")
        storage = pc.storage_status(self.root, config_dir=self.base / "config")
        self.assertEqual(storage["inbox"]["files"], 1)
        self.assertEqual(storage["notes"]["files"], 1)
        self.assertNotIn("团队讨论", json.dumps(storage, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
