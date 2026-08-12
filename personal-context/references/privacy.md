# Privacy and path policy

- Require an explicit `--root <vault-path>` for every data operation. Never hardcode a user's vault path in reusable files.
- Limit reads to paths the user names and files referenced by the selected vault. Do not scan a parent Obsidian Vault.
- Keep Source bytes, SQLite data, Wiki output, backups, local transcripts, and tests local. `qwen-mlx` has no cloud fallback.
- Use only synthetic data in Skill development and tests. Never point automated tests at a real personal vault.
- Store provider secrets only in environment variables or private machine configuration outside reusable Skill code and vault exports. Never log secrets.
- Treat query results and Wiki files as sensitive personal data even though they are derived.
- Distinguish local model execution from attachment transport. If a user uploads audio to a cloud-hosted Agent, the host may receive it before the local Provider runs.
- In `strict-local`, do not read transcript text into Agent context. In `agent-assisted`, only the consented Agent host may process it; host data policies still apply.
- Store consent receipts and model runtimes outside the database and Skill checkout. Receipts contain only scope, paths, versions, digest, host, and time.
- Before installation or copying into a shared location, inspect for personal absolute paths and real content.
