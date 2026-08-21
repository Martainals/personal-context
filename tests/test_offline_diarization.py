from __future__ import annotations

import copy
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
PROVIDERS = REPO_ROOT / "personal-context" / "scripts" / "providers"
sys.path.insert(0, str(PROVIDERS))
SCRIPTS = REPO_ROOT / "personal-context" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import diarization_3dspeaker  # noqa: E402
import artifacts  # noqa: E402
import personal_context_bootstrap as onboarding  # noqa: E402
import qwen_mlx  # noqa: E402
import transcript_assembly  # noqa: E402


class DiarizationEvidenceTests(unittest.TestCase):
    @staticmethod
    def _sentence_tail_settings() -> dict[str, object]:
        return {
            "boundary_realign_enabled": False,
            "sentence_tail_absorption_enabled": True,
            "sentence_tail_join_gap_seconds": 0.2,
            "sentence_tail_max_characters": 4,
            "sentence_tail_max_seconds": 0.8,
            "sentence_tail_pause_seconds": 0.2,
            "max_fragment_characters": 0,
            "max_fragment_seconds": 0.0,
            "max_fragment_gap_seconds": 0.0,
            "max_fragment_margin": 0.0,
            "max_same_speaker_gap_seconds": 1.2,
            "max_segment_characters": 280,
            "sentence_pause_seconds": 0.8,
        }

    def test_sentence_tail_absorption_keeps_question_with_previous_speaker(self) -> None:
        words = [
            {"start": 3.28, "end": 3.84, "text": "你不是买"},
            {"start": 4.0, "end": 4.4, "text": "了是吧？"},
            {"start": 4.64, "end": 5.2, "text": "不是，是这样。"},
        ]
        diarization = [
            {
                "start": 3.0,
                "end": 4.125,
                "speaker": 0,
                "confidence": 0.8,
                "margin": 0.25,
            },
            {
                "start": 4.125,
                "end": 6.0,
                "speaker": 1,
                "confidence": 0.8,
                "margin": 0.4,
            },
        ]

        segments = transcript_assembly.merge_words(
            words, diarization, self._sentence_tail_settings()
        )

        self.assertEqual(
            [(item["speaker"], item["text"]) for item in segments],
            [("S01", "你不是买了是吧？"), ("S02", "不是，是这样。")],
        )

    def test_sentence_tail_absorption_preserves_real_short_interjection(self) -> None:
        words = [
            {"start": 1.12, "end": 1.84, "text": "不能再熬夜了，我"},
            {"start": 1.92, "end": 2.0, "text": "操！"},
            {"start": 2.4, "end": 3.28, "text": "啊，你买了？"},
        ]
        diarization = [
            {
                "start": 0.0,
                "end": 1.875,
                "speaker": 0,
                "confidence": 0.8,
                "margin": 0.25,
            },
            {
                "start": 1.875,
                "end": 4.125,
                "speaker": 1,
                "confidence": 0.8,
                "margin": 0.25,
            },
        ]

        segments = transcript_assembly.merge_words(
            words, diarization, self._sentence_tail_settings()
        )

        self.assertEqual(
            [(item["speaker"], item["text"]) for item in segments],
            [
                ("S01", "不能再熬夜了，我"),
                ("S02", "操！"),
                ("S02", "啊，你买了？"),
            ],
        )

    def test_sentence_tail_absorption_never_steals_after_complete_sentence(self) -> None:
        words = [
            {"start": 0.0, "end": 1.0, "text": "我已经说完了。"},
            {"start": 1.08, "end": 1.4, "text": "你呢？"},
            {"start": 1.6, "end": 2.2, "text": "我觉得可以。"},
        ]
        diarization = [
            {
                "start": 0.0,
                "end": 1.04,
                "speaker": 0,
                "confidence": 0.9,
                "margin": 0.6,
            },
            {
                "start": 1.04,
                "end": 3.0,
                "speaker": 1,
                "confidence": 0.9,
                "margin": 0.6,
            },
        ]

        segments = transcript_assembly.merge_words(
            words, diarization, self._sentence_tail_settings()
        )

        self.assertEqual(
            [(item["speaker"], item["text"]) for item in segments],
            [
                ("S01", "我已经说完了。"),
                ("S02", "你呢？"),
                ("S02", "我觉得可以。"),
            ],
        )

    def test_diarizer_model_provenance_is_safe_for_the_artifact_store(self) -> None:
        manifest = onboarding.load_manifest("qwen-mlx-3dspeaker")
        details = diarization_3dspeaker._result_details(
            manifest,
            speaker_count=2,
            output_speakers=2,
            speech_chunks=4,
        )
        payload = {
            "segments": [
                {
                    "start": 0.0,
                    "end": 1.0,
                    "speaker": 0,
                    "confidence": 0.9,
                    "margin": 0.8,
                }
            ],
            "details": details,
        }

        with tempfile.TemporaryDirectory(prefix="personal-context-safe-provenance-") as temporary:
            store = artifacts.ArtifactStore(
                Path(temporary),
                "a" * 64,
                "b" * 64,
            )
            written = store.write("diarization", "offline-turns", "c" * 64, payload)

        self.assertEqual(written.status, "hit")
        self.assertIn("speaker_encoder", details["models"])
        self.assertNotIn("embedding", details["models"])

    def test_offline_protocol_preflight_rejects_unsafe_metadata_before_inference(self) -> None:
        unsafe_payload = {
            "segments": [
                {
                    "start": 0.0,
                    "end": 1.0,
                    "speaker": 0,
                    "confidence": 0.9,
                    "margin": 0.8,
                }
            ],
            "details": {
                "backend": "3dspeaker-offline",
                "speaker_embedding": [0.1, 0.2],
            },
        }
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(unsafe_payload),
            stderr="",
        )
        command = [
            "/private/runtime/venv/bin/python",
            "/private/runtime/diarization_3dspeaker.py",
            "diarize",
            "--manifest",
            "/private/runtime/manifest.json",
            "--source-dir",
            "/private/runtime/source",
            "--models-dir",
            "/private/runtime/models",
        ]

        with mock.patch.object(qwen_mlx.subprocess, "run", return_value=completed) as run:
            with self.assertRaisesRegex(RuntimeError, "preflight"):
                qwen_mlx._run_offline_diarization(command, Path("audio.wav"), 2)

        self.assertEqual(run.call_count, 1)
        self.assertIn("preflight", run.call_args.args[0])
        self.assertNotIn("--audio", run.call_args.args[0])

    def test_offline_protocol_runs_inference_after_safe_preflight(self) -> None:
        safe_details = {
            "backend": "3dspeaker-offline",
            "models": {
                "speaker_encoder": {"repo_id": "example/encoder", "revision": "v1"},
                "vad": {"repo_id": "example/vad", "revision": "v2"},
            },
        }
        segment = {
            "start": 0.0,
            "end": 1.0,
            "speaker": 0,
            "confidence": 0.9,
            "margin": 0.8,
        }
        responses = [
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps({"details": safe_details}),
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps({"segments": [segment], "details": safe_details}),
                stderr="",
            ),
        ]
        command = [
            "/private/runtime/venv/bin/python",
            "/private/runtime/diarization_3dspeaker.py",
            "diarize",
            "--manifest",
            "/private/runtime/manifest.json",
            "--source-dir",
            "/private/runtime/source",
            "--models-dir",
            "/private/runtime/models",
        ]

        with mock.patch.object(qwen_mlx.subprocess, "run", side_effect=responses) as run:
            result = qwen_mlx._run_offline_diarization(command, Path("audio.wav"), 2)

        self.assertEqual(result["segments"], [segment])
        self.assertEqual(run.call_count, 2)
        self.assertIn("preflight", run.call_args_list[0].args[0])
        self.assertNotIn("--audio", run.call_args_list[0].args[0])
        self.assertIn("diarize", run.call_args_list[1].args[0])
        self.assertIn("--audio", run.call_args_list[1].args[0])

    def test_diarizer_keeps_library_logs_out_of_json_stdout(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()

        def noisy_diarize(*_: object) -> dict[str, object]:
            print("library notice")
            return {
                "segments": [
                    {
                        "start": 0.0,
                        "end": 1.0,
                        "speaker": 0,
                        "confidence": 0.9,
                        "margin": 0.8,
                    }
                ],
                "details": {"backend": "3dspeaker-offline"},
            }

        arguments = [
            "diarization_3dspeaker.py",
            "diarize",
            "--manifest",
            "manifest.json",
            "--source-dir",
            "source",
            "--models-dir",
            "models",
            "--audio",
            "audio.wav",
            "--speaker-count",
            "2",
        ]
        with mock.patch.object(sys, "argv", arguments), mock.patch.object(
            diarization_3dspeaker, "_load_manifest", return_value={}
        ), mock.patch.object(
            diarization_3dspeaker, "diarize", side_effect=noisy_diarize
        ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = diarization_3dspeaker.main()

        self.assertEqual(result, 0)
        self.assertIn("library notice", stderr.getvalue())
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["details"]["backend"], "3dspeaker-offline")

    def test_offline_command_preserves_virtualenv_python_symlink(self) -> None:
        with tempfile.TemporaryDirectory(prefix="personal-context-offline-python-") as temporary:
            base = Path(temporary)
            real_python = base / "python-3.12"
            real_python.write_text("synthetic interpreter", encoding="utf-8")
            venv_python = base / "venv" / "bin" / "python"
            venv_python.parent.mkdir(parents=True)
            venv_python.symlink_to(real_python)

            command = qwen_mlx._build_offline_command(
                str(venv_python),
                str(base / "diarizer.py"),
                str(base / "manifest.json"),
                str(base / "source"),
                str(base / "models"),
            )

        self.assertEqual(command[0], str(venv_python.absolute()))
        self.assertNotEqual(command[0], str(real_python.resolve()))

    def test_cluster_evidence_exposes_only_anonymous_scalar_scores(self) -> None:
        chunks = [[0.0, 1.5], [0.75, 2.25], [2.25, 3.75], [3.0, 4.5]]
        labels = [0, 0, 1, 1]
        embeddings = [
            [1.0, 0.0],
            [0.98, 0.02],
            [-1.0, 0.0],
            [-0.98, -0.02],
        ]

        segments = diarization_3dspeaker.anonymous_segments_with_evidence(
            chunks, labels, embeddings
        )

        self.assertEqual([item["speaker"] for item in segments], [0, 1])
        self.assertTrue(all(0.0 <= item["confidence"] <= 1.0 for item in segments))
        self.assertTrue(all(item["margin"] > 0.9 for item in segments))
        encoded = json.dumps(segments, ensure_ascii=False).casefold()
        self.assertNotIn("embedding", encoded)
        self.assertNotIn("centroid", encoded)
        self.assertEqual(
            set(segments[0]), {"start", "end", "speaker", "confidence", "margin"}
        )

    def test_auto_clustering_handles_fewer_chunks_than_the_speaker_limit(self) -> None:
        count = diarization_3dspeaker._speaker_count_from_eigenvalues(
            [0.0, 0.8], 1, 4
        )

        self.assertEqual(count, 1)

    def test_offline_profile_can_disable_broad_boundary_snapping(self) -> None:
        words = [
            {"start": 0.0, "end": 0.6, "text": "不能再熬"},
            {"start": 0.6, "end": 0.7, "text": "夜"},
            {"start": 0.7, "end": 0.8, "text": "了"},
            {"start": 1.1, "end": 1.2, "text": "我"},
        ]
        diarization = [
            {"start": 0.0, "end": 0.6, "speaker": 0, "confidence": 0.9, "margin": 0.8},
            {"start": 0.6, "end": 1.2, "speaker": 1, "confidence": 0.9, "margin": 0.8},
        ]
        settings = {
            "boundary_realign_enabled": False,
            "max_fragment_characters": 0,
            "max_fragment_seconds": 0.0,
            "max_fragment_gap_seconds": 0.0,
            "max_fragment_margin": 0.0,
            "max_same_speaker_gap_seconds": 1.2,
            "max_segment_characters": 280,
            "sentence_pause_seconds": 0.2,
        }

        segments = transcript_assembly.merge_words(words, diarization, settings)

        self.assertEqual(
            [(item["speaker"], item["text"]) for item in segments],
            [("S01", "不能再熬"), ("S02", "夜了"), ("S02", "我")],
        )


class OfflineBackendCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="personal-context-offline-diar-")
        self.base = Path(self.temp.name)
        self.audio = self.base / "synthetic.wav"
        self.audio.write_bytes(b"synthetic-audio-evidence")
        self.artifacts = self.base / "artifacts"
        self.models = self.base / "models"
        self.models.mkdir()
        self.scope = "a" * 64
        self.manifest = json.loads(
            (
                REPO_ROOT
                / "personal-context"
                / "assets"
                / "providers"
                / "qwen-mlx.lock.json"
            ).read_text(encoding="utf-8")
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _offline_profile(package: str) -> dict[str, object]:
        return {
            "provider": "qwen-mlx-3dspeaker",
            "profile_version": 1,
            "backend": "3dspeaker-offline",
            "source": {
                "repo": "https://github.com/modelscope/3D-Speaker.git",
                "revision": "065629c313eaf1a01c65c640c46d77e61e9607b4",
            },
            "runtime": {"packages": [package]},
            "models": {
                "embedding": {
                    "repo_id": "iic/speech_campplus_sv_zh_en_16k-common_advanced",
                    "revision": "v1.0.0",
                },
                "vad": {
                    "repo_id": "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
                    "revision": "v2.0.4",
                },
            },
            "diarization": {"stage_version": 1, "mode": "offline_clustering"},
        }

    def test_changing_only_diarizer_runtime_reuses_asr_and_alignment(self) -> None:
        counters = {"asr": 0, "alignment": 0, "offline": 0, "sortformer": 0}

        class AsrModel:
            def generate(self, path: str, language: str | None = None) -> object:
                del path, language
                counters["asr"] += 1
                return types.SimpleNamespace(text="你好，世界。")

        class AlignmentModel:
            def generate(
                self, path: str, text: str, language: str | None = None
            ) -> object:
                del path, text, language
                counters["alignment"] += 1
                return [
                    types.SimpleNamespace(text="你好", start_time=0.0, end_time=0.4),
                    types.SimpleNamespace(text="世界", start_time=0.4, end_time=0.8),
                ]

        def load_stt(_: Path, model: dict[str, object]) -> object:
            if "ForcedAligner" in str(model["repo_id"]):
                return AlignmentModel()
            return AsrModel()

        def offline_runner(audio: Path, speaker_count: int | None) -> dict[str, object]:
            del audio
            counters["offline"] += 1
            self.assertEqual(speaker_count, 2)
            return {
                "segments": [
                    {
                        "start": 0.0,
                        "end": 1.0,
                        "speaker": 0,
                        "confidence": 0.93,
                        "margin": 0.71,
                    }
                ],
                "details": {
                    "backend": "3dspeaker-offline",
                    "evidence": "cluster-distance",
                    "expected_speakers": 2,
                },
            }

        def fake_audio(_: Path) -> tuple[list[float], int]:
            return [0.1] * 16000, 16000

        def fake_wav(path: Path, samples: object, sample_rate: int = 16000) -> None:
            del samples, sample_rate
            path.write_bytes(b"normalized")

        with mock.patch.object(qwen_mlx, "_to_mono_16k", side_effect=fake_audio), mock.patch.object(
            qwen_mlx, "_write_wav", side_effect=fake_wav
        ), mock.patch.object(qwen_mlx, "_load_stt_model", side_effect=load_stt), mock.patch.object(
            qwen_mlx,
            "_load_vad_model",
            side_effect=lambda *_: counters.__setitem__("sortformer", counters["sortformer"] + 1),
        ):
            first = qwen_mlx.transcribe(
                copy.deepcopy(self.manifest),
                self.models,
                self.audio,
                self.base / "first.json",
                language="Chinese",
                title="合成",
                observed_at="2026-08-19T00:00:00Z",
                speaker_count=2,
                artifacts_dir=self.artifacts,
                vault_scope=self.scope,
                provider_name="qwen-mlx-3dspeaker",
                diarization_backend=self._offline_profile("torch==2.8.0"),
                offline_diarization_runner=offline_runner,
            )
            first_counts = dict(counters)
            second = qwen_mlx.transcribe(
                copy.deepcopy(self.manifest),
                self.models,
                self.audio,
                self.base / "second.json",
                language="Chinese",
                title="合成",
                observed_at="2026-08-19T00:00:00Z",
                speaker_count=2,
                artifacts_dir=self.artifacts,
                vault_scope=self.scope,
                provider_name="qwen-mlx-3dspeaker",
                diarization_backend=self._offline_profile("torch==2.8.1"),
                offline_diarization_runner=offline_runner,
            )

        self.assertEqual(first_counts, {"asr": 1, "alignment": 1, "offline": 1, "sortformer": 0})
        self.assertEqual(counters, {"asr": 1, "alignment": 1, "offline": 2, "sortformer": 0})
        self.assertEqual(len(first["cache"]["events"]), 4)
        self.assertEqual(first["cache"]["computed"], 4)
        self.assertEqual(len(second["cache"]["events"]), 4)
        self.assertEqual(second["cache"]["hits"], 3)
        self.assertEqual(second["cache"]["computed"], 1)
        self.assertEqual(first["cache"]["enabled"], True)
        self.assertEqual(second["cache"]["enabled"], True)
        self.assertEqual(
            json.loads((self.base / "second.json").read_text(encoding="utf-8"))["processing"]["provider"],
            "qwen-mlx-3dspeaker",
        )
        self.assertTrue(
            (
                self.artifacts
                / self.scope
                / qwen_mlx.digest_file(self.audio)
                / "diarization"
                / "offline-turns.json.gz"
            ).is_file()
        )


class ExperimentalProviderPlanTests(unittest.TestCase):
    def test_experimental_profile_enables_only_conservative_sentence_tail_absorption(
        self,
    ) -> None:
        settings = onboarding.load_manifest("qwen-mlx-3dspeaker")["word_assembly"]

        self.assertFalse(settings["boundary_realign_enabled"])
        self.assertTrue(settings["sentence_tail_absorption_enabled"])
        self.assertEqual(settings["sentence_tail_max_characters"], 4)
        self.assertEqual(settings["sentence_tail_max_seconds"], 0.8)
        self.assertIn("操", settings["sentence_tail_protected_responses"])
        self.assertIn("我操", settings["sentence_tail_protected_responses"])

    def test_macos_runtime_pins_only_required_modelscope_audio_stack(self) -> None:
        packages = onboarding.load_manifest("qwen-mlx-3dspeaker")["runtime"][
            "packages"
        ]

        self.assertIn("modelscope[framework]==1.39.0", packages)
        self.assertIn("funasr==1.4.2", packages)
        self.assertIn("PyYAML==6.0.2", packages)
        self.assertIn("tqdm==4.67.1", packages)
        self.assertFalse(
            any(package.startswith("modelscope[audio]") for package in packages)
        )

    def test_word_assembly_change_does_not_invalidate_offline_runtime(self) -> None:
        manifest = onboarding.load_manifest("qwen-mlx-3dspeaker")
        changed = copy.deepcopy(manifest)
        changed["word_assembly"]["sentence_tail_max_characters"] += 1

        self.assertEqual(
            onboarding.offline_runtime_digest(manifest),
            onboarding.offline_runtime_digest(changed),
        )
        self.assertNotEqual(
            onboarding.manifest_digest(manifest),
            onboarding.manifest_digest(changed),
        )

    def test_runtime_package_change_invalidates_offline_runtime(self) -> None:
        manifest = onboarding.load_manifest("qwen-mlx-3dspeaker")
        changed = copy.deepcopy(manifest)
        changed["runtime"]["packages"] = [*changed["runtime"]["packages"], "example==1.0"]

        self.assertNotEqual(
            onboarding.offline_runtime_digest(manifest),
            onboarding.offline_runtime_digest(changed),
        )

    def test_qwen_processing_policy_renews_consent_without_reinstalling_models(self) -> None:
        manifest = onboarding.load_manifest("qwen-mlx")
        changed_policy = copy.deepcopy(manifest)
        changed_policy["asr_recovery"]["minimum_repeated_run_characters"] += 1
        self.assertEqual(
            onboarding.qwen_runtime_digest(manifest),
            onboarding.qwen_runtime_digest(changed_policy),
        )
        changed_model = copy.deepcopy(manifest)
        changed_model["models"]["asr"]["revision"] = "1" * 40
        self.assertNotEqual(
            onboarding.qwen_runtime_digest(manifest),
            onboarding.qwen_runtime_digest(changed_model),
        )

        compatible = {
            "system": "Darwin",
            "machine": "arm64",
            "python": "3.9.0",
            "qwen_mlx_compatible": True,
            "qwen_mlx_reason": None,
            "git_available": True,
            "git_path": "/usr/bin/git",
        }
        with tempfile.TemporaryDirectory(prefix="personal-context-qwen-upgrade-") as temporary:
            base = Path(temporary)
            root = base / "vault"
            config = base / "config"
            runtime = onboarding.runtime_dir(config)
            python_path = onboarding._venv_python(runtime / "venv")
            python_path.parent.mkdir(parents=True)
            python_path.write_bytes(b"synthetic")
            for model in manifest["models"].values():
                (
                    runtime
                    / "models"
                    / model["repo_id"].replace("/", "--")
                ).mkdir(parents=True)
            onboarding._write_private_json(
                runtime / "runtime.json",
                {
                    "provider": "qwen-mlx",
                    "profile_version": 3,
                    "manifest_digest": "59c246c139563a578339f0bd9fdde16f71c35b1a570ddf0b18ec7d56a65db750",
                    "python": manifest["runtime"]["python"],
                    "packages": manifest["runtime"]["packages"],
                    "model_roles": list(manifest["models"]),
                },
            )
            with mock.patch.object(onboarding, "platform_probe", return_value=compatible):
                status = onboarding.provider_status("qwen-mlx", config)
                plan = onboarding.bootstrap_plan(
                    root,
                    config_dir=config,
                    mode="strict-local",
                    provider="qwen-mlx",
                    agent_host="codex",
                    database_state={"status": "current", "version": 1},
                )
        self.assertTrue(status["ready"])
        self.assertFalse(plan["installation"]["required"])
        self.assertEqual(plan["provider_profile"]["asr_recovery"]["subchunk_seconds"], 30)

    def test_experimental_provider_is_explicit_and_auto_default_does_not_change(self) -> None:
        self.assertEqual(onboarding.select_provider("auto"), "qwen-mlx")
        self.assertEqual(
            onboarding.select_provider("qwen-mlx-3dspeaker"),
            "qwen-mlx-3dspeaker",
        )
        manifest = onboarding.load_manifest("qwen-mlx-3dspeaker")
        self.assertEqual(manifest["backend"], "3dspeaker-offline")
        self.assertTrue(manifest["experimental"])
        self.assertNotEqual(
            onboarding.provider_profile_digest("qwen-mlx"),
            onboarding.provider_profile_digest("qwen-mlx-3dspeaker"),
        )
        self.assertEqual(
            onboarding.manifest_digest(onboarding.load_manifest("qwen-mlx")),
            "e247c08fecc9d9c299821b29b6e643f60b19d0305f08e446201fb3ffc2f1d92e",
        )

    def test_bootstrap_plan_discloses_separate_runtime_and_in_memory_voice_features(self) -> None:
        with tempfile.TemporaryDirectory(prefix="personal-context-provider-plan-") as temporary:
            base = Path(temporary)
            root = base / "vault"
            config = base / "config"
            with mock.patch.object(
                onboarding,
                "provider_status",
                return_value={
                    "provider": "qwen-mlx-3dspeaker",
                    "compatible": True,
                    "installed": False,
                    "ready": False,
                    "base_runtime_ready": True,
                    "diarization_runtime_ready": False,
                },
            ):
                plan = onboarding.bootstrap_plan(
                    root,
                    config_dir=config,
                    mode="strict-local",
                    provider="qwen-mlx-3dspeaker",
                    agent_host="codex",
                    database_state={"status": "missing"},
                )

        actions = [item["action"] for item in plan["steps"]]
        self.assertIn("install-private-diarization-runtime", actions)
        self.assertNotIn("install-private-runtime", actions)
        self.assertTrue(plan["provider_profile"]["experimental"])
        self.assertTrue(
            plan["provider_profile"]["privacy"]["embeddings_in_memory_only"]
        )
        diarizer_step = next(
            item
            for item in plan["steps"]
            if item["action"] == "install-private-diarization-runtime"
        )
        self.assertEqual(diarizer_step["system_requirements"], ["git"])

    def test_fresh_open_source_install_plan_includes_both_private_runtimes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="personal-context-fresh-install-") as temporary:
            base = Path(temporary)
            root = base / "vault"
            config = base / "config"
            with mock.patch.object(
                onboarding,
                "provider_status",
                return_value={
                    "provider": "qwen-mlx-3dspeaker",
                    "compatible": True,
                    "installed": False,
                    "ready": False,
                    "base_runtime_ready": False,
                    "diarization_runtime_ready": False,
                },
            ):
                plan = onboarding.bootstrap_plan(
                    root,
                    config_dir=config,
                    mode="strict-local",
                    provider="qwen-mlx-3dspeaker",
                    agent_host="portable-agent",
                    database_state={"status": "missing"},
                )

        actions = [item["action"] for item in plan["steps"]]
        self.assertLess(
            actions.index("install-private-runtime"),
            actions.index("install-private-diarization-runtime"),
        )
        base_manifest = onboarding.load_manifest("qwen-mlx")
        diarizer_manifest = onboarding.load_manifest("qwen-mlx-3dspeaker")
        base_step = next(
            item for item in plan["steps"] if item["action"] == "install-private-runtime"
        )
        self.assertEqual(
            {item["role"] for item in base_step["models"]}, {"asr", "aligner"}
        )
        self.assertNotIn(
            "diarizer", {item["role"] for item in base_step["models"]}
        )
        self.assertEqual(
            plan["installation"]["download_estimate_gb"],
            base_manifest["install_profiles"]["asr_alignment"][
                "download_estimate_gb"
            ]
            + diarizer_manifest["limits"]["download_estimate_gb"],
        )
        self.assertEqual(
            plan["installation"]["minimum_free_disk_gb"],
            base_manifest["install_profiles"]["asr_alignment"][
                "minimum_free_disk_gb"
            ]
            + diarizer_manifest["limits"]["minimum_free_disk_gb"],
        )
        self.assertEqual(plan["installation"]["resume_action"], "rerun-bootstrap-apply")
        self.assertFalse(root.exists())
        self.assertFalse(config.exists())

    def test_incomplete_private_runtime_reports_missing_components(self) -> None:
        compatible = {
            "system": "Darwin",
            "machine": "arm64",
            "python": "3.9.0",
            "qwen_mlx_compatible": True,
            "qwen_mlx_reason": None,
        }
        with tempfile.TemporaryDirectory(prefix="personal-context-runtime-status-") as temporary:
            with mock.patch.object(onboarding, "platform_probe", return_value=compatible):
                status = onboarding._offline_diarization_status(Path(temporary))

        self.assertFalse(status["ready"])
        self.assertEqual(
            set(status["missing_components"]),
            {"python", "source", "embedding_model", "vad_model", "runtime_marker"},
        )

    def test_experimental_provider_reports_missing_git_before_source_install(self) -> None:
        no_git = {
            "system": "Darwin",
            "machine": "arm64",
            "python": "3.9.0",
            "qwen_mlx_compatible": True,
            "qwen_mlx_reason": None,
            "git_available": False,
            "git_path": None,
        }
        with tempfile.TemporaryDirectory(prefix="personal-context-no-git-") as temporary:
            with mock.patch.object(onboarding, "platform_probe", return_value=no_git):
                status = onboarding._offline_diarization_status(Path(temporary))

        self.assertFalse(status["compatible"])
        self.assertIn("git", status["reason"].lower())

    def test_bootstrap_apply_never_reports_ready_after_incomplete_install(self) -> None:
        compatible = {
            "system": "Darwin",
            "machine": "arm64",
            "python": "3.9.0",
            "qwen_mlx_compatible": True,
            "qwen_mlx_reason": None,
        }
        incomplete = {
            "provider": "qwen-mlx-3dspeaker",
            "compatible": True,
            "installed": False,
            "ready": False,
            "base_runtime_ready": True,
            "diarization_runtime_ready": False,
            "missing_components": ["diarization:runtime_marker"],
        }
        with tempfile.TemporaryDirectory(prefix="personal-context-apply-verify-") as temporary:
            base = Path(temporary)
            root = base / "vault"
            config = base / "config"
            with mock.patch.object(onboarding, "platform_probe", return_value=compatible):
                plan_digest = onboarding.consent_scope_digest(
                    root,
                    provider="qwen-mlx-3dspeaker",
                    mode="strict-local",
                    agent_host="portable-agent",
                )
                onboarding.record_consent(
                    root,
                    config_dir=config,
                    mode="strict-local",
                    provider="qwen-mlx-3dspeaker",
                    agent_host="portable-agent",
                    accepted_digest=plan_digest,
                )
            install_base = mock.Mock()
            install_diarizer = mock.Mock()
            with mock.patch.object(
                onboarding, "provider_status", side_effect=[incomplete, incomplete]
            ):
                with self.assertRaises(onboarding.BootstrapError):
                    onboarding.bootstrap_apply(
                        root,
                        config_dir=config,
                        provider="qwen-mlx-3dspeaker",
                        agent_host="portable-agent",
                        database_state={"status": "current", "version": 1},
                        init_vault=mock.Mock(),
                        install_runtime=install_base,
                        install_diarization_runtime=install_diarizer,
                    )

        install_base.assert_not_called()
        install_diarizer.assert_called_once_with(config)

    def test_fresh_apply_installs_in_order_and_ready_rerun_installs_nothing(self) -> None:
        compatible = {
            "system": "Darwin",
            "machine": "arm64",
            "python": "3.9.0",
            "qwen_mlx_compatible": True,
            "qwen_mlx_reason": None,
        }
        incomplete = {
            "provider": "qwen-mlx-3dspeaker",
            "compatible": True,
            "installed": False,
            "ready": False,
            "base_runtime_ready": False,
            "diarization_runtime_ready": False,
            "missing_components": ["base:python", "diarization:python"],
        }
        ready = {
            "provider": "qwen-mlx-3dspeaker",
            "compatible": True,
            "installed": True,
            "ready": True,
            "base_runtime_ready": True,
            "diarization_runtime_ready": True,
            "missing_components": [],
        }
        with tempfile.TemporaryDirectory(prefix="personal-context-apply-order-") as temporary:
            base = Path(temporary)
            root = base / "vault"
            config = base / "config"
            with mock.patch.object(onboarding, "platform_probe", return_value=compatible):
                onboarding.record_consent(
                    root,
                    config_dir=config,
                    mode="strict-local",
                    provider="qwen-mlx-3dspeaker",
                    agent_host="portable-agent",
                    accepted_digest=onboarding.consent_scope_digest(
                        root,
                        provider="qwen-mlx-3dspeaker",
                        mode="strict-local",
                        agent_host="portable-agent",
                    ),
                )
            order: list[str] = []
            with mock.patch.object(
                onboarding, "provider_status", side_effect=[incomplete, ready]
            ):
                result = onboarding.bootstrap_apply(
                    root,
                    config_dir=config,
                    provider="qwen-mlx-3dspeaker",
                    agent_host="portable-agent",
                    database_state={"status": "current", "version": 1},
                    init_vault=mock.Mock(),
                    install_asr_alignment_runtime=lambda _: order.append("base"),
                    install_diarization_runtime=lambda _: order.append("diarization"),
                )
            self.assertEqual(result["status"], "ready")
            self.assertEqual(order, ["base", "diarization"])

            install_base = mock.Mock()
            install_diarizer = mock.Mock()
            with mock.patch.object(onboarding, "provider_status", return_value=ready):
                repeated = onboarding.bootstrap_apply(
                    root,
                    config_dir=config,
                    provider="qwen-mlx-3dspeaker",
                    agent_host="portable-agent",
                    database_state={"status": "current", "version": 1},
                    init_vault=mock.Mock(),
                    install_runtime=install_base,
                    install_asr_alignment_runtime=install_base,
                    install_diarization_runtime=install_diarizer,
                )
            self.assertEqual(repeated["status"], "ready")
            install_base.assert_not_called()
            install_diarizer.assert_not_called()

    def test_private_installer_uses_pinned_source_and_model_download_contract(self) -> None:
        compatible = {
            "system": "Darwin",
            "machine": "arm64",
            "python": "3.9.0",
            "qwen_mlx_compatible": True,
            "qwen_mlx_reason": None,
        }
        manifest = onboarding.load_manifest("qwen-mlx-3dspeaker")
        with tempfile.TemporaryDirectory(prefix="personal-context-installer-") as temporary:
            config = Path(temporary) / "config"
            runtime = onboarding.diarization_runtime_dir(config)
            uv_path = onboarding._venv_uv(runtime / "bootstrap")
            python_path = onboarding._venv_python(runtime / "venv")
            uv_path.parent.mkdir(parents=True)
            uv_path.write_text("synthetic uv", encoding="utf-8")
            python_path.parent.mkdir(parents=True)
            python_path.write_text("synthetic python", encoding="utf-8")
            commands: list[list[str]] = []

            def fake_run(
                command: list[str], *, env: dict[str, str] | None = None
            ) -> object:
                del env
                commands.append(command)
                if command[:3] == ["git", "clone", "--no-checkout"]:
                    checkout = Path(command[-1])
                    (checkout / ".git").mkdir(parents=True)
                    (checkout / "speakerlab").mkdir()
                if len(command) > 2 and command[1].endswith(
                    "diarization_3dspeaker.py"
                ) and command[2] == "download":
                    models = Path(command[command.index("--models-dir") + 1])
                    embedding = models / "embedding"
                    vad = models / "vad"
                    embedding.mkdir(parents=True)
                    vad.mkdir()
                    (models / "model-paths.json").write_text(
                        json.dumps(
                            {"embedding": str(embedding), "vad": str(vad)}
                        ),
                        encoding="utf-8",
                    )
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")

            with mock.patch.object(
                onboarding, "platform_probe", return_value=compatible
            ), mock.patch.object(
                onboarding.shutil, "which", return_value="/usr/bin/git"
            ), mock.patch.object(onboarding, "_run_checked", side_effect=fake_run):
                status = onboarding.install_3dspeaker_runtime(config)

        self.assertTrue(status["ready"])
        clone = next(command for command in commands if command[:2] == ["git", "clone"])
        self.assertEqual(clone[-2], manifest["source"]["repo"])
        fetch = next(command for command in commands if "fetch" in command)
        self.assertEqual(fetch[-1], manifest["source"]["revision"])
        package_install = next(
            command
            for command in commands
            if command[:3] == [str(uv_path), "pip", "install"]
        )
        for package in manifest["runtime"]["packages"]:
            self.assertIn(package, package_install)
        download = next(
            command
            for command in commands
            if len(command) > 2 and command[2] == "download"
        )
        self.assertIn("--models-dir", download)

    def test_qwen_downloader_can_install_only_asr_and_alignment_models(self) -> None:
        manifest = onboarding.load_manifest("qwen-mlx")
        calls: list[dict[str, str]] = []

        def snapshot_download(**kwargs: str) -> None:
            calls.append(kwargs)

        fake_hub = types.SimpleNamespace(snapshot_download=snapshot_download)
        with tempfile.TemporaryDirectory(prefix="personal-context-qwen-models-") as temporary:
            with mock.patch.dict(sys.modules, {"huggingface_hub": fake_hub}):
                result = qwen_mlx.download_models(
                    manifest,
                    Path(temporary),
                    model_roles=["asr", "aligner"],
                )

        self.assertEqual([item["role"] for item in result["models"]], ["asr", "aligner"])
        self.assertEqual(
            {item["repo_id"] for item in calls},
            {
                manifest["models"]["asr"]["repo_id"],
                manifest["models"]["aligner"]["repo_id"],
            },
        )


if __name__ == "__main__":
    unittest.main()
