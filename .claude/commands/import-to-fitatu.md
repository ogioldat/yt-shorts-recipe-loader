---
description: Import recipes from the library into Fitatu, recording import_status
---

Import recipes into Fitatu using the `import-recipes-to-fitatu` skill.

Always run `--dry-run` first and report what would fail before sending anything.
Fitatu's recipe creation is not idempotent, so never re-send a recipe whose
`import_status` is already `ok`.

Importing resolves products first, against verified Fitatu entries only. An
ingredient with no verified match fails its whole recipe rather than being
dropped — report those names rather than working around them.

$ARGUMENTS may pass flags through (e.g. `--limit 3`, `--dry-run`).
