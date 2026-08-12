#!/usr/bin/env python3
"""Pinned MLX implementation that emits the canonical transcript contract."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import tempfile
import wave
from pathlib import Path
from typing import Any, Iterable, Optional


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


def download_models(manifest: dict[str, Any], models_dir: Path) -> dict[str, Any]:
    from huggingface_hub import snapshot_download

    models_dir.mkdir(parents=True, exist_ok=True)
    downloaded = []
    for role, model in manifest["models"].items():
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


def _collect_diarization(model: Any, audio_path: Path, chunk_seconds: float) -> list[dict[str, Any]]:
    found: dict[tuple[int, int, int], dict[str, Any]] = {}
    for result in model.generate_stream(str(audio_path), chunk_duration=chunk_seconds, verbose=False):
        for segment in getattr(result, "segments", []) or []:
            start = float(getattr(segment, "start", 0.0))
            end = float(getattr(segment, "end", start))
            speaker = int(getattr(segment, "speaker", 0))
            key = (round(start * 1000), round(end * 1000), speaker)
            found[key] = {"start": start, "end": end, "speaker": speaker}
    return sorted(found.values(), key=lambda item: (item["start"], item["end"], item["speaker"]))


def _speaker_for(start: float, end: float, diarization: list[dict[str, Any]]) -> str:
    midpoint = (start + end) / 2.0
    candidates = []
    for item in diarization:
        overlap = max(0.0, min(end, item["end"]) - max(start, item["start"]))
        contains = item["start"] <= midpoint <= item["end"]
        if overlap > 0 or contains:
            candidates.append((overlap, contains, -item["start"], item["speaker"]))
    speaker = max(candidates)[3] if candidates else 0
    return f"S{speaker + 1:02d}"


def _merge_words(words: list[dict[str, Any]], diarization: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for word in words:
        speaker = _speaker_for(word["start"], word["end"], diarization)
        if (
            merged
            and merged[-1]["speaker"] == speaker
            and word["start"] - merged[-1]["end"] <= 1.2
            and len(merged[-1]["text"]) < 280
        ):
            separator = "" if _joins_without_space(merged[-1]["text"], word["text"]) else " "
            merged[-1]["text"] += separator + word["text"]
            merged[-1]["end"] = max(merged[-1]["end"], word["end"])
        else:
            merged.append({"start": word["start"], "end": word["end"], "speaker": speaker, "text": word["text"]})
    return [
        {
            "start_ms": max(0, round(item["start"] * 1000)),
            "end_ms": max(0, round(item["end"] * 1000)),
            "speaker": item["speaker"],
            "text": item["text"].strip(),
        }
        for item in merged
        if item["text"].strip()
    ]


def _joins_without_space(left: str, right: str) -> bool:
    if not left or not right:
        return True
    return ord(left[-1]) > 127 or ord(right[0]) > 127 or right[0] in ",.!?;:，。！？；：、"


def transcribe(
    manifest: dict[str, Any],
    models_dir: Path,
    audio_path: Path,
    output_path: Path,
    *,
    language: Optional[str],
    title: Optional[str],
    observed_at: Optional[str],
) -> dict[str, Any]:
    from mlx_audio.stt import load as load_stt
    from mlx_audio.vad import load as load_vad

    samples, sample_rate = _to_mono_16k(audio_path)
    if len(samples) == 0:
        raise RuntimeError("Audio contains no samples")
    models = manifest["models"]
    asr = load_stt(str(model_path(models_dir, models["asr"]["repo_id"])))
    aligner = load_stt(str(model_path(models_dir, models["aligner"]["repo_id"])))
    diarizer = load_vad(str(model_path(models_dir, models["diarizer"]["repo_id"])))
    chunk_seconds = int(manifest["limits"]["asr_chunk_seconds"])
    chunk_samples = chunk_seconds * sample_rate
    words: list[dict[str, Any]] = []
    fallbacks: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="personal-context-audio-") as temporary:
        temporary_dir = Path(temporary)
        normalized_path = temporary_dir / "normalized.wav"
        _write_wav(normalized_path, samples, sample_rate)
        diarization = _collect_diarization(
            diarizer, normalized_path, float(manifest["limits"]["diarization_chunk_seconds"])
        )
        for index, start_sample in enumerate(range(0, len(samples), chunk_samples)):
            chunk = samples[start_sample : start_sample + chunk_samples]
            chunk_path = temporary_dir / f"chunk-{index:05d}.wav"
            _write_wav(chunk_path, chunk, sample_rate)
            result = asr.generate(str(chunk_path), language=language) if language else asr.generate(str(chunk_path))
            text = str(getattr(result, "text", result)).strip()
            if not text:
                continue
            aligned = aligner.generate(str(chunk_path), text=text, language=language) if language else aligner.generate(str(chunk_path), text=text)
            offset = start_sample / sample_rate
            chunk_words = []
            for item in _alignment_items(aligned):
                token = str(getattr(item, "text", "")).strip()
                if not token:
                    continue
                start = offset + float(getattr(item, "start_time", 0.0))
                end = offset + float(getattr(item, "end_time", getattr(item, "start_time", 0.0)))
                chunk_words.append({"start": start, "end": max(start, end), "text": token})
            if chunk_words:
                words.extend(chunk_words)
            else:
                end = offset + len(chunk) / sample_rate
                fallbacks.append({"start": offset, "end": end, "text": text})
    segments = _merge_words(words, diarization)
    for item in fallbacks:
        segments.append(
            {
                "start_ms": round(item["start"] * 1000),
                "end_ms": round(item["end"] * 1000),
                "speaker": _speaker_for(item["start"], item["end"], diarization),
                "text": item["text"],
            }
        )
    segments.sort(key=lambda item: (item["start_ms"], item["end_ms"]))
    if not segments:
        raise RuntimeError("No speech was transcribed")
    source_hash = digest_file(audio_path)
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
            "provider": manifest["provider"],
            "profile_version": manifest["profile_version"],
            "source_audio_sha256": source_hash,
            "runtime": manifest["runtime"],
            "models": {
                role: {"repo_id": model["repo_id"], "revision": model["revision"]}
                for role, model in models.items()
            },
            "language_hint": language,
            "maximum_speakers": manifest["limits"]["maximum_speakers"],
        },
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_name(f".{output_path.name}.{os.getpid()}.tmp")
    try:
        temporary_path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary_path, output_path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    return {"status": "transcribed", "segments": len(segments), "output": str(output_path)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Pinned qwen-mlx provider for personal-context")
    subparsers = parser.add_subparsers(dest="command", required=True)
    command = subparsers.add_parser("download")
    command.add_argument("--manifest", required=True)
    command.add_argument("--models-dir", required=True)
    command = subparsers.add_parser("transcribe")
    command.add_argument("--manifest", required=True)
    command.add_argument("--models-dir", required=True)
    command.add_argument("--audio", required=True)
    command.add_argument("--output", required=True)
    command.add_argument("--language")
    command.add_argument("--title")
    command.add_argument("--observed-at")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        manifest = load_manifest(Path(args.manifest).resolve())
        if args.command == "download":
            result = download_models(manifest, Path(args.models_dir).resolve())
        else:
            result = transcribe(
                manifest,
                Path(args.models_dir).resolve(),
                Path(args.audio).resolve(),
                Path(args.output).resolve(),
                language=args.language,
                title=args.title,
                observed_at=args.observed_at,
            )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"error": str(exc), "command": args.command}, ensure_ascii=False), file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
