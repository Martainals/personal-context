# Query and audit

## Retrieval

`retrieve <query>` searches a rebuildable local index. Results contain:

- `authority=approved_memory` for explicitly approved long-term conclusions;
- `authority=source_evidence` for Statements, Claims, Decisions, and Actions that remain evidence;
- Source ID, name, SHA-256, observation time;
- Segment ID, time range, and speaker when available.

Never present a source-evidence Claim as an established Fact. When results conflict, show both citations and review states. V1 uses deterministic substring search, not semantic/vector search.

## Audit

Run `audit` after failed imports, before and after migration, and when evidence looks inconsistent. It checks:

- Schema status, required tables, SQLite integrity, and foreign keys;
- missing Source blobs and Source SHA-256 mismatches;
- orphan Segments;
- Memories without a Source or approved CandidateMemory;
- Segment content-hash mismatches.

Audit is read-only and remains the appropriate entry point for unknown, newer, or damaged Schemas. Fix root causes through restore or versioned migration; do not patch authoritative rows manually.
