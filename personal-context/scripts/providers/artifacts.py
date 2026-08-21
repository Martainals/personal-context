"""Private, checksummed JSON artifact storage for local transcription stages.

Artifacts are evidence-processing intermediates, not database records.  The
store accepts JSON values only, rejects biometric/embedding-shaped fields, and
never uses pickle or another executable serialization format.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import gzip
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional


ARTIFACT_CONTRACT_VERSION = 1
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_FORBIDDEN_FIELD_MARKERS = ("embedding", "voiceprint", "voiceembedding", "speakerembedding", "声纹")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def component_cache_key(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def vault_scope_hash(vault_root: Path) -> str:
    normalized = str(vault_root.expanduser().resolve())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(path, 0o700)


def _validate_hex64(value: str, label: str) -> str:
    if not _HEX64.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase SHA-256 hex digest")
    return value


def _validate_name(value: str, label: str) -> str:
    if not _NAME.fullmatch(value):
        raise ValueError(f"{label} must contain only lowercase letters, digits, and hyphens")
    return value


def _validate_payload(value: Any, path: tuple[str, ...] = ()) -> None:
    if value is None or isinstance(value, (str, bool, int, float)):
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            raise ValueError("Artifact payload contains a non-finite number")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_payload(item, path + (str(index),))
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("Artifact object keys must be strings")
            normalized = "".join(character for character in key.casefold() if character.isalnum())
            if any(marker in normalized for marker in _FORBIDDEN_FIELD_MARKERS):
                location = ".".join(path + (key,))
                raise ValueError(f"Biometric or embedding artifact fields are forbidden: {location}")
            _validate_payload(item, path + (key,))
        return
    raise ValueError(f"Artifact payload is not JSON-compatible: {type(value).__name__}")


def validate_artifact_payload(value: Any) -> None:
    """Validate a prospective artifact before starting expensive work."""
    _validate_payload(value)


@dataclass(frozen=True)
class ArtifactResult:
    status: str
    path: Path
    payload: Optional[Any] = None
    payload_sha256: Optional[str] = None
    reason: Optional[str] = None


class ArtifactStore:
    """One vault-scoped, recording-addressed artifact store."""

    def __init__(self, base_dir: Path, scope_hash: str, audio_sha256: str) -> None:
        self.base_dir = base_dir.expanduser().resolve()
        self.scope_hash = _validate_hex64(scope_hash, "scope_hash")
        self.audio_sha256 = _validate_hex64(audio_sha256, "audio_sha256")
        self.scope_dir = self.base_dir / self.scope_hash
        self.recording_dir = self.scope_dir / self.audio_sha256

    def path_for(self, stage: str, name: str) -> Path:
        return self.recording_dir / _validate_name(stage, "stage") / f"{_validate_name(name, 'name')}.json.gz"

    def _lock_path(self) -> Path:
        return self.scope_dir / ".locks" / f"{self.audio_sha256}.lock"

    @contextlib.contextmanager
    def recording_lock(self, *, timeout_seconds: float = 120.0) -> Iterator[None]:
        lock_path = self._lock_path()
        _private_directory(self.base_dir)
        _private_directory(self.scope_dir)
        _private_directory(lock_path.parent)
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        if os.name != "nt":
            os.chmod(lock_path, 0o600)
        handle = os.fdopen(descriptor, "a+b", buffering=0)
        acquired = False
        try:
            deadline = time.monotonic() + timeout_seconds
            if os.name == "nt":
                import msvcrt

                if lock_path.stat().st_size == 0:
                    handle.write(b"0")
                while not acquired:
                    try:
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        acquired = True
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError(f"Timed out waiting for recording artifact lock: {lock_path}")
                        time.sleep(0.05)
            else:
                import fcntl

                while not acquired:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        acquired = True
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError(f"Timed out waiting for recording artifact lock: {lock_path}")
                        time.sleep(0.05)
            yield
        finally:
            if acquired:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def read(self, stage: str, name: str, expected_key: str) -> ArtifactResult:
        path = self.path_for(stage, name)
        _validate_hex64(expected_key, "expected_key")
        if not path.exists():
            return ArtifactResult("miss", path, reason="missing")
        if path.is_symlink() or not path.is_file():
            return ArtifactResult("corrupt", path, reason="unsafe_path")
        try:
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                envelope = json.load(handle)
            if not isinstance(envelope, dict):
                raise ValueError("envelope_not_object")
            if envelope.get("artifact_contract") != ARTIFACT_CONTRACT_VERSION:
                return ArtifactResult("stale", path, reason="contract_changed")
            if envelope.get("stage") != stage or envelope.get("name") != name:
                raise ValueError("identity_mismatch")
            if envelope.get("cache_key") != expected_key:
                return ArtifactResult("stale", path, reason="cache_key_changed")
            payload = envelope.get("payload")
            _validate_payload(payload)
            payload_sha256 = component_cache_key(payload)
            if envelope.get("payload_sha256") != payload_sha256:
                raise ValueError("checksum_mismatch")
            return ArtifactResult("hit", path, payload, payload_sha256)
        except (OSError, EOFError, UnicodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
            return ArtifactResult("corrupt", path, reason=str(exc))

    def write(self, stage: str, name: str, cache_key: str, payload: Any) -> ArtifactResult:
        path = self.path_for(stage, name)
        _validate_hex64(cache_key, "cache_key")
        _validate_payload(payload)
        payload_sha256 = component_cache_key(payload)
        envelope = {
            "artifact_contract": ARTIFACT_CONTRACT_VERSION,
            "cache_key": cache_key,
            "name": name,
            "payload": payload,
            "payload_sha256": payload_sha256,
            "stage": stage,
        }
        encoded = (_canonical_json(envelope) + "\n").encode("utf-8")
        _private_directory(self.base_dir)
        _private_directory(self.scope_dir)
        _private_directory(self.recording_dir)
        _private_directory(path.parent)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as raw:
                with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
                    compressed.write(encoded)
                raw.flush()
                os.fsync(raw.fileno())
            if os.name != "nt":
                os.chmod(temporary, 0o600)
            os.replace(temporary, path)
            if os.name != "nt":
                os.chmod(path, 0o600)
            return ArtifactResult("hit", path, payload, payload_sha256)
        finally:
            if temporary.exists():
                temporary.unlink()


def _inspect_artifact(path: Path, recording_dir: Path) -> dict[str, Any]:
    relative = path.relative_to(recording_dir)
    stat = path.stat() if path.exists() and not path.is_symlink() else None
    result = {
        "path": relative.as_posix(),
        "bytes": stat.st_size if stat is not None else 0,
        "last_written_at": (
            dt.datetime.fromtimestamp(stat.st_mtime, tz=dt.timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
            if stat is not None
            else None
        ),
        "status": "corrupt",
    }
    try:
        if path.is_symlink() or not path.is_file() or len(relative.parts) != 2:
            raise ValueError("unsafe_path")
        stage = _validate_name(relative.parts[0], "stage")
        filename = relative.parts[1]
        if not filename.endswith(".json.gz"):
            raise ValueError("unexpected_extension")
        name = _validate_name(filename[: -len(".json.gz")], "name")
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            envelope = json.load(handle)
        if not isinstance(envelope, dict):
            raise ValueError("envelope_not_object")
        if envelope.get("artifact_contract") != ARTIFACT_CONTRACT_VERSION:
            raise ValueError("contract_changed")
        if envelope.get("stage") != stage or envelope.get("name") != name:
            raise ValueError("identity_mismatch")
        _validate_hex64(str(envelope.get("cache_key")), "cache_key")
        payload = envelope.get("payload")
        _validate_payload(payload)
        if envelope.get("payload_sha256") != component_cache_key(payload):
            raise ValueError("checksum_mismatch")
        result["status"] = "valid"
        result["stage"] = stage
    except (OSError, EOFError, UnicodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
        result["reason"] = str(exc)
    return result


def inspect_artifacts(
    base_dir: Path, scope_hash: str, *, audio_sha256: Optional[str] = None
) -> dict[str, Any]:
    base = base_dir.expanduser().resolve()
    scope = _validate_hex64(scope_hash, "scope_hash")
    selected_audio = _validate_hex64(audio_sha256, "audio_sha256") if audio_sha256 else None
    scope_dir = base / scope
    recording_dirs: list[Path] = []
    if scope_dir.is_dir() and not scope_dir.is_symlink():
        if selected_audio is not None:
            candidate = scope_dir / selected_audio
            if candidate.is_dir() and not candidate.is_symlink():
                recording_dirs.append(candidate)
        else:
            recording_dirs = sorted(
                path
                for path in scope_dir.iterdir()
                if path.is_dir() and not path.is_symlink() and _HEX64.fullmatch(path.name)
            )
    recordings = []
    valid_total = 0
    corrupt_total = 0
    byte_total = 0
    for recording_dir in recording_dirs:
        inspected = [
            _inspect_artifact(path, recording_dir)
            for path in sorted(recording_dir.rglob("*.json.gz"))
        ]
        valid = sum(1 for item in inspected if item["status"] == "valid")
        corrupt = len(inspected) - valid
        size = sum(int(item["bytes"]) for item in inspected)
        stages: dict[str, int] = {}
        stage_details: dict[str, dict[str, Any]] = {}
        for item in inspected:
            if item["status"] == "valid":
                stage = str(item["stage"])
                stages[stage] = stages.get(stage, 0) + 1
                detail = stage_details.setdefault(
                    stage, {"artifacts": 0, "bytes": 0, "last_written_at": None}
                )
                detail["artifacts"] += 1
                detail["bytes"] += int(item["bytes"])
                if item["last_written_at"] and (
                    detail["last_written_at"] is None
                    or item["last_written_at"] > detail["last_written_at"]
                ):
                    detail["last_written_at"] = item["last_written_at"]
        last_written_at = max(
            (str(item["last_written_at"]) for item in inspected if item["last_written_at"]),
            default=None,
        )
        recordings.append(
            {
                "audio_sha256": recording_dir.name,
                "artifacts": len(inspected),
                "valid_artifacts": valid,
                "corrupt_artifacts": corrupt,
                "bytes": size,
                "stages": stages,
                "stage_details": stage_details,
                "last_written_at": last_written_at,
            }
        )
        valid_total += valid
        corrupt_total += corrupt
        byte_total += size
    recordings.sort(
        key=lambda item: (str(item["last_written_at"] or ""), str(item["audio_sha256"])),
        reverse=True,
    )
    return {
        "artifact_contract": ARTIFACT_CONTRACT_VERSION,
        "artifact_root": str(base),
        "scope_hash": scope,
        "recording_count": len(recordings),
        "valid_artifacts": valid_total,
        "corrupt_artifacts": corrupt_total,
        "bytes": byte_total,
        "recordings": recordings,
    }


def prune_artifacts(
    base_dir: Path,
    scope_hash: str,
    *,
    audio_sha256: Optional[str] = None,
    apply: bool = False,
) -> dict[str, Any]:
    status = inspect_artifacts(base_dir, scope_hash, audio_sha256=audio_sha256)
    base = Path(status["artifact_root"])
    scope = str(status["scope_hash"])
    targets = [
        {
            "audio_sha256": item["audio_sha256"],
            "bytes": item["bytes"],
            "artifacts": item["artifacts"],
            "path": str(base / scope / item["audio_sha256"]),
        }
        for item in status["recordings"]
    ]
    removed = 0
    if apply:
        for target in targets:
            store = ArtifactStore(base, scope, str(target["audio_sha256"]))
            with store.recording_lock():
                recording_dir = store.recording_dir
                if recording_dir.is_symlink():
                    raise ValueError(f"Refusing to prune symlinked recording cache: {recording_dir}")
                if recording_dir.is_dir():
                    shutil.rmtree(recording_dir)
                    removed += 1
    return {
        "dry_run": not apply,
        "artifact_contract": ARTIFACT_CONTRACT_VERSION,
        "scope_hash": scope,
        "targets": targets,
        "target_bytes": sum(int(item["bytes"]) for item in targets),
        "removed_recordings": removed,
    }
