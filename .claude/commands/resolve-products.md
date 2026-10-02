---
description: Map recipe ingredient names to Fitatu product ids via the fitatu MCP
---

Resolve ingredient names to Fitatu product ids using the `resolve-fitatu-products`
skill.

Work through `uv run python -m recipe_library.products pending`, most-used names
first, batching queries to `search_food`. Record each decision with
`products record` or `products unresolved`, then run `products apply`.

$ARGUMENTS may limit how many names to work through in this session (e.g. `30`),
or name specific ingredients. With no arguments, work the whole pending list.
