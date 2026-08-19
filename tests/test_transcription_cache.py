from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from typing import Optional
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
PROVIDERS = REPO_ROOT / "personal-context" / "scripts" / "providers"
sys.path.insert(0, str(PROVIDERS))

import qwen_mlx  # noqa: E402

SCRIPTS = REPO_ROOT / "personal-context" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import personal_context as pc  # noqa: E402


class ArtifactStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="personal-context-artifacts-")
        self.base = Path(self.temp.name) / "artifacts"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_private_atomic_gzip_json_recovers_only_the_corrupt_chunk(self) -> None:
        import artifacts

        scope = artifacts.vault_scope_hash(Path("/synthetic/vault"))
        store = artifacts.ArtifactStore(self.base, scope, "a" * 64)
        first_key = artifacts.component_cache_key({"stage": "asr", "chunk": 0})
        second_key = artifacts.component_cache_key({"stage": "asr", "chunk": 1})

        with store.recording_lock():
            first = store.write("asr", "chunk-00000", first_key, {"text": "第一段"})
            second = store.write("asr", "chunk-00001", second_key, {"text": "第二段"})

        self.assertEqual(store.read("asr", "chunk-00000", first_key).status, "hit")
        self.assertEqual(store.read("asr", "chunk-00001", second_key).payload, {"text": "第二段"})
        self.assertEqual(first.path.suffixes, [".json", ".gz"])
        if os.name != "nt":
            self.assertEqual(first.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.base.stat().st_mode & 0o777, 0o700)
            self.assertEqual(store.scope_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual(first.path.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(store.recording_dir.stat().st_mode & 0o777, 0o700)
            lock_path = store.scope_dir / ".locks" / f"{store.audio_sha256}.lock"
            self.assertEqual(lock_path.stat().st_mode & 0o777, 0o600)

        first.path.write_bytes(b"not-a-gzip-artifact")
        damaged = store.read("asr", "chunk-00000", first_key)
        untouched = store.read("asr", "chunk-00001", second_key)
        self.assertEqual(damaged.status, "corrupt")
        self.assertEqual(untouched.status, "hit")
        self.assertEqual(untouched.payload, {"text": "第二段"})

        with store.recording_lock():
            repaired = store.write("asr", "chunk-00000", first_key, {"text": "重新计算"})
        self.assertEqual(repaired.status, "hit")
        self.assertEqual(store.read("asr", "chunk-00000", first_key).payload, {"text": "重新计算"})

        with self.assertRaises(ValueError):
            store.write("asr", "forbidden", first_key, {"speaker_embedding": [0.1, 0.2]})

    def test_recording_lock_serializes_concurrent_writers(self) -> None:
        import artifacts

        scope = artifacts.vault_scope_hash(Path("/synthetic/vault"))
        store = artifacts.ArtifactStore(self.base, scope, "b" * 64)
        first_acquired = threading.Event()
        release_first = threading.Event()
        second_acquired = threading.Event()

        def first_writer() -> None:
            with store.recording_lock(timeout_seconds=2):
                first_acquired.set()
                release_first.wait(timeout=2)

        def second_writer() -> None:
            first_acquired.wait(timeout=2)
            with store.recording_lock(timeout_seconds=2):
                second_acquired.set()

        first = threading.Thread(target=first_writer)
        second = threading.Thread(target=second_writer)
        first.start()
        second.start()
        self.assertTrue(first_acquired.wait(timeout=1))
        time.sleep(0.1)
        self.assertFalse(second_acquired.is_set())
        release_first.set()
        first.join(timeout=2)
        second.join(timeout=2)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertTrue(second_acquired.is_set())

    def test_cache_status_and_prune_default_to_non_mutating_preview(self) -> None:
        import artifacts

        scope = artifacts.vault_scope_hash(Path("/synthetic/vault"))
        for audio_hash, text in (("d" * 64, "第一条"), ("e" * 64, "第二条")):
            store = artifacts.ArtifactStore(self.base, scope, audio_hash)
            key = artifacts.component_cache_key({"audio": audio_hash})
            with store.recording_lock():
                store.write("asr", "chunk-00000", key, {"text": text})

        status = artifacts.inspect_artifacts(self.base, scope)
        self.assertEqual(status["recording_count"], 2)
        self.assertEqual(status["corrupt_artifacts"], 0)
        self.assertIsNotNone(status["recordings"][0]["last_written_at"])
        self.assertEqual(status["recordings"][0]["stage_details"]["asr"]["artifacts"], 1)
        self.assertGreater(status["recordings"][0]["stage_details"]["asr"]["bytes"], 0)
        preview = artifacts.prune_artifacts(self.base, scope, audio_sha256="d" * 64, apply=False)
        self.assertTrue(preview["dry_run"])
        self.assertEqual([item["audio_sha256"] for item in preview["targets"]], ["d" * 64])
        self.assertTrue((self.base / scope / ("d" * 64)).is_dir())
        self.assertTrue((self.base / scope / ("e" * 64)).is_dir())

        applied = artifacts.prune_artifacts(self.base, scope, audio_sha256="d" * 64, apply=True)
        self.assertFalse(applied["dry_run"])
        self.assertFalse((self.base / scope / ("d" * 64)).exists())
        self.assertTrue((self.base / scope / ("e" * 64)).is_dir())

    def test_cache_status_and_prune_are_available_through_the_json_cli(self) -> None:
        import artifacts

        vault = Path(self.temp.name) / "synthetic vault"
        config = Path(self.temp.name) / "config"
        vault.mkdir()
        audio_hash = "f" * 64
        scope = artifacts.vault_scope_hash(vault)
        store = artifacts.ArtifactStore(config / "artifacts", scope, audio_hash)
        key = artifacts.component_cache_key({"stage": "asr"})
        with store.recording_lock():
            store.write("asr", "chunk-00000", key, {"text": "合成"})
        script = REPO_ROOT / "personal-context" / "scripts" / "personal_context.py"
        common = ["--root", str(vault), "--config-dir", str(config)]
        status = subprocess.run(
            [sys.executable, str(script), "transcription-cache-status", *common],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(json.loads(status.stdout)["recording_count"], 1)
        preview = subprocess.run(
            [sys.executable, str(script), "transcription-cache-prune", *common],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertTrue(json.loads(preview.stdout)["dry_run"])
        self.assertTrue(store.recording_dir.is_dir())

    def test_cache_status_maps_hash_to_source_and_prunes_by_source_id(self) -> None:
        import artifacts

        vault = Path(self.temp.name) / "mapped vault"
        config = Path(self.temp.name) / "mapped config"
        pc.init_vault(vault)
        audio = Path(self.temp.name) / "命名录音.wav"
        audio.write_bytes(b"synthetic named audio")
        ingested = pc.ingest(vault, [audio], observed_at="2026-08-18T00:00:00Z", dry_run=False)
        source_id = ingested["items"][0]["source_id"]
        audio_hash = ingested["items"][0]["content_hash"]
        scope = artifacts.vault_scope_hash(vault)
        store = artifacts.ArtifactStore(config / "artifacts", scope, audio_hash)
        key = artifacts.component_cache_key({"stage": "asr"})
        with store.recording_lock():
            store.write("asr", "chunk-00000", key, {"text": "只存在缓存，不输出正文"})

        status = pc.transcription_cache_status(
            vault, config_dir=config, audio=None, source_id=None, limit=10
        )
        recording = status["recordings"][0]
        self.assertEqual(recording["source_status"], "linked")
        self.assertEqual(recording["source_id"], source_id)
        self.assertEqual(recording["original_name"], audio.name)
        self.assertNotIn("只存在缓存", json.dumps(status, ensure_ascii=False))

        script = REPO_ROOT / "personal-context" / "scripts" / "personal_context.py"
        cli_status = subprocess.run(
            [
                sys.executable,
                str(script),
                "transcription-cache-status",
                "--root",
                str(vault),
                "--config-dir",
                str(config),
                "--source-id",
                source_id,
                "--limit",
                "1",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(cli_status.returncode, 0, cli_status.stderr)
        self.assertEqual(json.loads(cli_status.stdout)["recordings"][0]["original_name"], audio.name)

        preview = pc.transcription_cache_prune(
            vault,
            config_dir=config,
            audio=None,
            source_id=source_id,
            apply=False,
        )
        self.assertEqual(preview["targets"][0]["source_id"], source_id)
        self.assertEqual(preview["targets"][0]["original_name"], audio.name)
        self.assertTrue(store.recording_dir.is_dir())

    def test_storage_status_reports_only_metadata_and_transient_residue(self) -> None:
        vault = Path(self.temp.name) / "storage vault"
        config = Path(self.temp.name) / "storage config"
        pc.init_vault(vault)
        audio = Path(self.temp.name) / "存储录音.wav"
        audio.write_bytes(b"synthetic source bytes")
        pc.ingest(vault, [audio], observed_at="2026-08-18T00:00:00Z", dry_run=False)
        (vault / "inbox" / "存储录音-录音转写.md").write_text("合成逐字稿", encoding="utf-8")
        stale = config / "capture-jobs" / "capture-audio-stale"
        stale.mkdir(parents=True)
        (stale / "transcript.v1.json").write_text("sensitive transient", encoding="utf-8")

        status = pc.storage_status(vault, config_dir=config)

        self.assertEqual(status["source_blobs"]["recordings"], 1)
        self.assertGreater(status["source_blobs"]["bytes"], 0)
        self.assertEqual(status["inbox"]["files"], 1)
        self.assertEqual(status["transient_jobs"]["jobs"], 1)
        self.assertGreater(status["transient_jobs"]["bytes"], 0)
        rendered = json.dumps(status, ensure_ascii=False)
        self.assertNotIn("合成逐字稿", rendered)
        self.assertNotIn("sensitive transient", rendered)

        script = REPO_ROOT / "personal-context" / "scripts" / "personal_context.py"
        cli_status = subprocess.run(
            [
                sys.executable,
                str(script),
                "storage-status",
                "--root",
                str(vault),
                "--config-dir",
                str(config),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(cli_status.returncode, 0, cli_status.stderr)
        self.assertEqual(json.loads(cli_status.stdout)["transient_jobs"]["jobs"], 1)


class ProviderStageCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="personal-context-stage-cache-")
        self.base = Path(self.temp.name)
        self.audio = self.base / "synthetic.wav"
        self.audio.write_bytes(b"synthetic-audio-evidence")
        self.artifacts_dir = self.base / "artifacts"
        self.models_dir = self.base / "models"
        self.models_dir.mkdir()
        self.manifest = json.loads(
            (REPO_ROOT / "personal-context" / "assets" / "providers" / "qwen-mlx.lock.json").read_text(
                encoding="utf-8"
            )
        )
        self.scope = "c" * 64

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _fake_model_modules(self, counters: dict[str, int]) -> dict[str, types.ModuleType]:
        class AsrModel:
            def generate(self, path: str, language: Optional[str] = None) -> object:
                del path, language
                counters["asr_generate"] += 1
                return types.SimpleNamespace(text="你好世界")

        class AlignerModel:
            def generate(self, path: str, text: str, language: Optional[str] = None) -> object:
                del path, text, language
                counters["alignment_generate"] += 1
                return [
                    types.SimpleNamespace(text="你好", start_time=0.0, end_time=0.4),
                    types.SimpleNamespace(text="世界", start_time=0.4, end_time=0.8),
                ]

        class Modules:
            chunk_len = 0
            chunk_right_context = 0
            fifo_len = 0
            spkcache_update_period = 0
            spkcache_len = 0

        class DiarizerModel:
            config = types.SimpleNamespace(
                modules_config=Modules(),
                processor_config=types.SimpleNamespace(hop_length=160, sampling_rate=16000),
                fc_encoder_config=types.SimpleNamespace(subsampling_factor=8),
            )

            def generate_stream(self, path: str, **kwargs: object) -> list[object]:
                del path, kwargs
                counters["diarization_generate"] += 1
                return [types.SimpleNamespace(speaker_probs=[[0.9, 0.1], [0.1, 0.9]] * 8)]

        stt = types.ModuleType("mlx_audio.stt")
        vad = types.ModuleType("mlx_audio.vad")

        def load_stt(path: str) -> object:
            if "ForcedAligner" in path:
                counters["alignment_load"] += 1
                return AlignerModel()
            counters["asr_load"] += 1
            return AsrModel()

        def load_vad(path: str) -> object:
            del path
            counters["diarization_load"] += 1
            return DiarizerModel()

        stt.load = load_stt  # type: ignore[attr-defined]
        vad.load = load_vad  # type: ignore[attr-defined]
        package = types.ModuleType("mlx_audio")
        package.stt = stt  # type: ignore[attr-defined]
        package.vad = vad  # type: ignore[attr-defined]
        return {"mlx_audio": package, "mlx_audio.stt": stt, "mlx_audio.vad": vad}

    def test_hot_cache_and_metadata_changes_do_not_reload_models(self) -> None:
        counters = {
            "asr_load": 0,
            "alignment_load": 0,
            "diarization_load": 0,
            "asr_generate": 0,
            "alignment_generate": 0,
            "diarization_generate": 0,
        }

        def fake_audio(_: Path) -> tuple[list[float], int]:
            return [0.1] * 16000, 16000

        def fake_wav(path: Path, samples: object, sample_rate: int = 16000) -> None:
            del samples, sample_rate
            path.write_bytes(b"normalized")

        cold = self.base / "cold.json"
        hot = self.base / "hot.json"
        changed = self.base / "changed.json"
        modules = self._fake_model_modules(counters)
        with mock.patch.dict(sys.modules, modules), mock.patch.object(
            qwen_mlx, "_to_mono_16k", side_effect=fake_audio
        ), mock.patch.object(qwen_mlx, "_write_wav", side_effect=fake_wav):
            qwen_mlx.transcribe(
                copy.deepcopy(self.manifest),
                self.models_dir,
                self.audio,
                cold,
                language="Chinese",
                title="固定标题",
                observed_at="2026-08-16T10:00:00Z",
                speaker_count=2,
                artifacts_dir=self.artifacts_dir,
                vault_scope=self.scope,
            )
            cold_counts = dict(counters)
            qwen_mlx.transcribe(
                copy.deepcopy(self.manifest),
                self.models_dir,
                self.audio,
                hot,
                language="Chinese",
                title="固定标题",
                observed_at="2026-08-16T10:00:00Z",
                speaker_count=2,
                artifacts_dir=self.artifacts_dir,
                vault_scope=self.scope,
            )
            self.assertEqual(counters, cold_counts)
            self.assertEqual(cold.read_bytes(), hot.read_bytes())

            qwen_mlx.transcribe(
                copy.deepcopy(self.manifest),
                self.models_dir,
                self.audio,
                changed,
                language="Chinese",
                title="改变标题",
                observed_at="2026-08-16T11:00:00Z",
                speaker_count=1,
                artifacts_dir=self.artifacts_dir,
                vault_scope=self.scope,
            )
            postprocessing_manifest = copy.deepcopy(self.manifest)
            postprocessing_manifest["diarization"]["postprocessing"][
                "max_weak_switch_margin"
            ] = 0.11
            qwen_mlx.transcribe(
                postprocessing_manifest,
                self.models_dir,
                self.audio,
                self.base / "postprocessing-changed.json",
                language="Chinese",
                title="改变标题",
                observed_at="2026-08-16T11:00:00Z",
                speaker_count=1,
                artifacts_dir=self.artifacts_dir,
                vault_scope=self.scope,
            )
        self.assertEqual(counters, cold_counts)
        self.assertEqual(json.loads(changed.read_text(encoding="utf-8"))["event"]["title"], "改变标题")

    def test_language_and_model_revisions_invalidate_only_their_components(self) -> None:
        cases = (
            ("language", "English", None, (2, 2, 1)),
            ("asr", "Chinese", ("asr", "1" * 40), (2, 1, 1)),
            ("aligner", "Chinese", ("aligner", "2" * 40), (1, 2, 1)),
            ("diarizer", "Chinese", ("diarizer", "3" * 40), (1, 1, 2)),
            ("runtime-package", "Chinese", ("runtime-package", "mlx-audio[stt]==9.9.9"), (2, 2, 2)),
        )
        for name, changed_language, revision_change, expected_loads in cases:
            with self.subTest(name=name):
                counters = {
                    "asr_load": 0,
                    "alignment_load": 0,
                    "diarization_load": 0,
                    "asr_generate": 0,
                    "alignment_generate": 0,
                    "diarization_generate": 0,
                }
                artifacts_dir = self.artifacts_dir / name
                manifest = copy.deepcopy(self.manifest)
                modules = self._fake_model_modules(counters)

                def fake_audio(_: Path) -> tuple[list[float], int]:
                    return [0.1] * 16000, 16000

                def fake_wav(path: Path, samples: object, sample_rate: int = 16000) -> None:
                    del samples, sample_rate
                    path.write_bytes(b"normalized")

                with mock.patch.dict(sys.modules, modules), mock.patch.object(
                    qwen_mlx, "_to_mono_16k", side_effect=fake_audio
                ), mock.patch.object(qwen_mlx, "_write_wav", side_effect=fake_wav):
                    qwen_mlx.transcribe(
                        manifest,
                        self.models_dir,
                        self.audio,
                        self.base / f"{name}-cold.json",
                        language="Chinese",
                        title="固定",
                        observed_at="2026-08-16T10:00:00Z",
                        speaker_count=2,
                        artifacts_dir=artifacts_dir,
                        vault_scope=self.scope,
                    )
                    changed_manifest = copy.deepcopy(manifest)
                    if revision_change is not None:
                        role, revision = revision_change
                        if role == "runtime-package":
                            changed_manifest["runtime"]["packages"] = [revision]
                        else:
                            changed_manifest["models"][role]["revision"] = revision
                    qwen_mlx.transcribe(
                        changed_manifest,
                        self.models_dir,
                        self.audio,
                        self.base / f"{name}-changed.json",
                        language=changed_language,
                        title="固定",
                        observed_at="2026-08-16T10:00:00Z",
                        speaker_count=2,
                        artifacts_dir=artifacts_dir,
                        vault_scope=self.scope,
                    )
                self.assertEqual(
                    (
                        counters["asr_load"],
                        counters["alignment_load"],
                        counters["diarization_load"],
                    ),
                    expected_loads,
                )

    def test_one_corrupt_asr_chunk_recomputes_only_that_chunk(self) -> None:
        import artifacts

        counters = {
            "asr_load": 0,
            "alignment_load": 0,
            "diarization_load": 0,
            "asr_generate": 0,
            "alignment_generate": 0,
            "diarization_generate": 0,
        }
        manifest = copy.deepcopy(self.manifest)
        manifest["limits"]["asr_chunk_seconds"] = 1
        modules = self._fake_model_modules(counters)

        def fake_audio(_: Path) -> tuple[list[float], int]:
            return [0.1] * 32000, 16000

        def fake_wav(path: Path, samples: object, sample_rate: int = 16000) -> None:
            del samples, sample_rate
            path.write_bytes(b"normalized")

        cold = self.base / "corrupt-cold.json"
        repaired = self.base / "corrupt-repaired.json"
        with mock.patch.dict(sys.modules, modules), mock.patch.object(
            qwen_mlx, "_to_mono_16k", side_effect=fake_audio
        ), mock.patch.object(qwen_mlx, "_write_wav", side_effect=fake_wav):
            qwen_mlx.transcribe(
                manifest,
                self.models_dir,
                self.audio,
                cold,
                language="Chinese",
                title="固定",
                observed_at="2026-08-16T10:00:00Z",
                speaker_count=2,
                artifacts_dir=self.artifacts_dir,
                vault_scope=self.scope,
            )
            cold_counts = dict(counters)
            audio_hash = hashlib.sha256(self.audio.read_bytes()).hexdigest()
            store = artifacts.ArtifactStore(self.artifacts_dir, self.scope, audio_hash)
            store.path_for("asr", "chunk-00001").write_bytes(b"damaged")
            qwen_mlx.transcribe(
                manifest,
                self.models_dir,
                self.audio,
                repaired,
                language="Chinese",
                title="固定",
                observed_at="2026-08-16T10:00:00Z",
                speaker_count=2,
                artifacts_dir=self.artifacts_dir,
                vault_scope=self.scope,
            )
        self.assertEqual(counters["asr_generate"], cold_counts["asr_generate"] + 1)
        self.assertEqual(counters["alignment_generate"], cold_counts["alignment_generate"])
        self.assertEqual(counters["diarization_generate"], cold_counts["diarization_generate"])
        self.assertEqual(cold.read_bytes(), repaired.read_bytes())

    def test_no_cache_bypasses_storage_and_refresh_stage_is_targeted(self) -> None:
        counters = {
            "asr_load": 0,
            "alignment_load": 0,
            "diarization_load": 0,
            "asr_generate": 0,
            "alignment_generate": 0,
            "diarization_generate": 0,
        }
        modules = self._fake_model_modules(counters)

        def fake_audio(_: Path) -> tuple[list[float], int]:
            return [0.1] * 16000, 16000

        def fake_wav(path: Path, samples: object, sample_rate: int = 16000) -> None:
            del samples, sample_rate
            path.write_bytes(b"normalized")

        with mock.patch.dict(sys.modules, modules), mock.patch.object(
            qwen_mlx, "_to_mono_16k", side_effect=fake_audio
        ), mock.patch.object(qwen_mlx, "_write_wav", side_effect=fake_wav):
            for index in range(2):
                qwen_mlx.transcribe(
                    copy.deepcopy(self.manifest),
                    self.models_dir,
                    self.audio,
                    self.base / f"no-cache-{index}.json",
                    language="Chinese",
                    title="固定",
                    observed_at="2026-08-16T10:00:00Z",
                    speaker_count=2,
                    no_cache=True,
                )
        self.assertFalse(self.artifacts_dir.exists())
        self.assertEqual(
            (counters["asr_generate"], counters["alignment_generate"], counters["diarization_generate"]),
            (2, 2, 2),
        )

        counters = {key: 0 for key in counters}
        modules = self._fake_model_modules(counters)
        with mock.patch.dict(sys.modules, modules), mock.patch.object(
            qwen_mlx, "_to_mono_16k", side_effect=fake_audio
        ), mock.patch.object(qwen_mlx, "_write_wav", side_effect=fake_wav):
            qwen_mlx.transcribe(
                copy.deepcopy(self.manifest),
                self.models_dir,
                self.audio,
                self.base / "refresh-cold.json",
                language="Chinese",
                title="固定",
                observed_at="2026-08-16T10:00:00Z",
                speaker_count=2,
                artifacts_dir=self.artifacts_dir,
                vault_scope=self.scope,
            )
            qwen_mlx.transcribe(
                copy.deepcopy(self.manifest),
                self.models_dir,
                self.audio,
                self.base / "refresh-alignment.json",
                language="Chinese",
                title="固定",
                observed_at="2026-08-16T10:00:00Z",
                speaker_count=2,
                artifacts_dir=self.artifacts_dir,
                vault_scope=self.scope,
                refresh_stage="alignment",
            )
            after_alignment = dict(counters)
            qwen_mlx.transcribe(
                copy.deepcopy(self.manifest),
                self.models_dir,
                self.audio,
                self.base / "refresh-diarization.json",
                language="Chinese",
                title="固定",
                observed_at="2026-08-16T10:00:00Z",
                speaker_count=2,
                artifacts_dir=self.artifacts_dir,
                vault_scope=self.scope,
                refresh_stage="diarization",
            )
            after_diarization = dict(counters)
            qwen_mlx.transcribe(
                copy.deepcopy(self.manifest),
                self.models_dir,
                self.audio,
                self.base / "refresh-asr.json",
                language="Chinese",
                title="固定",
                observed_at="2026-08-16T10:00:00Z",
                speaker_count=2,
                artifacts_dir=self.artifacts_dir,
                vault_scope=self.scope,
                refresh_stage="asr",
            )
            after_asr = dict(counters)
            qwen_mlx.transcribe(
                copy.deepcopy(self.manifest),
                self.models_dir,
                self.audio,
                self.base / "refresh-all.json",
                language="Chinese",
                title="固定",
                observed_at="2026-08-16T10:00:00Z",
                speaker_count=2,
                artifacts_dir=self.artifacts_dir,
                vault_scope=self.scope,
                refresh_stage="all",
            )
        self.assertEqual(
            (
                after_alignment["asr_generate"],
                after_alignment["alignment_generate"],
                after_alignment["diarization_generate"],
            ),
            (1, 2, 1),
        )
        self.assertEqual(
            (
                after_diarization["asr_generate"],
                after_diarization["alignment_generate"],
                after_diarization["diarization_generate"],
            ),
            (1, 2, 2),
        )
        self.assertEqual(
            (after_asr["asr_generate"], after_asr["alignment_generate"], after_asr["diarization_generate"]),
            (2, 2, 2),
        )
        self.assertEqual(
            (counters["asr_generate"], counters["alignment_generate"], counters["diarization_generate"]),
            (3, 3, 3),
        )
