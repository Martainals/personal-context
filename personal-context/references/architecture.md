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

The private configuration and runtime use the operating-system user configuration directory or explicit `--config-dir`. Model weights, consent receipts, and caches never live in the Git checkout or database.

## Vault layout

```text
<root>/
├── context.sqlite3     # authoritative structured database
├── blobs/              # immutable, SHA-256-addressed Source bytes
├── inbox/              # optional user staging area
├── wiki/               # generated human-readable view
└── backups/            # migration backups
```

The database and immutable Source blobs are authoritative. `wiki/` and `search_index` are disposable compiled views.

## Pipeline

```text
explicitly named audio or document
→ immutable Source
→ optional local Provider → timestamped structured transcript
→ one Event
→ Statement / Entity / Decision / Action / Claim
→ CandidateMemory
→ explicit user review
→ approved Memory
→ compiled Wiki
→ retrieval with Source and Segment citation
```

Capture and long-term memory formation are separate transactions and separate commands. Structured transcript import never approves memory.

## Determinism and transactions

- Source IDs derive from SHA-256 content hashes.
- Derived stable IDs use deterministic UUIDv5 inputs.
- Source content is copied to a content-addressed blob and verified before its database row is committed.
- A transcript batch is validated before its transaction begins; all derived rows commit or roll back together.
- Repeating the same file or transcript uses unique hashes and stable IDs, producing no duplicate logical records.
- Bulk operations expose `--dry-run`; migrations require `--apply` after a default dry-run.

## Schema 1 limits

Schema 1 does not implement vector databases, voiceprints, file watchers, continuous collection, automatic personality modeling, unreviewed memory promotion, or cloud synchronization. `Hypothesis`, `Pattern`, `Experiment`, and `SelfModelEntry` are reserved concepts only and are never automatically generated.
