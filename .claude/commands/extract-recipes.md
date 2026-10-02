---
description: Extract recipes from fetched YouTube shorts into YAML files
---

Extract recipes from videos that have been fetched but not yet extracted.

Use the `extract-recipes` skill. Work through every video the ledger
(`state/processed.yaml`) shows as needing extraction — no `extracted_at`, or a
`description_sha256` that no longer matches `raw/<id>/meta.json`.

$ARGUMENTS can narrow the work to specific video ids. With no arguments,
process the whole pending queue.
