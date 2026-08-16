"""Pure transcript assembly from aligned words and speaker turns.

This module deliberately has no MLX, filesystem, cache, or database imports.
It turns already-derived alignment and diarization evidence into the stable
``transcript.v1`` segment shape.
"""

from __future__ import annotations

from typing import Any, Optional


def speaker_details(
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


def speaker_for(start: float, end: float, diarization: list[dict[str, Any]]) -> str:
    return speaker_details(start, end, diarization)[0]


def smooth_word_speakers(
    words: list[dict[str, Any]], settings: dict[str, Any]
) -> list[dict[str, Any]]:
    smoothed = [dict(item) for item in words]
    if len(smoothed) < 3:
        return smoothed
    max_characters = int(settings["max_fragment_characters"])
    max_seconds = float(settings["max_fragment_seconds"])
    max_gap = float(settings["max_fragment_gap_seconds"])
    max_margin = float(settings["max_fragment_margin"])
    punctuation = set(" \t\r\n,.。，！？!?;；:：、")
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
        mean_margin = sum(
            float(item.get("speaker_margin", 1.0)) for item in smoothed[start:end]
        ) / (end - start)
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


def realign_word_boundaries(
    words: list[dict[str, Any]], settings: dict[str, Any]
) -> list[dict[str, Any]]:
    """Move a frame-level speaker change to a nearby explicit word pause."""
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
                    internal_gap = float(realigned[end]["start"]) - float(realigned[end - 1]["end"])
                    if internal_gap > join_gap_seconds:
                        break
                token = "".join(
                    character
                    for character in str(realigned[end].get("text", ""))
                    if character not in punctuation
                )
                compact_characters += len(token)
                duration = float(realigned[end]["end"]) - float(realigned[boundary]["start"])
                if compact_characters > max_shift_characters or duration > max_shift_seconds:
                    break
                pause_after = float(realigned[end + 1]["start"]) - float(realigned[end]["end"])
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
            if not compact or compact in backchannels or (
                previous_text and previous_text[-1] in sentence_end
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
        if not compact or compact in backchannels or (
            previous_text and previous_text[-1] in sentence_end
        ):
            continue
        for item in realigned[candidate_start:boundary]:
            item["speaker"] = new_speaker
    return realigned


def joins_without_space(left: str, right: str) -> bool:
    if not left or not right:
        return True
    return ord(left[-1]) > 127 or ord(right[0]) > 127 or right[0] in ",.!?;:，。！？；：、"


def merge_words(
    words: list[dict[str, Any]],
    diarization: list[dict[str, Any]],
    settings: dict[str, Any],
) -> list[dict[str, Any]]:
    labeled: list[dict[str, Any]] = []
    for word in words:
        speaker, confidence, margin = speaker_details(word["start"], word["end"], diarization)
        labeled.append(
            {
                **word,
                "speaker": speaker,
                "speaker_confidence": confidence,
                "speaker_margin": margin,
            }
        )
    labeled = realign_word_boundaries(labeled, settings)
    labeled = smooth_word_speakers(labeled, settings)
    merged: list[dict[str, Any]] = []
    for word in labeled:
        speaker = word["speaker"]
        if (
            merged
            and merged[-1]["speaker"] == speaker
            and word["start"] - merged[-1]["end"] <= float(settings["max_same_speaker_gap_seconds"])
            and len(merged[-1]["text"]) < int(settings["max_segment_characters"])
        ):
            separator = "" if joins_without_space(merged[-1]["text"], word["text"]) else " "
            merged[-1]["text"] += separator + word["text"]
            merged[-1]["end"] = max(merged[-1]["end"], word["end"])
        else:
            merged.append(
                {"start": word["start"], "end": word["end"], "speaker": speaker, "text": word["text"]}
            )
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


def assemble_transcript_segments(
    words: list[dict[str, Any]],
    fallbacks: list[dict[str, Any]],
    diarization: list[dict[str, Any]],
    settings: dict[str, Any],
) -> list[dict[str, Any]]:
    segments = merge_words(words, diarization, settings)
    for item in fallbacks:
        segments.append(
            {
                "start_ms": round(item["start"] * 1000),
                "end_ms": round(item["end"] * 1000),
                "speaker": speaker_for(item["start"], item["end"], diarization),
                "text": item["text"],
            }
        )
    segments.sort(key=lambda item: (item["start_ms"], item["end_ms"]))
    return segments
