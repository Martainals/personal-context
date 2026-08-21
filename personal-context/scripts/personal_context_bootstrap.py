#!/usr/bin/env python3
"""Agent-neutral onboarding, consent, and private runtime management."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import venv
from pathlib import Path
from typing import Any, Callable, Optional

from providers.artifacts import (
    ARTIFACT_CONTRACT_VERSION,
    inspect_artifacts,
    prune_artifacts,
    vault_scope_hash,
)


NOTICE_VERSION = 2
MODES = {"strict-local", "agent-assisted"}
PROVIDERS = {"auto", "transcript-only", "qwen-mlx", "qwen-mlx-3dspeaker"}
LOCAL_AUDIO_PROVIDERS = {"qwen-mlx", "qwen-mlx-3dspeaker"}

NOTICE = {
    "version": NOTICE_VERSION,
    "local_changes": [
        "Create a user-selected SQLite vault and local evidence directories.",
        "Create an isolated private runtime outside the vault when local audio is enabled.",
        "Download pinned runtime packages and model weights only after explicit consent.",
        "Persist checksummed gzip JSON transcription-stage artifacts outside the vault by default so interrupted or repeated work can resume.",
    ],
    "privacy": [
        "The qwen-mlx provider processes audio locally and never has a cloud fallback.",
        "Strict-local mode keeps transcript text out of the agent context.",
        "Agent-assisted mode permits the named agent host to process transcript text under that host's data policy.",
        "Changing provider, processing mode, vault, this notice version, or an agent-assisted host requires renewed consent.",
        "Cached text, alignment, and raw diarization probabilities remain sensitive local data until the user explicitly prunes them; they contain no voiceprints or speaker embeddings.",
    ],
    "governance": [
        "Imported speech remains a Claim rather than an established Fact.",
        "Long-term CandidateMemory records still require explicit item-level approval.",
        "The Skill does not scan unspecified directories, run a daemon, create voiceprints, or silently auto-update.",
        "Transcription cache cleanup is never automatic: status is read-only and pruning previews by default before an explicit apply.",
    ],
}


class BootstrapError(RuntimeError):
    """Expected onboarding or runtime failure."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def manifest_digest(manifest: dict[str, Any]) -> str:
    identity = dict(manifest)
    if identity.get("provider") == "qwen-mlx":
        identity.pop("install_profiles", None)
    return hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()


def offline_runtime_digest(manifest: dict[str, Any]) -> str:
    """Hash only the fields that determine the private 3D-Speaker runtime."""
    identity = {
        "provider": manifest["provider"],
        "runtime": manifest["runtime"],
        "source": {
            "repo": manifest["source"]["repo"],
            "revision": manifest["source"]["revision"],
        },
        "models": {
            role: {
                key: model[key]
                for key in ("repo_id", "revision", "checkpoint")
                if key in model
            }
            for role, model in manifest["models"].items()
        },
    }
    return hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()


def qwen_runtime_digest(
    manifest: dict[str, Any], model_roles: Optional[tuple[str, ...]] = None
) -> str:
    """Hash only packages and model revisions installed in the Qwen runtime."""
    selected_roles = tuple(model_roles or tuple(manifest["models"]))
    identity = {
        "provider": manifest["provider"],
        "runtime": manifest["runtime"],
        "models": {
            role: {
                key: manifest["models"][role][key]
                for key in ("repo_id", "revision")
                if key in manifest["models"][role]
            }
            for role in selected_roles
        },
    }
    return hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()


def notice_digest() -> str:
    return hashlib.sha256(canonical_json(NOTICE).encode("utf-8")).hexdigest()


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def digest_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def default_config_dir() -> Path:
    override = os.environ.get("PERSONAL_CONTEXT_HOME")
    if override:
        return Path(override).expanduser().resolve()
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return (Path(base) / "personal-context").resolve()
    if system == "Darwin":
        return (Path.home() / "Library" / "Application Support" / "personal-context").resolve()
    base = os.environ.get("XDG_CONFIG_HOME")
    return ((Path(base).expanduser() if base else Path.home() / ".config") / "personal-context").resolve()


def resolve_config_dir(value: Optional[str]) -> Path:
    return Path(value).expanduser().resolve() if value else default_config_dir()


def runtime_dir(config_dir: Path) -> Path:
    return config_dir / "runtime"


def artifacts_dir(config_dir: Path) -> Path:
    return config_dir / "artifacts"


def _receipt_path(root: Path, config_dir: Path) -> Path:
    token = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()
    return config_dir / "consents" / f"{token}.json"


def _read_json(path: Path) -> Optional[dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BootstrapError(f"Cannot read private configuration {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BootstrapError(f"Private configuration is not a JSON object: {path}")
    return value


def _write_private_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=str(path.parent))
    temp_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        if os.name != "nt":
            os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def load_manifest(provider: str = "qwen-mlx") -> dict[str, Any]:
    filenames = {
        "qwen-mlx": "qwen-mlx.lock.json",
        "qwen-mlx-3dspeaker": "3dspeaker-offline.lock.json",
    }
    if provider not in filenames:
        raise BootstrapError(f"Provider has no lock manifest: {provider}")
    path = Path(__file__).resolve().parent.parent / "assets" / "providers" / filenames[provider]
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BootstrapError(f"Cannot read provider lock file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BootstrapError("Provider lock file is invalid.")
    return value


def provider_profile_digest(provider: str) -> str:
    if provider == "transcript-only":
        return hashlib.sha256(b"transcript-only-provider-v1").hexdigest()
    if provider == "qwen-mlx-3dspeaker":
        profile: Any = {
            "base_manifest_digest": manifest_digest(load_manifest("qwen-mlx")),
            "diarization": load_manifest(provider),
        }
    else:
        return manifest_digest(load_manifest(provider))
    return hashlib.sha256(canonical_json(profile).encode("utf-8")).hexdigest()


def consent_scope_digest(root: Path, *, provider: str, mode: str, agent_host: str) -> str:
    scope = {
        "notice_digest": notice_digest(),
        "vault_root": str(root.resolve()),
        "provider": provider,
        "provider_profile_digest": provider_profile_digest(provider),
        "mode": mode,
        "agent_host": agent_host if mode == "agent-assisted" else None,
    }
    return hashlib.sha256(canonical_json(scope).encode("utf-8")).hexdigest()


def platform_probe() -> dict[str, Any]:
    system = platform.system() or "Unknown"
    machine = (platform.machine() or "unknown").lower()
    compatible = system == "Darwin" and machine in {"arm64", "aarch64"}
    git_path = shutil.which("git")
    return {
        "system": system,
        "machine": machine,
        "python": platform.python_version(),
        "qwen_mlx_compatible": compatible,
        "qwen_mlx_reason": None if compatible else "qwen-mlx requires macOS on Apple Silicon",
        "git_available": bool(git_path),
        "git_path": git_path,
    }


def select_provider(requested: str) -> str:
    if requested not in PROVIDERS:
        raise BootstrapError(f"Unknown provider: {requested}")
    if requested == "auto":
        return "qwen-mlx" if platform_probe()["qwen_mlx_compatible"] else "transcript-only"
    return requested


def _select_with_receipt(requested: str, receipt: Optional[dict[str, Any]]) -> str:
    if requested == "auto" and receipt and receipt.get("provider") in {
        "transcript-only",
        "qwen-mlx",
        "qwen-mlx-3dspeaker",
    }:
        return str(receipt["provider"])
    return select_provider(requested)


def _venv_python(path: Path) -> Path:
    return path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _venv_uv(path: Path) -> Path:
    return path / ("Scripts/uv.exe" if os.name == "nt" else "bin/uv")


def diarization_runtime_dir(config_dir: Path) -> Path:
    return runtime_dir(config_dir) / "diarizers" / "3dspeaker-offline"


def _qwen_runtime_status(
    config_dir: Path, required_roles: Optional[tuple[str, ...]] = None
) -> dict[str, Any]:
    probe = platform_probe()
    runtime = runtime_dir(config_dir)
    manifest = load_manifest("qwen-mlx")
    python_path = _venv_python(runtime / "venv")
    selected_roles = tuple(required_roles or tuple(manifest["models"]))
    unknown = [role for role in selected_roles if role not in manifest["models"]]
    if unknown:
        raise BootstrapError("Unknown qwen-mlx runtime roles: " + ", ".join(unknown))
    model_checks = []
    for role, model in manifest["models"].items():
        local_path = runtime / "models" / model["repo_id"].replace("/", "--")
        model_checks.append(
            {
                "role": role,
                "path": str(local_path),
                "present": local_path.is_dir(),
                "required": role in selected_roles,
            }
        )
    marker = _read_json(runtime / "runtime.json")
    marker_roles = set(
        (marker or {}).get("model_roles") or tuple(manifest["models"])
    )
    expected_runtime_digest = qwen_runtime_digest(manifest, selected_roles)
    legacy_profile_3_marker = bool(
        marker
        and marker.get("runtime_digest") is None
        and marker.get("manifest_digest")
        == "59c246c139563a578339f0bd9fdde16f71c35b1a570ddf0b18ec7d56a65db750"
        and marker.get("python") == manifest["runtime"]["python"]
        and marker.get("packages") == manifest["runtime"]["packages"]
    )
    marker_current = bool(
        marker
        and marker.get("provider") == "qwen-mlx"
        and (
            marker.get("runtime_digest") == expected_runtime_digest
            or legacy_profile_3_marker
        )
        and set(selected_roles).issubset(marker_roles)
    )
    installed = (
        python_path.is_file()
        and all(item["present"] for item in model_checks if item["required"])
        and marker_current
    )
    missing_components = []
    if not python_path.is_file():
        missing_components.append("python")
    missing_components.extend(
        f"{item['role']}_model"
        for item in model_checks
        if item["required"] and not item["present"]
    )
    if not marker_current:
        missing_components.append("runtime_marker")
    return {
        "provider": "qwen-mlx",
        "compatible": probe["qwen_mlx_compatible"],
        "reason": probe["qwen_mlx_reason"],
        "installed": installed,
        "ready": probe["qwen_mlx_compatible"] and installed,
        "runtime": str(runtime),
        "python": str(python_path),
        "models": model_checks,
        "marker_current": marker_current,
        "missing_components": missing_components,
        "required_model_roles": list(selected_roles),
    }


def _offline_diarization_status(config_dir: Path) -> dict[str, Any]:
    probe = platform_probe()
    runtime = diarization_runtime_dir(config_dir)
    manifest = load_manifest("qwen-mlx-3dspeaker")
    python_path = _venv_python(runtime / "venv")
    source_path = runtime / "source"
    model_marker = _read_json(runtime / "models" / "model-paths.json")
    models = []
    for role in manifest["models"]:
        local_path = Path(str((model_marker or {}).get(role, "")))
        models.append(
            {
                "role": role,
                "path": str(local_path) if str(local_path) != "." else None,
                "present": bool(str(local_path) != "." and local_path.is_dir()),
            }
        )
    marker = _read_json(runtime / "runtime.json")
    digest = offline_runtime_digest(manifest)
    legacy_runtime_matches = bool(
        marker
        and marker.get("python") == manifest["runtime"]["python"]
        and marker.get("packages") == manifest["runtime"]["packages"]
        and marker.get("source_revision") == manifest["source"]["revision"]
        and all(
            item["present"]
            and Path(str(item["path"])).name == manifest["models"][item["role"]]["revision"]
            and manifest["models"][item["role"]]["repo_id"].replace("/", "--")
            in str(item["path"])
            for item in models
        )
    )
    marker_current = bool(
        marker
        and marker.get("provider") == "qwen-mlx-3dspeaker"
        and marker.get("source_revision") == manifest["source"]["revision"]
        and (
            marker.get("runtime_digest") == digest
            or (
                marker.get("runtime_digest") is None
                and legacy_runtime_matches
            )
        )
    )
    installed = bool(
        python_path.is_file()
        and (source_path / "speakerlab").is_dir()
        and all(item["present"] for item in models)
        and marker_current
    )
    git_available = bool(probe.get("git_available", shutil.which("git")))
    compatible = bool(
        probe["qwen_mlx_compatible"] and (git_available or installed)
    )
    reason = probe["qwen_mlx_reason"]
    if probe["qwen_mlx_compatible"] and not git_available and not installed:
        reason = "3D-Speaker installation requires the git executable"
    missing_components = []
    if not python_path.is_file():
        missing_components.append("python")
    if not (source_path / "speakerlab").is_dir():
        missing_components.append("source")
    missing_components.extend(
        f"{item['role']}_model" for item in models if not item["present"]
    )
    if not marker_current:
        missing_components.append("runtime_marker")
    return {
        "provider": "3dspeaker-offline",
        "compatible": compatible,
        "reason": reason,
        "installed": installed,
        "ready": compatible and installed,
        "runtime": str(runtime),
        "python": str(python_path),
        "source": str(source_path),
        "models": models,
        "marker_current": marker_current,
        "missing_components": missing_components,
    }


def provider_status(provider: str, config_dir: Path) -> dict[str, Any]:
    selected = select_provider(provider)
    if selected == "transcript-only":
        return {"provider": selected, "compatible": True, "installed": True, "ready": True}
    base = _qwen_runtime_status(
        config_dir,
        required_roles=(
            ("asr", "aligner")
            if selected == "qwen-mlx-3dspeaker"
            else None
        ),
    )
    if selected == "qwen-mlx":
        return base
    offline = _offline_diarization_status(config_dir)
    return {
        "provider": selected,
        "compatible": bool(base["compatible"] and offline["compatible"]),
        "reason": base.get("reason") or offline.get("reason"),
        "installed": bool(base["installed"] and offline["installed"]),
        "ready": bool(base["ready"] and offline["ready"]),
        "base_runtime_ready": bool(base["ready"]),
        "diarization_runtime_ready": bool(offline["ready"]),
        "base_runtime": base,
        "diarization_runtime": offline,
        "missing_components": [
            *[f"base:{item}" for item in base.get("missing_components", [])],
            *[
                f"diarization:{item}"
                for item in offline.get("missing_components", [])
            ],
        ],
        "experimental": True,
    }


def _receipt_matches(
    receipt: Optional[dict[str, Any]], *, root: Path, provider: str, agent_host: Optional[str]
) -> tuple[bool, Optional[str]]:
    if not receipt:
        return False, "missing"
    if receipt.get("notice_digest") != notice_digest() or receipt.get("notice_version") != NOTICE_VERSION:
        return False, "notice_changed"
    if receipt.get("vault_root") != str(root.resolve()):
        return False, "vault_changed"
    if receipt.get("provider") != provider:
        return False, "provider_changed"
    mode = receipt.get("mode")
    if mode not in MODES:
        return False, "invalid_mode"
    if mode == "agent-assisted" and receipt.get("agent_host") != agent_host:
        return False, "agent_host_changed"
    expected_scope = consent_scope_digest(
        root,
        provider=provider,
        mode=str(mode),
        agent_host=str(receipt.get("agent_host") or ""),
    )
    if receipt.get("plan_digest") != expected_scope:
        return False, "scope_changed"
    return True, None


def bootstrap_status(
    root: Path,
    *,
    config_dir: Path,
    provider: str,
    agent_host: Optional[str],
    database_state: dict[str, Any],
) -> dict[str, Any]:
    receipt_path = _receipt_path(root, config_dir)
    receipt = _read_json(receipt_path)
    selected = _select_with_receipt(provider, receipt)
    consent_ok, consent_reason = _receipt_matches(
        receipt, root=root, provider=selected, agent_host=agent_host
    )
    runtime = provider_status(selected, config_dir)
    database_status = database_state.get("status")
    if not consent_ok and database_status == "missing":
        status = "uninitialized"
    elif not consent_ok:
        status = "needs_consent"
    elif not runtime["compatible"]:
        status = "incompatible"
    elif database_status == "missing":
        status = "needs_vault"
    elif database_status == "older":
        status = "migration_required"
    elif database_status != "current":
        status = "incompatible"
    elif not runtime["ready"]:
        status = "needs_runtime"
    else:
        status = "ready"
    return {
        "status": status,
        "vault_root": str(root.resolve()),
        "config_dir": str(config_dir),
        "provider": selected,
        "agent_host": agent_host,
        "consent": {
            "valid": consent_ok,
            "reason": consent_reason,
            "receipt": str(receipt_path),
            "mode": receipt.get("mode") if receipt else None,
            "plan_digest": receipt.get("plan_digest") if receipt else None,
        },
        "database": database_state,
        "runtime": runtime,
        "notice_digest": notice_digest(),
        "next_action": {
            "uninitialized": "Show bootstrap-plan, obtain explicit consent, then record-consent.",
            "needs_consent": "Show bootstrap-plan and renew explicit consent.",
            "needs_vault": "Run bootstrap-apply after verifying the receipt.",
            "needs_runtime": "Run bootstrap-apply to install the consented private runtime.",
            "migration_required": "Audit, back up, preview migration, then apply migration.",
            "incompatible": "Use transcript-only or a supported provider; never silently use cloud processing.",
            "ready": "Use the normal capture, review, and retrieval workflow.",
        }[status],
    }


def bootstrap_plan(
    root: Path,
    *,
    config_dir: Path,
    mode: str,
    provider: str,
    agent_host: str,
    database_state: dict[str, Any],
) -> dict[str, Any]:
    if mode not in MODES:
        raise BootstrapError(f"Unknown processing mode: {mode}")
    selected = select_provider(provider)
    runtime = provider_status(selected, config_dir)
    base_manifest = load_manifest("qwen-mlx") if selected in LOCAL_AUDIO_PROVIDERS else None
    offline_manifest = (
        load_manifest("qwen-mlx-3dspeaker")
        if selected == "qwen-mlx-3dspeaker"
        else None
    )
    if mode == "agent-assisted" and not agent_host.strip():
        raise BootstrapError("agent-assisted mode requires a non-empty --agent-host.")
    steps = [
        {"action": "record-consent", "writes": str(_receipt_path(root, config_dir))},
    ]
    if database_state.get("status") == "missing":
        steps.append({"action": "init-vault", "writes": str(root.resolve())})
    if selected in LOCAL_AUDIO_PROVIDERS:
        steps.append(
            {
                "action": "enable-private-transcription-artifacts",
                "writes": str(artifacts_dir(config_dir) / vault_scope_hash(root)),
                "default_enabled": True,
                "artifact_contract": ARTIFACT_CONTRACT_VERSION,
                "format": "checksummed gzip JSON",
                "manual_prune_only": True,
            }
        )
    base_runtime_ready = bool(
        runtime.get("base_runtime_ready", runtime.get("ready", False))
    )
    if selected in LOCAL_AUDIO_PROVIDERS and not base_runtime_ready:
        install_profile_name = (
            "asr_alignment" if selected == "qwen-mlx-3dspeaker" else "full"
        )
        install_profile = base_manifest["install_profiles"][install_profile_name]
        steps.append(
            {
                "action": "install-private-runtime",
                "install_profile": install_profile_name,
                "writes": str(runtime_dir(config_dir)),
                "network": True,
                "download_estimate_gb": install_profile["download_estimate_gb"],
                "minimum_free_disk_gb": install_profile["minimum_free_disk_gb"],
                "packages": base_manifest["runtime"]["packages"],
                "models": [
                    {
                        "role": role,
                        "repo_id": item["repo_id"],
                        "revision": item["revision"],
                        "license": item["license"],
                    }
                    for role, item in base_manifest["models"].items()
                    if role in install_profile["model_roles"]
                ],
            }
        )
    if (
        selected == "qwen-mlx-3dspeaker"
        and not bool(runtime.get("diarization_runtime_ready", False))
    ):
        steps.append(
            {
                "action": "install-private-diarization-runtime",
                "writes": str(diarization_runtime_dir(config_dir)),
                "network": True,
                "download_estimate_gb": offline_manifest["limits"]["download_estimate_gb"],
                "minimum_free_disk_gb": offline_manifest["limits"]["minimum_free_disk_gb"],
                "packages": offline_manifest["runtime"]["packages"],
                "source": offline_manifest["source"],
                "system_requirements": ["git"],
                "models": [
                    {
                        "role": role,
                        "repo_id": item["repo_id"],
                        "revision": item["revision"],
                        "license": item["license"],
                    }
                    for role, item in offline_manifest["models"].items()
                ],
                "privacy": offline_manifest["privacy"],
                "experimental": True,
            }
        )
    install_steps = [
        step
        for step in steps
        if step["action"]
        in {"install-private-runtime", "install-private-diarization-runtime"}
    ]
    return {
        "dry_run": True,
        "vault_root": str(root.resolve()),
        "config_dir": str(config_dir),
        "mode": mode,
        "provider": selected,
        "agent_host": agent_host,
        "platform": platform_probe(),
        "compatible": runtime["compatible"],
        "notice": NOTICE,
        "notice_digest": notice_digest(),
        "provider_profile": (
            {
                "profile_version": (
                    offline_manifest["profile_version"]
                    if offline_manifest
                    else base_manifest["profile_version"]
                ),
                "maximum_speakers": (
                    offline_manifest["limits"]["maximum_speakers"]
                    if offline_manifest
                    else base_manifest["limits"]["maximum_speakers"]
                ),
                "asr_chunk_seconds": base_manifest["limits"]["asr_chunk_seconds"],
                "diarization": (
                    offline_manifest["diarization"]
                    if offline_manifest
                    else base_manifest["diarization"]
                ),
                "artifacts": base_manifest.get("artifacts"),
                "asr_recovery": base_manifest.get("asr_recovery"),
                "experimental": bool(offline_manifest),
                "privacy": offline_manifest.get("privacy") if offline_manifest else None,
            }
            if base_manifest
            else None
        ),
        "installation": {
            "required": bool(install_steps),
            "network": any(bool(step.get("network")) for step in install_steps),
            "download_estimate_gb": sum(
                float(step.get("download_estimate_gb", 0)) for step in install_steps
            ),
            "minimum_free_disk_gb": sum(
                float(step.get("minimum_free_disk_gb", 0)) for step in install_steps
            ),
            "private_runtime_roots": [step["writes"] for step in install_steps],
            "system_python_modified": False,
            "background_service": False,
            "resume_action": "rerun-bootstrap-apply",
        },
        "plan_digest": consent_scope_digest(
            root, provider=selected, mode=mode, agent_host=agent_host
        ),
        "steps": steps,
        "requires_explicit_user_consent": True,
        "cloud_fallback": False,
    }


def record_consent(
    root: Path,
    *,
    config_dir: Path,
    mode: str,
    provider: str,
    agent_host: str,
    accepted_digest: str,
) -> dict[str, Any]:
    if mode not in MODES:
        raise BootstrapError(f"Unknown processing mode: {mode}")
    selected = select_provider(provider)
    if selected in LOCAL_AUDIO_PROVIDERS and not platform_probe()["qwen_mlx_compatible"]:
        raise BootstrapError(f"{selected} is incompatible with this machine; use transcript-only.")
    if mode == "agent-assisted" and not agent_host.strip():
        raise BootstrapError("agent-assisted mode requires a non-empty --agent-host.")
    expected_scope = consent_scope_digest(
        root, provider=selected, mode=mode, agent_host=agent_host
    )
    if accepted_digest != expected_scope:
        raise BootstrapError("Plan digest does not match. Show the current bootstrap-plan and ask again.")
    receipt = {
        "receipt_version": 1,
        "notice_version": NOTICE_VERSION,
        "notice_digest": notice_digest(),
        "plan_digest": expected_scope,
        "vault_root": str(root.resolve()),
        "provider": selected,
        "mode": mode,
        "agent_host": agent_host,
        "accepted_at": utc_now(),
        "accepted_by": "user",
    }
    path = _receipt_path(root, config_dir)
    _write_private_json(path, receipt)
    return {
        "status": "consent_recorded",
        "receipt": str(path),
        "provider": selected,
        "mode": mode,
        "agent_host": agent_host,
        "notice_digest": notice_digest(),
        "plan_digest": expected_scope,
    }


def _run_checked(
    command: list[str], *, env: Optional[dict[str, str]] = None
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, capture_output=True, text=True, check=False, env=env)
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "unknown error")[-6000:]
        raise BootstrapError(f"Runtime command failed ({command[0]}): {details.strip()}")
    return result


def install_qwen_mlx_runtime(
    config_dir: Path, *, model_roles: Optional[tuple[str, ...]] = None
) -> dict[str, Any]:
    probe = platform_probe()
    if not probe["qwen_mlx_compatible"]:
        raise BootstrapError(probe["qwen_mlx_reason"] or "qwen-mlx is incompatible")
    manifest = load_manifest()
    selected_roles = tuple(model_roles or tuple(manifest["models"]))
    install_profile = next(
        (
            value
            for value in manifest["install_profiles"].values()
            if tuple(value["model_roles"]) == selected_roles
        ),
        None,
    )
    if install_profile is None:
        raise BootstrapError(
            "Unsupported qwen-mlx install role set: " + ", ".join(selected_roles)
        )
    runtime = runtime_dir(config_dir)
    free_gb = shutil.disk_usage(config_dir.parent if config_dir.parent.exists() else Path.home()).free / (1024 ** 3)
    required = float(install_profile["minimum_free_disk_gb"])
    if free_gb < required:
        raise BootstrapError(f"At least {required:g} GB free disk is required; detected {free_gb:.1f} GB.")
    runtime.mkdir(parents=True, exist_ok=True)
    bootstrap_venv = runtime / "bootstrap"
    uv_path = _venv_uv(bootstrap_venv)
    if not uv_path.is_file():
        venv.EnvBuilder(with_pip=True, clear=False).create(bootstrap_venv)
        _run_checked(
            [str(_venv_python(bootstrap_venv)), "-m", "pip", "install", "--disable-pip-version-check", f"uv=={manifest['runtime']['uv']}"]
        )
    env = os.environ.copy()
    env.update(
        {
            "UV_PYTHON_INSTALL_DIR": str(runtime / "python"),
            "UV_PYTHON_INSTALL_BIN": "0",
            "UV_CACHE_DIR": str(runtime / "cache"),
            "HF_HOME": str(runtime / "huggingface"),
        }
    )
    venv_path = runtime / "venv"
    python_path = _venv_python(venv_path)
    if not python_path.is_file():
        _run_checked(
            [str(uv_path), "venv", "--python", manifest["runtime"]["python"], str(venv_path)], env=env
        )
    _run_checked(
        [str(uv_path), "pip", "install", "--python", str(python_path), *manifest["runtime"]["packages"]],
        env=env,
    )
    provider_script = Path(__file__).resolve().parent / "providers" / "qwen_mlx.py"
    manifest_path = Path(__file__).resolve().parent.parent / "assets" / "providers" / "qwen-mlx.lock.json"
    _run_checked(
        [
            str(python_path),
            str(provider_script),
            "download",
            "--manifest",
            str(manifest_path),
            "--models-dir",
            str(runtime / "models"),
            *[
                item
                for role in selected_roles
                for item in ("--model-role", role)
            ],
        ],
        env=env,
    )
    previous_marker = _read_json(runtime / "runtime.json") or {}
    current_runtime_digest = qwen_runtime_digest(manifest, selected_roles)
    previous_marker_current = bool(
        previous_marker.get("provider") == "qwen-mlx"
        and previous_marker.get("runtime_digest") == current_runtime_digest
    )
    previous_roles = (
        set(previous_marker.get("model_roles") or tuple(manifest["models"]))
        if previous_marker_current
        else set()
    )
    marker = {
        "provider": "qwen-mlx",
        "profile_version": manifest["profile_version"],
        "manifest_digest": manifest_digest(manifest),
        "runtime_digest": current_runtime_digest,
        "installed_at": utc_now(),
        "python": manifest["runtime"]["python"],
        "packages": manifest["runtime"]["packages"],
        "model_roles": [
            role
            for role, model in manifest["models"].items()
            if role in set(selected_roles)
            or (
                role in previous_roles
                and (
                    runtime
                    / "models"
                    / model["repo_id"].replace("/", "--")
                ).is_dir()
            )
        ],
    }
    _write_private_json(runtime / "runtime.json", marker)
    return _qwen_runtime_status(config_dir, required_roles=selected_roles)


def install_qwen_asr_alignment_runtime(config_dir: Path) -> dict[str, Any]:
    return install_qwen_mlx_runtime(
        config_dir, model_roles=("asr", "aligner")
    )


def install_3dspeaker_runtime(config_dir: Path) -> dict[str, Any]:
    probe = platform_probe()
    if not probe["qwen_mlx_compatible"]:
        raise BootstrapError(probe["qwen_mlx_reason"] or "3D-Speaker is incompatible")
    if not shutil.which("git"):
        raise BootstrapError(
            "3D-Speaker installation requires the git executable on PATH"
        )
    manifest = load_manifest("qwen-mlx-3dspeaker")
    runtime = diarization_runtime_dir(config_dir)
    free_gb = shutil.disk_usage(
        config_dir.parent if config_dir.parent.exists() else Path.home()
    ).free / (1024 ** 3)
    required = float(manifest["limits"]["minimum_free_disk_gb"])
    if free_gb < required:
        raise BootstrapError(
            f"At least {required:g} GB free disk is required for 3D-Speaker; detected {free_gb:.1f} GB."
        )
    runtime.mkdir(parents=True, exist_ok=True)
    bootstrap_venv = runtime / "bootstrap"
    uv_path = _venv_uv(bootstrap_venv)
    if not uv_path.is_file():
        venv.EnvBuilder(with_pip=True, clear=False).create(bootstrap_venv)
        _run_checked(
            [
                str(_venv_python(bootstrap_venv)),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                f"uv=={manifest['runtime']['uv']}",
            ]
        )
    env = os.environ.copy()
    env.update(
        {
            "UV_PYTHON_INSTALL_DIR": str(runtime / "python"),
            "UV_PYTHON_INSTALL_BIN": "0",
            "UV_CACHE_DIR": str(runtime / "cache"),
            "MODELSCOPE_CACHE": str(runtime / "modelscope"),
        }
    )
    venv_path = runtime / "venv"
    python_path = _venv_python(venv_path)
    if not python_path.is_file():
        _run_checked(
            [str(uv_path), "venv", "--python", manifest["runtime"]["python"], str(venv_path)],
            env=env,
        )
    _run_checked(
        [
            str(uv_path),
            "pip",
            "install",
            "--python",
            str(python_path),
            *manifest["runtime"]["packages"],
        ],
        env=env,
    )
    source_dir = runtime / "source"
    if source_dir.exists() and not (source_dir / ".git").is_dir():
        raise BootstrapError(f"Refusing to reuse a non-git 3D-Speaker source directory: {source_dir}")
    if not source_dir.exists():
        temporary_source = runtime / f".source-clone-{os.getpid()}"
        if temporary_source.exists():
            shutil.rmtree(temporary_source)
        try:
            _run_checked(
                [
                    "git",
                    "clone",
                    "--no-checkout",
                    manifest["source"]["repo"],
                    str(temporary_source),
                ],
                env=env,
            )
            os.replace(temporary_source, source_dir)
        finally:
            if temporary_source.exists():
                shutil.rmtree(temporary_source)
    _run_checked(
        [
            "git",
            "-C",
            str(source_dir),
            "fetch",
            "--depth",
            "1",
            "origin",
            manifest["source"]["revision"],
        ],
        env=env,
    )
    _run_checked(
        [
            "git",
            "-C",
            str(source_dir),
            "checkout",
            "--detach",
            manifest["source"]["revision"],
        ],
        env=env,
    )
    provider_script = Path(__file__).resolve().parent / "providers" / "diarization_3dspeaker.py"
    manifest_path = (
        Path(__file__).resolve().parent.parent
        / "assets"
        / "providers"
        / "3dspeaker-offline.lock.json"
    )
    _run_checked(
        [
            str(python_path),
            str(provider_script),
            "download",
            "--manifest",
            str(manifest_path),
            "--source-dir",
            str(source_dir),
            "--models-dir",
            str(runtime / "models"),
        ],
        env=env,
    )
    marker = {
        "provider": "qwen-mlx-3dspeaker",
        "profile_version": manifest["profile_version"],
        "manifest_digest": manifest_digest(manifest),
        "runtime_digest": offline_runtime_digest(manifest),
        "source_revision": manifest["source"]["revision"],
        "installed_at": utc_now(),
        "python": manifest["runtime"]["python"],
        "packages": manifest["runtime"]["packages"],
    }
    _write_private_json(runtime / "runtime.json", marker)
    return _offline_diarization_status(config_dir)


def bootstrap_apply(
    root: Path,
    *,
    config_dir: Path,
    provider: str,
    agent_host: Optional[str],
    database_state: dict[str, Any],
    init_vault: Callable[[Path], dict[str, Any]],
    install_runtime: Callable[[Path], dict[str, Any]] = install_qwen_mlx_runtime,
    install_asr_alignment_runtime: Callable[
        [Path], dict[str, Any]
    ] = install_qwen_asr_alignment_runtime,
    install_diarization_runtime: Callable[[Path], dict[str, Any]] = install_3dspeaker_runtime,
) -> dict[str, Any]:
    receipt = _read_json(_receipt_path(root, config_dir))
    selected = _select_with_receipt(provider, receipt)
    consent_ok, reason = _receipt_matches(receipt, root=root, provider=selected, agent_host=agent_host)
    if not consent_ok:
        raise BootstrapError(f"Valid consent is required before bootstrap-apply: {reason}")
    current_provider = provider_status(selected, config_dir)
    if not current_provider["compatible"]:
        raise BootstrapError(current_provider.get("reason") or f"Provider {selected} is incompatible")
    if database_state.get("status") == "missing":
        database = init_vault(root)
    elif database_state.get("status") == "current":
        database = {"status": "already_initialized", "root": str(root), "schema_version": database_state.get("version")}
    else:
        raise BootstrapError(f"Cannot bootstrap over a {database_state.get('status')} database.")
    runtime = current_provider
    if selected in LOCAL_AUDIO_PROVIDERS:
        base_ready = bool(runtime.get("base_runtime_ready", runtime.get("ready", False)))
        if not base_ready:
            if selected == "qwen-mlx-3dspeaker":
                install_asr_alignment_runtime(config_dir)
            else:
                install_runtime(config_dir)
        if selected == "qwen-mlx-3dspeaker" and not bool(
            runtime.get("diarization_runtime_ready", False)
        ):
            install_diarization_runtime(config_dir)
        runtime = provider_status(selected, config_dir)
        if not runtime["ready"]:
            missing = runtime.get("missing_components") or ["unknown"]
            raise BootstrapError(
                "Private provider installation is incomplete; rerun bootstrap-status "
                "and bootstrap-apply. Missing components: " + ", ".join(missing)
            )
    return {"status": "ready", "database": database, "runtime": runtime, "consent": str(_receipt_path(root, config_dir))}


def transcribe_audio(
    root: Path,
    *,
    config_dir: Path,
    provider: str,
    agent_host: Optional[str],
    audio: Path,
    output: Path,
    language: Optional[str],
    title: Optional[str],
    observed_at: Optional[str],
    speaker_count: Optional[int] = None,
    no_cache: bool = False,
    refresh_stage: Optional[str] = None,
) -> dict[str, Any]:
    receipt = _read_json(_receipt_path(root, config_dir))
    selected = _select_with_receipt(provider, receipt)
    consent_ok, reason = _receipt_matches(receipt, root=root, provider=selected, agent_host=agent_host)
    if not consent_ok:
        raise BootstrapError(f"Valid consent is required before transcription: {reason}")
    if selected == "transcript-only":
        raise BootstrapError("transcript-only cannot transcribe audio; import an existing transcript instead.")
    status = provider_status(selected, config_dir)
    if not status["ready"]:
        raise BootstrapError(f"The {selected} runtime is not ready; run bootstrap-apply first.")
    audio_path = audio.expanduser().resolve()
    if not audio_path.is_file():
        raise BootstrapError(f"Audio file not found: {audio_path}")
    output_path = output.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    runtime = runtime_dir(config_dir)
    manifest_path = Path(__file__).resolve().parent.parent / "assets" / "providers" / "qwen-mlx.lock.json"
    command = [
        str(_venv_python(runtime / "venv")),
        str(Path(__file__).resolve().parent / "providers" / "qwen_mlx.py"),
        "transcribe",
        "--manifest",
        str(manifest_path),
        "--models-dir",
        str(runtime / "models"),
        "--audio",
        str(audio_path),
        "--output",
        str(output_path),
        "--artifacts-dir",
        str(artifacts_dir(config_dir)),
        "--vault-scope",
        vault_scope_hash(root),
        "--provider-name",
        selected,
    ]
    if selected == "qwen-mlx-3dspeaker":
        offline_runtime = diarization_runtime_dir(config_dir)
        command.extend(
            [
                "--diarization-manifest",
                str(
                    Path(__file__).resolve().parent.parent
                    / "assets"
                    / "providers"
                    / "3dspeaker-offline.lock.json"
                ),
                "--offline-python",
                str(_venv_python(offline_runtime / "venv")),
                "--offline-script",
                str(
                    Path(__file__).resolve().parent
                    / "providers"
                    / "diarization_3dspeaker.py"
                ),
                "--offline-source-dir",
                str(offline_runtime / "source"),
                "--offline-models-dir",
                str(offline_runtime / "models"),
            ]
        )
    if language:
        command.extend(["--language", language])
    if speaker_count is not None:
        maximum_speakers = int(load_manifest(selected)["limits"]["maximum_speakers"])
        if not 1 <= speaker_count <= maximum_speakers:
            raise BootstrapError(f"speaker-count must be between 1 and {maximum_speakers}.")
        command.extend(["--speaker-count", str(speaker_count)])
    if title:
        command.extend(["--title", title])
    if observed_at:
        command.extend(["--observed-at", observed_at])
    if no_cache:
        command.append("--no-cache")
    if refresh_stage is not None:
        if refresh_stage not in {"asr", "alignment", "diarization", "all"}:
            raise BootstrapError(f"Unknown refresh stage: {refresh_stage}")
        if no_cache:
            raise BootstrapError("--no-cache cannot be combined with --refresh-stage.")
        command.extend(["--refresh-stage", refresh_stage])
    env = os.environ.copy()
    env["HF_HOME"] = str(runtime / "huggingface")
    env["HF_HUB_OFFLINE"] = "1"
    if selected == "qwen-mlx-3dspeaker":
        env["MODELSCOPE_CACHE"] = str(diarization_runtime_dir(config_dir) / "modelscope")
    provider_process = _run_checked(command, env=env)
    provider_operation: dict[str, Any] = {}
    if provider_process is not None and provider_process.stdout.strip():
        try:
            parsed_operation = json.loads(provider_process.stdout)
        except json.JSONDecodeError as exc:
            raise BootstrapError(f"Provider operation metadata is not valid JSON: {exc}") from exc
        if not isinstance(parsed_operation, dict):
            raise BootstrapError("Provider operation metadata must be a JSON object.")
        provider_operation = parsed_operation
    if not output_path.is_file():
        raise BootstrapError("Transcription provider completed without producing the requested output.")
    try:
        document = json.loads(output_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BootstrapError(f"Provider output is not valid UTF-8 JSON: {exc}") from exc
    segments = document.get("segments") if isinstance(document, dict) else None
    processing = document.get("processing") if isinstance(document, dict) else None
    if not isinstance(segments, list) or not segments:
        raise BootstrapError("Provider output has no transcript segments.")
    if not isinstance(processing, dict) or processing.get("provider") != selected:
        raise BootstrapError("Provider output is missing matching processing provenance.")
    audio_hash = digest_file(audio_path)
    if processing.get("source_audio_sha256") != audio_hash:
        raise BootstrapError("Provider output does not match the named source audio hash.")
    output_hash = digest_file(output_path)
    return {
        "status": "transcribed",
        "provider": selected,
        "mode": receipt.get("mode"),
        "transcript": str(output_path),
        "sha256": output_hash,
        "bytes": output_path.stat().st_size,
        "segments": len(segments),
        "cache": provider_operation.get("cache"),
        "text_exposed_to_agent": receipt.get("mode") == "agent-assisted",
    }


def _optional_audio_hash(audio: Optional[Path]) -> Optional[str]:
    if audio is None:
        return None
    path = audio.expanduser().resolve()
    if not path.is_file():
        raise BootstrapError(f"Audio file not found: {path}")
    return digest_file(path)


def transcription_cache_status(
    root: Path,
    *,
    config_dir: Path,
    audio: Optional[Path] = None,
    audio_sha256: Optional[str] = None,
) -> dict[str, Any]:
    if audio is not None and audio_sha256 is not None:
        raise BootstrapError("Select cache by audio path or audio SHA-256, not both.")
    result = inspect_artifacts(
        artifacts_dir(config_dir),
        vault_scope_hash(root),
        audio_sha256=_optional_audio_hash(audio) if audio is not None else audio_sha256,
    )
    return {"status": "ok", "vault_root": str(root.resolve()), **result}


def transcription_cache_prune(
    root: Path,
    *,
    config_dir: Path,
    audio: Optional[Path] = None,
    audio_sha256: Optional[str] = None,
    apply: bool = False,
) -> dict[str, Any]:
    if audio is not None and audio_sha256 is not None:
        raise BootstrapError("Select cache by audio path or audio SHA-256, not both.")
    result = prune_artifacts(
        artifacts_dir(config_dir),
        vault_scope_hash(root),
        audio_sha256=_optional_audio_hash(audio) if audio is not None else audio_sha256,
        apply=apply,
    )
    return {"status": "pruned" if apply else "preview", "vault_root": str(root.resolve()), **result}
