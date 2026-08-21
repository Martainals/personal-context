#!/usr/bin/env python3
"""Offline 3D-Speaker diarization with non-biometric scalar evidence.

Speaker embeddings and cluster centres exist only in this subprocess.  The
only result crossing the process boundary is an anonymous recording-local
timeline with scalar confidence and margin values.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import sys
from pathlib import Path
from typing import Any, Iterable, Optional


def _dot(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


def _normalized(values: Iterable[float]) -> list[float]:
    vector = [float(value) for value in values]
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 1e-12:
        return [0.0 for _ in vector]
    return [value / norm for value in vector]


def anonymous_segments_with_evidence(
    chunks: list[list[float]], labels: list[int], embeddings: Iterable[Iterable[float]]
) -> list[dict[str, Any]]:
    """Return merged anonymous turns and discard all vector-shaped evidence."""
    vectors = [_normalized(vector) for vector in embeddings]
    if not chunks or len(chunks) != len(labels) or len(labels) != len(vectors):
        raise ValueError("chunks, labels, and embeddings must have the same non-zero length")
    dimensions = {len(vector) for vector in vectors}
    if len(dimensions) != 1 or not dimensions or 0 in dimensions:
        raise ValueError("speaker evidence vectors must have one non-zero dimension")

    ordered_labels: list[int] = []
    for label in labels:
        if int(label) not in ordered_labels:
            ordered_labels.append(int(label))
    remapped = {label: index for index, label in enumerate(ordered_labels)}
    centres: dict[int, list[float]] = {}
    for label in ordered_labels:
        members = [vectors[index] for index, item in enumerate(labels) if int(item) == label]
        centres[label] = _normalized(
            sum(member[dimension] for member in members) / len(members)
            for dimension in range(len(members[0]))
        )

    scored: list[dict[str, Any]] = []
    for chunk, raw_label, vector in zip(chunks, labels, vectors):
        if len(chunk) != 2:
            raise ValueError("each diarization chunk must contain start and end")
        start, end = float(chunk[0]), float(chunk[1])
        if start < 0 or end <= start:
            raise ValueError("diarization chunk times must be increasing and non-negative")
        similarities = sorted(
            (_dot(vector, centre), label) for label, centre in centres.items()
        )
        selected_similarity = _dot(vector, centres[int(raw_label)])
        alternatives = [score for score, label in similarities if label != int(raw_label)]
        second_similarity = max(alternatives, default=-1.0)
        scored.append(
            {
                "start": start,
                "end": end,
                "speaker": remapped[int(raw_label)],
                "confidence": max(0.0, min(1.0, (selected_similarity + 1.0) / 2.0)),
                "margin": max(-2.0, min(2.0, selected_similarity - second_similarity)),
            }
        )

    merged: list[dict[str, Any]] = []
    for current in scored:
        if not merged:
            merged.append(dict(current))
            continue
        previous = merged[-1]
        if current["speaker"] == previous["speaker"] and current["start"] <= previous["end"]:
            previous_duration = max(1e-9, float(previous["end"]) - float(previous["start"]))
            current_duration = max(1e-9, float(current["end"]) - float(current["start"]))
            total = previous_duration + current_duration
            previous["confidence"] = (
                float(previous["confidence"]) * previous_duration
                + float(current["confidence"]) * current_duration
            ) / total
            previous["margin"] = (
                float(previous["margin"]) * previous_duration
                + float(current["margin"]) * current_duration
            ) / total
            previous["end"] = max(float(previous["end"]), float(current["end"]))
            continue
        if current["speaker"] != previous["speaker"] and current["start"] < previous["end"]:
            boundary = (float(previous["end"]) + float(current["start"])) / 2.0
            previous["end"] = boundary
            current = {**current, "start": boundary}
        merged.append(dict(current))
    return [
        {
            "start": round(float(item["start"]), 6),
            "end": round(float(item["end"]), 6),
            "speaker": int(item["speaker"]),
            "confidence": round(float(item["confidence"]), 6),
            "margin": round(float(item["margin"]), 6),
        }
        for item in merged
    ]


def _load_manifest(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("backend") != "3dspeaker-offline":
        raise RuntimeError("Invalid 3D-Speaker offline manifest")
    return value


def _activate_source(source_dir: Path) -> None:
    source = source_dir.expanduser().resolve()
    if not (source / "speakerlab").is_dir():
        raise RuntimeError(f"3D-Speaker source is missing: {source}")
    sys.path.insert(0, str(source))


def _model_paths_file(models_dir: Path) -> Path:
    return models_dir / "model-paths.json"


def download_models(
    manifest: dict[str, Any], source_dir: Path, models_dir: Path
) -> dict[str, Any]:
    _activate_source(source_dir)
    from speakerlab.utils.utils import download_model_from_modelscope

    models_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}
    for role, model in manifest["models"].items():
        resolved = download_model_from_modelscope(
            str(model["repo_id"]), str(model["revision"]), str(models_dir)
        )
        paths[str(role)] = str(Path(resolved).resolve())
    target = _model_paths_file(models_dir)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(paths, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return {"status": "downloaded", "models": paths}


def _read_model_paths(models_dir: Path) -> dict[str, Path]:
    path = _model_paths_file(models_dir)
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("3D-Speaker model path marker is invalid")
    output = {str(role): Path(str(model_path)).resolve() for role, model_path in value.items()}
    if not output or any(not item.is_dir() for item in output.values()):
        raise RuntimeError("3D-Speaker model files are incomplete")
    return output


def _read_audio(audio_path: Path, sample_rate: int = 16000) -> Any:
    import numpy as np
    import soundfile as sf
    import torch
    from scipy.signal import resample_poly

    samples, source_rate = sf.read(str(audio_path), dtype="float32", always_2d=True)
    mono = np.asarray(samples, dtype=np.float32).mean(axis=1)
    if int(source_rate) != sample_rate:
        divisor = math.gcd(int(source_rate), sample_rate)
        mono = resample_poly(
            mono, sample_rate // divisor, int(source_rate) // divisor
        ).astype(np.float32)
    if mono.size == 0:
        raise RuntimeError("Audio contains no samples")
    return torch.from_numpy(mono).unsqueeze(0)


def _chunks(vad_times: list[list[float]], duration: float, step: float) -> list[list[float]]:
    output: list[list[float]] = []
    for start, end in vad_times:
        position = float(start)
        while position + duration < float(end) + step:
            output.append([position, min(position + duration, float(end))])
            position += step
    return output


def _speaker_count_from_eigenvalues(
    eigenvalues: Iterable[float], minimum: int, maximum: int
) -> int:
    values = sorted(float(value) for value in eigenvalues)
    floor = max(1, int(minimum))
    candidates = list(range(floor, min(int(maximum), len(values) - 1) + 1))
    gaps = [values[count] - values[count - 1] for count in candidates]
    return candidates[gaps.index(max(gaps))] if gaps else 1


def _circle_pad(waveform: Any, target: int) -> Any:
    import torch

    if waveform.shape[0] >= target:
        return waveform[:target]
    repeats = math.ceil(target / max(1, int(waveform.shape[0])))
    return torch.cat([waveform] * repeats)[:target]


def _cluster_labels(embeddings: Any, speaker_count: Optional[int], settings: dict[str, Any]) -> Any:
    import numpy as np
    from scipy.sparse.linalg import eigsh
    from sklearn.cluster import KMeans

    values = np.asarray(embeddings, dtype=np.float64)
    if values.ndim != 2 or values.shape[0] == 0:
        raise RuntimeError("3D-Speaker returned no usable speaker evidence")
    if values.shape[0] == 1:
        return np.zeros(1, dtype=int)
    normalized = values / np.maximum(np.linalg.norm(values, axis=1, keepdims=True), 1e-12)
    similarity = normalized @ normalized.T
    pval = float(settings.get("pval", 0.012))
    minimum_neighbours = int(settings.get("minimum_neighbours", 6))
    remove = min(int((1.0 - pval) * len(values)), len(values) - minimum_neighbours)
    if remove > 0:
        for index in range(len(values)):
            low = np.argsort(similarity[index])[:remove]
            similarity[index, low] = 0.0
    similarity = 0.5 * (similarity + similarity.T)
    laplacian = -similarity
    diagonal = np.sum(np.abs(similarity), axis=1)
    np.fill_diagonal(laplacian, diagonal)
    maximum = min(int(settings.get("maximum_speakers", 4)), len(values) - 1)
    eigen_count = max(1, min(maximum + 1, len(values)))
    if eigen_count >= len(values):
        eigenvalues, eigenvectors = np.linalg.eigh(laplacian)
    else:
        eigenvalues, eigenvectors = eigsh(laplacian, k=eigen_count, which="SM")
    order = np.argsort(eigenvalues)
    eigenvalues = eigenvalues[order]
    eigenvectors = eigenvectors[:, order]
    if speaker_count is None:
        clusters = _speaker_count_from_eigenvalues(
            eigenvalues,
            int(settings.get("minimum_speakers", 1)),
            maximum,
        )
    else:
        clusters = int(speaker_count)
    clusters = max(1, min(clusters, len(values)))
    if clusters == 1:
        return np.zeros(len(values), dtype=int)
    return KMeans(n_clusters=clusters, n_init=10, random_state=0).fit_predict(
        eigenvectors[:, :clusters]
    )


def _result_details(
    manifest: dict[str, Any],
    *,
    speaker_count: Optional[int],
    output_speakers: int,
    speech_chunks: int,
) -> dict[str, Any]:
    model_roles = {
        "embedding": "speaker_encoder",
        "vad": "vad",
    }
    unknown_roles = sorted(set(manifest["models"]) - set(model_roles))
    if unknown_roles:
        raise RuntimeError(
            "3D-Speaker result metadata has unknown model roles: "
            + ", ".join(unknown_roles)
        )
    models = {
        model_roles[role]: {
            "repo_id": item["repo_id"],
            "revision": item["revision"],
        }
        for role, item in manifest["models"].items()
    }
    return {
        "backend": "3dspeaker-offline",
        "mode": "offline_clustering",
        "expected_speakers": speaker_count,
        "output_speakers": int(output_speakers),
        "evidence": "cluster-distance",
        "speech_chunks": int(speech_chunks),
        "source_revision": manifest["source"]["revision"],
        "models": models,
    }


def diarize(
    manifest: dict[str, Any],
    source_dir: Path,
    models_dir: Path,
    audio_path: Path,
    speaker_count: Optional[int],
) -> dict[str, Any]:
    _activate_source(source_dir)
    import torch
    from modelscope.pipelines import pipeline
    from modelscope.utils.constant import Tasks
    from speakerlab.models.campplus.DTDNN import CAMPPlus
    from speakerlab.process.processor import FBank

    paths = _read_model_paths(models_dir)
    settings = manifest["diarization"]
    device = torch.device("cpu")
    waveform = _read_audio(audio_path)
    vad = pipeline(
        task=Tasks.voice_activity_detection,
        model=str(paths["vad"]),
        device="cpu",
        disable_pbar=True,
        disable_update=True,
    )
    vad_result = vad(waveform[0].numpy())[0]
    vad_times = [
        [float(item[0]) / 1000.0, float(item[1]) / 1000.0]
        for item in vad_result["value"]
    ]
    chunks = _chunks(
        vad_times,
        float(settings.get("chunk_seconds", 1.5)),
        float(settings.get("chunk_step_seconds", 0.75)),
    )
    if not chunks:
        raise RuntimeError("3D-Speaker VAD returned no speech chunks")

    model = CAMPPlus(feat_dim=80, embedding_size=192)
    checkpoint = paths["embedding"] / str(manifest["models"]["embedding"]["checkpoint"])
    model.load_state_dict(torch.load(str(checkpoint), map_location="cpu"))
    model.eval().to(device)
    extractor = FBank(n_mels=80, sample_rate=16000, mean_nor=True)
    waveforms = [
        waveform[0, int(start * 16000) : int(end * 16000)] for start, end in chunks
    ]
    maximum_length = max(int(item.shape[0]) for item in waveforms)
    batch_size = int(settings.get("batch_size", 64))
    embeddings = []
    with torch.no_grad():
        for start in range(0, len(waveforms), batch_size):
            batch = torch.stack(
                [_circle_pad(item, maximum_length) for item in waveforms[start : start + batch_size]]
            ).unsqueeze(1)
            features = torch.vmap(extractor)(batch)
            embeddings.append(model(features).cpu())
    evidence = torch.cat(embeddings, dim=0).numpy()
    labels = _cluster_labels(evidence, speaker_count, settings)
    segments = anonymous_segments_with_evidence(chunks, labels.tolist(), evidence.tolist())
    return {
        "segments": segments,
        "details": _result_details(
            manifest,
            speaker_count=speaker_count,
            output_speakers=len({item["speaker"] for item in segments}),
            speech_chunks=len(chunks),
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Pinned 3D-Speaker offline diarization")
    subparsers = parser.add_subparsers(dest="command", required=True)
    command = subparsers.add_parser("download")
    command.add_argument("--manifest", required=True)
    command.add_argument("--source-dir", required=True)
    command.add_argument("--models-dir", required=True)
    command = subparsers.add_parser("preflight")
    command.add_argument("--manifest", required=True)
    command.add_argument("--source-dir", required=True)
    command.add_argument("--models-dir", required=True)
    command.add_argument("--speaker-count", type=int, choices=range(1, 5))
    command = subparsers.add_parser("diarize")
    command.add_argument("--manifest", required=True)
    command.add_argument("--source-dir", required=True)
    command.add_argument("--models-dir", required=True)
    command.add_argument("--audio", required=True)
    command.add_argument("--speaker-count", type=int, choices=range(1, 5))
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        manifest = _load_manifest(Path(args.manifest).resolve())
        if args.command == "download":
            result = download_models(
                manifest, Path(args.source_dir).resolve(), Path(args.models_dir).resolve()
            )
        elif args.command == "preflight":
            result = {
                "details": _result_details(
                    manifest,
                    speaker_count=args.speaker_count,
                    output_speakers=0,
                    speech_chunks=0,
                )
            }
        else:
            # Third-party inference libraries may print notices to stdout. Keep the
            # subprocess protocol strict by routing those notices to stderr and
            # reserving stdout for the single JSON result below.
            with contextlib.redirect_stdout(sys.stderr):
                result = diarize(
                    manifest,
                    Path(args.source_dir).resolve(),
                    Path(args.models_dir).resolve(),
                    Path(args.audio).resolve(),
                    args.speaker_count,
                )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        print(
            json.dumps({"error": str(exc), "command": args.command}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
