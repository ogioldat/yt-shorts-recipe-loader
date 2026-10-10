---
description: Extract recipes from fetched YouTube shorts into the library
---

Extract recipes from videos that have been fetched but not yet extracted.

Use the `extract-recipes` skill. Start the API server, then work through every
video `GET /videos/pending-extraction` reports — never extracted, or a
`description_sha256` that no longer matches the stored description.

$ARGUMENTS can narrow the work to specific video ids. With no arguments,
process the whole pending queue.
