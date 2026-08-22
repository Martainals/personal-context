"""Validate Agent-authored note drafts and publish traceable Markdown atomically."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MARKER = re.compile(
    r"<!-- personal-context:generated note-markdown-v1 "
    r"body-sha256=([0-9a-f]{64}) source-audio-sha256=([0-9a-f]{64}) "
    r"transcript-body-sha256=([0-9a-f]{64}) complete=true -->"
)


class NoteMarkdownError(RuntimeError):
    """Invalid note draft or unsafe note publication."""


class ManualNoteEditError(NoteMarkdownError):
    """An existing note is no longer identical to generated output."""


@dataclass(frozen=True)
class RenderedNote:
    text: str
    title: str
    body_sha256: str
    source_audio_sha256: str
    transcript_body_sha256: str


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def render_note_markdown(
    draft: str,
    *,
    source_audio_sha256: str,
    transcript_body_sha256: str,
) -> RenderedNote:
    if not _SHA256.fullmatch(source_audio_sha256):
        raise NoteMarkdownError("source_audio_sha256 must be a lowercase SHA-256 digest")
    if not _SHA256.fullmatch(transcript_body_sha256):
        raise NoteMarkdownError("transcript_body_sha256 must be a lowercase SHA-256 digest")
    normalized = draft.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        raise NoteMarkdownError("Note draft must not be empty")
    if "personal-context:generated note-markdown-v1" in normalized:
        raise NoteMarkdownError("Note draft must not contain a generated integrity marker")
    first_line = normalized.splitlines()[0]
    if not first_line.startswith("# ") or not first_line[2:].strip():
        raise NoteMarkdownError("Note draft must begin with one Markdown H1 title")
    title = first_line[2:].strip()
    body = normalized + "\n"
    body_hash = _sha256_bytes(body.encode("utf-8"))
    marker = (
        "<!-- personal-context:generated note-markdown-v1 "
        f"body-sha256={body_hash} source-audio-sha256={source_audio_sha256} "
        f"transcript-body-sha256={transcript_body_sha256} complete=true -->"
    )
    return RenderedNote(
        text=body + marker + "\n",
        title=title,
        body_sha256=body_hash,
        source_audio_sha256=source_audio_sha256,
        transcript_body_sha256=transcript_body_sha256,
    )


def _parse_generated(value: str) -> tuple[str, str, str]:
    lines = value.splitlines(keepends=True)
    if not lines:
        raise ManualNoteEditError("Existing note has no machine-generated integrity marker")
    marker_line = lines[-1].rstrip("\r\n")
    matched = _MARKER.fullmatch(marker_line)
    if not matched:
        raise ManualNoteEditError("Existing note has no valid machine-generated integrity marker")
    body = "".join(lines[:-1])
    body_hash, source_hash, transcript_hash = matched.groups()
    if _sha256_bytes(body.encode("utf-8")) != body_hash:
        raise ManualNoteEditError("Existing note was edited after machine generation")
    return source_hash, transcript_hash, body_hash


def _safe_existing_fingerprint(
    path: Path,
    *,
    source_audio_sha256: str,
    transcript_body_sha256: str,
) -> Optional[str]:
    if path.is_symlink():
        raise ManualNoteEditError(f"Refusing to replace unsafe note path: {path}")
    if not path.exists():
        return None
    if not path.is_file():
        raise ManualNoteEditError(f"Refusing to replace unsafe note path: {path}")
    try:
        current = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ManualNoteEditError(f"Cannot validate existing note {path}: {exc}") from exc
    existing_source, existing_transcript, _ = _parse_generated(current)
    if existing_source != source_audio_sha256:
        raise ManualNoteEditError("Existing note belongs to different audio content")
    if existing_transcript != transcript_body_sha256:
        raise ManualNoteEditError("Existing note belongs to a different transcript revision")
    return _sha256_bytes(current.encode("utf-8"))


def generated_note_metadata(
    path: Path,
    *,
    source_audio_sha256: str,
    transcript_body_sha256: str,
) -> Optional[dict[str, Any]]:
    fingerprint = _safe_existing_fingerprint(
        path,
        source_audio_sha256=source_audio_sha256,
        transcript_body_sha256=transcript_body_sha256,
    )
    if fingerprint is None:
        return None
    current = path.read_text(encoding="utf-8")
    _, _, body_hash = _parse_generated(current)
    lines = current.splitlines()
    first_line = lines[0] if lines else ""
    title = first_line[2:].strip() if first_line.startswith("# ") else None
    return {
        "path": str(path.expanduser().absolute()),
        "bytes": len(current.encode("utf-8")),
        "sha256": fingerprint,
        "body_sha256": body_hash,
        "source_audio_sha256": source_audio_sha256,
        "transcript_body_sha256": transcript_body_sha256,
        "title": title,
    }


def publish_note_markdown(path: Path, rendered: RenderedNote) -> dict[str, Any]:
    path = path.expanduser().absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    original_fingerprint = _safe_existing_fingerprint(
        path,
        source_audio_sha256=rendered.source_audio_sha256,
        transcript_body_sha256=rendered.transcript_body_sha256,
    )
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
        current_fingerprint = _safe_existing_fingerprint(
            path,
            source_audio_sha256=rendered.source_audio_sha256,
            transcript_body_sha256=rendered.transcript_body_sha256,
        )
        if current_fingerprint != original_fingerprint:
            raise ManualNoteEditError("Existing note changed while a safe update was being prepared")
        os.replace(temporary, path)
        if os.name != "nt":
            os.chmod(path, 0o600)
        return {
            "path": str(path),
            "bytes": len(encoded),
            "sha256": _sha256_bytes(encoded),
            "body_sha256": rendered.body_sha256,
            "source_audio_sha256": rendered.source_audio_sha256,
            "transcript_body_sha256": rendered.transcript_body_sha256,
            "title": rendered.title,
        }
    finally:
        if temporary.exists():
            temporary.unlink()
