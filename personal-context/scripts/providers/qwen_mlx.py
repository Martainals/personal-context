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


def _collect_diarization(
    model: Any,
    audio_path: Path,
    profile: dict[str, Any],
    expected_speakers: Optional[int],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    streaming = profile["streaming"]
    postprocessing = profile["postprocessing"]
    _apply_streaming_profile(model, streaming)
    fallback_frame_seconds = float(profile["frame_seconds"])
    frame_seconds = _model_frame_seconds(model, fallback_frame_seconds)
    chunk_seconds = int(streaming["chunk_frames"]) * frame_seconds
    probabilities: list[list[float]] = []
    for result in model.generate_stream(
        str(audio_path),
        chunk_duration=chunk_seconds,
        threshold=float(postprocessing["activity_threshold"]),
        min_duration=0.0,
        merge_gap=0.0,
        spkcache_max=int(streaming["speaker_cache_frames"]),
        fifo_max=int(streaming["fifo_frames"]),
        verbose=False,
    ):
        probabilities.extend(_probability_rows(getattr(result, "speaker_probs", None)))
    if not probabilities:
        raise RuntimeError("Diarizer returned no speaker probabilities")
    segments, selected = _probabilities_to_diarization(
        probabilities,
        frame_seconds=frame_seconds,
        expected_speakers=expected_speakers,
        postprocessing=postprocessing,
        speaker_selection_window_seconds=chunk_seconds,
    )
    return segments, {
        "mode": profile["mode"],
        "frame_seconds": frame_seconds,
        "chunk_seconds": chunk_seconds,
        "expected_speakers": expected_speakers,
        "selected_model_slots": selected,
        "output_speakers": expected_speakers if expected_speakers is not None else len(selected),
        "postprocessing": postprocessing,
        "streaming": streaming,
    }


def _speaker_details(
    start: float, end: float, diarization: list[dict[str, Any]]
) -> tuple[str, float, float]:
    midpoint = (start + end) / 2.0
    candidates: list[tuple[float, bool, float, int]] = []
    for index, item in enumerate(diarization):
        overlap = max(0.0, min(end, item["end"]) - max(start, item["start"]))
        contains = item["start"] <= midpoint <= item["end"]
        if overlap > 0 or contains:
            candidates.append((overlap, contains, -item["start"], index))
    if candidates:
        selected = diarization[max(candidates)[3]]
    elif diarization:
        selected = min(
            diarization,
            key=lambda item: min(abs(midpoint - item["start"]), abs(midpoint - item["end"])),
        )
    else:
        selected = {"speaker": 0, "confidence": 0.0, "margin": 0.0}
    speaker = int(selected.get("speaker", 0))
    return (
        f"S{speaker + 1:02d}",
        float(selected.get("confidence", 0.0)),
        float(selected.get("margin", 0.0)),
    )


def _speaker_for(start: float, end: float, diarization: list[dict[str, Any]]) -> str:
    return _speaker_details(start, end, diarization)[0]


def _smooth_word_speakers(
    words: list[dict[str, Any]], settings: dict[str, Any]
) -> list[dict[str, Any]]:
    smoothed = [dict(item) for item in words]
    if len(smoothed) < 3:
        return smoothed
    max_characters = int(settings["max_fragment_characters"])
    max_seconds = float(settings["max_fragment_seconds"])
    max_gap = float(settings["max_fragment_gap_seconds"])
    max_margin = float(settings["max_fragment_margin"])
    punctuation = set(" \\t\\r\\n,.。，！？!?;；:：、")
    sentence_end = set("。！？!?;；")
    backchannels = {"嗯", "啊", "哦", "对", "是", "好", "行", "对的", "没错"}

    groups: list[tuple[int, int]] = []
    start = 0
    for index in range(1, len(smoothed) + 1):
        if index == len(smoothed) or smoothed[index]["speaker"] != smoothed[start]["speaker"]:
            groups.append((start, index))
            start = index
    for group_index in range(1, len(groups) - 1):
        start, end = groups[group_index]
        before_start, before_end = groups[group_index - 1]
        after_start, _ = groups[group_index + 1]
        before_speaker = smoothed[before_start]["speaker"]
        if before_speaker != smoothed[after_start]["speaker"] or before_speaker == smoothed[start]["speaker"]:
            continue
        text = "".join(str(item.get("text", "")) for item in smoothed[start:end])
        compact = "".join(character for character in text if character not in punctuation)
        duration = float(smoothed[end - 1]["end"]) - float(smoothed[start]["start"])
        gap_before = float(smoothed[start]["start"]) - float(smoothed[before_end - 1]["end"])
        gap_after = float(smoothed[after_start]["start"]) - float(smoothed[end - 1]["end"])
        mean_margin = sum(float(item.get("speaker_margin", 1.0)) for item in smoothed[start:end]) / (end - start)
        left_text = str(smoothed[before_end - 1].get("text", "")).rstrip()
        if (
            compact
            and compact not in backchannels
            and len(compact) <= max_characters
            and duration <= max_seconds
            and gap_before <= max_gap
            and gap_after <= max_gap
            and mean_margin <= max_margin
            and (not left_text or left_text[-1] not in sentence_end)
        ):
            for item in smoothed[start:end]:
                item["speaker"] = before_speaker
    return smoothed


def _realign_word_boundaries(
    words: list[dict[str, Any]], settings: dict[str, Any]
) -> list[dict[str, Any]]:
    """Move a late speaker boundary back to a nearby, explicit word pause.

    The diarizer operates on acoustic frames, so its boundary can trail the
    conversational turn by one or two aligned characters.  Only move a
    boundary when the current split cuts a continuous phrase and the proposed
    split follows a clear pause.  Keeping the correction deliberately small
    avoids erasing genuine short interjections.
    """
    realigned = [dict(item) for item in words]
    if len(realigned) < 2:
        return realigned

    pause_seconds = float(
        settings.get("boundary_pause_seconds", settings.get("max_fragment_gap_seconds", 0.2))
    )
    max_shift_seconds = float(
        settings.get("boundary_max_shift_seconds", settings.get("max_fragment_seconds", 1.0))
    )
    max_shift_characters = int(
        settings.get("boundary_max_shift_characters", settings.get("max_fragment_characters", 2))
    )
    join_gap_seconds = float(settings.get("boundary_join_gap_seconds", 0.08))
    punctuation = set(" \t\r\n,.。，！？!?;；:：、")
    sentence_end = set("。！？!?;；")
    backchannels = {"嗯", "啊", "哦", "对", "是", "好", "行", "对的", "没错"}

    boundaries = [
        index
        for index in range(1, len(realigned))
        if realigned[index]["speaker"] != realigned[index - 1]["speaker"]
    ]
    for boundary in boundaries:
        old_speaker = realigned[boundary - 1]["speaker"]
        new_speaker = realigned[boundary]["speaker"]
        boundary_gap = float(realigned[boundary]["start"]) - float(realigned[boundary - 1]["end"])
        if boundary_gap > join_gap_seconds:
            continue

        candidate_start: Optional[int] = None
        compact_characters = 0
        for start in range(boundary - 1, 0, -1):
            if realigned[start]["speaker"] != old_speaker:
                break
            if start < boundary - 1:
                internal_gap = float(realigned[start + 1]["start"]) - float(realigned[start]["end"])
                if internal_gap > join_gap_seconds:
                    break
            token = "".join(
                character
                for character in str(realigned[start].get("text", ""))
                if character not in punctuation
            )
            compact_characters += len(token)
            duration = float(realigned[boundary - 1]["end"]) - float(realigned[start]["start"])
            if compact_characters > max_shift_characters or duration > max_shift_seconds:
                break
            pause_before = float(realigned[start]["start"]) - float(realigned[start - 1]["end"])
            if pause_before >= pause_seconds:
                candidate_start = start
                break

        if candidate_start is None:
            candidate_end: Optional[int] = None
            compact_characters = 0
            for end in range(boundary, len(realigned) - 1):
                if realigned[end]["speaker"] != new_speaker:
                    break
                if end > boundary:
                    internal_gap = float(realigned[end]["start"]) - float(
                        realigned[end - 1]["end"]
                    )
                    if internal_gap > join_gap_seconds:
                        break
                token = "".join(
                    character
                    for character in str(realigned[end].get("text", ""))
                    if character not in punctuation
                )
                compact_characters += len(token)
                duration = float(realigned[end]["end"]) - float(
                    realigned[boundary]["start"]
                )
                if compact_characters > max_shift_characters or duration > max_shift_seconds:
                    break
                pause_after = float(realigned[end + 1]["start"]) - float(
                    realigned[end]["end"]
                )
                if pause_after >= pause_seconds:
                    candidate_end = end + 1
                    break
            if candidate_end is None:
                continue
            candidate_text = "".join(
                str(item.get("text", "")) for item in realigned[boundary:candidate_end]
            )
            compact = "".join(
                character for character in candidate_text if character not in punctuation
            )
            previous_text = str(realigned[boundary - 1].get("text", "")).rstrip()
            if (
                not compact
                or compact in backchannels
                or (previous_text and previous_text[-1] in sentence_end)
            ):
                continue
            for item in realigned[boundary:candidate_end]:
                item["speaker"] = old_speaker
            continue

        candidate_text = "".join(
            str(item.get("text", "")) for item in realigned[candidate_start:boundary]
        )
        compact = "".join(character for character in candidate_text if character not in punctuation)
        previous_text = str(realigned[candidate_start - 1].get("text", "")).rstrip()
        if (
            not compact
            or compact in backchannels
            or (previous_text and previous_text[-1] in sentence_end)
        ):
            continue
        for item in realigned[candidate_start:boundary]:
            item["speaker"] = new_speaker
    return realigned


def _merge_words(
    words: list[dict[str, Any]],
    diarization: list[dict[str, Any]],
    settings: dict[str, Any],
) -> list[dict[str, Any]]:
    labeled: list[dict[str, Any]] = []
    for word in words:
        speaker, confidence, margin = _speaker_details(word["start"], word["end"], diarization)
        labeled.append(
            {
                **word,
                "speaker": speaker,
                "speaker_confidence": confidence,
                "speaker_margin": margin,
            }
        )
    labeled = _realign_word_boundaries(labeled, settings)
    labeled = _smooth_word_speakers(labeled, settings)
    merged: list[dict[str, Any]] = []
    for word in labeled:
        speaker = word["speaker"]
        if (
            merged
            and merged[-1]["speaker"] == speaker
            and word["start"] - merged[-1]["end"] <= float(settings["max_same_speaker_gap_seconds"])
            and len(merged[-1]["text"]) < int(settings["max_segment_characters"])
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
    speaker_count: Optional[int] = None,
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
    maximum_speakers = int(manifest["limits"]["maximum_speakers"])
    if speaker_count is not None and not 1 <= speaker_count <= maximum_speakers:
        raise RuntimeError(f"speaker_count must be between 1 and {maximum_speakers}")
    chunk_seconds = int(manifest["limits"]["asr_chunk_seconds"])
    chunk_samples = chunk_seconds * sample_rate
    words: list[dict[str, Any]] = []
    fallbacks: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="personal-context-audio-") as temporary:
        temporary_dir = Path(temporary)
        normalized_path = temporary_dir / "normalized.wav"
        _write_wav(normalized_path, samples, sample_rate)
        diarization, diarization_details = _collect_diarization(
            diarizer,
            normalized_path,
            manifest["diarization"],
            speaker_count,
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
    segments = _merge_words(words, diarization, manifest["diarization"]["word_assembly"])
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
            "maximum_speakers": maximum_speakers,
            "speaker_count_hint": speaker_count,
            "diarization": diarization_details,
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
    command.add_argument("--speaker-count", type=int, choices=range(1, 5))
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
                speaker_count=args.speaker_count,
            )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"error": str(exc), "command": args.command}, ensure_ascii=False), file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
