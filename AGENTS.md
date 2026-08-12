# personal-context maintenance contract

This file is the repository-wide source of truth for coding agents. It applies to every file in this repository. `CLAUDE.md` and `GEMINI.md` only point here; do not duplicate these rules into host-specific files.

## Product boundary

`personal-context` is an open Agent Skill and a local executable system, not a Codex-only prompt. Preserve three independent layers:

1. `personal-context/SKILL.md`: agent-neutral policy and workflow.
2. `personal-context/scripts/`: deterministic local CLI, onboarding, provider adapters, and database logic.
3. User-owned state: private configuration/runtime and an explicitly selected vault. Never place user state in the Skill directory.

Codex metadata under `personal-context/agents/openai.yaml` is optional presentation metadata. Core behavior must not depend on it or any host-specific tool name.

## Non-negotiable invariants

- Keep evidence capture separate from long-term memory approval.
- Treat speech and imported statements as `Claim` by default, never as automatic `Fact`.
- Require explicit item-level user approval before creating `Memory`.
- Preserve immutable `Source` and `Segment` evidence and append-only review history.
- Require explicit `--root <vault>` for data commands. Do not infer, scan for, or hardcode a personal vault.
- Never add silent cloud fallback, background watchers, voiceprints, or automatic provider updates.
- Keep provider secrets outside source, database, logs, fixtures, and generated Wiki files.
- Use only synthetic fixtures in tests. Never run development tests against a real personal vault.

## Consent contract

First-use onboarding is a state machine, not prose remembered by an agent:

```text
bootstrap-status → bootstrap-plan → explicit user response
→ record-consent with the exact scoped plan digest → bootstrap-apply
```

Changing the privacy notice, provider, processing mode, vault, or an agent host permitted to process transcript text invalidates the receipt and requires renewed consent. Long-term memory approval remains separate from setup consent.

`strict-local` must not expose transcript text in command output. `agent-assisted` may let only the consented agent host process transcript text under that host's own data policy. Never describe local ASR as proof that the surrounding chat application did not receive an uploaded attachment.

If the notice wording changes materially, increment `NOTICE_VERSION` in `personal_context_bootstrap.py`, update its tests and references, and expect existing receipts to become invalid.

## Database and versioning

Skill version, database Schema version, consent notice version, and provider contract version are independent.

- Change `personal-context/VERSION` for a Skill release.
- Change `SCHEMA_VERSION` only when authoritative database structures or semantics change.
- Add a backup-first migration for every Schema change; never edit an existing user's database ad hoc.
- Keep audio provenance in the existing `processing_runs.parameters_json` unless a demonstrated query requirement justifies a Schema migration.
- Generated Wiki files and `search_index` remain disposable, rebuildable views.

The local audio feature introduced in Skill `0.2.0` must remain compatible with Schema `1`.

## Provider boundary

Every transcription provider must:

- be replaceable without changing database code;
- consume one explicitly named local audio file;
- emit `assets/templates/transcript.v1.json`-compatible UTF-8 JSON;
- include provider/model revision provenance in top-level `processing` metadata;
- avoid importing optional heavy dependencies in the core CLI process;
- expose deterministic compatibility and readiness checks;
- have no cloud fallback unless a future, separately consented provider explicitly says so.

Pinned packages and model revisions belong in `personal-context/assets/providers/*.lock.json`. Do not commit model weights, virtual environments, caches, real transcripts, or consent receipts.

`qwen-mlx` is the production audio profile for macOS Apple Silicon. Unsupported machines must retain the complete database and structured transcript workflow through `transcript-only`; do not pretend MLX is cross-platform.

## Cross-agent and cross-platform rules

- Use standard `SKILL.md` frontmatter and relative references.
- Prefer JSON stdout, JSON errors on stderr, stable exit codes, and non-interactive scripts.
- Do not depend on a particular agent's approval UI, environment variable, plugin manifest, or installation directory.
- Keep `scripts/personal_context.py` directly runnable with Python for Windows compatibility; `scripts/context` is only a Unix convenience wrapper.
- Treat “cross-agent” and “cross-compute-platform” as separate claims in documentation and tests.
- An agent without local file and process execution can read the Skill but cannot bootstrap the local runtime; report that limitation honestly.

## Editing discipline

- Use Python standard library for the database and onboarding control plane where practical.
- Isolate optional MLX imports inside the provider subprocess.
- Keep `SKILL.md` concise and move detailed behavior into one-level `references/` files.
- Avoid parallel implementations of schema, consent, or provider selection logic.
- Preserve unrelated user changes and inspect `git status` before and after work.

## Required validation

Run from the repository root:

```bash
PYTHONPYCACHEPREFIX=/tmp/personal-context-pycache \
  python3 -m unittest discover -s tests -v

PYTHONPYCACHEPREFIX=/tmp/personal-context-pycache \
  python3 -m py_compile \
  personal-context/scripts/personal_context.py \
  personal-context/scripts/personal_context_bootstrap.py \
  personal-context/scripts/providers/qwen_mlx.py
```

Also validate the Skill folder with the available Agent Skill validator. Tests must mock downloads and model inference; downloading multi-gigabyte weights is an explicit user-approved integration step, not a unit test.

Before release, verify:

- `VERSION`, README badges/table, `references/versioning.md`, and `agents/openai.yaml` agree;
- every provider lock uses exact package versions and model revisions;
- bootstrap dry-runs perform no writes;
- consent receipt invalidation and strict-local output behavior are covered;
- Schema 1 databases still pass `doctor` and `audit`;
- repository history contains no user data, credentials, runtime cache, or model weights.
