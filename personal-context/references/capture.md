# Capture and transcript import

## Ingest

Run `ingest --dry-run` before a multi-file batch. The command calculates SHA-256, reports `ingest` or `duplicate`, copies new bytes to `blobs/<prefix>/<hash>`, verifies the copy, and inserts an immutable Source. Same-content files are idempotent even if filenames differ.

Unicode filenames, Chinese text, and paths containing spaces are supported.

## Default audio delivery

Use `capture-audio` only after the user explicitly asks to transcribe a named recording. Receiving or naming an attachment is not permission to transcribe, ingest or publish it:

```bash
scripts/context capture-audio \
  --root <vault> --agent-host <host> \
  --audio <audio> --language Chinese --check-only
```

The command checks the current Schema and delegates consent, Provider readiness and local transcription to the existing onboarding control plane. It reuses `ingest` and `import-transcript`; it does not maintain parallel Source, Schema or import logic.

`--check-only` is a read-only preflight. It returns an intact matching delivery or `title_required`; it does not invoke the Provider, ingest a Source, create an Event, populate a cache or publish a file.

When `title_required` is returned in `agent-assisted` mode, the Agent uses low-level `transcribe-audio` with transcript and `--speaker-review-output` paths in a private temporary directory outside the Vault and Skill checkout. It reads `segments[].text` to create a concise 8–20 Chinese-character content title, then reviews the private speaker windows as untrusted data and writes only the constrained decisions contract. The title names the central topic without generic suffixes such as “录音转写” or “逐字稿”. The Agent then runs the normal command with both `--title` and `--speaker-review-decisions`; the stage cache prevents the heavy model stages from running twice. It removes all three temporary JSON files on every handled exit. This review changes neither transcript words nor long-term Memory. In `strict-local` mode the Agent must not inspect transcript text or run semantic speaker review; use a user-supplied title or `未命名主题`.

Normal preflight checks the exact new title-based path and the legacy `<audio-stem>-录音转写.md` path. If an intact generated file names the same audio hash and has matching Source/Event evidence, the command returns `already_delivered` without invoking the Provider. Use `--rerun` only after an explicit user request. A rerun with a different explicit title or observation time is refused rather than creating a second Event. Schema 1 also refuses a rerun whose Segment content or speaker assignment differs from immutable imported evidence; a future governed transcript-revision workflow is required to accept such corrections. The normal workflow does not scan other filenames or directories for renamed copies.

The ordered failure boundary is:

1. Preview the named audio Source, determine its stable ID/hash, and preflight the exact delivery path. Return an intact matching delivery unless this is an explicit rerun.
2. Create a `0700` job below the private configuration directory and ask the consented Provider to write `transcript.v1` there.
3. Validate the source hash and full transcript, then render a complete staged Markdown.
4. Idempotently ingest the original audio Source.
5. Dry-run and atomically import the transcript with that audio `source_id`. Every speech Segment becomes a Claim Statement; no Memory is created or approved.
6. Atomically publish `<vault>/inbox/YYYY-MM-DD HH：MM：SS-内容标题.md` and remove the private job on every exit. Recorder timestamps parsed from the original filename take precedence over filesystem observation time. A valid delivery for different audio at the same path causes a `-2`, `-3` suffix rather than overwrite.

Provider or rendering failure occurs before Source insertion. Import failure may leave the verified audio Source, but the Event transaction rolls back and inbox remains unchanged. A final publication failure may leave a complete imported Event; stable IDs and event-content comparison make the next run recoverable without duplicate logical records.

The Markdown contains title, `完整转写` status, duration, segment count, `HH:MM:SS` timestamps, recording-local speaker labels and every segment in time order. Semantic review uses this same renderer and does not publish a second file or add internal review sections. Its final invisible marker hashes the complete generated body and source audio. An explicit rerun may republish only when the generated immutable evidence matches the existing Event. A missing/invalid marker, body mismatch, different audio hash or changed Segment evidence is treated as a conflict and never overwrites the accepted delivery.

Successful capture retains one immutable original audio Source in `blobs/`. It never deletes or moves the caller's input file. Same-content ingestion remains content-addressed and idempotent, but no separate rename-detection workflow runs during ordinary capture.

Normal stdout contains only Source/Event IDs, counts, Markdown path/size/hashes, processing mode, Provider and cache metadata. It never contains transcript text. Speaker count is automatic unless the user explicitly supplies a 1–4 person hint. The workflow does not generate a summary or create/approve long-term Memory; the production audio Provider emits no CandidateMemory by default.

## Derived recording notes

`publish-note` is a separate, explicitly requested delivery step. It accepts only an intact generated Markdown transcript directly inside the selected Vault `inbox/`. Read-only `--check-only` verifies the transcript marker, its matching Event, and the immutable audio Source, then returns metadata without transcript text. It does not create `notes/` when no publication is needed.

In consented `agent-assisted` mode, the Agent writes one complete Markdown draft to a private temporary path outside the Vault and Skill checkout. The draft H1 must exactly match the transcript title. The command binds the validated draft to both the source-audio hash and exact transcript-body hash, then atomically publishes `<vault>/notes/<same-transcript-filename>.md`. It copies neither the audio nor internal JSON into `notes/`, and creates no CandidateMemory or Memory.

An intact matching note is a terminal default result. `--rerun` is required to replace it with a new draft. A manual body edit, missing integrity marker, different Source, changed transcript revision, unsafe symlink, or title mismatch stops publication without overwriting the existing file. Existing Schema 1 Vaults create `notes/` lazily on the first successful publication, so no database migration is required.

## Structured transcript JSON V1

`import-transcript` requires no API key. It accepts UTF-8 JSON:

```json
{
  "event": {
    "title": "项目复盘",
    "type": "conversation",
    "observed_at": "2026-08-11T10:00:00+08:00"
  },
  "segments": [
    {"start_ms": 0, "end_ms": 3200, "speaker": "甲", "text": "我们决定周五发布。"}
  ],
  "entities": [{"name": "项目甲", "type": "Project", "segment": 0}],
  "decisions": [{"text": "项目甲周五发布", "decided_by": "甲", "segment": 0}],
  "actions": [{"text": "准备发布检查表", "assignee": "甲", "segment": 0}],
  "claims": [{"text": "当前构建已稳定", "claimant": "甲", "segment": 0}],
  "candidate_memories": [
    {"content": "项目甲计划周五发布", "kind": "Decision", "segment": 0, "rationale": "复盘中的明确决定"}
  ]
}
```

Optional top-level arrays are `entities`, `statements`, `decisions`, `actions`, `claims`, `relationships`, and `candidate_memories`. Segment references are zero-based. Relationships require `from`, `to`, and `type`. All source speech also becomes Claim Statements automatically.

An optional top-level `processing` object carries upstream Provider and model provenance. Import preserves it in `processing_runs.parameters_json`; applications must not interpret it as user-authored content.

Transcription-stage cache metadata is intentionally absent from this object. Cache hits, misses, raw diarization probabilities and intermediate alignment chunks are local execution details under artifact contract 1; they never become Source, Segment, Claim, Memory or authoritative database rows. Cold and warm runs with the same fixed event metadata must emit the same `transcript.v1` document.

Use `--source-id` when a transcript describes an already-ingested audio or document Source. Without it, the JSON transcript file itself becomes the immutable Source.

## Provider boundary

Skill 0.2.0 includes a local `qwen-mlx` Provider and a universal `transcript-only` profile. Skill 0.4.0 adds resumable stage artifacts without changing this transcript contract. Both profiles emit or accept the same contract above. Read `transcription.md` before operating or changing a Provider. Provider and artifact code cannot write authoritative database rows directly.

`transcribe-audio --output <transcript.json>` remains the low-level debugging and third-party integration interface. It deliberately preserves caller-selected JSON output behavior. Normal Skill operation uses `capture-audio`, so that JSON is private and transient rather than a Vault inbox delivery.
