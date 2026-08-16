# First-use onboarding and consent

## State machine

Always begin with `bootstrap-status`. It is read-only and returns one of:

| Status | Meaning | Required action |
|---|---|---|
| `uninitialized` | No valid receipt and no database | Show `bootstrap-plan`, obtain consent, record it |
| `needs_consent` | Receipt is absent or invalidated | Explain the current plan and renew consent |
| `needs_vault` | Consent is valid but the database is absent | Run `bootstrap-apply` |
| `needs_runtime` | Database exists but the consented Provider is incomplete | Run `bootstrap-apply`; it resumes installation |
| `migration_required` | Database Schema is older | Audit, back up, preview, then migrate |
| `incompatible` | Schema, Provider, or platform cannot safely run | Stop or choose `transcript-only`; never switch to cloud silently |
| `ready` | Consent, database, and Provider agree | Continue with capture or retrieval |

## Required disclosure

Run `bootstrap-plan` and explain its returned facts, not a memorized approximation:

- exact Vault and private configuration paths;
- whether a model runtime will be installed and how much disk/network it needs;
- selected processing mode and Agent host;
- that local ASR has no cloud fallback;
- that the surrounding chat application may already have received a user-uploaded attachment;
- model license and four-speaker diarization limit;
- that local transcription persists checksummed stage artifacts by default, including text, alignment and raw recording-local diarization probabilities;
- the exact private artifact path, that it contains no voiceprints or speaker embeddings, and that cleanup is manual with a dry-run preview;
- that initial consent never approves long-term memory candidates.

Do not run `record-consent` until the user explicitly accepts. Pass the exact current `plan_digest`; it binds the notice, Vault, mode, Provider profile and, for agent-assisted mode, Agent host. A stale, cross-mode, cross-Vault, or invented digest is rejected.

## Consent scopes

`strict-local` authorizes local audio processing, the disclosed private stage artifacts, and local database writes. The Agent must not read transcript text; it may use command metadata, IDs, counts, audit results, and review actions.

`agent-assisted` additionally authorizes the named Agent host to process transcript text under that host's data policy. Changing host requires renewed consent. Changing provider, mode, Vault, or notice version also invalidates the receipt. Consent Notice 2 introduces default persistent transcription artifacts, so every receipt created under Notice 1 is intentionally invalid and must be renewed from a fresh `bootstrap-plan`; do not migrate or rewrite an old receipt in place.

Receipts live in the operating-system user configuration directory or an explicit `--config-dir`, not in SQLite and not in the reusable Skill. They contain no transcript text or secrets.

## Non-interactive design

Scripts never ask questions in the terminal. The Agent owns the user conversation; scripts validate state and persist the result. This makes the flow usable by Codex, Claude Code, Gemini CLI, CI tests, and future hosts without embedding one host's UI protocol.
