# Schemas

## Shared conventions

- IDs use stable typed prefixes (`src_`, `seg_`, `evt_`, `stm_`, `cand_`, `mem_`, and others).
- Important derived records carry a direct `source_id`; time-local evidence also carries `segment_id`.
- `content_hash` preserves deterministic content identity. Source hashes are SHA-256 of exact bytes.
- Timestamps are UTC ISO-8601 strings. `observed_at` records when evidence was observed; `valid_from` and `valid_to` describe when a proposition applies.
- Derived records have `created_at`, `updated_at`, `review_status`, `invalidated_at`, `supersedes_id`, `processing_run_id`, and `schema_version` where applicable.
- Corrections create a new record and use invalidation/supersession; they do not silently overwrite history.

## Core objects

| Object | Table | Identity and provenance | V1 purpose |
|---|---|---|---|
| SchemaMetadata | `schema_metadata` | key, value, updated_at | Schema and compatibility declarations |
| Source | `sources` | content hash, stored blob, observation/import times | Immutable original evidence |
| Segment | `segments` | Source + ordinal + time range + text hash | Immutable time-local evidence |
| Event | `events` | Source + processing run | One import/conversation occurrence |
| Entity | `entities` | Source/Event/optional Segment | Named person, project, place, or concept |
| Statement | `statements` | Source/Event/optional Segment, typed kind | Typed expression: Fact, Opinion, Decision, Action, or Claim |
| CandidateMemory | `candidate_memories` | Source/Event/optional Segment/Statement | Pending proposal; never authoritative |
| Memory | `memories` | unique approved CandidateMemory + review identity | Explicitly approved long-term memory |
| Relationship | `relationships` | two Entities + Source/Event/Segment | Evidence-backed connection |
| Action | `actions` | Statement + Source/Event/Segment | Trackable action, assignee, due time, status |
| Decision | `decisions` | Statement + Source/Event/Segment | Evidence-backed decision |
| Claim | `claims` | Statement + Source/Event/Segment | Reported assertion with support/counter-source counts |

`processing_runs` records processor identity, version, input hash, parameters, status, and times. `reviews` is the append-only approval/rejection audit trail. `migrations` records executed Schema changes. `search_index` is non-authoritative and rebuildable.

## Evidence immutability

SQLite triggers reject UPDATE and DELETE on Source and Segment. Derived objects use `invalidated_at` and `supersedes_id` for soft invalidation and replacement. Source removal, if added later, must be an explicit governed operation that preserves an audit tombstone; V1 exposes no deletion command.

## Claim versus Fact

A transcript segment automatically creates a `Statement(kind='Claim')`, because speech proves only that a statement was made. A structured annotation may propose a different kind, but it is still derived evidence. A `CandidateMemory(proposed_kind='Fact')` becomes a Fact Memory only through the explicit `approve` command.

## Reserved future objects

`Hypothesis`, `Pattern`, `Experiment`, and `SelfModelEntry` are intentionally not tables in V1. Future Schemas must introduce them through a versioned migration and must not backfill them automatically from one recording.
