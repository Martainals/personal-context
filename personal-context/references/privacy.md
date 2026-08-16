# Privacy and path policy

- Require an explicit `--root <vault-path>` for every data operation. Never hardcode a user's vault path in reusable files.
- Limit reads to paths the user names and files referenced by the selected vault. Do not scan a parent Obsidian Vault.
- Keep Source bytes, SQLite data, Wiki output, backups, local transcripts, transcription artifacts, and tests local. `qwen-mlx` has no cloud fallback.
- Use only synthetic data in Skill development and tests. Never point automated tests at a real personal vault.
- Store provider secrets only in environment variables or private machine configuration outside reusable Skill code and vault exports. Never log secrets.
- Treat query results and Wiki files as sensitive personal data even though they are derived.
- Distinguish local model execution from attachment transport. If a user uploads audio to a cloud-hosted Agent, the host may receive it before the local Provider runs.
- In `strict-local`, do not read transcript text into Agent context. In `agent-assisted`, only the consented Agent host may process it; host data policies still apply.
- Store consent receipts, model runtimes, and transcription artifacts outside the database, Vault, and Skill checkout. Receipts contain only scope, paths, versions, digest, host, and time.
- On macOS, the default artifact root is `~/Library/Application Support/personal-context/artifacts/`; other platforms use their private configuration directory. An explicit `--config-dir` relocates it. Entries are isolated by a hash of the explicitly selected Vault and by the audio SHA-256.
- Treat cached ASR text, alignment, raw diarization probabilities, speaker turns, and assembled segments as sensitive personal data. The store uses checksummed JSON/gzip, `0700` directories, `0600` files and locks where supported, and atomic writes.
- Never store pickle, voiceprints, speaker embeddings, cross-recording identity features, secrets, or automatic retention metadata in artifacts. Raw diarization probabilities are recording-local activity evidence, not biometric identity.
- Artifact cleanup is manual. `transcription-cache-status` is metadata-only; `transcription-cache-prune` defaults to dry-run and requires explicit `--apply` for deletion. Never run it in the background or as an automatic fallback.
- Before installation or copying into a shared location, inspect for personal absolute paths and real content.
