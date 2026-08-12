# Privacy and path policy

- Require an explicit `--root <vault-path>` for every data operation. Never hardcode a user's vault path in reusable files.
- Limit reads to paths the user names and files referenced by the selected vault. Do not scan a parent Obsidian Vault.
- Keep Source bytes, SQLite data, Wiki output, backups, and tests local. Never upload recordings or private content as part of V1.
- Use only synthetic data in Skill development and tests. Never point automated tests at a real personal vault.
- Store future provider secrets only in environment variables or private machine configuration outside reusable Skill code and vault exports. Never log secrets.
- Treat query results and Wiki files as sensitive personal data even though they are derived.
- Before installation or copying into a shared location, inspect for personal absolute paths and real content.
