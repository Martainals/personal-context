# Versioning and migration

Skill version and database Schema version are independent:

- Skill version: `VERSION` (`0.9.0`)
- Current Schema: `1`
- Minimum supported Schema: `1`
- Maximum supported Schema: `1`
- Consent notice: `2`
- Provider contract: `1`
- Artifact contract: `1`
- Note Markdown contract: `1`
- `qwen-mlx` profile: `4`
- `qwen-mlx-3dspeaker` profile: `1` (experimental)

`version [--root ...]` reports both declarations and, when supplied, database compatibility.

Skill 0.2.0 adds onboarding and replaceable local transcription without modifying authoritative database tables, so Schema remains 1. Provider provenance is stored in the existing `processing_runs.parameters_json`.

Skill 0.3.0 upgrades the pinned `qwen-mlx` profile to version 2. It replaces the arbitrary five-second diarization chunk with NVIDIA's high-context streaming parameter set, adds an optional per-recording speaker-count hint, preserves frame confidence for whole-recording channel selection, and smooths weak speaker flips before word/turn assembly. The transcript contract and Schema remain 1. Because the provider profile digest changes, existing qwen-mlx consent receipts and runtime markers must be renewed through the normal bootstrap plan and apply flow.

Skill 0.4.0 upgrades `qwen-mlx` to profile 3 and adds artifact contract 1: checksummed JSON/gzip cache entries for ASR chunks, alignment chunks, raw diarization probabilities, derived speaker turns and final assembly. Components use independent cache keys and lazy model loading; corrupt or absent entries are recomputed at stage or chunk granularity. The artifacts are private, disposable state outside the Vault and SQLite, so Schema 1 and provider/transcript contract 1 remain unchanged. Consent Notice 2 explicitly discloses the default persistent cache and manual deletion rules; every Notice 1 receipt becomes invalid and must be renewed rather than edited. Artifact contract, profile, notice and Skill versions can change independently in future releases.

Skill 0.5.0 adds the high-level `capture-audio` delivery boundary and generated Markdown integrity contract. `transcript.v1` remains the internal Provider/import format but normally exists only in a private transient job; the Vault inbox receives one complete Markdown per audio stem. This orchestration reuses existing Source and import functions and changes no authoritative database structure, model, cache artifact or consent semantics. Schema 1, provider/transcript contract 1, artifact contract 1, qwen-mlx profile 3 and Consent Notice 2 all remain current; upgrading the source requires neither database migration, consent renewal nor model reinstall.

Skill 0.5.1 makes transcription explicitly request-driven, returns an intact matching delivery without rerunning the Provider, and adds human-readable cache/source mapping plus aggregate storage metadata. It retains one immutable Vault audio Source, never deletes the caller's original, performs no cross-filename duplicate scan and never cleans caches automatically. These are orchestration and metadata-view changes only: Schema 1, Provider/transcript contract 1, artifact contract 1, qwen-mlx profile 3 and Consent Notice 2 remain unchanged.

Skill 0.6.0 restores punctuation already produced by ASR after forced alignment, splits final segments at sentence endings or 0.8-second pauses, defaults speaker count to automatic unless the user volunteers a hint, and publishes title-based `YYYY-MM-DD HH：MM：SS-内容标题.md` deliveries. Agent-assisted hosts may prepare the short title from a private transient low-level transcript; strict-local mode never exposes text for title generation. Existing ASR, alignment and diarization artifacts remain reusable because punctuation restoration has its own assembly-key version. Schema 1, Provider/transcript contract 1, artifact contract 1, qwen-mlx profile 3 and Consent Notice 2 remain unchanged, so this release requires no database migration, model reinstall or consent renewal.

Skill 0.7.0 adds the explicit experimental `qwen-mlx-3dspeaker` profile. It keeps the locked Qwen ASR and aligner, places 3D-Speaker/Torch in a separate runtime, persists only anonymous turns and scalar cluster-distance evidence, and keys the offline diarization stage independently from ASR and alignment. First-use planning lists both private environments on a fresh machine and only the missing diarizer for an existing current Qwen installation; interrupted installation is resumable and cannot report ready before every locked component passes status checks. The default `auto` choice and qwen-mlx profile 3 remain unchanged; existing qwen-mlx receipts and runtimes therefore remain valid. Selecting the experimental Provider changes the provider/profile digest and requires a new plan, explicit consent and installation. Schema 1, Provider/transcript contract 1, Artifact contract 1 and Consent Notice 2 remain unchanged.

Skill 0.7.1 upgrades `qwen-mlx` to profile 4 and adds a conservative ASR recovery policy. A normal 240-second chunk retains its profile-3 component identity and remains reusable. A chunk dominated by a catastrophic repeated-character run is regenerated as 30-second slices, checked again, aligned slice by slice and stored under a distinct recovery key. A still-pathological smaller slice fails before publication. Existing diarization and valid ASR/alignment sibling chunks remain reusable. Model revisions, runtime packages, Schema 1, provider/transcript contract 1, Artifact contract 1 and Consent Notice 2 remain unchanged, so no model download or database migration is required; the changed Provider profile requires a renewed bootstrap plan, consent receipt and runtime marker.

Skill 0.8.0 adds a host-neutral semantic speaker review protocol in consented `agent-assisted` mode. The Provider creates private six-minute review windows from the unchanged cached assembly plus recording-local confidence; the Agent can return only medium/high-confidence assignments to speakers already present in that recording. A local validator binds decisions to the audio and review-input hashes and preserves every segment's text, timestamps, order and count. Review files are transient rather than a new artifact stage, and final delivery still uses the existing single Markdown renderer. `strict-local` rejects the review options. Schema 1, Provider/transcript contract 1, Artifact contract 1, both provider profiles, model/runtime locks and Consent Notice 2 remain unchanged, so existing databases, models, caches and valid consent receipts need no migration or reinstall.

Skill 0.9.0 adds an explicitly requested recording-note delivery boundary. A consented `agent-assisted` host can organize an intact transcript and its associated immutable audio Source into one private Markdown draft; the local `publish-note` validator binds it to the exact audio and transcript hashes, protects manual edits, and publishes only `notes/<same-transcript-filename>.md`. Transcription-only requests still stop after the single `inbox` transcript, repeat note requests are idempotent, and note creation never reruns Provider stages or creates long-term Memory. New Vaults include `notes/`; existing Schema 1 Vaults create it lazily on first publication. Schema 1, Provider/transcript contract 1, Artifact contract 1, model/runtime locks and Consent Notice 2 remain unchanged, so no migration, consent renewal, model download or reinstall is required.

## Compatibility behavior

| Database state | Reads | Writes | Required action |
|---|---|---|---|
| current | yes | yes | normal operation |
| older | audit/compatible reads only | no | backup and migrate |
| newer | audit only | no | upgrade the Skill |
| unknown/damaged | audit only | no | restore or repair via a known migration |

## Migration sequence

1. `doctor --root <root>`
2. `audit --root <root>`
3. `migrate --root <root>` (dry-run by default)
4. Inspect the plan.
5. `migrate --root <root> --apply`
6. Verify SQLite integrity and run `audit`.

Apply creates a timestamped SQLite backup in `backups/` before changing Schema metadata. Migration records are stable and append-only. Re-running against the current Schema returns `already_current`; it does not repeat destructive work. V1 includes a guarded legacy Schema 0 → 1 bootstrap path only.
