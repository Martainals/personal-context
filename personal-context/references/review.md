# Review

## Review an event

Run `review <event-id>` to inspect Event metadata, Source hash/path, Segments, typed Statements, Entities, Decisions, Actions, Claims, Relationships, and CandidateMemory records together.

Run `candidates --status pending` to obtain the review queue. Before deciding, verify:

- exact Source and optional Segment time range;
- proposed Fact/Opinion/Decision/Action/Claim kind;
- speaker attribution and whether the text is merely a reported Claim;
- `observed_at`, `valid_from`, and `valid_to`;
- rationale, supporting evidence, counter-evidence, and existing conflicting Memory;
- whether the content is appropriate for long-term retention.

## Approve or reject

`approve <candidate-id> --reviewer <identity> [--reason ...]` appends an approval Review, marks the candidate approved, creates exactly one Memory, and refreshes the disposable index.

`reject <candidate-id> --reviewer <identity> [--reason ...]` appends a rejection Review and creates no Memory.

The commands are idempotent for the same terminal decision. Attempting the opposite decision is rejected instead of overwriting history.

## Changes after review

Do not edit an approved Memory in place. Capture fresh evidence, create a new CandidateMemory, review it, then introduce explicit invalidation/supersession in a future governed workflow. V1 stores the necessary fields but exposes no automatic conflict resolution.
