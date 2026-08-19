#!/usr/bin/env python3
"""Deterministic, local-first personal context database and CLI implementation."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Optional, Sequence, Union

from transcript_markdown import (
    ManualEditError,
    TranscriptMarkdownError,
    assert_safe_to_publish,
    generated_markdown_identity,
    generated_markdown_metadata,
    load_transcript,
    publish_markdown,
    render_transcript_markdown,
)


SCHEMA_VERSION = 1
MIN_SCHEMA_VERSION = 1
MAX_SCHEMA_VERSION = 1
ID_NAMESPACE = uuid.UUID("6e249f1a-7661-5fa8-8854-09541f14a9aa")
RECORD_KINDS = {"Fact", "Opinion", "Decision", "Action", "Claim"}
_RECORDING_TIMESTAMP = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})[ _T-](?P<hour>\d{2})[_:：-]"
    r"(?P<minute>\d{2})[_:：-](?P<second>\d{2})"
)
_GENERIC_DELIVERY_TITLE = re.compile(r"(?:[-_ ]*(?:录音转写|完整逐字稿|逐字稿|录音))+$")
EXPECTED_TABLES = {
    "schema_metadata",
    "processing_runs",
    "sources",
    "events",
    "segments",
    "entities",
    "statements",
    "candidate_memories",
    "memories",
    "relationships",
    "actions",
    "decisions",
    "claims",
    "reviews",
    "search_index",
    "migrations",
}


class ContextError(RuntimeError):
    """Expected, user-actionable failure."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_time(value: Optional[str], *, default: Optional[str] = None) -> str:
    if not value:
        return default or utc_now()
    raw = value.strip()
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContextError(f"Invalid ISO-8601 timestamp: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest_file(path: Path) -> tuple[str, int]:
    hasher = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(chunk)
            hasher.update(chunk)
    return hasher.hexdigest(), size


def digest_record(value: Any) -> str:
    return digest_bytes(canonical_json(value).encode("utf-8"))


def stable_id(prefix: str, value: Any) -> str:
    token = uuid.uuid5(ID_NAMESPACE, canonical_json(value)).hex[:24]
    return f"{prefix}_{token}"


def skill_version() -> str:
    return (Path(__file__).resolve().parent.parent / "VERSION").read_text(encoding="utf-8").strip()


def resolve_root(value: Union[str, os.PathLike]) -> Path:
    return Path(value).expanduser().resolve()


def db_path(root: Path) -> Path:
    return root / "context.sqlite3"


def connect(root: Path, *, readonly: bool = False) -> sqlite3.Connection:
    path = db_path(root)
    if not path.exists():
        raise ContextError(f"Database not found: {path}. Run init-vault first.")
    if readonly:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


SCHEMA_SQL = r"""
CREATE TABLE IF NOT EXISTS schema_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS processing_runs (
    id TEXT PRIMARY KEY,
    process_type TEXT NOT NULL,
    processor TEXT NOT NULL,
    processor_version TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running', 'completed', 'failed')),
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL UNIQUE,
    original_name TEXT NOT NULL,
    media_type TEXT,
    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
    stored_path TEXT NOT NULL UNIQUE,
    observed_at TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    schema_version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    title TEXT NOT NULL,
    event_type TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    valid_from TEXT,
    valid_to TEXT,
    content_hash TEXT NOT NULL,
    processing_run_id TEXT NOT NULL REFERENCES processing_runs(id),
    review_status TEXT NOT NULL DEFAULT 'observed',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    invalidated_at TEXT,
    supersedes_id TEXT REFERENCES events(id),
    schema_version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS segments (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    event_id TEXT NOT NULL REFERENCES events(id),
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    start_ms INTEGER CHECK(start_ms IS NULL OR start_ms >= 0),
    end_ms INTEGER CHECK(end_ms IS NULL OR end_ms >= start_ms),
    speaker TEXT,
    text TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    UNIQUE(source_id, ordinal)
);

CREATE TABLE IF NOT EXISTS entities (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    segment_id TEXT REFERENCES segments(id),
    event_id TEXT NOT NULL REFERENCES events(id),
    name TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    processing_run_id TEXT NOT NULL REFERENCES processing_runs(id),
    observed_at TEXT NOT NULL,
    valid_from TEXT,
    valid_to TEXT,
    review_status TEXT NOT NULL DEFAULT 'unreviewed',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    invalidated_at TEXT,
    supersedes_id TEXT REFERENCES entities(id),
    schema_version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS statements (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    segment_id TEXT REFERENCES segments(id),
    event_id TEXT NOT NULL REFERENCES events(id),
    speaker TEXT,
    kind TEXT NOT NULL CHECK(kind IN ('Fact', 'Opinion', 'Decision', 'Action', 'Claim')),
    text TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    processing_run_id TEXT NOT NULL REFERENCES processing_runs(id),
    observed_at TEXT NOT NULL,
    valid_from TEXT,
    valid_to TEXT,
    review_status TEXT NOT NULL DEFAULT 'unreviewed',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    invalidated_at TEXT,
    supersedes_id TEXT REFERENCES statements(id),
    schema_version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS candidate_memories (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    segment_id TEXT REFERENCES segments(id),
    event_id TEXT NOT NULL REFERENCES events(id),
    statement_id TEXT REFERENCES statements(id),
    proposed_kind TEXT NOT NULL CHECK(proposed_kind IN ('Fact', 'Opinion', 'Decision', 'Action', 'Claim')),
    content TEXT NOT NULL,
    rationale TEXT,
    content_hash TEXT NOT NULL,
    processing_run_id TEXT NOT NULL REFERENCES processing_runs(id),
    review_status TEXT NOT NULL DEFAULT 'pending' CHECK(review_status IN ('pending', 'approved', 'rejected')),
    observed_at TEXT NOT NULL,
    valid_from TEXT,
    valid_to TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    invalidated_at TEXT,
    supersedes_id TEXT REFERENCES candidate_memories(id),
    schema_version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL UNIQUE REFERENCES candidate_memories(id),
    source_id TEXT NOT NULL REFERENCES sources(id),
    segment_id TEXT REFERENCES segments(id),
    kind TEXT NOT NULL CHECK(kind IN ('Fact', 'Opinion', 'Decision', 'Action', 'Claim')),
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    review_status TEXT NOT NULL CHECK(review_status = 'approved'),
    approved_by TEXT NOT NULL,
    approved_at TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    valid_from TEXT,
    valid_to TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    invalidated_at TEXT,
    supersedes_id TEXT REFERENCES memories(id),
    schema_version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS relationships (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    segment_id TEXT REFERENCES segments(id),
    event_id TEXT NOT NULL REFERENCES events(id),
    from_entity_id TEXT NOT NULL REFERENCES entities(id),
    to_entity_id TEXT NOT NULL REFERENCES entities(id),
    relation_type TEXT NOT NULL,
    content TEXT,
    content_hash TEXT NOT NULL,
    processing_run_id TEXT NOT NULL REFERENCES processing_runs(id),
    observed_at TEXT NOT NULL,
    valid_from TEXT,
    valid_to TEXT,
    review_status TEXT NOT NULL DEFAULT 'unreviewed',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    invalidated_at TEXT,
    supersedes_id TEXT REFERENCES relationships(id),
    schema_version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS actions (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    segment_id TEXT REFERENCES segments(id),
    event_id TEXT NOT NULL REFERENCES events(id),
    statement_id TEXT REFERENCES statements(id),
    text TEXT NOT NULL,
    assignee_entity_id TEXT REFERENCES entities(id),
    due_at TEXT,
    action_status TEXT NOT NULL DEFAULT 'open',
    content_hash TEXT NOT NULL,
    processing_run_id TEXT NOT NULL REFERENCES processing_runs(id),
    observed_at TEXT NOT NULL,
    valid_from TEXT,
    valid_to TEXT,
    review_status TEXT NOT NULL DEFAULT 'unreviewed',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    invalidated_at TEXT,
    supersedes_id TEXT REFERENCES actions(id),
    schema_version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    segment_id TEXT REFERENCES segments(id),
    event_id TEXT NOT NULL REFERENCES events(id),
    statement_id TEXT REFERENCES statements(id),
    text TEXT NOT NULL,
    decided_by_entity_id TEXT REFERENCES entities(id),
    content_hash TEXT NOT NULL,
    processing_run_id TEXT NOT NULL REFERENCES processing_runs(id),
    observed_at TEXT NOT NULL,
    valid_from TEXT,
    valid_to TEXT,
    review_status TEXT NOT NULL DEFAULT 'unreviewed',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    invalidated_at TEXT,
    supersedes_id TEXT REFERENCES decisions(id),
    schema_version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS claims (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    segment_id TEXT REFERENCES segments(id),
    event_id TEXT NOT NULL REFERENCES events(id),
    statement_id TEXT REFERENCES statements(id),
    text TEXT NOT NULL,
    claimant TEXT,
    claim_status TEXT NOT NULL DEFAULT 'reported' CHECK(claim_status IN ('reported', 'verified', 'disputed')),
    supporting_source_count INTEGER NOT NULL DEFAULT 1,
    counter_source_count INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT NOT NULL,
    processing_run_id TEXT NOT NULL REFERENCES processing_runs(id),
    observed_at TEXT NOT NULL,
    valid_from TEXT,
    valid_to TEXT,
    review_status TEXT NOT NULL DEFAULT 'unreviewed',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    invalidated_at TEXT,
    supersedes_id TEXT REFERENCES claims(id),
    schema_version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS reviews (
    id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES candidate_memories(id),
    decision TEXT NOT NULL CHECK(decision IN ('approve', 'reject')),
    reviewer TEXT NOT NULL,
    reason TEXT,
    reviewed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS search_index (
    record_type TEXT NOT NULL,
    record_id TEXT NOT NULL,
    text TEXT NOT NULL,
    source_id TEXT NOT NULL,
    segment_id TEXT,
    kind TEXT,
    PRIMARY KEY(record_type, record_id)
);

CREATE TABLE IF NOT EXISTS migrations (
    id TEXT PRIMARY KEY,
    from_version INTEGER NOT NULL,
    to_version INTEGER NOT NULL,
    applied_at TEXT NOT NULL,
    report_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_segments_source ON segments(source_id);
CREATE INDEX IF NOT EXISTS idx_statements_event ON statements(event_id);
CREATE INDEX IF NOT EXISTS idx_candidates_status ON candidate_memories(review_status);
CREATE INDEX IF NOT EXISTS idx_memories_source ON memories(source_id);
CREATE INDEX IF NOT EXISTS idx_search_text ON search_index(text);

CREATE TRIGGER IF NOT EXISTS sources_no_update
BEFORE UPDATE ON sources BEGIN SELECT RAISE(ABORT, 'Source evidence is immutable'); END;
CREATE TRIGGER IF NOT EXISTS sources_no_delete
BEFORE DELETE ON sources BEGIN SELECT RAISE(ABORT, 'Source evidence is immutable'); END;
CREATE TRIGGER IF NOT EXISTS segments_no_update
BEFORE UPDATE ON segments BEGIN SELECT RAISE(ABORT, 'Segment evidence is immutable'); END;
CREATE TRIGGER IF NOT EXISTS segments_no_delete
BEFORE DELETE ON segments BEGIN SELECT RAISE(ABORT, 'Segment evidence is immutable'); END;
CREATE TRIGGER IF NOT EXISTS memories_no_update
BEFORE UPDATE ON memories BEGIN SELECT RAISE(ABORT, 'Approved memories cannot be silently overwritten'); END;
CREATE TRIGGER IF NOT EXISTS memories_no_delete
BEFORE DELETE ON memories BEGIN SELECT RAISE(ABORT, 'Approved memories require an auditable supersession'); END;
CREATE TRIGGER IF NOT EXISTS reviews_no_update
BEFORE UPDATE ON reviews BEGIN SELECT RAISE(ABORT, 'Review history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS reviews_no_delete
BEFORE DELETE ON reviews BEGIN SELECT RAISE(ABORT, 'Review history is append-only'); END;
"""


def table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def read_schema_version(connection: sqlite3.Connection) -> Optional[int]:
    if "schema_metadata" not in table_names(connection):
        return None
    row = connection.execute("SELECT value FROM schema_metadata WHERE key='schema_version'").fetchone()
    if not row:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return None


def schema_state(root: Path) -> dict[str, Any]:
    path = db_path(root)
    if not path.exists():
        return {"status": "missing", "version": None, "writable": False}
    try:
        with connect(root, readonly=True) as connection:
            version = read_schema_version(connection)
            missing = sorted(EXPECTED_TABLES - table_names(connection))
    except sqlite3.DatabaseError as exc:
        return {"status": "damaged", "version": None, "writable": False, "error": str(exc)}
    if version is None:
        status = "unknown"
    elif version < MIN_SCHEMA_VERSION:
        status = "older"
    elif version > MAX_SCHEMA_VERSION:
        status = "newer"
    elif missing:
        status = "damaged"
    elif version == SCHEMA_VERSION:
        status = "current"
    else:
        status = "compatible"
    return {"status": status, "version": version, "writable": status == "current", "missing_tables": missing}


def require_writable(root: Path) -> None:
    state = schema_state(root)
    messages = {
        "missing": "Database is missing; run init-vault.",
        "older": "Database schema is too old; writes are disabled until backup and migration.",
        "newer": "Database schema is newer than this Skill; refusing to write.",
        "unknown": "Database schema is unknown; only audit is safe.",
        "damaged": "Database schema is damaged or incomplete; only audit is safe.",
    }
    if not state["writable"]:
        raise ContextError(messages.get(state["status"], f"Database is not writable: {state['status']}"))


def initialize_schema(connection: sqlite3.Connection, *, version: int = SCHEMA_VERSION) -> None:
    connection.executescript(SCHEMA_SQL)
    now = utc_now()
    values = {
        "schema_version": str(version),
        "min_compatible_schema": str(MIN_SCHEMA_VERSION),
        "max_compatible_schema": str(MAX_SCHEMA_VERSION),
        "created_by_skill_version": skill_version(),
    }
    for key, value in values.items():
        connection.execute(
            "INSERT INTO schema_metadata(key, value, updated_at) VALUES(?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, now),
        )


def init_vault(root: Path) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    path = db_path(root)
    if path.exists():
        state = schema_state(root)
        if state["status"] == "current":
            return {"status": "already_initialized", "root": str(root), "schema_version": SCHEMA_VERSION}
        raise ContextError(f"Existing database is {state['status']}; refusing to initialize over it.")
    created_dirs: list[Path] = []
    try:
        for relative in ("blobs", "inbox", "wiki", "backups"):
            directory = root / relative
            directory.mkdir(exist_ok=True)
            created_dirs.append(directory)
        connection = sqlite3.connect(path)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            with connection:
                initialize_schema(connection)
        finally:
            connection.close()
    except Exception:
        if path.exists():
            path.unlink()
        for directory in reversed(created_dirs):
            try:
                directory.rmdir()
            except OSError:
                pass
        raise
    return {"status": "initialized", "root": str(root), "schema_version": SCHEMA_VERSION}


def doctor(root: Path) -> dict[str, Any]:
    state = schema_state(root)
    checks: list[dict[str, Any]] = []
    checks.append({"name": "root", "ok": root.is_dir(), "path": str(root)})
    checks.append({"name": "database", "ok": db_path(root).is_file(), "path": str(db_path(root))})
    for relative in ("blobs", "inbox", "wiki", "backups"):
        checks.append({"name": relative, "ok": (root / relative).is_dir(), "path": str(root / relative)})
    checks.append({"name": "schema", "ok": state["status"] == "current", **state})
    if db_path(root).exists() and state["status"] not in {"damaged", "unknown"}:
        try:
            with connect(root, readonly=True) as connection:
                result = connection.execute("PRAGMA integrity_check").fetchone()[0]
                checks.append({"name": "sqlite_integrity", "ok": result == "ok", "result": result})
        except sqlite3.DatabaseError as exc:
            checks.append({"name": "sqlite_integrity", "ok": False, "error": str(exc)})
    return {"ok": all(item["ok"] for item in checks), "checks": checks, "schema": state}


def _blob_relative(content_hash: str) -> Path:
    return Path("blobs") / content_hash[:2] / content_hash


def ingest(root: Path, paths: Sequence[Path], *, observed_at: Optional[str], dry_run: bool) -> dict[str, Any]:
    require_writable(root)
    if not paths:
        raise ContextError("At least one input file is required.")
    prepared: list[dict[str, Any]] = []
    for raw_path in paths:
        path = raw_path.expanduser().resolve()
        if not path.is_file():
            raise ContextError(f"Input is not a file: {path}")
        content_hash, size = digest_file(path)
        prepared.append(
            {
                "path": path,
                "hash": content_hash,
                "size": size,
                "observed_at": normalize_time(observed_at),
                "source_id": f"src_{content_hash[:24]}",
                "stored_path": _blob_relative(content_hash),
                "media_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            }
        )
    with connect(root, readonly=True) as connection:
        existing = {
            row[0]
            for row in connection.execute(
                f"SELECT content_hash FROM sources WHERE content_hash IN ({','.join('?' for _ in prepared)})",
                [item["hash"] for item in prepared],
            )
        }
    preview = [
        {
            "path": str(item["path"]),
            "source_id": item["source_id"],
            "content_hash": item["hash"],
            "action": "duplicate" if item["hash"] in existing else "ingest",
        }
        for item in prepared
    ]
    if dry_run:
        return {"dry_run": True, "items": preview}
    created_blobs: list[Path] = []
    try:
        for item in prepared:
            destination = root / item["stored_path"]
            if destination.exists():
                if digest_file(destination)[0] != item["hash"]:
                    raise ContextError(f"Blob hash collision or corruption at {destination}")
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(prefix=".incoming-", dir=destination.parent)
            os.close(fd)
            temp_path = Path(temp_name)
            try:
                shutil.copyfile(item["path"], temp_path)
                if digest_file(temp_path)[0] != item["hash"]:
                    raise ContextError(f"Copied content failed hash verification: {item['path']}")
                os.replace(temp_path, destination)
                created_blobs.append(destination)
            finally:
                if temp_path.exists():
                    temp_path.unlink()
        with connect(root) as connection, connection:
            for item in prepared:
                connection.execute(
                    "INSERT OR IGNORE INTO sources(id, content_hash, original_name, media_type, size_bytes, "
                    "stored_path, observed_at, imported_at, schema_version) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item["source_id"], item["hash"], item["path"].name, item["media_type"],
                        item["size"], item["stored_path"].as_posix(), item["observed_at"], utc_now(), SCHEMA_VERSION,
                    ),
                )
    except Exception:
        for destination in created_blobs:
            try:
                destination.unlink()
            except OSError:
                pass
        raise
    return {"dry_run": False, "items": preview}


def _validate_transcript(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ContextError("Transcript must be a JSON object.")
    event = data.get("event", {})
    if not isinstance(event, dict):
        raise ContextError("event must be an object.")
    segments = data.get("segments")
    if not isinstance(segments, list) or not segments:
        raise ContextError("segments must be a non-empty array.")
    cleaned_segments: list[dict[str, Any]] = []
    for ordinal, segment in enumerate(segments):
        if not isinstance(segment, dict) or not isinstance(segment.get("text"), str) or not segment["text"].strip():
            raise ContextError(f"segments[{ordinal}].text must be a non-empty string.")
        start = segment.get("start_ms")
        end = segment.get("end_ms")
        if start is not None and (not isinstance(start, int) or start < 0):
            raise ContextError(f"segments[{ordinal}].start_ms must be a non-negative integer.")
        if end is not None and (not isinstance(end, int) or end < 0 or (start is not None and end < start)):
            raise ContextError(f"segments[{ordinal}].end_ms must be >= start_ms.")
        cleaned_segments.append({"text": segment["text"].strip(), "speaker": segment.get("speaker"), "start_ms": start, "end_ms": end})
    cleaned: dict[str, Any] = {
        "event": {
            "title": str(event.get("title") or data.get("title") or "Imported transcript"),
            "type": str(event.get("type") or "conversation"),
            "observed_at": normalize_time(event.get("observed_at") or data.get("observed_at")),
            "valid_from": normalize_time(event["valid_from"]) if event.get("valid_from") else None,
            "valid_to": normalize_time(event["valid_to"]) if event.get("valid_to") else None,
        },
        "segments": cleaned_segments,
        "processing": data.get("processing", {}),
    }
    if not isinstance(cleaned["processing"], dict):
        raise ContextError("processing must be an object when provided.")
    for collection in ("entities", "statements", "decisions", "actions", "claims", "relationships", "candidate_memories"):
        value = data.get(collection, [])
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise ContextError(f"{collection} must be an array of objects.")
        cleaned[collection] = value
    for collection in ("statements", "decisions", "actions", "claims", "candidate_memories", "entities", "relationships"):
        for index, item in enumerate(cleaned[collection]):
            segment = item.get("segment")
            if segment is not None and (not isinstance(segment, int) or segment < 0 or segment >= len(cleaned_segments)):
                raise ContextError(f"{collection}[{index}].segment is out of range.")
    for index, item in enumerate(cleaned["statements"]):
        if item.get("kind", "Claim") not in RECORD_KINDS:
            raise ContextError(f"statements[{index}].kind is invalid.")
        if not str(item.get("text", "")).strip():
            raise ContextError(f"statements[{index}].text is required.")
    for collection in ("decisions", "actions", "claims", "candidate_memories"):
        for index, item in enumerate(cleaned[collection]):
            key = "content" if collection == "candidate_memories" else "text"
            if not str(item.get(key, "")).strip():
                raise ContextError(f"{collection}[{index}].{key} is required.")
    for index, item in enumerate(cleaned["candidate_memories"]):
        if item.get("kind", "Claim") not in RECORD_KINDS:
            raise ContextError(f"candidate_memories[{index}].kind is invalid.")
        statement_index = item.get("statement")
        if statement_index is not None and (
            not isinstance(statement_index, int)
            or statement_index < 0
            or statement_index >= len(cleaned["statements"])
        ):
            raise ContextError(f"candidate_memories[{index}].statement is out of range.")
    for index, item in enumerate(cleaned["entities"]):
        if not str(item.get("name", "")).strip():
            raise ContextError(f"entities[{index}].name is required.")
    for index, item in enumerate(cleaned["relationships"]):
        if not str(item.get("from", "")).strip() or not str(item.get("to", "")).strip() or not str(item.get("type", "")).strip():
            raise ContextError(f"relationships[{index}] requires from, to, and type.")
    return cleaned


def _segment_id(source_id: str, ordinal: int, segment: dict[str, Any]) -> str:
    return stable_id("seg", [source_id, ordinal, segment])


def _segment_ref(segment_ids: list[str], item: dict[str, Any]) -> Optional[str]:
    index = item.get("segment")
    return segment_ids[index] if index is not None else None


def _statement_insert(
    connection: sqlite3.Connection,
    *, source_id: str,
    event_id: str,
    segment_id: Optional[str],
    speaker: Optional[str],
    kind: str,
    text: str,
    run_id: str,
    observed_at: str,
    valid_from: Optional[str] = None,
    valid_to: Optional[str] = None,
) -> str:
    payload = [source_id, event_id, segment_id, speaker, kind, text, valid_from, valid_to]
    record_id = stable_id("stm", payload)
    now = utc_now()
    connection.execute(
        "INSERT OR IGNORE INTO statements(id, source_id, segment_id, event_id, speaker, kind, text, content_hash, "
        "processing_run_id, observed_at, valid_from, valid_to, created_at, updated_at, schema_version) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (record_id, source_id, segment_id, event_id, speaker, kind, text, digest_record(payload), run_id,
         observed_at, valid_from, valid_to, now, now, SCHEMA_VERSION),
    )
    return record_id


def import_transcript(
    root: Path,
    transcript_path: Path,
    *,
    source_id: Optional[str],
    dry_run: bool,
    fail_after: Optional[str] = None,
) -> dict[str, Any]:
    require_writable(root)
    path = transcript_path.expanduser().resolve()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContextError(f"Cannot read structured transcript {path}: {exc}") from exc
    cleaned = _validate_transcript(data)
    transcript_hash, _ = digest_file(path)
    source_preview: Optional[dict[str, Any]] = None
    effective_source_id = source_id
    if source_id:
        with connect(root, readonly=True) as connection:
            if not connection.execute("SELECT 1 FROM sources WHERE id=?", (source_id,)).fetchone():
                raise ContextError(f"Unknown source_id: {source_id}")
    else:
        effective_source_id = f"src_{transcript_hash[:24]}"
        source_preview = ingest(root, [path], observed_at=cleaned["event"]["observed_at"], dry_run=True)["items"][0]
    assert effective_source_id is not None
    counts = {
        "segments": len(cleaned["segments"]),
        "statements": len(cleaned["segments"]) + len(cleaned["statements"]) + len(cleaned["decisions"]) + len(cleaned["actions"]) + len(cleaned["claims"]),
        "entities": len(cleaned["entities"]),
        "decisions": len(cleaned["decisions"]),
        "actions": len(cleaned["actions"]),
        "claims": len(cleaned["claims"]),
        "relationships": len(cleaned["relationships"]),
        "candidate_memories": len(cleaned["candidate_memories"]),
    }
    event_payload = [effective_source_id, cleaned["event"]]
    event_id = stable_id("evt", event_payload)
    if dry_run:
        return {"dry_run": True, "source": source_preview or {"source_id": effective_source_id, "action": "use_existing"}, "event_id": event_id, "counts": counts}
    if not source_id:
        ingest(root, [path], observed_at=cleaned["event"]["observed_at"], dry_run=False)
    now = utc_now()
    run_payload = [effective_source_id, transcript_hash, "structured-transcript-v1", cleaned["processing"]]
    run_id = stable_id("run", run_payload)
    segment_ids = [_segment_id(effective_source_id, ordinal, segment) for ordinal, segment in enumerate(cleaned["segments"])]
    with connect(root) as connection, connection:
        connection.execute(
            "INSERT OR IGNORE INTO processing_runs(id, process_type, processor, processor_version, input_hash, "
            "parameters_json, status, started_at, finished_at) VALUES(?, 'import-transcript', 'personal-context', ?, ?, ?, 'completed', ?, ?)",
            (
                run_id,
                skill_version(),
                transcript_hash,
                canonical_json({"format": "structured-json-v1", "upstream": cleaned["processing"]}),
                now,
                now,
            ),
        )
        event = cleaned["event"]
        connection.execute(
            "INSERT OR IGNORE INTO events(id, source_id, title, event_type, observed_at, valid_from, valid_to, "
            "content_hash, processing_run_id, created_at, updated_at, schema_version) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (event_id, effective_source_id, event["title"], event["type"], event["observed_at"], event["valid_from"],
             event["valid_to"], digest_record(event_payload), run_id, now, now, SCHEMA_VERSION),
        )
        for ordinal, segment in enumerate(cleaned["segments"]):
            segment_payload = [effective_source_id, event_id, ordinal, segment]
            connection.execute(
                "INSERT OR IGNORE INTO segments(id, source_id, event_id, ordinal, start_ms, end_ms, speaker, text, "
                "content_hash, observed_at, created_at, schema_version) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (segment_ids[ordinal], effective_source_id, event_id, ordinal, segment["start_ms"], segment["end_ms"],
                 segment["speaker"], segment["text"], digest_record(segment_payload), event["observed_at"], now, SCHEMA_VERSION),
            )
            # Speech is evidence that a claim was uttered; it is never promoted to Fact automatically.
            _statement_insert(connection, source_id=effective_source_id, event_id=event_id,
                              segment_id=segment_ids[ordinal], speaker=segment["speaker"], kind="Claim",
                              text=segment["text"], run_id=run_id, observed_at=event["observed_at"])
        if fail_after == "segments":
            raise ContextError("Simulated import failure")
        entity_ids: dict[str, str] = {}

        def ensure_entity(name: str, entity_type: str = "Person", segment_id: Optional[str] = None) -> str:
            canonical_name = name.strip()
            payload = [effective_source_id, event_id, segment_id, canonical_name, entity_type]
            entity_id = stable_id("ent", payload)
            connection.execute(
                "INSERT OR IGNORE INTO entities(id, source_id, segment_id, event_id, name, entity_type, canonical_name, "
                "content_hash, processing_run_id, observed_at, created_at, updated_at, schema_version) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (entity_id, effective_source_id, segment_id, event_id, canonical_name, entity_type, canonical_name,
                 digest_record(payload), run_id, event["observed_at"], now, now, SCHEMA_VERSION),
            )
            entity_ids[canonical_name] = entity_id
            return entity_id

        for item in cleaned["entities"]:
            ensure_entity(str(item["name"]), str(item.get("type") or "Person"), _segment_ref(segment_ids, item))
        explicit_statement_ids: list[str] = []
        for item in cleaned["statements"]:
            explicit_statement_ids.append(_statement_insert(
                connection, source_id=effective_source_id, event_id=event_id,
                segment_id=_segment_ref(segment_ids, item), speaker=item.get("speaker"),
                kind=item.get("kind", "Claim"), text=str(item["text"]).strip(), run_id=run_id,
                observed_at=event["observed_at"],
                valid_from=normalize_time(item["valid_from"]) if item.get("valid_from") else None,
                valid_to=normalize_time(item["valid_to"]) if item.get("valid_to") else None,
            ))
        for collection, kind, table in (("decisions", "Decision", "decisions"), ("actions", "Action", "actions"), ("claims", "Claim", "claims")):
            for item in cleaned[collection]:
                text = str(item["text"]).strip()
                segment_id = _segment_ref(segment_ids, item)
                statement_id = _statement_insert(
                    connection, source_id=effective_source_id, event_id=event_id, segment_id=segment_id,
                    speaker=item.get("speaker") or item.get("claimant"), kind=kind, text=text, run_id=run_id,
                    observed_at=event["observed_at"],
                )
                payload = [effective_source_id, event_id, segment_id, text, item]
                record_id = stable_id({"decisions": "dec", "actions": "act", "claims": "clm"}[collection], payload)
                common = (record_id, effective_source_id, segment_id, event_id, statement_id, text)
                if table == "decisions":
                    actor_id = ensure_entity(str(item["decided_by"]), segment_id=segment_id) if item.get("decided_by") else None
                    connection.execute(
                        "INSERT OR IGNORE INTO decisions(id, source_id, segment_id, event_id, statement_id, text, "
                        "decided_by_entity_id, content_hash, processing_run_id, observed_at, created_at, updated_at, schema_version) "
                        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        common + (actor_id, digest_record(payload), run_id, event["observed_at"], now, now, SCHEMA_VERSION),
                    )
                elif table == "actions":
                    assignee_id = ensure_entity(str(item["assignee"]), segment_id=segment_id) if item.get("assignee") else None
                    due_at = normalize_time(item["due_at"]) if item.get("due_at") else None
                    connection.execute(
                        "INSERT OR IGNORE INTO actions(id, source_id, segment_id, event_id, statement_id, text, "
                        "assignee_entity_id, due_at, action_status, content_hash, processing_run_id, observed_at, created_at, updated_at, schema_version) "
                        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        common + (assignee_id, due_at, str(item.get("status") or "open"), digest_record(payload), run_id,
                                  event["observed_at"], now, now, SCHEMA_VERSION),
                    )
                else:
                    connection.execute(
                        "INSERT OR IGNORE INTO claims(id, source_id, segment_id, event_id, statement_id, text, claimant, "
                        "claim_status, supporting_source_count, counter_source_count, content_hash, processing_run_id, "
                        "observed_at, created_at, updated_at, schema_version) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        common + (item.get("claimant") or item.get("speaker"), str(item.get("status") or "reported"),
                                  int(item.get("supporting_source_count", 1)), int(item.get("counter_source_count", 0)),
                                  digest_record(payload), run_id, event["observed_at"], now, now, SCHEMA_VERSION),
                    )
        for item in cleaned["relationships"]:
            segment_id = _segment_ref(segment_ids, item)
            from_id = entity_ids.get(str(item["from"]).strip()) or ensure_entity(str(item["from"]), segment_id=segment_id)
            to_id = entity_ids.get(str(item["to"]).strip()) or ensure_entity(str(item["to"]), segment_id=segment_id)
            payload = [effective_source_id, event_id, segment_id, from_id, to_id, item["type"], item.get("content")]
            relationship_id = stable_id("rel", payload)
            connection.execute(
                "INSERT OR IGNORE INTO relationships(id, source_id, segment_id, event_id, from_entity_id, to_entity_id, "
                "relation_type, content, content_hash, processing_run_id, observed_at, created_at, updated_at, schema_version) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (relationship_id, effective_source_id, segment_id, event_id, from_id, to_id, str(item["type"]),
                 item.get("content"), digest_record(payload), run_id, event["observed_at"], now, now, SCHEMA_VERSION),
            )
        for item in cleaned["candidate_memories"]:
            segment_id = _segment_ref(segment_ids, item)
            statement_id = None
            if isinstance(item.get("statement"), int):
                statement_index = item["statement"]
                if statement_index < 0 or statement_index >= len(explicit_statement_ids):
                    raise ContextError("candidate_memories.statement is out of range.")
                statement_id = explicit_statement_ids[statement_index]
            content = str(item["content"]).strip()
            kind = str(item.get("kind") or "Claim")
            payload = [effective_source_id, event_id, segment_id, statement_id, kind, content]
            candidate_id = stable_id("cand", payload)
            connection.execute(
                "INSERT OR IGNORE INTO candidate_memories(id, source_id, segment_id, event_id, statement_id, "
                "proposed_kind, content, rationale, content_hash, processing_run_id, observed_at, valid_from, valid_to, "
                "created_at, updated_at, schema_version) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (candidate_id, effective_source_id, segment_id, event_id, statement_id, kind, content, item.get("rationale"),
                 digest_record(payload), run_id, event["observed_at"],
                 normalize_time(item["valid_from"]) if item.get("valid_from") else None,
                 normalize_time(item["valid_to"]) if item.get("valid_to") else None,
                 now, now, SCHEMA_VERSION),
            )
        rebuild_search_index(connection)
    return {"dry_run": False, "source_id": effective_source_id, "event_id": event_id, "counts": counts}


def _filename_recorded_at(audio_path: Path) -> Optional[dt.datetime]:
    matched = _RECORDING_TIMESTAMP.match(audio_path.stem)
    if matched is None:
        return None
    try:
        naive = dt.datetime.strptime(
            f"{matched.group('date')} {matched.group('hour')}:{matched.group('minute')}:"
            f"{matched.group('second')}",
            "%Y-%m-%d %H:%M:%S",
        )
    except ValueError:
        return None
    local_zone = dt.datetime.now().astimezone().tzinfo or dt.timezone.utc
    return naive.replace(tzinfo=local_zone)


def _capture_observed_at(root: Path, source_id: str, audio_path: Path) -> str:
    with connect(root, readonly=True) as connection:
        existing = connection.execute(
            "SELECT observed_at FROM sources WHERE id=?", (source_id,)
        ).fetchone()
    if existing:
        return str(existing[0])
    timestamp = _filename_recorded_at(audio_path)
    if timestamp is None:
        timestamp = dt.datetime.fromtimestamp(audio_path.stat().st_mtime, tz=dt.timezone.utc)
    return timestamp.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _delivery_title(value: Optional[str]) -> str:
    cleaned = _GENERIC_DELIVERY_TITLE.sub("", str(value or "").strip())
    cleaned = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", " ", cleaned)
    cleaned = " ".join(cleaned.split()).strip(" .-")
    return (cleaned or "未命名主题")[:32].rstrip()


def _delivery_filename(audio_path: Path, title: str, observed_at: str) -> str:
    matched = _RECORDING_TIMESTAMP.match(audio_path.stem)
    if matched is not None:
        date = matched.group("date")
        hour = matched.group("hour")
        minute = matched.group("minute")
        second = matched.group("second")
    else:
        parsed = dt.datetime.fromisoformat(observed_at.replace("Z", "+00:00")).astimezone()
        date = parsed.strftime("%Y-%m-%d")
        hour = parsed.strftime("%H")
        minute = parsed.strftime("%M")
        second = parsed.strftime("%S")
    return f"{date} {hour}：{minute}：{second}-{_delivery_title(title)}.md"


def _available_delivery_path(base_path: Path, source_hash: str) -> Path:
    """Select the stable path for this audio without overwriting another delivery."""
    for number in range(1, 1000):
        candidate = (
            base_path
            if number == 1
            else base_path.with_name(f"{base_path.stem}-{number}{base_path.suffix}")
        )
        try:
            identity = generated_markdown_identity(candidate)
        except ManualEditError as exc:
            raise ContextError(str(exc)) from exc
        if identity is None or identity["source_audio_sha256"] == source_hash:
            return candidate
    raise ContextError("Too many transcript deliveries share the same recording time and title")


def _latest_source_event(root: Path, source_id: str) -> Optional[dict[str, str]]:
    with connect(root, readonly=True) as connection:
        row = connection.execute(
            "SELECT id, title, observed_at FROM events "
            "WHERE source_id=? AND invalidated_at IS NULL "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (source_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "event_id": str(row["id"]),
        "title": str(row["title"]),
        "observed_at": str(row["observed_at"]),
    }


def _matching_delivery(
    root: Path,
    output_path: Path,
    *,
    source_id: str,
    source_hash: str,
) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    try:
        assert_safe_to_publish(output_path, source_audio_sha256=source_hash)
        delivery = generated_markdown_metadata(
            output_path, source_audio_sha256=source_hash
        )
    except ManualEditError as exc:
        raise ContextError(str(exc)) from exc
    existing = (
        _existing_delivery_record(
            root,
            source_id=source_id,
            source_hash=source_hash,
            segment_count=int(delivery["segments"]),
            title=str(delivery["title"]) if delivery.get("title") else None,
        )
        if delivery is not None
        else None
    )
    return delivery, existing


def _existing_event_matches(
    root: Path, event_id: str, source_id: str, transcript: dict[str, Any]
) -> Optional[bool]:
    cleaned = _validate_transcript(transcript)
    expected = [
        {
            "ordinal": ordinal,
            "start_ms": item["start_ms"],
            "end_ms": item["end_ms"],
            "speaker": item["speaker"],
            "text": item["text"],
        }
        for ordinal, item in enumerate(cleaned["segments"])
    ]
    with connect(root, readonly=True) as connection:
        event = connection.execute(
            "SELECT source_id FROM events WHERE id=?", (event_id,)
        ).fetchone()
        if event is None:
            return None
        if event[0] != source_id:
            return False
        actual = [
            dict(row)
            for row in connection.execute(
                "SELECT ordinal, start_ms, end_ms, speaker, text FROM segments "
                "WHERE event_id=? ORDER BY ordinal",
                (event_id,),
            ).fetchall()
        ]
    return actual == expected


def _existing_delivery_record(
    root: Path,
    *,
    source_id: str,
    source_hash: str,
    segment_count: int,
    title: Optional[str],
) -> Optional[dict[str, Any]]:
    with connect(root, readonly=True) as connection:
        row = connection.execute(
            "SELECT e.id, e.title, e.observed_at, COUNT(sg.id) AS segment_count "
            "FROM events e JOIN sources s ON s.id=e.source_id "
            "LEFT JOIN segments sg ON sg.event_id=e.id "
            "WHERE s.id=? AND s.content_hash=? AND e.invalidated_at IS NULL "
            "AND (? IS NULL OR e.title=?) "
            "GROUP BY e.id, e.title, e.observed_at "
            "HAVING COUNT(sg.id)=? ORDER BY e.created_at DESC, e.id DESC LIMIT 1",
            (source_id, source_hash, title, title, segment_count),
        ).fetchone()
        if row is None:
            return None
        counts = {"events": 1}
        for table in (
            "segments",
            "statements",
            "entities",
            "decisions",
            "actions",
            "claims",
            "relationships",
            "candidate_memories",
        ):
            counts[table] = connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE event_id=?", (row["id"],)
            ).fetchone()[0]
    return {
        "event_id": str(row["id"]),
        "title": str(row["title"]),
        "observed_at": str(row["observed_at"]),
        "counts": counts,
    }


def capture_audio(
    root: Path,
    *,
    config_dir: Path,
    provider: str,
    agent_host: Optional[str],
    audio: Path,
    language: Optional[str],
    title: Optional[str],
    observed_at: Optional[str],
    speaker_count: Optional[int] = None,
    rerun: bool = False,
    check_only: bool = False,
) -> dict[str, Any]:
    """Capture one audio Source, import evidence, and publish only complete Markdown."""
    import personal_context_bootstrap as onboarding

    root = root.expanduser().resolve()
    config_dir = config_dir.expanduser().resolve()
    health = doctor(root)
    if not health["ok"]:
        raise ContextError("Vault is not ready for capture-audio; run doctor and repair it first.")
    audio_path = audio.expanduser().resolve()
    if not audio_path.is_file():
        raise ContextError(f"Input is not a file: {audio_path}")

    preview = ingest(root, [audio_path], observed_at=observed_at, dry_run=True)["items"][0]
    source_id = str(preview["source_id"])
    source_hash = str(preview["content_hash"])
    effective_observed_at = normalize_time(observed_at) if observed_at else _capture_observed_at(
        root, source_id, audio_path
    )
    inbox_path = root / "inbox"
    if inbox_path.is_symlink():
        raise ContextError("Vault inbox must be a real directory, not a symbolic link.")
    source_event = _latest_source_event(root, source_id)
    effective_title = _delivery_title(
        source_event["title"] if source_event is not None else title
    )
    if source_event is not None:
        effective_observed_at = source_event["observed_at"]
    output_path = _available_delivery_path(
        inbox_path / _delivery_filename(audio_path, effective_title, effective_observed_at),
        source_hash,
    )
    legacy_output_path = inbox_path / f"{audio_path.stem or '录音'}-录音转写.md"
    delivery: Optional[dict[str, Any]] = None
    existing_delivery: Optional[dict[str, Any]] = None
    for candidate in dict.fromkeys((output_path, legacy_output_path)):
        candidate_delivery, candidate_existing = _matching_delivery(
            root,
            candidate,
            source_id=source_id,
            source_hash=source_hash,
        )
        if candidate_delivery is not None and candidate_existing is not None:
            output_path = candidate
            delivery = candidate_delivery
            existing_delivery = candidate_existing
            break
    if delivery is not None and existing_delivery is not None:
        if rerun:
            if title is not None and _delivery_title(title) != _delivery_title(
                existing_delivery["title"]
            ):
                raise ContextError(
                    "Rerun title differs from the existing recording event; refusing to create a second event."
                )
            if observed_at is not None and normalize_time(observed_at) != existing_delivery["observed_at"]:
                raise ContextError(
                    "Rerun observed_at differs from the existing recording event; refusing to create a second event."
                )
        else:
            return {
                "status": "already_delivered",
                "source_id": source_id,
                "event_id": existing_delivery["event_id"],
                "counts": existing_delivery["counts"],
                "markdown": delivery,
                "provider": None,
                "cache": {"enabled": True, "status": "not_run"},
                "mode": None,
                "text_exposed_to_agent": False,
            }
    if check_only:
        return {
            "status": "title_required",
            "source_id": source_id,
            "provider": None,
            "cache": {"enabled": True, "status": "not_run"},
            "mode": None,
            "text_exposed_to_agent": False,
        }
    if source_event is not None and title is not None and _delivery_title(title) != effective_title:
        raise ContextError(
            "This recording already has an event title; refusing to create a second event with a new title."
        )

    jobs_root = config_dir / "capture-jobs"
    jobs_root.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(config_dir, 0o700)
        os.chmod(jobs_root, 0o700)
    with tempfile.TemporaryDirectory(prefix="capture-audio-", dir=jobs_root) as job_name:
        job_dir = Path(job_name)
        if os.name != "nt":
            os.chmod(job_dir, 0o700)
        transcript_path = job_dir / "transcript.v1.json"
        staged_markdown_path = job_dir / "delivery.md"
        transcribe_result = onboarding.transcribe_audio(
            root,
            config_dir=config_dir,
            provider=provider,
            agent_host=agent_host,
            audio=audio_path,
            output=transcript_path,
            language=language,
            title=effective_title,
            observed_at=effective_observed_at,
            speaker_count=speaker_count,
        )
        if not transcript_path.is_file():
            raise ContextError("Transcription completed without private transcript.v1 output.")
        if os.name != "nt":
            os.chmod(transcript_path, 0o600)
        try:
            transcript = load_transcript(transcript_path)
            processing = transcript.get("processing")
            if not isinstance(processing, dict) or processing.get("source_audio_sha256") != source_hash:
                raise TranscriptMarkdownError("Transcript source audio hash does not match the named audio")
            rendered = render_transcript_markdown(
                transcript, source_audio_sha256=source_hash
            )
            publish_markdown(staged_markdown_path, rendered)
        except TranscriptMarkdownError as exc:
            raise ContextError(str(exc)) from exc

        ingest(root, [audio_path], observed_at=effective_observed_at, dry_run=False)
        import_preview = import_transcript(
            root, transcript_path, source_id=source_id, dry_run=True
        )
        event_id = str(import_preview["event_id"])
        existing_match = _existing_event_matches(root, event_id, source_id, transcript)
        if existing_match is False:
            raise ContextError(
                "The existing event differs from this transcript; refusing to add duplicate immutable segments."
            )
        if existing_match is None:
            imported = import_transcript(
                root, transcript_path, source_id=source_id, dry_run=False
            )
        else:
            imported = {
                "source_id": source_id,
                "event_id": event_id,
                "counts": import_preview["counts"],
            }
        try:
            markdown = publish_markdown(output_path, rendered)
        except TranscriptMarkdownError as exc:
            raise ContextError(str(exc)) from exc

    counts = {"events": 1, **imported["counts"]}
    return {
        "status": "captured",
        "source_id": source_id,
        "event_id": imported["event_id"],
        "counts": counts,
        "markdown": markdown,
        "provider": transcribe_result.get("provider"),
        "cache": transcribe_result.get("cache"),
        "mode": transcribe_result.get("mode"),
        "text_exposed_to_agent": False,
    }


def _cache_hash_for_source(root: Path, source_id: str) -> str:
    with connect(root, readonly=True) as connection:
        row = connection.execute(
            "SELECT content_hash FROM sources WHERE id=?", (source_id,)
        ).fetchone()
    if row is None:
        raise ContextError(f"Unknown source_id: {source_id}")
    return str(row["content_hash"])


def _cache_source_index(root: Path, hashes: Sequence[str]) -> dict[str, dict[str, Any]]:
    if not hashes or not db_path(root).is_file():
        return {}
    placeholders = ",".join("?" for _ in hashes)
    try:
        with connect(root, readonly=True) as connection:
            rows = connection.execute(
                "SELECT id, content_hash, original_name, media_type, size_bytes "
                f"FROM sources WHERE content_hash IN ({placeholders})",
                list(hashes),
            ).fetchall()
    except (ContextError, sqlite3.DatabaseError):
        return {}
    return {
        str(row["content_hash"]): {
            "source_id": str(row["id"]),
            "original_name": str(row["original_name"]),
            "source_media_type": str(row["media_type"]),
            "source_bytes": int(row["size_bytes"]),
        }
        for row in rows
    }


def _attach_cache_sources(root: Path, items: list[dict[str, Any]]) -> None:
    index = _cache_source_index(root, [str(item["audio_sha256"]) for item in items])
    for item in items:
        source = index.get(str(item["audio_sha256"]))
        if source is None:
            item["source_status"] = "unbound"
            item["source_id"] = None
            item["original_name"] = None
        else:
            item["source_status"] = "linked"
            item.update(source)


def transcription_cache_status(
    root: Path,
    *,
    config_dir: Path,
    audio: Optional[Path] = None,
    source_id: Optional[str] = None,
    limit: Optional[int] = None,
) -> dict[str, Any]:
    import personal_context_bootstrap as onboarding

    if audio is not None and source_id is not None:
        raise ContextError("Select cache by audio path or source_id, not both.")
    if limit is not None and limit < 1:
        raise ContextError("Cache status limit must be at least 1.")
    result = onboarding.transcription_cache_status(
        root,
        config_dir=config_dir,
        audio=audio,
        audio_sha256=_cache_hash_for_source(root, source_id) if source_id else None,
    )
    recordings = list(result["recordings"])
    _attach_cache_sources(root, recordings)
    recordings.sort(
        key=lambda item: (str(item.get("last_written_at") or ""), str(item["audio_sha256"])),
        reverse=True,
    )
    result["total_recording_count"] = len(recordings)
    if limit is not None:
        recordings = recordings[:limit]
    result["recordings"] = recordings
    result["recording_count"] = len(recordings)
    return result


def transcription_cache_prune(
    root: Path,
    *,
    config_dir: Path,
    audio: Optional[Path] = None,
    source_id: Optional[str] = None,
    apply: bool = False,
) -> dict[str, Any]:
    import personal_context_bootstrap as onboarding

    if audio is not None and source_id is not None:
        raise ContextError("Select cache by audio path or source_id, not both.")
    result = onboarding.transcription_cache_prune(
        root,
        config_dir=config_dir,
        audio=audio,
        audio_sha256=_cache_hash_for_source(root, source_id) if source_id else None,
        apply=apply,
    )
    targets = list(result["targets"])
    _attach_cache_sources(root, targets)
    result["targets"] = targets
    return result


def _filesystem_inventory(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"files": 0, "bytes": 0, "last_written_at": None}
    if path.is_symlink():
        return {"files": 0, "bytes": 0, "last_written_at": None, "unsafe": True}
    candidates = [path] if path.is_file() else list(path.rglob("*"))
    files = [item for item in candidates if item.is_file() and not item.is_symlink()]
    total = sum(item.stat().st_size for item in files)
    latest = max((item.stat().st_mtime for item in files), default=None)
    return {
        "files": len(files),
        "bytes": total,
        "last_written_at": (
            dt.datetime.fromtimestamp(latest, tz=dt.timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
            if latest is not None
            else None
        ),
    }


def _named_directory_inventory(parent: Path, prefix: str) -> dict[str, Any]:
    directories = (
        [item for item in parent.iterdir() if item.is_dir() and not item.is_symlink() and item.name.startswith(prefix)]
        if parent.is_dir() and not parent.is_symlink()
        else []
    )
    inventories = [_filesystem_inventory(item) for item in directories]
    return {
        "jobs": len(directories),
        "files": sum(int(item["files"]) for item in inventories),
        "bytes": sum(int(item["bytes"]) for item in inventories),
        "last_written_at": max(
            (str(item["last_written_at"]) for item in inventories if item["last_written_at"]),
            default=None,
        ),
    }


def storage_status(root: Path, *, config_dir: Path) -> dict[str, Any]:
    import personal_context_bootstrap as onboarding
    from providers.artifacts import vault_scope_hash

    root = root.expanduser().resolve()
    config_dir = config_dir.expanduser().resolve()
    with connect(root, readonly=True) as connection:
        source_row = connection.execute(
            "SELECT COUNT(*) AS recordings, COALESCE(SUM(size_bytes), 0) AS bytes FROM sources"
        ).fetchone()
    cache = onboarding.transcription_cache_status(root, config_dir=config_dir)
    runtime = config_dir / "runtime"
    temp_root = Path(tempfile.gettempdir())
    lock_root = (
        config_dir
        / "artifacts"
        / vault_scope_hash(root)
        / ".locks"
    )
    return {
        "status": "ok",
        "vault_root": str(root),
        "source_blobs": {
            "recordings": int(source_row["recordings"]),
            "bytes": int(source_row["bytes"]),
            "on_disk": _filesystem_inventory(root / "blobs"),
        },
        "database": _filesystem_inventory(db_path(root)),
        "inbox": _filesystem_inventory(root / "inbox"),
        "transcription_cache": {
            "recordings": int(cache["recording_count"]),
            "artifacts": int(cache["valid_artifacts"]) + int(cache["corrupt_artifacts"]),
            "bytes": int(cache["bytes"]),
            "corrupt_artifacts": int(cache["corrupt_artifacts"]),
        },
        "runtime": {
            "models": _filesystem_inventory(runtime / "models"),
            "package_cache": _filesystem_inventory(runtime / "cache"),
            "environment": {
                name: _filesystem_inventory(runtime / name)
                for name in ("bootstrap", "python", "venv", "huggingface")
            },
        },
        "transient_jobs": _named_directory_inventory(
            config_dir / "capture-jobs", "capture-audio-"
        ),
        "normalized_audio_temp": _named_directory_inventory(
            temp_root, "personal-context-audio-"
        ),
        "cache_locks": _filesystem_inventory(lock_root),
    }


def _rows(connection: sqlite3.Connection, sql: str, parameters: Sequence[Any] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(sql, parameters).fetchall()]


def review_event(root: Path, event_id: str) -> dict[str, Any]:
    with connect(root, readonly=True) as connection:
        event_row = connection.execute(
            "SELECT e.*, s.original_name, s.content_hash AS source_hash, s.stored_path "
            "FROM events e JOIN sources s ON s.id=e.source_id WHERE e.id=?",
            (event_id,),
        ).fetchone()
        if not event_row:
            raise ContextError(f"Unknown event: {event_id}")
        result = {"event": dict(event_row)}
        result["segments"] = _rows(connection, "SELECT * FROM segments WHERE event_id=? ORDER BY ordinal", (event_id,))
        result["statements"] = _rows(connection, "SELECT * FROM statements WHERE event_id=? ORDER BY created_at, id", (event_id,))
        result["entities"] = _rows(connection, "SELECT * FROM entities WHERE event_id=? ORDER BY name", (event_id,))
        result["decisions"] = _rows(connection, "SELECT * FROM decisions WHERE event_id=? ORDER BY created_at, id", (event_id,))
        result["actions"] = _rows(connection, "SELECT * FROM actions WHERE event_id=? ORDER BY created_at, id", (event_id,))
        result["claims"] = _rows(connection, "SELECT * FROM claims WHERE event_id=? ORDER BY created_at, id", (event_id,))
        result["relationships"] = _rows(connection, "SELECT * FROM relationships WHERE event_id=? ORDER BY created_at, id", (event_id,))
        result["candidate_memories"] = _rows(
            connection, "SELECT * FROM candidate_memories WHERE event_id=? ORDER BY created_at, id", (event_id,)
        )
        return result


def list_candidates(root: Path, status: str) -> dict[str, Any]:
    if status not in {"pending", "approved", "rejected", "all"}:
        raise ContextError(f"Invalid candidate status: {status}")
    with connect(root, readonly=True) as connection:
        where = "" if status == "all" else "WHERE c.review_status=?"
        parameters: tuple[Any, ...] = () if status == "all" else (status,)
        rows = _rows(
            connection,
            "SELECT c.*, s.original_name, s.content_hash AS source_hash, sg.start_ms, sg.end_ms "
            "FROM candidate_memories c JOIN sources s ON s.id=c.source_id "
            "LEFT JOIN segments sg ON sg.id=c.segment_id " + where + " ORDER BY c.created_at, c.id",
            parameters,
        )
    return {"status": status, "count": len(rows), "candidates": rows}


def rebuild_search_index(connection: sqlite3.Connection) -> int:
    connection.execute("DELETE FROM search_index")
    statements = (
        ("memory", "SELECT id, content, source_id, segment_id, kind FROM memories WHERE invalidated_at IS NULL"),
        ("statement", "SELECT id, text, source_id, segment_id, kind FROM statements WHERE invalidated_at IS NULL"),
        ("decision", "SELECT id, text, source_id, segment_id, 'Decision' AS kind FROM decisions WHERE invalidated_at IS NULL"),
        ("action", "SELECT id, text, source_id, segment_id, 'Action' AS kind FROM actions WHERE invalidated_at IS NULL"),
        ("claim", "SELECT id, text, source_id, segment_id, 'Claim' AS kind FROM claims WHERE invalidated_at IS NULL"),
    )
    count = 0
    for record_type, sql in statements:
        for row in connection.execute(sql):
            connection.execute(
                "INSERT INTO search_index(record_type, record_id, text, source_id, segment_id, kind) VALUES(?, ?, ?, ?, ?, ?)",
                (record_type, row[0], row[1], row[2], row[3], row[4]),
            )
            count += 1
    return count


def rebuild_index_command(root: Path, *, dry_run: bool) -> dict[str, Any]:
    require_writable(root)
    with connect(root, readonly=True) as connection:
        expected = sum(
            connection.execute(f"SELECT COUNT(*) FROM {table} WHERE invalidated_at IS NULL").fetchone()[0]
            for table in ("memories", "statements", "decisions", "actions", "claims")
        )
    if dry_run:
        return {"dry_run": True, "would_index": expected}
    with connect(root) as connection, connection:
        count = rebuild_search_index(connection)
    return {"dry_run": False, "indexed": count}


def decide_candidate(
    root: Path,
    candidate_id: str,
    *,
    decision: str,
    reviewer: str,
    reason: Optional[str],
) -> dict[str, Any]:
    require_writable(root)
    if decision not in {"approve", "reject"}:
        raise ContextError(f"Invalid decision: {decision}")
    reviewer = reviewer.strip()
    if not reviewer:
        raise ContextError("reviewer must not be empty.")
    with connect(root) as connection, connection:
        candidate = connection.execute("SELECT * FROM candidate_memories WHERE id=?", (candidate_id,)).fetchone()
        if not candidate:
            raise ContextError(f"Unknown candidate: {candidate_id}")
        expected_status = "approved" if decision == "approve" else "rejected"
        if candidate["review_status"] == expected_status:
            memory = connection.execute("SELECT id FROM memories WHERE candidate_id=?", (candidate_id,)).fetchone()
            return {
                "status": "already_reviewed",
                "candidate_id": candidate_id,
                "decision": decision,
                "memory_id": memory[0] if memory else None,
            }
        if candidate["review_status"] != "pending":
            raise ContextError(
                f"Candidate is already {candidate['review_status']}; reviews are append-only and cannot be overwritten."
            )
        reviewed_at = utc_now()
        review_id = stable_id("rev", [candidate_id, decision, reviewer, reason, reviewed_at])
        connection.execute(
            "INSERT INTO reviews(id, candidate_id, decision, reviewer, reason, reviewed_at) VALUES(?, ?, ?, ?, ?, ?)",
            (review_id, candidate_id, decision, reviewer, reason, reviewed_at),
        )
        connection.execute(
            "UPDATE candidate_memories SET review_status=?, updated_at=? WHERE id=? AND review_status='pending'",
            (expected_status, reviewed_at, candidate_id),
        )
        memory_id = None
        if decision == "approve":
            memory_payload = [candidate_id, candidate["source_id"], candidate["segment_id"], candidate["proposed_kind"], candidate["content"]]
            memory_id = stable_id("mem", memory_payload)
            connection.execute(
                "INSERT INTO memories(id, candidate_id, source_id, segment_id, kind, content, content_hash, "
                "review_status, approved_by, approved_at, observed_at, valid_from, valid_to, created_at, updated_at, schema_version) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, 'approved', ?, ?, ?, ?, ?, ?, ?, ?)",
                (memory_id, candidate_id, candidate["source_id"], candidate["segment_id"], candidate["proposed_kind"],
                 candidate["content"], digest_record(memory_payload), reviewer, reviewed_at, candidate["observed_at"],
                 candidate["valid_from"], candidate["valid_to"], reviewed_at, reviewed_at, SCHEMA_VERSION),
            )
        rebuild_search_index(connection)
    return {"status": expected_status, "candidate_id": candidate_id, "review_id": review_id, "memory_id": memory_id}


def retrieve(root: Path, query: str, *, limit: int) -> dict[str, Any]:
    query = query.strip()
    if not query:
        raise ContextError("Query must not be empty.")
    if limit < 1 or limit > 100:
        raise ContextError("limit must be between 1 and 100.")
    escaped_query = query.replace("%", "\\%").replace("_", "\\_")
    pattern = "%" + escaped_query + "%"
    with connect(root, readonly=True) as connection:
        rows = _rows(
            connection,
            "SELECT i.record_type, i.record_id, i.text, i.kind, i.source_id, i.segment_id, "
            "s.original_name, s.content_hash AS source_hash, s.observed_at AS source_observed_at, "
            "sg.start_ms, sg.end_ms, sg.speaker, "
            "CASE i.record_type WHEN 'memory' THEN 0 WHEN 'decision' THEN 1 WHEN 'action' THEN 2 ELSE 3 END AS rank "
            "FROM search_index i JOIN sources s ON s.id=i.source_id LEFT JOIN segments sg ON sg.id=i.segment_id "
            "WHERE i.text LIKE ? ESCAPE '\\' ORDER BY rank, i.record_type, i.record_id LIMIT ?",
            (pattern, limit),
        )
    results = []
    for row in rows:
        results.append(
            {
                "record_type": row["record_type"],
                "record_id": row["record_id"],
                "kind": row["kind"],
                "text": row["text"],
                "authority": "approved_memory" if row["record_type"] == "memory" else "source_evidence",
                "source": {
                    "id": row["source_id"],
                    "name": row["original_name"],
                    "content_hash": row["source_hash"],
                    "observed_at": row["source_observed_at"],
                    "segment_id": row["segment_id"],
                    "start_ms": row["start_ms"],
                    "end_ms": row["end_ms"],
                    "speaker": row["speaker"],
                },
            }
        )
    return {"query": query, "count": len(results), "results": results}


def _markdown_escape(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _data_as_of(connection: sqlite3.Connection) -> str:
    row = connection.execute(
        "SELECT MAX(ts) FROM ("
        "SELECT imported_at AS ts FROM sources UNION ALL "
        "SELECT updated_at AS ts FROM events UNION ALL "
        "SELECT updated_at AS ts FROM candidate_memories UNION ALL "
        "SELECT updated_at AS ts FROM memories UNION ALL "
        "SELECT reviewed_at AS ts FROM reviews"
        ")"
    ).fetchone()
    if row and row[0]:
        return row[0]
    metadata = connection.execute("SELECT MAX(updated_at) FROM schema_metadata").fetchone()
    return metadata[0] if metadata and metadata[0] else "unknown"


def _wiki_files(connection: sqlite3.Connection) -> tuple[dict[str, str], str]:
    memories = _rows(
        connection,
        "SELECT m.*, s.original_name, s.content_hash AS source_hash, sg.start_ms, sg.end_ms "
        "FROM memories m JOIN sources s ON s.id=m.source_id LEFT JOIN segments sg ON sg.id=m.segment_id "
        "WHERE m.invalidated_at IS NULL ORDER BY m.approved_at, m.id",
    )
    events = _rows(
        connection,
        "SELECT e.id, e.title, e.event_type, e.observed_at, s.id AS source_id, s.original_name, s.content_hash AS source_hash "
        "FROM events e JOIN sources s ON s.id=e.source_id WHERE e.invalidated_at IS NULL ORDER BY e.observed_at, e.id",
    )
    data_as_of = _data_as_of(connection)
    index = (
        "# Personal Context Wiki\n\n"
        "> Generated view. The SQLite database and immutable Sources remain authoritative.\n\n"
        f"Data as of: {data_as_of}\n\n"
        f"- [Approved memories](memories.md): {len(memories)}\n"
        f"- [Events](events.md): {len(events)}\n"
    )
    memory_lines = [
        "# Approved memories",
        "",
        "> Generated from explicitly approved CandidateMemory records. Do not edit as source data.",
        "",
    ]
    for memory in memories:
        location = ""
        if memory["start_ms"] is not None:
            location = f" @ {memory['start_ms']}–{memory['end_ms']} ms"
        memory_lines.extend(
            [
                f"## {_markdown_escape(memory['content'])}",
                "",
                f"- Kind: {memory['kind']}",
                f"- Approved: {memory['approved_at']} by {memory['approved_by']}",
                f"- Source: `{memory['source_id']}` — {_markdown_escape(memory['original_name'])}{location}",
                f"- Source SHA-256: `{memory['source_hash']}`",
                f"- Memory ID: `{memory['id']}`",
                "",
            ]
        )
    event_lines = ["# Events", "", "> Generated evidence index.", ""]
    for event in events:
        event_lines.extend(
            [
                f"## {_markdown_escape(event['title'])}",
                "",
                f"- Type: {event['event_type']}",
                f"- Observed: {event['observed_at']}",
                f"- Source: `{event['source_id']}` — {_markdown_escape(event['original_name'])}",
                f"- Source SHA-256: `{event['source_hash']}`",
                f"- Event ID: `{event['id']}`",
                "",
            ]
        )
    return {
        "index.md": index,
        "memories.md": "\n".join(memory_lines) + "\n",
        "events.md": "\n".join(event_lines) + "\n",
    }, data_as_of


def compile_wiki(root: Path, *, dry_run: bool) -> dict[str, Any]:
    require_writable(root)
    with connect(root, readonly=True) as connection:
        files, data_as_of = _wiki_files(connection)
    markdown_preview = [
        {"path": f"wiki/{name}", "bytes": len(content.encode("utf-8")), "sha256": digest_bytes(content.encode("utf-8"))}
        for name, content in sorted(files.items())
    ]
    manifest = {"data_as_of": data_as_of, "files": markdown_preview, "schema_version": SCHEMA_VERSION}
    manifest_content = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    files[".personal-context-manifest.json"] = manifest_content
    preview = [
        {"path": f"wiki/{name}", "bytes": len(content.encode("utf-8")), "sha256": digest_bytes(content.encode("utf-8"))}
        for name, content in sorted(files.items())
    ]
    if dry_run:
        return {"dry_run": True, "files": preview}
    wiki_dir = root / "wiki"
    wiki_dir.mkdir(exist_ok=True)
    for name, content in files.items():
        destination = wiki_dir / name
        fd, temp_name = tempfile.mkstemp(prefix=f".{name}-", dir=wiki_dir)
        os.close(fd)
        temp_path = Path(temp_name)
        try:
            temp_path.write_text(content, encoding="utf-8")
            os.replace(temp_path, destination)
        finally:
            if temp_path.exists():
                temp_path.unlink()
    with connect(root) as connection, connection:
        indexed = rebuild_search_index(connection)
    return {"dry_run": False, "files": preview, "indexed": indexed}


def audit(root: Path) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    state = schema_state(root)
    if state["status"] != "current":
        issues.append({"code": "schema_status", "severity": "error", "details": state})
    if not db_path(root).exists():
        return {"ok": False, "schema": state, "issue_count": len(issues), "issues": issues}
    try:
        with connect(root, readonly=True) as connection:
            present = table_names(connection)
            for table in sorted(EXPECTED_TABLES - present):
                issues.append({"code": "missing_table", "severity": "error", "table": table})
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                issues.append({"code": "sqlite_integrity", "severity": "error", "details": integrity})
            for row in connection.execute("PRAGMA foreign_key_check"):
                issues.append({"code": "foreign_key", "severity": "error", "table": row[0], "rowid": row[1], "parent": row[2]})
            if "sources" in present:
                for source in connection.execute("SELECT id, content_hash, stored_path FROM sources"):
                    blob = root / source["stored_path"]
                    if not blob.is_file():
                        issues.append({"code": "missing_blob", "severity": "error", "source_id": source["id"], "path": str(blob)})
                    else:
                        actual_hash, _ = digest_file(blob)
                        if actual_hash != source["content_hash"]:
                            issues.append({"code": "source_hash_mismatch", "severity": "error", "source_id": source["id"]})
            if {"segments", "sources"}.issubset(present):
                for row in connection.execute(
                    "SELECT sg.id FROM segments sg LEFT JOIN sources s ON s.id=sg.source_id WHERE s.id IS NULL"
                ):
                    issues.append({"code": "orphan_segment", "severity": "error", "segment_id": row[0]})
            if {"memories", "sources"}.issubset(present):
                for row in connection.execute(
                    "SELECT m.id FROM memories m LEFT JOIN sources s ON s.id=m.source_id WHERE s.id IS NULL"
                ):
                    issues.append({"code": "memory_without_source", "severity": "error", "memory_id": row[0]})
            if {"memories", "candidate_memories"}.issubset(present):
                for row in connection.execute(
                    "SELECT m.id FROM memories m LEFT JOIN candidate_memories c ON c.id=m.candidate_id "
                    "WHERE c.id IS NULL OR c.review_status!='approved'"
                ):
                    issues.append({"code": "memory_without_approval", "severity": "error", "memory_id": row[0]})
            if "segments" in present:
                for segment in connection.execute(
                    "SELECT id, source_id, event_id, ordinal, start_ms, end_ms, speaker, text, content_hash FROM segments"
                ):
                    payload = [segment["source_id"], segment["event_id"], segment["ordinal"], {
                        "text": segment["text"], "speaker": segment["speaker"],
                        "start_ms": segment["start_ms"], "end_ms": segment["end_ms"],
                    }]
                    if digest_record(payload) != segment["content_hash"]:
                        issues.append({"code": "segment_hash_mismatch", "severity": "error", "segment_id": segment["id"]})
    except sqlite3.DatabaseError as exc:
        issues.append({"code": "database_error", "severity": "error", "details": str(exc)})
    return {"ok": not issues, "schema": state, "issue_count": len(issues), "issues": issues}


def migrate(root: Path, *, apply: bool) -> dict[str, Any]:
    state = schema_state(root)
    version = state.get("version")
    if state["status"] == "current":
        return {"dry_run": not apply, "status": "already_current", "schema_version": SCHEMA_VERSION, "steps": []}
    if state["status"] == "newer":
        raise ContextError("Database is newer than this Skill; downgrade is not supported.")
    if state["status"] in {"missing", "unknown", "damaged"}:
        raise ContextError(f"Cannot migrate a {state['status']} database; run audit and restore a valid backup.")
    if version != 0:
        raise ContextError(f"No migration path from schema {version} to {SCHEMA_VERSION}.")
    steps = [
        {"from": 0, "to": 1, "description": "Create V1 tables, indexes, immutability triggers, and compatibility metadata."}
    ]
    if not apply:
        return {"dry_run": True, "status": "migration_available", "from_version": version, "to_version": SCHEMA_VERSION, "steps": steps}
    backup_dir = root / "backups"
    backup_dir.mkdir(exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup_path = backup_dir / f"context-schema-{version}-before-{SCHEMA_VERSION}-{stamp}.sqlite3"
    with connect(root, readonly=True) as source_connection, sqlite3.connect(backup_path) as backup_connection:
        source_connection.backup(backup_connection)
    migration_id = stable_id("mig", [version, SCHEMA_VERSION, digest_file(backup_path)[0]])
    try:
        with connect(root) as connection, connection:
            initialize_schema(connection, version=SCHEMA_VERSION)
            report = {"steps": steps, "backup": str(backup_path)}
            connection.execute(
                "INSERT OR IGNORE INTO migrations(id, from_version, to_version, applied_at, report_json) VALUES(?, ?, ?, ?, ?)",
                (migration_id, version, SCHEMA_VERSION, utc_now(), canonical_json(report)),
            )
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise ContextError(f"Migration integrity check failed: {integrity}")
    except Exception:
        raise ContextError(f"Migration failed; original backup is available at {backup_path}")
    audit_result = audit(root)
    if not audit_result["ok"]:
        raise ContextError(f"Migration completed but audit failed; backup is available at {backup_path}")
    return {
        "dry_run": False,
        "status": "migrated",
        "from_version": version,
        "to_version": SCHEMA_VERSION,
        "backup": str(backup_path),
        "migration_id": migration_id,
        "audit": audit_result,
    }


def version_info(root: Optional[Path]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "skill_version": skill_version(),
        "schema_version": SCHEMA_VERSION,
        "min_supported_schema": MIN_SCHEMA_VERSION,
        "max_supported_schema": MAX_SCHEMA_VERSION,
        "provider_contract_version": 1,
        "artifact_contract_version": 1,
        "consent_notice_version": 2,
    }
    if root is not None:
        result["database"] = schema_state(root)
        result["root"] = str(root)
    return result


def print_json(value: Any, *, stream: Any = sys.stdout) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), file=stream)


def _add_root(parser: argparse.ArgumentParser, *, required: bool = True) -> None:
    parser.add_argument("--root", required=required, help="Personal context vault path (never inferred from source code).")


def _add_bootstrap_options(
    parser: argparse.ArgumentParser, *, require_agent_host: bool = False, include_provider: bool = True
) -> None:
    parser.add_argument("--config-dir", help="Private machine configuration; defaults to the operating-system user config directory.")
    if include_provider:
        parser.add_argument("--provider", choices=("auto", "transcript-only", "qwen-mlx"), default="auto")
    parser.add_argument("--agent-host", required=require_agent_host, help="Stable host name such as codex, claude-code, or gemini-cli.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="context", description="Local, auditable personal context management.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    command = subparsers.add_parser("init-vault", help="Initialize an empty V1 data vault.")
    _add_root(command)

    command = subparsers.add_parser("bootstrap-status", help="Read onboarding, consent, database, and local provider state as JSON.")
    _add_root(command)
    _add_bootstrap_options(command)

    command = subparsers.add_parser("bootstrap-plan", help="Preview the exact first-use notice, downloads, and local writes.")
    _add_root(command)
    _add_bootstrap_options(command, require_agent_host=True)
    command.add_argument("--mode", choices=("strict-local", "agent-assisted"), required=True)

    command = subparsers.add_parser("record-consent", help="Persist consent only after the user accepts the current bootstrap plan.")
    _add_root(command)
    _add_bootstrap_options(command, require_agent_host=True)
    command.add_argument("--mode", choices=("strict-local", "agent-assisted"), required=True)
    command.add_argument("--accept-plan", required=True, help="Exact scoped SHA-256 plan digest shown by bootstrap-plan.")

    command = subparsers.add_parser("bootstrap-apply", help="Create the vault and consented isolated local provider runtime.")
    _add_root(command)
    _add_bootstrap_options(command)

    command = subparsers.add_parser("provider-doctor", help="Probe provider compatibility and installation without changing state.")
    _add_bootstrap_options(command)

    command = subparsers.add_parser("transcribe-audio", help="Transcribe one named local audio file to transcript.v1 JSON.")
    _add_root(command)
    _add_bootstrap_options(command)
    command.add_argument("--audio", required=True)
    command.add_argument("--output", required=True)
    command.add_argument("--language", help="Optional provider language hint, for example Chinese or English.")
    command.add_argument(
        "--speaker-count",
        type=int,
        choices=range(1, 5),
        help="Known number of speakers in this recording; omit to auto-detect up to four.",
    )
    command.add_argument("--title")
    command.add_argument("--observed-at", help="Optional ISO-8601 event observation time.")
    command.add_argument("--no-cache", action="store_true", help="Bypass transcription cache reads and writes for this run.")
    command.add_argument(
        "--refresh-stage",
        choices=("asr", "alignment", "diarization", "all"),
        help="Recompute one cached transcription stage while retaining other valid artifacts.",
    )

    command = subparsers.add_parser(
        "capture-audio",
        help="Capture audio evidence and deliver one complete Markdown transcript to the Vault inbox.",
    )
    _add_root(command)
    _add_bootstrap_options(command)
    command.add_argument("--audio", required=True)
    command.add_argument("--language", help="Optional provider language hint, for example Chinese or English.")
    command.add_argument(
        "--speaker-count",
        type=int,
        choices=range(1, 5),
        help="Known number of speakers in this recording; omit to auto-detect up to four.",
    )
    command.add_argument("--title")
    command.add_argument("--observed-at", help="Optional ISO-8601 event observation time.")
    command.add_argument(
        "--rerun",
        action="store_true",
        help="Explicitly rerun an already delivered recording without creating a second Markdown file.",
    )
    command.add_argument(
        "--check-only",
        action="store_true",
        help="Read-only preflight: return an existing delivery or report that a title is needed.",
    )

    command = subparsers.add_parser(
        "transcription-cache-status",
        help="Inspect private transcription artifact metadata without exposing transcript text.",
    )
    _add_root(command)
    _add_bootstrap_options(command, include_provider=False)
    cache_selector = command.add_mutually_exclusive_group()
    cache_selector.add_argument("--audio", help="Limit status to one named audio file.")
    cache_selector.add_argument("--source-id", help="Limit status to one ingested Source ID.")
    command.add_argument("--limit", type=int, help="Return only the most recently written cache entries.")

    command = subparsers.add_parser(
        "transcription-cache-prune",
        help="Preview or apply explicit removal of private transcription artifacts.",
    )
    _add_root(command)
    _add_bootstrap_options(command, include_provider=False)
    cache_selector = command.add_mutually_exclusive_group()
    cache_selector.add_argument("--audio", help="Limit pruning to one named audio file.")
    cache_selector.add_argument("--source-id", help="Limit pruning to one ingested Source ID.")
    prune_mode = command.add_mutually_exclusive_group()
    prune_mode.add_argument("--dry-run", action="store_true", help="Preview only; this is the default.")
    prune_mode.add_argument("--apply", action="store_true", help="Remove the selected cache entries after previewing.")

    command = subparsers.add_parser(
        "storage-status",
        help="Inspect vault, cache, runtime, and transient storage metadata without reading content.",
    )
    _add_root(command)
    _add_bootstrap_options(command, include_provider=False)

    command = subparsers.add_parser("doctor", help="Check directories, database integrity, schema, and compatibility.")
    _add_root(command)

    command = subparsers.add_parser("ingest", help="Create immutable Source records and content-addressed blobs.")
    _add_root(command)
    command.add_argument("paths", nargs="+", help="One or more local files.")
    command.add_argument("--observed-at", help="ISO-8601 observation time; defaults to now.")
    command.add_argument("--dry-run", action="store_true", help="Preview the complete batch without writing.")

    command = subparsers.add_parser("import-transcript", help="Import structured JSON transcription without an API key.")
    _add_root(command)
    command.add_argument("transcript", help="Structured UTF-8 JSON transcript.")
    command.add_argument("--source-id", help="Existing immutable Source represented by this transcript.")
    command.add_argument("--dry-run", action="store_true", help="Validate and preview without writing.")

    command = subparsers.add_parser("review", help="Show one Event and its evidence chain.")
    _add_root(command)
    command.add_argument("event_id")

    command = subparsers.add_parser("candidates", help="List CandidateMemory records by review status.")
    _add_root(command)
    command.add_argument("--status", choices=("pending", "approved", "rejected", "all"), default="pending")

    for name in ("approve", "reject"):
        command = subparsers.add_parser(name, help=f"{name.title()} one CandidateMemory and append a review record.")
        _add_root(command)
        command.add_argument("candidate_id")
        command.add_argument("--reviewer", default="user", help="Identity recorded in the audit trail.")
        command.add_argument("--reason", help="Optional review rationale.")

    command = subparsers.add_parser("retrieve", help="Search approved memories and source evidence with citations.")
    _add_root(command)
    command.add_argument("query")
    command.add_argument("--limit", type=int, default=20)

    command = subparsers.add_parser("audit", help="Check provenance, hashes, foreign keys, and schema integrity.")
    _add_root(command)

    command = subparsers.add_parser("compile-wiki", help="Regenerate the human-readable Wiki view from the database.")
    _add_root(command)
    command.add_argument("--dry-run", action="store_true", help="Preview generated files without writing.")

    command = subparsers.add_parser("rebuild-index", help="Delete and deterministically rebuild the disposable search index.")
    _add_root(command)
    command.add_argument("--dry-run", action="store_true", help="Report expected index size without writing.")

    command = subparsers.add_parser("migrate", help="Preview or apply a backup-first schema migration.")
    _add_root(command)
    command.add_argument("--apply", action="store_true", help="Apply migration; without this flag the command is dry-run only.")

    command = subparsers.add_parser("version", help="Show Skill and Schema compatibility versions.")
    _add_root(command, required=False)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    import personal_context_bootstrap as onboarding

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        root = resolve_root(args.root) if getattr(args, "root", None) else None
        if args.command == "init-vault":
            result = init_vault(root)
        elif args.command == "bootstrap-status":
            result = onboarding.bootstrap_status(
                root,
                config_dir=onboarding.resolve_config_dir(args.config_dir),
                provider=args.provider,
                agent_host=args.agent_host,
                database_state=schema_state(root),
            )
        elif args.command == "bootstrap-plan":
            result = onboarding.bootstrap_plan(
                root,
                config_dir=onboarding.resolve_config_dir(args.config_dir),
                mode=args.mode,
                provider=args.provider,
                agent_host=args.agent_host,
                database_state=schema_state(root),
            )
        elif args.command == "record-consent":
            result = onboarding.record_consent(
                root,
                config_dir=onboarding.resolve_config_dir(args.config_dir),
                mode=args.mode,
                provider=args.provider,
                agent_host=args.agent_host,
                accepted_digest=args.accept_plan,
            )
        elif args.command == "bootstrap-apply":
            result = onboarding.bootstrap_apply(
                root,
                config_dir=onboarding.resolve_config_dir(args.config_dir),
                provider=args.provider,
                agent_host=args.agent_host,
                database_state=schema_state(root),
                init_vault=init_vault,
            )
        elif args.command == "provider-doctor":
            result = onboarding.provider_status(args.provider, onboarding.resolve_config_dir(args.config_dir))
        elif args.command == "transcribe-audio":
            result = onboarding.transcribe_audio(
                root,
                config_dir=onboarding.resolve_config_dir(args.config_dir),
                provider=args.provider,
                agent_host=args.agent_host,
                audio=Path(args.audio),
                output=Path(args.output),
                language=args.language,
                title=args.title,
                observed_at=args.observed_at,
                speaker_count=args.speaker_count,
                no_cache=args.no_cache,
                refresh_stage=args.refresh_stage,
            )
        elif args.command == "capture-audio":
            result = capture_audio(
                root,
                config_dir=onboarding.resolve_config_dir(args.config_dir),
                provider=args.provider,
                agent_host=args.agent_host,
                audio=Path(args.audio),
                language=args.language,
                title=args.title,
                observed_at=args.observed_at,
                speaker_count=args.speaker_count,
                rerun=args.rerun,
                check_only=args.check_only,
            )
        elif args.command == "transcription-cache-status":
            result = transcription_cache_status(
                root,
                config_dir=onboarding.resolve_config_dir(args.config_dir),
                audio=Path(args.audio) if args.audio else None,
                source_id=args.source_id,
                limit=args.limit,
            )
        elif args.command == "transcription-cache-prune":
            result = transcription_cache_prune(
                root,
                config_dir=onboarding.resolve_config_dir(args.config_dir),
                audio=Path(args.audio) if args.audio else None,
                source_id=args.source_id,
                apply=args.apply,
            )
        elif args.command == "storage-status":
            result = storage_status(
                root,
                config_dir=onboarding.resolve_config_dir(args.config_dir),
            )
        elif args.command == "doctor":
            result = doctor(root)
        elif args.command == "ingest":
            result = ingest(root, [Path(value) for value in args.paths], observed_at=args.observed_at, dry_run=args.dry_run)
        elif args.command == "import-transcript":
            result = import_transcript(root, Path(args.transcript), source_id=args.source_id, dry_run=args.dry_run)
        elif args.command == "review":
            result = review_event(root, args.event_id)
        elif args.command == "candidates":
            result = list_candidates(root, args.status)
        elif args.command in {"approve", "reject"}:
            result = decide_candidate(root, args.candidate_id, decision=args.command, reviewer=args.reviewer, reason=args.reason)
        elif args.command == "retrieve":
            result = retrieve(root, args.query, limit=args.limit)
        elif args.command == "audit":
            result = audit(root)
        elif args.command == "compile-wiki":
            result = compile_wiki(root, dry_run=args.dry_run)
        elif args.command == "rebuild-index":
            result = rebuild_index_command(root, dry_run=args.dry_run)
        elif args.command == "migrate":
            result = migrate(root, apply=args.apply)
        elif args.command == "version":
            result = version_info(root)
        else:
            parser.error(f"Unknown command: {args.command}")
            return 2
        print_json(result)
        return 0
    except (ContextError, onboarding.BootstrapError, sqlite3.DatabaseError, OSError) as exc:
        print_json({"error": str(exc), "command": args.command}, stream=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
