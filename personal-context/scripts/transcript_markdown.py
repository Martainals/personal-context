"""Validate transcript.v1 documents and publish complete Markdown atomically."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MARKER = re.compile(
    r"<!-- personal-context:generated transcript-markdown-v1 "
    r"body-sha256=([0-9a-f]{64}) source-audio-sha256=([0-9a-f]{64}) "
    r"segment-count=([0-9]+) complete=true -->"
)


class TranscriptMarkdownError(RuntimeError):
    """Invalid transcript or unsafe Markdown publication."""


class ManualEditError(TranscriptMarkdownError):
    """An existing delivery is no longer identical to generated output."""


@dataclass(frozen=True)
class RenderedMarkdown:
    text: str
    title: str
    duration_ms: int
    segment_count: int
    body_sha256: str
    source_audio_sha256: str


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _clean_label(value: Any, fallback: str) -> str:
    rendered = " ".join(str(value or fallback).replace("\r", "\n").splitlines()).strip()
    return rendered or fallback


def _timestamp(milliseconds: int) -> str:
    total_seconds = max(0, milliseconds) // 1000
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _validate_transcript(value: Any) -> tuple[str, list[dict[str, Any]], int]:
    if not isinstance(value, dict):
        raise TranscriptMarkdownError("transcript.v1 must be a JSON object")
    event = value.get("event")
    if not isinstance(event, dict):
        raise TranscriptMarkdownError("transcript.v1 event must be an object")
    processing = value.get("processing", {})
    if not isinstance(processing, dict):
        raise TranscriptMarkdownError("transcript.v1 processing must be an object")
    if processing.get("contract") not in {None, "transcript.v1"}:
        raise TranscriptMarkdownError("Unsupported transcript contract")
    raw_segments = value.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        raise TranscriptMarkdownError("transcript.v1 segments must be a non-empty array")

    segments: list[dict[str, Any]] = []
    for ordinal, raw in enumerate(raw_segments):
        if not isinstance(raw, dict):
            raise TranscriptMarkdownError(f"segments[{ordinal}] must be an object")
        start = raw.get("start_ms")
        end = raw.get("end_ms")
        text = raw.get("text")
        if not isinstance(start, int) or isinstance(start, bool) or start < 0:
            raise TranscriptMarkdownError(f"segments[{ordinal}].start_ms must be a non-negative integer")
        if not isinstance(end, int) or isinstance(end, bool) or end < start:
            raise TranscriptMarkdownError(f"segments[{ordinal}].end_ms must be >= start_ms")
        if not isinstance(text, str) or not text.strip():
            raise TranscriptMarkdownError(f"segments[{ordinal}].text must be non-empty")
        segments.append(
            {
                "start_ms": start,
                "end_ms": end,
                "speaker": _clean_label(raw.get("speaker"), "未知人物"),
                "text": text.replace("\r\n", "\n").replace("\r", "\n").strip(),
                "ordinal": ordinal,
            }
        )
    segments.sort(key=lambda item: (item["start_ms"], item["end_ms"], item["ordinal"]))
    return _clean_label(event.get("title"), "录音转写"), segments, max(
        item["end_ms"] for item in segments
    )


def render_transcript_markdown(
    transcript: Any, *, source_audio_sha256: str
) -> RenderedMarkdown:
    if not _SHA256.fullmatch(source_audio_sha256):
        raise TranscriptMarkdownError("source_audio_sha256 must be a lowercase SHA-256 digest")
    title, segments, duration_ms = _validate_transcript(transcript)
    lines = [
        f"# {title}",
        "",
        "- 状态：完整转写",
        f"- 时长：{_timestamp(duration_ms)}",
        f"- 段落：{len(segments)}",
        "",
        "## 完整逐字稿",
        "",
    ]
    for item in segments:
        lines.extend(
            [
                f"### {_timestamp(item['start_ms'])} · {item['speaker']}",
                "",
                item["text"],
                "",
            ]
        )
    body = "\n".join(lines).rstrip() + "\n"
    body_hash = _sha256_bytes(body.encode("utf-8"))
    marker = (
        "<!-- personal-context:generated transcript-markdown-v1 "
        f"body-sha256={body_hash} source-audio-sha256={source_audio_sha256} "
        f"segment-count={len(segments)} complete=true -->"
    )
    return RenderedMarkdown(
        text=body + marker + "\n",
        title=title,
        duration_ms=duration_ms,
        segment_count=len(segments),
        body_sha256=body_hash,
        source_audio_sha256=source_audio_sha256,
    )


def load_transcript(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TranscriptMarkdownError(f"Cannot read transcript.v1 JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise TranscriptMarkdownError("transcript.v1 must be a JSON object")
    return value


def _parse_generated(value: str) -> tuple[str, str, int]:
    lines = value.splitlines(keepends=True)
    if not lines:
        raise ManualEditError("Existing Markdown has no machine-generated integrity marker")
    marker_line = lines[-1].rstrip("\r\n")
    matched = _MARKER.fullmatch(marker_line)
    if not matched:
        raise ManualEditError("Existing Markdown has no valid machine-generated integrity marker")
    body = "".join(lines[:-1])
    body_hash, source_hash, count = matched.groups()
    if _sha256_bytes(body.encode("utf-8")) != body_hash:
        raise ManualEditError("Existing Markdown was edited after machine generation")
    return source_hash, body_hash, int(count)


def _safe_existing_fingerprint(path: Path, source_audio_sha256: str) -> Optional[str]:
    if path.is_symlink():
        raise ManualEditError(f"Refusing to replace unsafe Markdown path: {path}")
    if not path.exists():
        return None
    if not path.is_file():
        raise ManualEditError(f"Refusing to replace unsafe Markdown path: {path}")
    try:
        current = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ManualEditError(f"Cannot validate existing Markdown {path}: {exc}") from exc
    existing_source, _, _ = _parse_generated(current)
    if existing_source != source_audio_sha256:
        raise ManualEditError("Existing Markdown belongs to different audio content")
    return _sha256_bytes(current.encode("utf-8"))


def assert_safe_to_publish(path: Path, *, source_audio_sha256: str) -> None:
    _safe_existing_fingerprint(path, source_audio_sha256)


def generated_markdown_identity(path: Path) -> Optional[dict[str, Any]]:
    """Return the source identity for one intact generated delivery, never its body."""
    if path.is_symlink():
        raise ManualEditError(f"Refusing to inspect unsafe Markdown path: {path}")
    if not path.exists():
        return None
    if not path.is_file():
        raise ManualEditError(f"Refusing to inspect unsafe Markdown path: {path}")
    try:
        current = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ManualEditError(f"Cannot validate existing Markdown {path}: {exc}") from exc
    source_hash, body_hash, segment_count = _parse_generated(current)
    return {
        "source_audio_sha256": source_hash,
        "body_sha256": body_hash,
        "segments": segment_count,
    }


def generated_markdown_metadata(
    path: Path, *, source_audio_sha256: str
) -> Optional[dict[str, Any]]:
    """Return integrity metadata for one existing generated delivery, never its text."""
    fingerprint = _safe_existing_fingerprint(path, source_audio_sha256)
    if fingerprint is None:
        return None
    try:
        current = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ManualEditError(f"Cannot validate existing Markdown {path}: {exc}") from exc
    existing_source, body_hash, segment_count = _parse_generated(current)
    lines = current.splitlines()
    first_line = lines[0] if lines else ""
    title = first_line[2:].strip() if first_line.startswith("# ") else None
    return {
        "path": str(path.expanduser().absolute()),
        "bytes": len(current.encode("utf-8")),
        "sha256": fingerprint,
        "body_sha256": body_hash,
        "source_audio_sha256": existing_source,
        "segments": segment_count,
        "title": title,
    }


def publish_markdown(path: Path, rendered: RenderedMarkdown) -> dict[str, Any]:
    path = path.expanduser().absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    original_fingerprint = _safe_existing_fingerprint(path, rendered.source_audio_sha256)
    encoded = rendered.text.encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        current_fingerprint = _safe_existing_fingerprint(path, rendered.source_audio_sha256)
        if current_fingerprint != original_fingerprint:
            raise ManualEditError("Existing Markdown changed while a safe update was being prepared")
        os.replace(temporary, path)
        if os.name != "nt":
            os.chmod(path, 0o600)
        return {
            "path": str(path),
            "bytes": len(encoded),
            "sha256": _sha256_bytes(encoded),
            "body_sha256": rendered.body_sha256,
            "segments": rendered.segment_count,
        }
    finally:
        if temporary.exists():
            temporary.unlink()
