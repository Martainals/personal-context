# Architecture

## Boundaries

`personal-context` is reusable software and policy. A user-selected vault and private runtime are user-owned state. The call direction is:

```text
User → compatible local Agent → personal-context → configured vault
```

The vault never invokes the Skill. Every data command receives `--root`; the implementation contains no personal absolute path. The database control plane uses Python's standard library plus SQLite. Optional model dependencies remain isolated in a Provider subprocess.

## Three layers

```text
Agent-neutral SKILL.md and JSON CLI
→ consented private runtime and replaceable Provider
→ one user-selected SQLite Vault
```

The private configuration and runtime use the operating-system user configuration directory or explicit `--config-dir`. Model weights, consent receipts, caches, and transient capture jobs never live in the Git checkout or database.

## Vault layout

```text
<root>/
├── context.sqlite3     # authoritative structured database
├── blobs/              # immutable, SHA-256-addressed Source bytes
├── inbox/              # completed human-readable deliveries only
├── wiki/               # generated human-readable view
└── backups/            # migration backups
```

The database and immutable Source blobs are authoritative. Title-based `inbox/*.md`, `wiki/`, and `search_index` are derived human-readable views. Normal audio capture never persists `transcript.v1` JSON in the Vault.

## Pipeline

```text
explicitly named audio
→ private transient job → local Provider → transcript.v1 → validated Markdown staging
→ immutable Source
→ atomic transcript import
→ one Event
→ Statement / Entity / Decision / Action / Claim
→ CandidateMemory
→ atomic inbox Markdown publication → transient job cleanup
→ explicit user review
→ approved Memory
→ compiled Wiki
→ retrieval with Source and Segment citation
```

Capture and long-term memory formation are separate transactions and separate commands. Structured transcript import never approves memory.

`capture-audio` orders work so Provider and Markdown validation happen before database writes, transcript import commits before inbox publication, and the job directory is removed on every exit. An import failure can leave only the already-verified immutable audio Source; it cannot leave a partial Event or inbox delivery. If final publication fails after import, stable IDs make a retry safe. A manually edited generated Markdown fails integrity preflight before Provider or database work and is never replaced.

An intact generated delivery with matching Source/Event evidence is a terminal default result: `capture-audio` returns its metadata without invoking the Provider. Only an explicit rerun proceeds beyond that boundary, and it cannot silently change the existing Event title or observation time. Status commands remain metadata-only views over the Vault, private artifacts and known runtime directories.

## Determinism and transactions

- Source IDs derive from SHA-256 content hashes.
- Derived stable IDs use deterministic UUIDv5 inputs.
- Source content is copied to a content-addressed blob and verified before its database row is committed.
- A transcript batch is validated before its transaction begins; all derived rows commit or roll back together.
- Repeating the same file or transcript uses unique hashes and stable IDs, producing no duplicate logical records.
- Markdown uses same-directory temporary files, `fsync`, atomic replacement, and a generated-body hash. Only an unedited generated file for the same audio hash can be updated.
- Bulk operations expose `--dry-run`; migrations require `--apply` after a default dry-run.

## Schema 1 limits

Schema 1 does not implement vector databases, voiceprints, file watchers, continuous collection, automatic personality modeling, unreviewed memory promotion, or cloud synchronization. `Hypothesis`, `Pattern`, `Experiment`, and `SelfModelEntry` are reserved concepts only and are never automatically generated.
