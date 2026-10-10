---
description: Fix ingredient names that would not match a verified Fitatu product
---

Use the `resolve-fitatu-products` skill.

Matching itself is automatic now — it runs as the first phase of every import.
This is for the leftovers: read `products failures`, and fix what is fixable
with a search-phrase alias or a hand-recorded mapping.

Read each refusal's reason first. "None of the candidates is verified" is the
rule working correctly and is not yours to fix. A blank is better than a wrong
id, so leave a name unresolved rather than reaching for the nearest match.

$ARGUMENTS may limit how many names to work through in this session (e.g. `30`),
or name specific ingredients. With no arguments, work the whole failure list.
