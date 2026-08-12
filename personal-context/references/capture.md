# Capture and transcript import

## Ingest

Run `ingest --dry-run` before a multi-file batch. The command calculates SHA-256, reports `ingest` or `duplicate`, copies new bytes to `blobs/<prefix>/<hash>`, verifies the copy, and inserts an immutable Source. Same-content files are idempotent even if filenames differ.

Unicode filenames, Chinese text, and paths containing spaces are supported.

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

Use `--source-id` when a transcript describes an already-ingested audio or document Source. Without it, the JSON transcript file itself becomes the immutable Source.

## Provider boundary

V1 has no transcription provider and does not require a key. A future provider must implement a replaceable adapter, read secrets only from environment variables or private local configuration, use mocks in tests, and emit the structured JSON contract above before database import.
