# Wiki and search index

The Wiki is a compiled reading view, not the sole authority. `compile-wiki` regenerates only managed files in `<root>/wiki/`: `index.md`, `memories.md`, `events.md`, and `.personal-context-manifest.json`. Each Memory entry includes its approval information and Source citation.

Run `compile-wiki --dry-run` first to preview every managed path, byte count, and SHA-256 value. The preview uses the database-derived `data_as_of` value, so an unchanged database produces byte-identical applied files. The applied command writes each file atomically and rebuilds the search index.

The `search_index` table is derived entirely from approved Memories and source-backed Statements, Decisions, Actions, and Claims. `rebuild-index --dry-run` previews its size; `rebuild-index` deletes and recreates all rows transactionally. Losing the Wiki or index does not lose authoritative data.

Do not manually edit generated files expecting changes to flow back into the database. Capture new evidence and use the review workflow instead.
