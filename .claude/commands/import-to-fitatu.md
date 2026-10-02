---
description: Import recipes from recipes/ into Fitatu, recording import_status
---

Import recipes into Fitatu using the `import_recipes_to_fitatu` skill.

Always run `--dry-run` first and report what would fail before sending anything.
Fitatu's recipe creation is not idempotent, so never re-send a recipe whose
`import_status` is already `ok`.

$ARGUMENTS may pass flags through (e.g. `--limit 3`, `--dry-run`).
