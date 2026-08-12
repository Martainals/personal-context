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


NOTICE_VERSION = 1
MODES = {"strict-local", "agent-assisted"}
PROVIDERS = {"auto", "transcript-only", "qwen-mlx"}

NOTICE = {
    "version": NOTICE_VERSION,
    "local_changes": [
        "Create a user-selected SQLite vault and local evidence directories.",
        "Create an isolated private runtime outside the vault when local audio is enabled.",
        "Download pinned runtime packages and model weights only after explicit consent.",
    ],
    "privacy": [
        "The qwen-mlx provider processes audio locally and never has a cloud fallback.",
        "Strict-local mode keeps transcript text out of the agent context.",
        "Agent-assisted mode permits the named agent host to process transcript text under that host's data policy.",
        "Changing provider, processing mode, vault, this notice version, or an agent-assisted host requires renewed consent.",
    ],
    "governance": [
        "Imported speech remains a Claim rather than an established Fact.",
        "Long-term CandidateMemory records still require explicit item-level approval.",
        "The Skill does not scan unspecified directories, run a daemon, create voiceprints, or silently auto-update.",
    ],
}


class BootstrapError(RuntimeError):
    """Expected onboarding or runtime failure."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


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


def load_manifest() -> dict[str, Any]:
    path = Path(__file__).resolve().parent.parent / "assets" / "providers" / "qwen-mlx.lock.json"
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
    manifest = load_manifest()
    return hashlib.sha256(canonical_json(manifest).encode("utf-8")).hexdigest()


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
    return {
        "system": system,
        "machine": machine,
        "python": platform.python_version(),
        "qwen_mlx_compatible": compatible,
        "qwen_mlx_reason": None if compatible else "qwen-mlx requires macOS on Apple Silicon",
    }


def select_provider(requested: str) -> str:
    if requested not in PROVIDERS:
        raise BootstrapError(f"Unknown provider: {requested}")
    if requested == "auto":
        return "qwen-mlx" if platform_probe()["qwen_mlx_compatible"] else "transcript-only"
    return requested


def _select_with_receipt(requested: str, receipt: Optional[dict[str, Any]]) -> str:
    if requested == "auto" and receipt and receipt.get("provider") in {"transcript-only", "qwen-mlx"}:
        return str(receipt["provider"])
    return select_provider(requested)


def _venv_python(path: Path) -> Path:
    return path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _venv_uv(path: Path) -> Path:
    return path / ("Scripts/uv.exe" if os.name == "nt" else "bin/uv")


def provider_status(provider: str, config_dir: Path) -> dict[str, Any]:
    selected = select_provider(provider)
    if selected == "transcript-only":
        return {"provider": selected, "compatible": True, "installed": True, "ready": True}
    probe = platform_probe()
    runtime = runtime_dir(config_dir)
    manifest = load_manifest()
    python_path = _venv_python(runtime / "venv")
    model_checks = []
    for role, model in manifest["models"].items():
        local_path = runtime / "models" / model["repo_id"].replace("/", "--")
        model_checks.append({"role": role, "path": str(local_path), "present": local_path.is_dir()})
    marker = _read_json(runtime / "runtime.json")
    marker_current = bool(
        marker
        and marker.get("provider") == selected
        and marker.get("profile_version") == manifest.get("profile_version")
        and marker.get("manifest_digest") == hashlib.sha256(canonical_json(manifest).encode("utf-8")).hexdigest()
    )
    installed = python_path.is_file() and all(item["present"] for item in model_checks) and marker_current
    return {
        "provider": selected,
        "compatible": probe["qwen_mlx_compatible"],
        "reason": probe["qwen_mlx_reason"],
        "installed": installed,
        "ready": probe["qwen_mlx_compatible"] and installed,
        "runtime": str(runtime),
        "python": str(python_path),
        "models": model_checks,
        "marker_current": marker_current,
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
    manifest = load_manifest() if selected == "qwen-mlx" else None
    if mode == "agent-assisted" and not agent_host.strip():
        raise BootstrapError("agent-assisted mode requires a non-empty --agent-host.")
    steps = [
        {"action": "record-consent", "writes": str(_receipt_path(root, config_dir))},
    ]
    if database_state.get("status") == "missing":
        steps.append({"action": "init-vault", "writes": str(root.resolve())})
    if selected == "qwen-mlx" and not runtime["ready"]:
        steps.append(
            {
                "action": "install-private-runtime",
                "writes": str(runtime_dir(config_dir)),
                "network": True,
                "download_estimate_gb": manifest["limits"]["download_estimate_gb"],
                "minimum_free_disk_gb": manifest["limits"]["minimum_free_disk_gb"],
                "packages": manifest["runtime"]["packages"],
                "models": [
                    {
                        "role": role,
                        "repo_id": item["repo_id"],
                        "revision": item["revision"],
                        "license": item["license"],
                    }
                    for role, item in manifest["models"].items()
                ],
            }
        )
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
    if selected == "qwen-mlx" and not platform_probe()["qwen_mlx_compatible"]:
        raise BootstrapError("qwen-mlx is incompatible with this machine; use transcript-only.")
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


def _run_checked(command: list[str], *, env: Optional[dict[str, str]] = None) -> None:
    result = subprocess.run(command, capture_output=True, text=True, check=False, env=env)
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "unknown error")[-6000:]
        raise BootstrapError(f"Runtime command failed ({command[0]}): {details.strip()}")


def install_qwen_mlx_runtime(config_dir: Path) -> dict[str, Any]:
    probe = platform_probe()
    if not probe["qwen_mlx_compatible"]:
        raise BootstrapError(probe["qwen_mlx_reason"] or "qwen-mlx is incompatible")
    manifest = load_manifest()
    runtime = runtime_dir(config_dir)
    free_gb = shutil.disk_usage(config_dir.parent if config_dir.parent.exists() else Path.home()).free / (1024 ** 3)
    required = float(manifest["limits"]["minimum_free_disk_gb"])
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
        ],
        env=env,
    )
    marker = {
        "provider": "qwen-mlx",
        "profile_version": manifest["profile_version"],
        "manifest_digest": hashlib.sha256(canonical_json(manifest).encode("utf-8")).hexdigest(),
        "installed_at": utc_now(),
        "python": manifest["runtime"]["python"],
        "packages": manifest["runtime"]["packages"],
    }
    _write_private_json(runtime / "runtime.json", marker)
    return provider_status("qwen-mlx", config_dir)


def bootstrap_apply(
    root: Path,
    *,
    config_dir: Path,
    provider: str,
    agent_host: Optional[str],
    database_state: dict[str, Any],
    init_vault: Callable[[Path], dict[str, Any]],
    install_runtime: Callable[[Path], dict[str, Any]] = install_qwen_mlx_runtime,
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
    if selected == "qwen-mlx" and not runtime["ready"]:
        runtime = install_runtime(config_dir)
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
        raise BootstrapError("The qwen-mlx runtime is not ready; run bootstrap-apply first.")
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
    ]
    if language:
        command.extend(["--language", language])
    if title:
        command.extend(["--title", title])
    if observed_at:
        command.extend(["--observed-at", observed_at])
    env = os.environ.copy()
    env["HF_HOME"] = str(runtime / "huggingface")
    env["HF_HUB_OFFLINE"] = "1"
    _run_checked(command, env=env)
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
        "text_exposed_to_agent": receipt.get("mode") == "agent-assisted",
    }
