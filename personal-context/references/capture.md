# Capture and transcript import

## Ingest

Run `ingest --dry-run` before a multi-file batch. The command calculates SHA-256, reports `ingest` or `duplicate`, copies new bytes to `blobs/<prefix>/<hash>`, verifies the copy, and inserts an immutable Source. Same-content files are idempotent even if filenames differ.

Unicode filenames, Chinese text, and paths containing spaces are supported.

## Default audio delivery

Use `capture-audio` for an ordinary user request to transcribe a named recording:

```bash
scripts/context capture-audio \
  --root <vault> --agent-host <host> \
  --audio <audio> --language Chinese [--speaker-count 1..4]
```

The command checks the current Schema and delegates consent, Provider readiness and local transcription to the existing onboarding control plane. It reuses `ingest` and `import-transcript`; it does not maintain parallel Source, Schema or import logic.

The ordered failure boundary is:

1. Preview the named audio Source, determine its stable ID/hash, and preflight any existing delivery.
2. Create a `0700` job below the private configuration directory and ask the consented Provider to write `transcript.v1` there.
3. Validate the source hash and full transcript, then render a complete staged Markdown.
4. Idempotently ingest the original audio Source.
5. Dry-run and atomically import the transcript with that audio `source_id`. Every speech Segment becomes a Claim Statement; no Memory is created or approved.
6. Atomically publish `<vault>/inbox/<audio-stem>-录音转写.md` and remove the private job on every exit.

Provider or rendering failure occurs before Source insertion. Import failure may leave the verified audio Source, but the Event transaction rolls back and inbox remains unchanged. A final publication failure may leave a complete imported Event; stable IDs and event-content comparison make the next run recoverable without duplicate logical records.

The Markdown contains title, `完整转写` status, duration, segment count, `HH:MM:SS` timestamps, recording-local speaker labels and every segment in time order. Its final invisible marker hashes the complete generated body and source audio. A later run may update only an unmodified machine-generated file for the same audio content. A missing/invalid marker, body mismatch or different audio hash is treated as a manual/conflicting file and never overwritten.

Normal stdout contains only Source/Event IDs, counts, Markdown path/size/hashes, processing mode, Provider and cache metadata. It never contains transcript text. The workflow does not generate a summary or create/approve long-term Memory; the production audio Provider emits no CandidateMemory by default.

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
