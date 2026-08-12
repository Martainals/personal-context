# Local transcription Provider

## Production profile

`qwen-mlx` is the first production audio profile for macOS Apple Silicon:

| Role | Locked model | Purpose |
|---|---|---|
| ASR | `mlx-community/Qwen3-ASR-1.7B-bf16` | High-accuracy multilingual transcription |
| Alignment | `mlx-community/Qwen3-ForcedAligner-0.6B-bf16` | Character/word timestamps |
| Diarization | `mlx-community/diar_streaming_sortformer_4spk-v2.1-fp32` | Local speaker intervals, maximum four speakers |

Exact Hugging Face revisions, package versions, licenses, chunk sizes, and disk estimates live in `assets/providers/qwen-mlx.lock.json`. Do not replace a revision with a moving branch.

The private installer bootstraps pinned `uv`, lets it install a private Python 3.12, creates an isolated virtual environment, installs pinned `mlx-audio`, and downloads pinned snapshots beneath the private runtime directory. It does not modify system Python or shell profiles and does not start a server.

## Provider contract

The heavy Provider runs in a subprocess and receives one explicit local audio path. It must atomically write UTF-8 `transcript.v1` JSON and print only non-sensitive operation metadata. The output contains:

- Event metadata;
- ordered Segment objects with millisecond timestamps and recording-local `S01`–`S04` labels;
- empty structured-extraction arrays by default;
- top-level `processing` provenance containing package/model revisions and source audio hash.

`import-transcript` stores that provenance inside the existing `processing_runs.parameters_json`; no second database and no Schema change are needed.

## Processing pipeline

```text
named audio file
→ decode, mono, 16 kHz normalization
→ 5-second streaming Sortformer diarization
→ ASR chunks of at most 240 seconds
→ Qwen forced alignment
→ timestamp/speaker merge
→ transcript.v1.json
→ existing import-transcript
```

The Source must be the original audio. Run `ingest` first and pass its `source_id` to `import-transcript`; otherwise the JSON file itself becomes the Source.

## Limits and quality handling

- Speaker labels identify tracks within one recording only. They are not identities and must not be correlated across recordings.
- Sortformer supports at most four speakers and may degrade with five or more speakers, non-English meetings, long recordings, noise, or overlapping speech.
- ASR accuracy does not establish truth. Every speech Segment becomes a Claim Statement.
- Do not silently substitute CAM++, pyannote, a cloud API, or another model. A future replacement must be a separately locked Provider and may require renewed consent.
- Unit tests mock model installation and inference. A real model download and representative-audio evaluation are explicit integration steps.
