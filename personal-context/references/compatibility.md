# Compatibility

## Agent compatibility

The portable contract is standard `SKILL.md` plus local scripts with JSON I/O. Codex metadata is optional.

| Host capability | Result |
|---|---|
| Reads Agent Skills, local files, and runs processes | Full onboarding and database workflow |
| Runs Python/files but has no native Skill loader | Invoke `scripts/personal_context.py` directly using these references |
| Browser-only or no local process access | Cannot bootstrap or operate the local Vault |

Use the repository's standard Skill directory for each host. Installation location is host-specific; database and runtime behavior are not.

The Unix `scripts/context` wrapper is a convenience. The canonical cross-platform entry is the Python file:

```text
python3 scripts/personal_context.py ...   # macOS/Linux
py scripts/personal_context.py ...        # Windows
```

## Compute compatibility

| Environment | SQLite/database | Local audio profile |
|---|---:|---|
| macOS Apple Silicon | Full | `qwen-mlx` |
| Linux/Windows with or without NVIDIA | Full | `transcript-only` in this release |
| Unknown CPU-only environment | Full | `transcript-only` |

`auto` selects `qwen-mlx` only on macOS arm64/aarch64; otherwise it selects `transcript-only`. It never selects a cloud service.

Cross-agent support does not imply that MLX runs on every operating system. Future CUDA or CPU Providers must implement the same transcript contract without importing their dependencies into the core CLI.

## Python compatibility

The database and onboarding control plane support Python 3.9+. The isolated MLX runtime uses its locked Python and packages and does not rely on the system Python satisfying MLX requirements.
