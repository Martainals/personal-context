# Memory policy

## Type distinctions

- **Fact**: explicitly reviewed proposition accepted as factual within a stated validity period.
- **Opinion**: attributed preference, evaluation, or belief; do not convert it into a Fact.
- **Decision**: a choice that was made, including who/when when evidence supports it.
- **Action**: a commitment or task, with optional assignee, due time, and status.
- **Claim**: an assertion made by a source or speaker whose truth is not established merely by being said.

Always preserve attribution. “A said X” supports the Fact that A made a statement, not automatically the truth of X.

## Long-term memory gate

1. Import creates `CandidateMemory(review_status='pending')` only.
2. Show the candidate together with Event, Source, Segment, proposed kind, validity window, rationale, supporting evidence, and contrary evidence if available.
3. Require an explicit user action before running `approve`.
4. Append a Review containing reviewer, decision, reason, and time.
5. Create a Memory only for approval. Rejection appends a Review and creates no Memory.

Approval and rejection are terminal in V1. Do not reverse or overwrite a prior review silently; propose a superseding candidate instead.

## Evidence quality

Do not invent decimal confidence scores. Prefer supporting Source count, counter-Source count, concrete citations, observation time, validity time, and review status. One recording cannot modify a long-term personality judgment or Self Model.

## Conflicts

Preserve both conflicting records and their provenance. Mark invalidation or supersession explicitly only when the user reviews the change. Query output should surface the distinction between approved Memory and raw source evidence.
