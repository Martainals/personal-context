#!/usr/bin/env python3
"""Pinned MLX implementation that emits the canonical transcript contract."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import math
import os
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

try:
    from .artifacts import (
        ARTIFACT_CONTRACT_VERSION,
        ArtifactStore,
        component_cache_key,
        validate_artifact_payload,
    )
    from .transcript_assembly import (
        PUNCTUATION_RESTORATION_VERSION,
        assemble_transcript_segments,
        joins_without_space,
        merge_words,
        realign_word_boundaries,
        restore_asr_punctuation,
        smooth_word_speakers,
        speaker_details,
        speaker_for,
    )
    from .semantic_speaker_review import (
        apply_decisions as apply_semantic_speaker_decisions,
        build_review_input as build_semantic_speaker_review_input,
        load_decisions as load_semantic_speaker_decisions,
        validate_decisions as validate_semantic_speaker_decisions,
        write_private_json as write_semantic_speaker_review_input,
    )
except ImportError:  # Direct execution from the providers directory.
    from artifacts import (
        ARTIFACT_CONTRACT_VERSION,
        ArtifactStore,
        component_cache_key,
        validate_artifact_payload,
    )
    from transcript_assembly import (
        PUNCTUATION_RESTORATION_VERSION,
        assemble_transcript_segments,
        joins_without_space,
        merge_words,
        realign_word_boundaries,
        restore_asr_punctuation,
        smooth_word_speakers,
        speaker_details,
        speaker_for,
    )
    from semantic_speaker_review import (
        apply_decisions as apply_semantic_speaker_decisions,
        build_review_input as build_semantic_speaker_review_input,
        load_decisions as load_semantic_speaker_decisions,
        validate_decisions as validate_semantic_speaker_decisions,
        write_private_json as write_semantic_speaker_review_input,
    )


NORMALIZATION_VERSION = "mono-16k-v1"
ASR_STAGE_VERSION = 1
ALIGNMENT_STAGE_VERSION = 1
DIARIZATION_STAGE_VERSION = 1
SPEAKER_TURNS_STAGE_VERSION = 1
ASSEMBLY_STAGE_VERSION = 1


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def digest_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def load_manifest(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("provider") != "qwen-mlx":
        raise RuntimeError("Invalid qwen-mlx manifest")
    return value


def model_path(models_dir: Path, repo_id: str) -> Path:
    return models_dir / repo_id.replace("/", "--")


def download_models(
    manifest: dict[str, Any],
    models_dir: Path,
    *,
    model_roles: Optional[list[str]] = None,
) -> dict[str, Any]:
    from huggingface_hub import snapshot_download

    models_dir.mkdir(parents=True, exist_ok=True)
    selected_roles = model_roles or list(manifest["models"])
    unknown = [role for role in selected_roles if role not in manifest["models"]]
    if unknown:
        raise RuntimeError("Unknown qwen-mlx model roles: " + ", ".join(unknown))
    downloaded = []
    for role in selected_roles:
        model = manifest["models"][role]
        destination = model_path(models_dir, model["repo_id"])
        snapshot_download(
            repo_id=model["repo_id"],
            revision=model["revision"],
            local_dir=str(destination),
        )
        downloaded.append({"role": role, "repo_id": model["repo_id"], "revision": model["revision"], "path": str(destination)})
    return {"status": "downloaded", "models": downloaded}


def _to_mono_16k(audio_path: Path) -> tuple[Any, int]:
    import numpy as np
    from mlx_audio.audio_io import read as audio_read
    from scipy.signal import resample_poly

    samples, sample_rate = audio_read(str(audio_path))
    samples = np.asarray(samples, dtype=np.float32)
    if samples.ndim == 2:
        axis = 1 if samples.shape[1] <= 8 else 0
        samples = samples.mean(axis=axis)
    samples = samples.reshape(-1)
    if int(sample_rate) != 16000:
        divisor = math.gcd(int(sample_rate), 16000)
        samples = resample_poly(samples, 16000 // divisor, int(sample_rate) // divisor).astype(np.float32)
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    if peak > 1.0:
        samples = samples / peak
    return samples, 16000


def _write_wav(path: Path, samples: Any, sample_rate: int = 16000) -> None:
    import numpy as np

    encoded = (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(encoded)


def _alignment_items(value: Any) -> Iterable[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return value
    for attribute in ("items", "segments", "words"):
        candidate = getattr(value, attribute, None)
        if candidate is not None and not callable(candidate):
            return candidate
    return []


def _probability_rows(value: Any) -> list[list[float]]:
    if value is None:
        return []
    raw = value.tolist() if hasattr(value, "tolist") else value
    if not isinstance(raw, (list, tuple)):
        return []
    rows: list[list[float]] = []
    for row in raw:
        if isinstance(row, (list, tuple)):
            rows.append([float(item) for item in row])
    return rows


def _model_frame_seconds(model: Any, fallback: float) -> float:
    config = getattr(model, "config", None)
    processor = getattr(config, "processor_config", None)
    encoder = getattr(config, "fc_encoder_config", None)
    try:
        hop_length = int(getattr(processor, "hop_length"))
        sample_rate = int(getattr(processor, "sampling_rate"))
        subsampling = int(getattr(encoder, "subsampling_factor"))
    except (TypeError, ValueError):
        return fallback
    if hop_length <= 0 or sample_rate <= 0 or subsampling <= 0:
        return fallback
    return (hop_length * subsampling) / sample_rate


def _apply_streaming_profile(model: Any, streaming: dict[str, Any]) -> None:
    config = getattr(model, "config", None)
    modules = getattr(config, "modules_config", None)
    if modules is None:
        raise RuntimeError("Pinned diarizer does not expose streaming configuration")
    values = {
        "chunk_len": int(streaming["chunk_frames"]),
        "chunk_right_context": int(streaming["right_context_frames"]),
        "fifo_len": int(streaming["fifo_frames"]),
        "spkcache_update_period": int(streaming["speaker_cache_update_frames"]),
        "spkcache_len": int(streaming["speaker_cache_frames"]),
    }
    for attribute, value in values.items():
        if not hasattr(modules, attribute):
            raise RuntimeError(f"Pinned diarizer is missing streaming setting: {attribute}")
        setattr(modules, attribute, value)


def _runs(labels: list[Optional[int]]) -> list[tuple[int, int, Optional[int]]]:
    if not labels:
        return []
    found: list[tuple[int, int, Optional[int]]] = []
    start = 0
    for index in range(1, len(labels) + 1):
        if index == len(labels) or labels[index] != labels[start]:
            found.append((start, index, labels[start]))
            start = index
    return found


def _select_speaker_slots(
    probabilities: list[list[float]], expected_speakers: Optional[int], threshold: float
) -> list[int]:
    width = max((len(row) for row in probabilities), default=0)
    if width == 0:
        return []
    if expected_speakers is None:
        return list(range(width))
    if expected_speakers < 1 or expected_speakers > width:
        raise RuntimeError(f"speaker_count must be between 1 and {width}")

    scores = []
    for speaker in range(width):
        values = [row[speaker] if speaker < len(row) else 0.0 for row in probabilities]
        excess = sum(max(0.0, value - threshold) for value in values)
        active = sum(1 for value in values if value >= threshold)
        total = sum(values)
        first_active = next((index for index, value in enumerate(values) if value >= threshold), len(values))
        scores.append((excess, active, total, -speaker, speaker, first_active))
    strongest = sorted(scores, reverse=True)[:expected_speakers]
    return [item[4] for item in sorted(strongest, key=lambda item: (item[5], item[4]))]


def _collapse_two_speaker_slots(
    probabilities: list[list[float]],
    *,
    frame_seconds: float,
    threshold: float,
    window_seconds: float,
) -> tuple[list[list[float]], list[int]]:
    """Map a stable anchor plus a window-local alternate to two identities.

    Sortformer exposes four fixed output slots even when a recording is known
    to contain two people.  On long recordings the second person's slot can
    change after cache compression.  Preserve the globally strongest anchor,
    but choose the strongest non-anchor slot independently in each model-sized
    window and map every such alternate to logical speaker 1.
    """
    width = max((len(row) for row in probabilities), default=0)
    if width < 2:
        raise RuntimeError("Two-speaker diarization requires at least two model slots")
    anchor = _select_speaker_slots(probabilities, 1, threshold)[0]
    used_slots = [anchor]
    logical_rows: list[list[float]] = []
    window_frames = max(1, round(window_seconds / frame_seconds))
    for window_start in range(0, len(probabilities), window_frames):
        window = probabilities[window_start : window_start + window_frames]
        scores: list[tuple[float, int, float, int, int]] = []
        for speaker in range(width):
            if speaker == anchor:
                continue
            values = [row[speaker] if speaker < len(row) else 0.0 for row in window]
            excess = sum(max(0.0, value - threshold) for value in values)
            active = sum(1 for value in values if value >= threshold)
            total = sum(values)
            first_active = next(
                (index for index, value in enumerate(values) if value >= threshold),
                len(values),
            )
            scores.append((excess, active, total, -speaker, first_active))
        alternate = max(scores)[3] * -1
        if alternate not in used_slots:
            used_slots.append(alternate)
        for row in window:
            logical_rows.append(
                [
                    row[anchor] if anchor < len(row) else 0.0,
                    row[alternate] if alternate < len(row) else 0.0,
                ]
            )
    return logical_rows, used_slots


def _probabilities_to_diarization(
    probabilities: list[list[float]],
    *,
    frame_seconds: float,
    expected_speakers: Optional[int],
    postprocessing: dict[str, Any],
    speaker_selection_window_seconds: float = 60.0,
) -> tuple[list[dict[str, Any]], list[int]]:
    if not probabilities:
        return [], []
    threshold = float(postprocessing["activity_threshold"])
    reassignment_threshold = float(postprocessing["reassignment_threshold"])
    if expected_speakers == 2 and max(len(row) for row in probabilities) > 2:
        working_probabilities, selected_model_slots = _collapse_two_speaker_slots(
            probabilities,
            frame_seconds=frame_seconds,
            threshold=threshold,
            window_seconds=speaker_selection_window_seconds,
        )
        selected = [0, 1]
    else:
        working_probabilities = probabilities
        selected_model_slots = _select_speaker_slots(
            working_probabilities, expected_speakers, threshold
        )
        selected = selected_model_slots
    if not selected:
        return [], []

    labels: list[Optional[int]] = []
    for row in working_probabilities:
        all_peak = max(row, default=0.0)
        speaker = max(selected, key=lambda item: row[item] if item < len(row) else 0.0)
        selected_peak = row[speaker] if speaker < len(row) else 0.0
        if selected_peak >= threshold or (all_peak >= threshold and selected_peak >= reassignment_threshold):
            labels.append(speaker)
        else:
            labels.append(None)

    max_gap = float(postprocessing["max_silence_gap_seconds"])
    for start, end, label in _runs(labels):
        if (
            label is None
            and start > 0
            and end < len(labels)
            and (end - start) * frame_seconds <= max_gap
            and labels[start - 1] == labels[end]
        ):
            labels[start:end] = [labels[start - 1]] * (end - start)

    min_speech = float(postprocessing["min_speech_seconds"])
    for start, end, label in _runs(labels):
        if label is None or (end - start) * frame_seconds >= min_speech:
            continue
        replacement = None
        if start > 0 and end < len(labels) and labels[start - 1] == labels[end]:
            replacement = labels[start - 1]
        labels[start:end] = [replacement] * (end - start)

    max_weak_switch = float(postprocessing["max_weak_switch_seconds"])
    max_weak_margin = float(postprocessing["max_weak_switch_margin"])
    for start, end, label in _runs(labels):
        if (
            label is None
            or start == 0
            or end == len(labels)
            or labels[start - 1] != labels[end]
            or labels[start - 1] == label
            or (end - start) * frame_seconds > max_weak_switch
        ):
            continue
        margins = []
        for row in working_probabilities[start:end]:
            ranked = sorted((row[item] if item < len(row) else 0.0 for item in selected), reverse=True)
            margins.append(ranked[0] - (ranked[1] if len(ranked) > 1 else 0.0))
        if margins and sum(margins) / len(margins) <= max_weak_margin:
            labels[start:end] = [labels[start - 1]] * (end - start)

    output_index = {speaker: index for index, speaker in enumerate(selected)}
    segments: list[dict[str, Any]] = []
    for start, end, speaker in _runs(labels):
        if speaker is None:
            continue
        confidences = []
        margins = []
        for row in working_probabilities[start:end]:
            confidence = row[speaker] if speaker < len(row) else 0.0
            alternatives = [row[item] if item < len(row) else 0.0 for item in selected if item != speaker]
            confidences.append(confidence)
            margins.append(confidence - max(alternatives, default=0.0))
        segments.append(
            {
                "start": start * frame_seconds,
                "end": end * frame_seconds,
                "speaker": output_index[speaker],
                "confidence": sum(confidences) / len(confidences),
                "margin": sum(margins) / len(margins),
            }
        )
    return segments, selected_model_slots


def _collect_raw_diarization(
    model: Any,
    audio_path: Path,
    profile: dict[str, Any],
) -> dict[str, Any]:
    streaming = profile["streaming"]
    inference = profile.get(
        "inference",
        {
            "activity_threshold": 0.5,
            "min_duration": 0.0,
            "merge_gap": 0.0,
        },
    )
    _apply_streaming_profile(model, streaming)
    fallback_frame_seconds = float(profile["frame_seconds"])
    frame_seconds = _model_frame_seconds(model, fallback_frame_seconds)
    chunk_seconds = int(streaming["chunk_frames"]) * frame_seconds
    probabilities: list[list[float]] = []
    for result in model.generate_stream(
        str(audio_path),
        chunk_duration=chunk_seconds,
        threshold=float(inference["activity_threshold"]),
        min_duration=float(inference["min_duration"]),
        merge_gap=float(inference["merge_gap"]),
        spkcache_max=int(streaming["speaker_cache_frames"]),
        fifo_max=int(streaming["fifo_frames"]),
        verbose=False,
    ):
        probabilities.extend(_probability_rows(getattr(result, "speaker_probs", None)))
    if not probabilities:
        raise RuntimeError("Diarizer returned no speaker probabilities")
    return {
        "probabilities": probabilities,
        "frame_seconds": frame_seconds,
        "chunk_seconds": chunk_seconds,
    }


def _derive_speaker_turns(
    raw: dict[str, Any],
    profile: dict[str, Any],
    expected_speakers: Optional[int],
) -> dict[str, Any]:
    probabilities = raw["probabilities"]
    frame_seconds = float(raw["frame_seconds"])
    chunk_seconds = float(raw["chunk_seconds"])
    postprocessing = profile["postprocessing"]
    segments, selected = _probabilities_to_diarization(
        probabilities,
        frame_seconds=frame_seconds,
        expected_speakers=expected_speakers,
        postprocessing=postprocessing,
        speaker_selection_window_seconds=chunk_seconds,
    )
    return {
        "segments": segments,
        "details": {
            "mode": profile["mode"],
            "frame_seconds": frame_seconds,
            "chunk_seconds": chunk_seconds,
            "expected_speakers": expected_speakers,
            "selected_model_slots": selected,
            "output_speakers": expected_speakers if expected_speakers is not None else len(selected),
            "postprocessing": postprocessing,
            "streaming": profile["streaming"],
        },
    }


def _collect_diarization(
    model: Any,
    audio_path: Path,
    profile: dict[str, Any],
    expected_speakers: Optional[int],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Compatibility wrapper for the pre-cache provider interface."""
    derived = _derive_speaker_turns(
        _collect_raw_diarization(model, audio_path, profile), profile, expected_speakers
    )
    return derived["segments"], derived["details"]


def _run_offline_diarization(
    command: list[str], audio_path: Path, speaker_count: Optional[int]
) -> dict[str, Any]:
    if len(command) < 3 or command[2] != "diarize":
        raise RuntimeError("Offline diarization command is missing its protocol action")
    preflight_invocation = [*command]
    preflight_invocation[2] = "preflight"
    if speaker_count is not None:
        preflight_invocation.extend(["--speaker-count", str(speaker_count)])
    preflight = subprocess.run(
        preflight_invocation, capture_output=True, text=True, check=False
    )
    if preflight.returncode != 0:
        details = (preflight.stderr or preflight.stdout or "unknown error")[-6000:]
        raise RuntimeError(
            f"Offline diarization preflight failed: {details.strip()}"
        )
    try:
        preflight_payload = json.loads(preflight.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Offline diarization preflight returned invalid JSON: {exc}"
        ) from exc
    if not isinstance(preflight_payload, dict) or not isinstance(
        preflight_payload.get("details"), dict
    ):
        raise RuntimeError("Offline diarization preflight is missing result metadata")
    try:
        validate_artifact_payload(preflight_payload)
    except ValueError as exc:
        raise RuntimeError(
            f"Offline diarization preflight rejected unsafe artifact metadata: {exc}"
        ) from exc

    invocation = [*command, "--audio", str(audio_path)]
    if speaker_count is not None:
        invocation.extend(["--speaker-count", str(speaker_count)])
    result = subprocess.run(invocation, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "unknown error")[-6000:]
        raise RuntimeError(f"Offline diarization failed: {details.strip()}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Offline diarization returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Offline diarization output must be a JSON object")
    segments = payload.get("segments")
    details = payload.get("details")
    if not isinstance(segments, list) or not segments or not isinstance(details, dict):
        raise RuntimeError("Offline diarization output is missing segments or details")
    for item in segments:
        if not isinstance(item, dict) or set(item) != {
            "start",
            "end",
            "speaker",
            "confidence",
            "margin",
        }:
            raise RuntimeError("Offline diarization segment has an invalid shape")
        start = float(item["start"])
        end = float(item["end"])
        confidence = float(item["confidence"])
        margin = float(item["margin"])
        if (
            start < 0
            or end <= start
            or not 0.0 <= confidence <= 1.0
            or not math.isfinite(margin)
        ):
            raise RuntimeError("Offline diarization segment has invalid scalar values")
    return {"segments": segments, "details": details}


def _build_offline_command(
    python_path: str,
    script_path: str,
    manifest_path: str,
    source_dir: str,
    models_dir: str,
) -> list[str]:
    # A virtualenv's Python is normally a symlink. Resolving it would bypass the
    # virtualenv and run the base interpreter without the pinned diarizer packages.
    python_executable = Path(python_path).expanduser().absolute()
    return [
        str(python_executable),
        str(Path(script_path).expanduser().resolve()),
        "diarize",
        "--manifest",
        str(Path(manifest_path).expanduser().resolve()),
        "--source-dir",
        str(Path(source_dir).expanduser().resolve()),
        "--models-dir",
        str(Path(models_dir).expanduser().resolve()),
    ]


# Preserve the provider's historical private names while keeping all word/turn
# assembly behavior in the dependency-free transcript_assembly module.
_speaker_details = speaker_details
_speaker_for = speaker_for
_smooth_word_speakers = smooth_word_speakers
_realign_word_boundaries = realign_word_boundaries
_merge_words = merge_words
_joins_without_space = joins_without_space


def _load_stt_model(models_dir: Path, model: dict[str, Any]) -> Any:
    from mlx_audio.stt import load as load_stt

    return load_stt(str(model_path(models_dir, model["repo_id"])))


def _load_vad_model(models_dir: Path, model: dict[str, Any]) -> Any:
    from mlx_audio.vad import load as load_vad

    return load_vad(str(model_path(models_dir, model["repo_id"])))


def _artifact_config(manifest: dict[str, Any]) -> dict[str, Any]:
    configured = manifest.get("artifacts") or {}
    contract = int(configured.get("contract_version", ARTIFACT_CONTRACT_VERSION))
    if contract != ARTIFACT_CONTRACT_VERSION:
        raise RuntimeError(
            f"Unsupported artifact contract {contract}; expected {ARTIFACT_CONTRACT_VERSION}"
        )
    return {
        "contract_version": contract,
        "normalization_version": configured.get("normalization_version", NORMALIZATION_VERSION),
        "stage_versions": {
            "asr": int((configured.get("stage_versions") or {}).get("asr", ASR_STAGE_VERSION)),
            "alignment": int(
                (configured.get("stage_versions") or {}).get("alignment", ALIGNMENT_STAGE_VERSION)
            ),
            "diarization": int(
                (configured.get("stage_versions") or {}).get(
                    "diarization", DIARIZATION_STAGE_VERSION
                )
            ),
            "speaker-turns": int(
                (configured.get("stage_versions") or {}).get(
                    "speaker-turns", SPEAKER_TURNS_STAGE_VERSION
                )
            ),
            "assembly": int(
                (configured.get("stage_versions") or {}).get("assembly", ASSEMBLY_STAGE_VERSION)
            ),
        },
    }


def _model_identity(model: dict[str, Any]) -> dict[str, str]:
    return {"repo_id": str(model["repo_id"]), "revision": str(model["revision"])}


def _runtime_packages_for(manifest: dict[str, Any], stage: str) -> list[str]:
    configured = manifest.get("stage_runtime_packages") or {}
    packages = configured.get(stage, manifest["runtime"]["packages"])
    return [str(item) for item in packages]


def _asr_recovery_profile(manifest: dict[str, Any]) -> dict[str, Any]:
    configured = manifest.get("asr_recovery") or {}
    profile = {
        "version": int(configured.get("version", 1)),
        "subchunk_seconds": int(configured.get("subchunk_seconds", 30)),
        "minimum_text_characters": int(configured.get("minimum_text_characters", 200)),
        "minimum_repeated_run_characters": int(
            configured.get("minimum_repeated_run_characters", 80)
        ),
        "minimum_repeated_run_ratio": float(
            configured.get("minimum_repeated_run_ratio", 0.25)
        ),
    }
    if profile["version"] < 1 or profile["subchunk_seconds"] < 1:
        raise RuntimeError("Invalid ASR recovery version or subchunk duration")
    if profile["minimum_text_characters"] < 1:
        raise RuntimeError("Invalid ASR recovery minimum text length")
    if profile["minimum_repeated_run_characters"] < 2:
        raise RuntimeError("Invalid ASR recovery repeated-run length")
    if not 0.0 < profile["minimum_repeated_run_ratio"] <= 1.0:
        raise RuntimeError("Invalid ASR recovery repeated-run ratio")
    return profile


def _detect_asr_pathology(
    text: str, profile: dict[str, Any]
) -> Optional[dict[str, Any]]:
    compact = "".join(character for character in text if not character.isspace())
    if len(compact) < int(profile["minimum_text_characters"]):
        return None
    longest = 0
    current = 0
    previous: Optional[str] = None
    for character in compact:
        if character == previous:
            current += 1
        else:
            previous = character
            current = 1
        longest = max(longest, current)
    ratio = longest / len(compact)
    if (
        longest < int(profile["minimum_repeated_run_characters"])
        or ratio < float(profile["minimum_repeated_run_ratio"])
    ):
        return None
    return {
        "kind": "repeated-character-run",
        "text_characters": len(compact),
        "repeated_run_characters": longest,
        "repeated_run_ratio": round(ratio, 6),
    }


def _cached_payload(
    store: Optional[ArtifactStore],
    *,
    stage: str,
    name: str,
    cache_key: str,
    refresh: bool,
    compute: Callable[[], Any],
    cache_events: list[dict[str, str]],
) -> tuple[Any, str]:
    lookup_status: Optional[str] = None
    if store is not None and not refresh:
        found = store.read(stage, name, cache_key)
        if found.status == "hit":
            cache_events.append({"stage": stage, "name": name, "status": "hit"})
            return found.payload, str(found.payload_sha256)
        lookup_status = found.status
    payload = compute()
    if store is None:
        cache_events.append({"stage": stage, "name": name, "status": "disabled"})
        return payload, component_cache_key(payload)
    written = store.write(stage, name, cache_key, payload)
    cache_events.append(
        {
            "stage": stage,
            "name": name,
            "status": "refreshed" if refresh else str(lookup_status or "miss"),
        }
    )
    return payload, str(written.payload_sha256)


def _cached_asr_payload(
    store: Optional[ArtifactStore],
    *,
    name: str,
    primary_cache_key: str,
    recovery_cache_key: str,
    recovery_profile: dict[str, Any],
    refresh: bool,
    compute_primary: Callable[[], dict[str, Any]],
    compute_recovery: Callable[[dict[str, Any]], dict[str, Any]],
    cache_events: list[dict[str, str]],
) -> tuple[dict[str, Any], str]:
    lookup_status: Optional[str] = None
    cached_pathology: Optional[dict[str, Any]] = None
    if store is not None and not refresh:
        recovered = store.read("asr", name, recovery_cache_key)
        if recovered.status == "hit":
            cache_events.append({"stage": "asr", "name": name, "status": "hit"})
            return dict(recovered.payload), str(recovered.payload_sha256)
        primary = store.read("asr", name, primary_cache_key)
        if primary.status == "hit":
            payload = dict(primary.payload)
            pathology = _detect_asr_pathology(str(payload.get("text", "")), recovery_profile)
            if pathology is None:
                cache_events.append({"stage": "asr", "name": name, "status": "hit"})
                return payload, str(primary.payload_sha256)
            lookup_status = "pathological"
            cached_pathology = pathology
        else:
            lookup_status = primary.status

    primary_payload = None if cached_pathology is not None else compute_primary()
    pathology = cached_pathology or _detect_asr_pathology(
        str((primary_payload or {}).get("text", "")), recovery_profile
    )
    recovered_payload = pathology is not None
    payload = compute_recovery(pathology) if pathology is not None else dict(primary_payload or {})
    selected_key = recovery_cache_key if recovered_payload else primary_cache_key
    if store is None:
        cache_events.append(
            {
                "stage": "asr",
                "name": name,
                "status": "recovered-disabled" if recovered_payload else "disabled",
            }
        )
        return payload, component_cache_key(payload)
    written = store.write("asr", name, selected_key, payload)
    if recovered_payload:
        status = "refreshed-recovered" if refresh else "recovered"
    else:
        status = "refreshed" if refresh else str(lookup_status or "miss")
    cache_events.append({"stage": "asr", "name": name, "status": status})
    return payload, str(written.payload_sha256)


def transcribe(
    manifest: dict[str, Any],
    models_dir: Path,
    audio_path: Path,
    output_path: Path,
    *,
    language: Optional[str],
    title: Optional[str],
    observed_at: Optional[str],
    speaker_count: Optional[int] = None,
    artifacts_dir: Optional[Path] = None,
    vault_scope: Optional[str] = None,
    no_cache: bool = False,
    refresh_stage: Optional[str] = None,
    provider_name: Optional[str] = None,
    speaker_review_output: Optional[Path] = None,
    speaker_review_decisions: Optional[Path] = None,
    diarization_backend: Optional[dict[str, Any]] = None,
    offline_diarization_runner: Optional[
        Callable[[Path, Optional[int]], dict[str, Any]]
    ] = None,
) -> dict[str, Any]:
    if refresh_stage not in {None, "asr", "alignment", "diarization", "all"}:
        raise RuntimeError(f"Unknown refresh stage: {refresh_stage}")
    if no_cache and refresh_stage is not None:
        raise RuntimeError("--no-cache cannot be combined with --refresh-stage")
    if not no_cache and (artifacts_dir is None or vault_scope is None):
        raise RuntimeError("Cached transcription requires artifacts_dir and vault_scope")
    selected_provider = provider_name or str(manifest["provider"])
    offline_backend = diarization_backend is not None
    if offline_backend:
        if diarization_backend.get("backend") != "3dspeaker-offline":
            raise RuntimeError("Unsupported offline diarization backend")
        if offline_diarization_runner is None:
            raise RuntimeError("Offline diarization requires an isolated runner")
    samples, sample_rate = _to_mono_16k(audio_path)
    if len(samples) == 0:
        raise RuntimeError("Audio contains no samples")
    source_hash = digest_file(audio_path)
    models = manifest["models"]
    maximum_speakers = int(manifest["limits"]["maximum_speakers"])
    if speaker_count is not None and not 1 <= speaker_count <= maximum_speakers:
        raise RuntimeError(f"speaker_count must be between 1 and {maximum_speakers}")
    artifact_config = _artifact_config(manifest)
    word_assembly = dict(manifest["diarization"]["word_assembly"])
    if offline_backend:
        word_assembly.update(diarization_backend.get("word_assembly") or {})
    common_key = {
        "artifact_contract": artifact_config["contract_version"],
        "normalization_version": artifact_config["normalization_version"],
        "source_audio_sha256": source_hash,
    }
    chunk_seconds = int(manifest["limits"]["asr_chunk_seconds"])
    chunk_samples = chunk_seconds * sample_rate
    asr_recovery_profile = _asr_recovery_profile(manifest)
    recovery_subchunk_samples = int(asr_recovery_profile["subchunk_seconds"]) * sample_rate
    words: list[dict[str, Any]] = []
    fallbacks: list[dict[str, Any]] = []
    asr_recoveries: list[dict[str, Any]] = []
    cache_events: list[dict[str, str]] = []
    store = (
        None
        if no_cache
        else ArtifactStore(Path(artifacts_dir), str(vault_scope), source_hash)
    )
    lock = store.recording_lock() if store is not None else contextlib.nullcontext()
    asr_model: Optional[Any] = None
    alignment_model: Optional[Any] = None
    diarization_model: Optional[Any] = None

    def ensure_asr_model() -> Any:
        nonlocal asr_model
        if asr_model is None:
            asr_model = _load_stt_model(models_dir, models["asr"])
        return asr_model

    def ensure_alignment_model() -> Any:
        nonlocal alignment_model
        if alignment_model is None:
            alignment_model = _load_stt_model(models_dir, models["aligner"])
        return alignment_model

    def ensure_diarization_model() -> Any:
        nonlocal diarization_model
        if diarization_model is None:
            diarization_model = _load_vad_model(models_dir, models["diarizer"])
        return diarization_model

    with tempfile.TemporaryDirectory(prefix="personal-context-audio-") as temporary:
        temporary_dir = Path(temporary)
        normalized_path = temporary_dir / "normalized.wav"
        _write_wav(normalized_path, samples, sample_rate)
        with lock:
            if offline_backend:
                offline_key = component_cache_key(
                    {
                        **common_key,
                        "component": "offline-diarization",
                        "component_version": int(
                            (diarization_backend.get("diarization") or {}).get(
                                "stage_version", 1
                            )
                        ),
                        "backend": diarization_backend["backend"],
                        "profile_version": diarization_backend.get("profile_version"),
                        "source": diarization_backend.get("source"),
                        "models": diarization_backend.get("models"),
                        "runtime_packages": (diarization_backend.get("runtime") or {}).get(
                            "packages", []
                        ),
                        "settings": diarization_backend.get("diarization"),
                        "speaker_count": speaker_count,
                    }
                )
                speaker_turns, speaker_turns_sha = _cached_payload(
                    store,
                    stage="diarization",
                    name="offline-turns",
                    cache_key=offline_key,
                    refresh=refresh_stage in {"diarization", "all"},
                    compute=lambda: offline_diarization_runner(
                        normalized_path, speaker_count
                    ),
                    cache_events=cache_events,
                )
            else:
                raw_key = component_cache_key(
                    {
                        **common_key,
                        "component": "diarization",
                        "component_version": artifact_config["stage_versions"]["diarization"],
                        "model": _model_identity(models["diarizer"]),
                        "runtime_packages": _runtime_packages_for(manifest, "diarization"),
                        "streaming": manifest["diarization"]["streaming"],
                        "inference": manifest["diarization"].get("inference", {}),
                    }
                )
                raw_diarization, raw_sha = _cached_payload(
                    store,
                    stage="diarization",
                    name="raw-probabilities",
                    cache_key=raw_key,
                    refresh=refresh_stage in {"diarization", "all"},
                    compute=lambda: _collect_raw_diarization(
                        ensure_diarization_model(), normalized_path, manifest["diarization"]
                    ),
                    cache_events=cache_events,
                )
                speaker_turns_key = component_cache_key(
                    {
                        **common_key,
                        "component": "speaker-turns",
                        "component_version": artifact_config["stage_versions"]["speaker-turns"],
                        "raw_diarization_sha256": raw_sha,
                        "speaker_count": speaker_count,
                        "postprocessing": manifest["diarization"]["postprocessing"],
                    }
                )
                speaker_turns, speaker_turns_sha = _cached_payload(
                    store,
                    stage="speaker-turns",
                    name="turns",
                    cache_key=speaker_turns_key,
                    refresh=refresh_stage in {"diarization", "all"},
                    compute=lambda: _derive_speaker_turns(
                        raw_diarization, manifest["diarization"], speaker_count
                    ),
                    cache_events=cache_events,
                )
            diarization = speaker_turns["segments"]
            diarization_details = speaker_turns["details"]

            alignment_shas: list[str] = []
            punctuation_inputs: list[dict[str, Any]] = []
            for index, start_sample in enumerate(range(0, len(samples), chunk_samples)):
                chunk = samples[start_sample : start_sample + chunk_samples]
                chunk_name = f"chunk-{index:05d}"
                chunk_path = temporary_dir / f"{chunk_name}.wav"
                chunk_written = False

                def ensure_chunk_path() -> Path:
                    nonlocal chunk_written
                    if not chunk_written:
                        _write_wav(chunk_path, chunk, sample_rate)
                        chunk_written = True
                    return chunk_path

                chunk_identity = {
                    "chunk_index": index,
                    "chunk_seconds": chunk_seconds,
                    "sample_rate": sample_rate,
                    "start_sample": start_sample,
                    "sample_count": len(chunk),
                }
                asr_key = component_cache_key(
                    {
                        **common_key,
                        "component": "asr",
                        "component_version": artifact_config["stage_versions"]["asr"],
                        "model": _model_identity(models["asr"]),
                        "runtime_packages": _runtime_packages_for(manifest, "asr"),
                        "language": language,
                        "chunk": chunk_identity,
                    }
                )

                def compute_asr() -> dict[str, Any]:
                    model = ensure_asr_model()
                    result = (
                        model.generate(str(ensure_chunk_path()), language=language)
                        if language
                        else model.generate(str(ensure_chunk_path()))
                    )
                    return {"text": str(getattr(result, "text", result)).strip(), **chunk_identity}

                recovery_key = component_cache_key(
                    {
                        **common_key,
                        "component": "asr-recovery",
                        "primary_cache_key": asr_key,
                        "policy": asr_recovery_profile,
                    }
                )

                def compute_recovery(pathology: dict[str, Any]) -> dict[str, Any]:
                    recovery_slices = []
                    model = ensure_asr_model()
                    for recovery_index, relative_start in enumerate(
                        range(0, len(chunk), recovery_subchunk_samples)
                    ):
                        recovery_chunk = chunk[
                            relative_start : relative_start + recovery_subchunk_samples
                        ]
                        recovery_path = temporary_dir / (
                            f"{chunk_name}-recovery-{recovery_index:05d}.wav"
                        )
                        _write_wav(recovery_path, recovery_chunk, sample_rate)
                        result = (
                            model.generate(str(recovery_path), language=language)
                            if language
                            else model.generate(str(recovery_path))
                        )
                        recovery_text = str(getattr(result, "text", result)).strip()
                        if _detect_asr_pathology(recovery_text, asr_recovery_profile) is not None:
                            raise RuntimeError(
                                f"Pathological repetition remained in smaller ASR slice "
                                f"{chunk_name}/{recovery_index:05d}; refusing to publish"
                            )
                        recovery_slices.append(
                            {
                                "relative_start_sample": relative_start,
                                "sample_count": len(recovery_chunk),
                                "text": recovery_text,
                            }
                        )
                    return {
                        "text": "".join(item["text"] for item in recovery_slices),
                        **chunk_identity,
                        "recovery": {
                            "version": asr_recovery_profile["version"],
                            "reason": pathology,
                            "subchunk_seconds": asr_recovery_profile["subchunk_seconds"],
                            "subchunks": len(recovery_slices),
                        },
                        "recovery_slices": recovery_slices,
                    }

                asr_payload, asr_sha = _cached_asr_payload(
                    store,
                    name=chunk_name,
                    primary_cache_key=asr_key,
                    recovery_cache_key=recovery_key,
                    recovery_profile=asr_recovery_profile,
                    refresh=refresh_stage in {"asr", "all"},
                    compute_primary=compute_asr,
                    compute_recovery=compute_recovery,
                    cache_events=cache_events,
                )
                text = str(asr_payload["text"])
                recovery_details = asr_payload.get("recovery")
                if isinstance(recovery_details, dict):
                    asr_recoveries.append(
                        {
                            "chunk_index": index,
                            "subchunks": int(recovery_details.get("subchunks", 0)),
                            "reason": recovery_details.get("reason"),
                        }
                    )
                alignment_key = component_cache_key(
                    {
                        **common_key,
                        "component": "alignment",
                        "component_version": artifact_config["stage_versions"]["alignment"],
                        "model": _model_identity(models["aligner"]),
                        "runtime_packages": _runtime_packages_for(manifest, "alignment"),
                        "language": language,
                        "chunk": chunk_identity,
                        "asr_payload_sha256": asr_sha,
                    }
                )

                def compute_alignment() -> dict[str, Any]:
                    if not text:
                        return {
                            "words": [],
                            "fallback": None,
                            "fallbacks": [],
                            **chunk_identity,
                        }
                    model = ensure_alignment_model()
                    chunk_words = []
                    chunk_fallbacks = []

                    def align_part(
                        path: Path, part_text: str, offset: float, duration: float
                    ) -> None:
                        if not part_text:
                            return
                        aligned = (
                            model.generate(str(path), text=part_text, language=language)
                            if language
                            else model.generate(str(path), text=part_text)
                        )
                        part_words = []
                        for item in _alignment_items(aligned):
                            token = str(getattr(item, "text", "")).strip()
                            if not token:
                                continue
                            start = offset + float(getattr(item, "start_time", 0.0))
                            end = offset + float(
                                getattr(item, "end_time", getattr(item, "start_time", 0.0))
                            )
                            part_words.append(
                                {"start": start, "end": max(start, end), "text": token}
                            )
                        if part_words:
                            chunk_words.extend(part_words)
                        else:
                            chunk_fallbacks.append(
                                {"start": offset, "end": offset + duration, "text": part_text}
                            )

                    recovery_slices = asr_payload.get("recovery_slices")
                    if isinstance(recovery_slices, list):
                        for recovery_index, recovery_slice in enumerate(recovery_slices):
                            relative_start = int(recovery_slice["relative_start_sample"])
                            sample_count = int(recovery_slice["sample_count"])
                            part_text = str(recovery_slice["text"])
                            recovery_path = temporary_dir / (
                                f"{chunk_name}-recovery-{recovery_index:05d}.wav"
                            )
                            if not recovery_path.exists():
                                _write_wav(
                                    recovery_path,
                                    chunk[relative_start : relative_start + sample_count],
                                    sample_rate,
                                )
                            align_part(
                                recovery_path,
                                part_text,
                                (start_sample + relative_start) / sample_rate,
                                sample_count / sample_rate,
                            )
                    else:
                        align_part(
                            ensure_chunk_path(),
                            text,
                            start_sample / sample_rate,
                            len(chunk) / sample_rate,
                        )
                    fallback = chunk_fallbacks[0] if len(chunk_fallbacks) == 1 else None
                    return {
                        "words": chunk_words,
                        "fallback": fallback,
                        "fallbacks": chunk_fallbacks if len(chunk_fallbacks) != 1 else [],
                        **chunk_identity,
                    }

                alignment_payload, alignment_sha = _cached_payload(
                    store,
                    stage="alignment",
                    name=chunk_name,
                    cache_key=alignment_key,
                    refresh=refresh_stage in {"alignment", "all"},
                    compute=compute_alignment,
                    cache_events=cache_events,
                )
                alignment_shas.append(alignment_sha)
                punctuated_words, punctuation_details = restore_asr_punctuation(
                    text, alignment_payload["words"]
                )
                words.extend(punctuated_words)
                punctuation_inputs.append(
                    {
                        "version": PUNCTUATION_RESTORATION_VERSION,
                        "asr_payload_sha256": asr_sha,
                        "alignment_payload_sha256": alignment_sha,
                        "status": punctuation_details["status"],
                        "similarity": punctuation_details["similarity"],
                        "restored": punctuation_details["restored"],
                    }
                )
                if alignment_payload.get("fallback") is not None:
                    fallbacks.append(alignment_payload["fallback"])
                fallbacks.extend(alignment_payload.get("fallbacks") or [])

            assembly_key = component_cache_key(
                {
                    **common_key,
                    "component": "assembly",
                    "component_version": artifact_config["stage_versions"]["assembly"],
                    "alignment_payload_sha256": alignment_shas,
                    "punctuation_restoration": punctuation_inputs,
                    "speaker_turns_sha256": speaker_turns_sha,
                    "word_assembly": word_assembly,
                }
            )
            assembly_payload, _ = _cached_payload(
                store,
                stage="assembly",
                name="segments",
                cache_key=assembly_key,
                refresh=refresh_stage == "all",
                compute=lambda: {
                    "segments": assemble_transcript_segments(
                        words,
                        fallbacks,
                        diarization,
                        word_assembly,
                    )
                },
                cache_events=cache_events,
            )
            segments = assembly_payload["segments"]
    if not segments:
        raise RuntimeError("No speech was transcribed")
    semantic_review = None
    if speaker_review_output is not None or speaker_review_decisions is not None:
        sound_evidence = []
        for segment in segments:
            _, confidence, margin = speaker_details(
                int(segment["start_ms"]) / 1000.0,
                int(segment["end_ms"]) / 1000.0,
                diarization,
            )
            sound_evidence.append({"confidence": confidence, "margin": margin})
        review_input = build_semantic_speaker_review_input(
            audio_sha256=source_hash,
            segments=segments,
            speaker_count=speaker_count,
            sound_evidence=sound_evidence,
        )
        if speaker_review_output is not None:
            write_semantic_speaker_review_input(speaker_review_output, review_input)
        if speaker_review_decisions is not None:
            decisions = load_semantic_speaker_decisions(speaker_review_decisions)
            accepted = validate_semantic_speaker_decisions(review_input, decisions)
            segments = apply_semantic_speaker_decisions(review_input, decisions)
            semantic_review = {
                "contract": str(decisions["contract"]),
                "input_sha256": str(review_input["input_sha256"]),
                "reviewer": dict(decisions["reviewer"]),
                "accepted_operations": len(accepted),
                "text_timestamps_and_order_preserved": True,
            }
    document = {
        "event": {
            "title": title or audio_path.stem,
            "type": "recording",
            "observed_at": observed_at or utc_now(),
        },
        "segments": segments,
        "entities": [],
        "statements": [],
        "decisions": [],
        "actions": [],
        "claims": [],
        "relationships": [],
        "candidate_memories": [],
        "processing": {
            "contract": "transcript.v1",
            "provider": selected_provider,
            "profile_version": (
                diarization_backend.get("profile_version")
                if offline_backend
                else manifest["profile_version"]
            ),
            "source_audio_sha256": source_hash,
            "runtime": (
                {
                    "asr_alignment": manifest["runtime"],
                    "diarization": diarization_backend.get("runtime"),
                }
                if offline_backend
                else manifest["runtime"]
            ),
            "models": (
                {
                    "asr": _model_identity(models["asr"]),
                    "aligner": _model_identity(models["aligner"]),
                    "diarization": diarization_backend.get("models"),
                }
                if offline_backend
                else {
                    role: {"repo_id": model["repo_id"], "revision": model["revision"]}
                    for role, model in models.items()
                }
            ),
            "language_hint": language,
            "maximum_speakers": maximum_speakers,
            "speaker_count_hint": speaker_count,
            "diarization": diarization_details,
            "punctuation_restoration": {
                "version": PUNCTUATION_RESTORATION_VERSION,
                "chunks": len(punctuation_inputs),
                "restored": sum(int(item["restored"]) for item in punctuation_inputs),
                "statuses": {
                    status: sum(
                        1 for item in punctuation_inputs if item["status"] == status
                    )
                    for status in sorted({item["status"] for item in punctuation_inputs})
                },
            },
            "asr_recovery": {
                "version": asr_recovery_profile["version"],
                "chunks": len(asr_recoveries),
                "subchunks": sum(item["subchunks"] for item in asr_recoveries),
                "chunk_indices": [item["chunk_index"] for item in asr_recoveries],
                "reasons": [item["reason"] for item in asr_recoveries],
            },
            **(
                {"semantic_speaker_review": semantic_review}
                if semantic_review is not None
                else {}
            ),
        },
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_name(f".{output_path.name}.{os.getpid()}.tmp")
    try:
        temporary_path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary_path, output_path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    hits = sum(1 for item in cache_events if item["status"] == "hit")
    return {
        "status": "transcribed",
        "segments": len(segments),
        "output": str(output_path),
        "cache": {
            "enabled": store is not None,
            "hits": hits,
            "computed": len(cache_events) - hits,
            "events": cache_events,
        },
        "speaker_review": {
            "prepared": speaker_review_output is not None,
            "applied": semantic_review is not None,
            "input_sha256": (
                str(review_input["input_sha256"])
                if speaker_review_output is not None or speaker_review_decisions is not None
                else None
            ),
            "accepted_operations": (
                int(semantic_review["accepted_operations"])
                if semantic_review is not None
                else 0
            ),
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Pinned qwen-mlx provider for personal-context")
    subparsers = parser.add_subparsers(dest="command", required=True)
    command = subparsers.add_parser("download")
    command.add_argument("--manifest", required=True)
    command.add_argument("--models-dir", required=True)
    command.add_argument(
        "--model-role",
        action="append",
        choices=("asr", "aligner", "diarizer"),
        dest="model_roles",
    )
    command = subparsers.add_parser("transcribe")
    command.add_argument("--manifest", required=True)
    command.add_argument("--models-dir", required=True)
    command.add_argument("--audio", required=True)
    command.add_argument("--output", required=True)
    command.add_argument("--language")
    command.add_argument("--speaker-count", type=int, choices=range(1, 5))
    command.add_argument("--title")
    command.add_argument("--observed-at")
    command.add_argument("--artifacts-dir")
    command.add_argument("--vault-scope")
    command.add_argument("--provider-name")
    command.add_argument("--speaker-review-output")
    command.add_argument("--speaker-review-decisions")
    command.add_argument("--diarization-manifest")
    command.add_argument("--offline-python")
    command.add_argument("--offline-script")
    command.add_argument("--offline-source-dir")
    command.add_argument("--offline-models-dir")
    command.add_argument("--no-cache", action="store_true")
    command.add_argument(
        "--refresh-stage", choices=("asr", "alignment", "diarization", "all")
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        manifest = load_manifest(Path(args.manifest).resolve())
        if args.command == "download":
            result = download_models(
                manifest,
                Path(args.models_dir).resolve(),
                model_roles=args.model_roles,
            )
        else:
            offline_manifest = None
            offline_runner = None
            if args.diarization_manifest:
                required = {
                    "offline_python": args.offline_python,
                    "offline_script": args.offline_script,
                    "offline_source_dir": args.offline_source_dir,
                    "offline_models_dir": args.offline_models_dir,
                }
                missing = [name for name, value in required.items() if not value]
                if missing:
                    raise RuntimeError(
                        "Offline diarization is missing arguments: " + ", ".join(missing)
                    )
                offline_manifest_path = Path(args.diarization_manifest).resolve()
                offline_manifest = json.loads(
                    offline_manifest_path.read_text(encoding="utf-8")
                )
                if not isinstance(offline_manifest, dict):
                    raise RuntimeError("Offline diarization manifest must be a JSON object")
                offline_command = _build_offline_command(
                    args.offline_python,
                    args.offline_script,
                    str(offline_manifest_path),
                    args.offline_source_dir,
                    args.offline_models_dir,
                )
                offline_runner = lambda audio, count: _run_offline_diarization(
                    offline_command, audio, count
                )
            result = transcribe(
                manifest,
                Path(args.models_dir).resolve(),
                Path(args.audio).resolve(),
                Path(args.output).resolve(),
                language=args.language,
                title=args.title,
                observed_at=args.observed_at,
                speaker_count=args.speaker_count,
                artifacts_dir=Path(args.artifacts_dir).resolve() if args.artifacts_dir else None,
                vault_scope=args.vault_scope,
                no_cache=args.no_cache,
                refresh_stage=args.refresh_stage,
                provider_name=args.provider_name,
                speaker_review_output=(
                    Path(args.speaker_review_output).resolve()
                    if args.speaker_review_output
                    else None
                ),
                speaker_review_decisions=(
                    Path(args.speaker_review_decisions).resolve()
                    if args.speaker_review_decisions
                    else None
                ),
                diarization_backend=offline_manifest,
                offline_diarization_runner=offline_runner,
            )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"error": str(exc), "command": args.command}, ensure_ascii=False), file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
