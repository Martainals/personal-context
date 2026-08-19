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

The heavy Provider runs in a subprocess and receives one explicit local audio path. It must atomically write UTF-8 `transcript.v1` JSON and print only non-sensitive operation metadata. `capture-audio` places that JSON in a private transient job; `transcribe-audio --output` lets debugging/integration callers choose a persistent path. The output contains:

- Event metadata;
- ordered Segment objects with millisecond timestamps and recording-local `S01`–`S04` labels;
- empty structured-extraction arrays by default;
- top-level `processing` provenance containing package/model revisions and source audio hash.

`import-transcript` stores that provenance inside the existing `processing_runs.parameters_json`; no second database and no Schema change are needed.

Stage artifacts are a separate resumability contract, not part of `transcript.v1` and not imported into SQLite. Skill 0.4.0 uses artifact contract 1 while the transcript/provider contract remains 1.

## Processing pipeline

```text
named audio file
→ decode, mono, 16 kHz normalization
→ cached high-context streaming Sortformer raw probabilities
→ cached ASR chunks of at most 240 seconds
→ cached Qwen forced-alignment chunks
→ cached speaker turns derived from raw probabilities
→ cached confidence-aware final assembly
→ private job transcript.v1.json
→ staged complete Markdown
→ existing ingest + import-transcript
→ atomic inbox Markdown publication
→ private job cleanup
```

Completed recordings use the pinned high-accuracy streaming profile: 340 new frames, 40 future-context frames, 40 FIFO frames, a 300-frame cache update period, and 188 speaker-cache frames. At 80 ms per diarization frame this gives 27.2 seconds of new audio plus 3.2 seconds of future context per main step. The Provider then consolidates probabilities over the whole recording before assigning words.

When the user knows the number of speakers, pass `--speaker-count 1..4`. The Provider selects that many stable model slots over the whole recording, remaps them to contiguous `S01` labels, and treats brief activity in rejected slots as uncertain evidence rather than a new person. Without the hint, all four model slots remain available.

Post-processing removes sub-100 ms speech fragments, bridges sub-150 ms silence gaps, repairs weak sub-240 ms speaker flips, and only merges a one- or two-character sentence fragment when the surrounding speaker agrees and the switch margin is low. Strong short backchannels remain separate.

The Source must be the original audio. The default `capture-audio` path guarantees this association. Low-level callers must run `ingest` first and pass its `source_id` to `import-transcript`; otherwise the JSON file itself becomes the Source.

The stage cache remains the only persisted recovery surface for model work. The transient transcript JSON is deleted after both success and failure, while cached valid stages allow a retry to resume computation. Inbox never stores Provider JSON, diagnostics or test reports; it receives only the completed Markdown delivery.

## Stage artifact store

Persistent artifacts are enabled by default only after the user accepts Consent Notice 2. The default macOS location is:

```text
~/Library/Application Support/personal-context/artifacts/
└── <vault-scope-hash>/
    └── <audio-sha256>/
        ├── asr/chunk-00000.json.gz
        ├── alignment/chunk-00000.json.gz
        ├── diarization/raw-probabilities.json.gz
        ├── speaker-turns/turns.json.gz
        └── assembly/segments.json.gz
```

An explicit `--config-dir` changes only the private base path. The Vault scope is the SHA-256 of the resolved, explicitly supplied `--root`; the audio directory is addressed by the source file SHA-256. Artifacts are canonical UTF-8 JSON inside deterministic gzip envelopes with payload checksums. Directories use mode `0700`, files and per-recording locks use `0600` where the platform supports POSIX permissions. Writes use a same-directory temporary file, `fsync`, and atomic replacement. The format never uses pickle or executable serialization.

One lock serializes work for each recording. A missing, stale, truncated, invalid or checksum-mismatched file is a miss only for that exact stage or chunk; valid sibling chunks remain reusable. Model loading is lazy, so a cache hit does not load that stage's optional MLX model.

The five component keys are independently versioned:

| Artifact | Key inputs | Explicitly excluded |
|---|---|---|
| raw diarization | audio SHA, artifact/normalization/stage versions, diarizer revision, pinned runtime package versions, streaming and inference parameters | speaker count, post-processing, title, observed time |
| ASR chunk | audio SHA, versions, ASR revision, pinned runtime package versions, language and chunk identity | speaker count, diarization rules, title, observed time |
| alignment chunk | audio SHA, versions, aligner revision, pinned runtime package versions, language, chunk identity and ASR payload hash | speaker count, title, observed time |
| speaker turns | raw-diarization payload hash, speaker count and post-processing rules | ASR, alignment, title, observed time |
| final assembly | alignment payload hashes, speaker-turn payload hash and assembly rules | title and observed time |

Therefore changing `title` or `observed_at` only changes the final document metadata. Changing `speaker_count` or post-processing derives new turns from cached raw probabilities without rerunning ASR, alignment or the diarizer. A model, package, language, normalization, chunking or stage-version change invalidates only components whose key names that input.

Operational controls:

```bash
# Bypass all artifact reads and writes for one transcription.
scripts/context transcribe-audio ... --no-cache

# Recompute the named model stage; dependent lightweight stages are re-derived as needed.
scripts/context transcribe-audio ... --refresh-stage asr
scripts/context transcribe-audio ... --refresh-stage alignment
scripts/context transcribe-audio ... --refresh-stage diarization
scripts/context transcribe-audio ... --refresh-stage all

# Read metadata only, or preview/apply explicit deletion. Select by input path or Source ID.
scripts/context transcription-cache-status --root <vault>
scripts/context transcription-cache-status --root <vault> --limit 10
scripts/context transcription-cache-status --root <vault> --source-id <source>
scripts/context transcription-cache-prune --root <vault> --dry-run
scripts/context transcription-cache-prune --root <vault> --source-id <source> --dry-run
scripts/context transcription-cache-prune --root <vault> --apply
scripts/context storage-status --root <vault>
```

`--no-cache` and `--refresh-stage` are mutually exclusive. Cache status maps an artifact hash to an existing Source name when available and returns per-stage counts, bytes and last-write times. An unmatched cache is labelled `unbound`; no transcript payload is exposed. `--limit` sorts by last write, not last access. Prune is never automatic and defaults to dry-run; `--apply` removes only recording directories in the selected Vault scope, or the selected audio/Source entry. `storage-status` reports aggregate metadata for Source blobs, SQLite, Inbox, artifacts, private runtime, locks and recognizable transient-job directories without reading their contents.

## Limits and quality handling

- Speaker labels identify tracks within one recording only. They are not identities and must not be correlated across recordings.
- Raw diarization artifacts are frame probabilities for recording-local output slots. They are not voiceprints, speaker embeddings or biometric identities; those representations are forbidden from the artifact store.
- Sortformer supports at most four speakers and may degrade with five or more speakers, non-English meetings, long recordings, noise, or overlapping speech.
- ASR accuracy does not establish truth. Every speech Segment becomes a Claim Statement.
- Do not silently substitute CAM++, pyannote, a cloud API, or another model. A future replacement must be a separately locked Provider and may require renewed consent.
- Unit tests mock model installation and inference. A real model download and representative-audio evaluation are explicit integration steps.
