# Versioning and migration

Skill version and database Schema version are independent:

- Skill version: `VERSION` (`0.1.0` for V1)
- Current Schema: `1`
- Minimum supported Schema: `1`
- Maximum supported Schema: `1`

`version [--root ...]` reports both declarations and, when supplied, database compatibility.

## Compatibility behavior

| Database state | Reads | Writes | Required action |
|---|---|---|---|
| current | yes | yes | normal operation |
| older | audit/compatible reads only | no | backup and migrate |
| newer | audit only | no | upgrade the Skill |
| unknown/damaged | audit only | no | restore or repair via a known migration |

## Migration sequence

1. `doctor --root <root>`
2. `audit --root <root>`
3. `migrate --root <root>` (dry-run by default)
4. Inspect the plan.
5. `migrate --root <root> --apply`
6. Verify SQLite integrity and run `audit`.

Apply creates a timestamped SQLite backup in `backups/` before changing Schema metadata. Migration records are stable and append-only. Re-running against the current Schema returns `already_current`; it does not repeat destructive work. V1 includes a guarded legacy Schema 0 → 1 bootstrap path only.
