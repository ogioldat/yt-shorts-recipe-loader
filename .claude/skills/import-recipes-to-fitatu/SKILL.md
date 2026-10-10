---
name: import-recipes-to-fitatu
description: Use when importing recipes from the library into Fitatu as real Fitatu recipes - runs recipe_library.fitatu_import, which resolves products against verified Fitatu entries and then uses the fitatu-api HTTP client directly, recording import_status ok/error/not_imported. Covers the duplicate hazard, gram-to-measure conversion, and reading the dry run before sending anything.
---

# Importing recipes into Fitatu

This step is **deterministic**. No model decides anything. Your job is to run
it, read what it reports, and decide what to do about the failures.

It runs in two phases, and the first one used to be a separate manual step:

1. **Resolve.** Every ingredient name is matched to a Fitatu product — from the
   database if already mapped, otherwise by searching Fitatu live, discarding
   everything Fitatu has not verified, and scoring what remains. The answer is
   stored, so a name used in thirty recipes costs one lookup.
2. **Import.** The resolved products' measures convert each quantity, and
   `create_recipe` posts the result.

Both phases use the `fitatu-api` HTTP client directly. **No MCP server is
involved in either one.**

**Only verified Fitatu products are ever used.** That is a constraint on the
`products` table, not a filter in the import path, so there is no code path that
can bypass it. A name whose only candidates are unverified stays unresolved, and
the recipe using it refuses to import.

## Read this before sending anything

**Fitatu's recipe creation is not idempotent.** `POST /recipes` with the same
body twice creates two recipes. There is no deduplication on Fitatu's side.

That is what `import_status` is for:

| status | meaning |
|---|---|
| `not_imported` | never attempted (also the default when the field is absent) |
| `ok` | created in Fitatu; `fitatu_recipe_id` holds the id |
| `error` | attempted and failed; `import_error` says why |

A recipe marked `ok` is **never sent again**. `--force` overrides that and
**will duplicate recipes in Fitatu**. Only use it if you know the earlier import
did not actually land, and prefer deleting the duplicate in Fitatu afterwards over
guessing.

## Always dry-run first

```bash
uv run python -m recipe_library.fitatu_import --status      # the tally
uv run python -m recipe_library.fitatu_import --dry-run     # build, report, send nothing
```

A dry run reports every problem, **contacts nothing and writes nothing** — not
even failures — so it is free to repeat.

The trade-off: with no Fitatu connection, phase 1 can only consult what is
already mapped. So a dry run tells you a name is unresolved, but not whether a
live search *would* resolve it. To find that out, resolve first and then dry-run:

```bash
uv run python -m recipe_library.products resolve --limit 20
uv run python -m recipe_library.fitatu_import --dry-run
```

Then import for real, starting small:

```bash
uv run python -m recipe_library.fitatu_import --limit 3
uv run python -m recipe_library.fitatu_import
```

Check the first few in the Fitatu app before importing all of them. Quantities
are the thing to verify: a conversion error is invisible in the database and
obvious in the app. So is a wrong product — `products resolve` prints which
product each name matched and at what score, and that is the moment to read it.

## Why recipes fail, and what to do

Fitatu does not accept grams. Every item is `{itemId, measureId,
measureQuantity}`, where the quantity counts units *of that measure* — so 500 g
against a 150 g portion becomes 3.333 portions. Grams-per-measure comes from the
product cache.

| Reported problem | What it means | Fix |
|---|---|---|
| `unresolved ingredient(s): NAME (...)` | no verified product matched that name | `/resolve-fitatu-products` |
| `no Fitatu product mapped` | resolution was skipped with `--no-resolve` | drop the flag |
| `no quantity stated in the source video` | the recipe genuinely has none | nothing to fix automatically; see below |
| `unit 'piece' has no mass` | a count, not a weight | resolve a product whose measure is a piece, or accept the loss |
| `the Fitatu measure is not millilitre-based` | ml against a gram measure | re-resolve the product choosing an ml measure |
| `Fitatu measure has no gram weight` | the cached measure cannot convert | re-resolve, preferring a gram measure |

**A recipe imports whole or not at all.** A recipe in Fitatu holding three of its
eight ingredients would silently under-report calories, and nobody would ever
notice. `--allow-partial` overrides this and warns loudly; reach for it only when
the omitted ingredients genuinely do not matter nutritionally (herbs, salt).

**Never edit a recipe's quantities to make an import succeed.** A blank quantity
is the honest record of what the video said. Changing it to make Fitatu happy is
exactly the corruption this project is built to avoid.

## Options

| flag | effect |
|---|---|
| `--dry-run` | build and report; contact nothing, write nothing |
| `--limit N` | import at most N recipes |
| `--recipe ID` | import just that recipe, by id; repeatable |
| `--status` | print the tally and exit |
| `--shared` | make recipes public; default is private |
| `--allow-partial` | import with ingredients missing (under-reports) |
| `--skip-errors` | do not retry recipes that failed before |
| `--force` | re-send already-imported recipes (**duplicates them**) |
| `--no-resolve` | import only what is already mapped; skips phase 1 |
| `--retry-failed` | re-search names previously recorded as having no match |

Failed recipes are retried automatically on the next run, since an `error` is
usually a missing mapping or a transient API problem.

`--recipe` takes the recipe id, and is the way to retry one recipe after fixing
its products:

```bash
uv run python -m recipe_library.fitatu_import --recipe tteok-bokki --dry-run
uv run python -m recipe_library.fitatu_import --recipe tteok-bokki
```

It narrows the list, it does not widen it: a named recipe already marked `ok` is
still skipped, and an unknown id is refused before anything is sent.

## Credentials

Read from `FITATU_EMAIL` / `FITATU_PASSWORD` in the environment, falling back to
the gitignored `mcp/fitatu/.env` so the account details are not stored twice. The
command fails with an actionable message when they are absent.

## Afterwards

```bash
uv run python -m recipe_library.validate    # the schema checks import_status too
uv run python -m recipe_library.fitatu_import --status
```

Then commit `state/library.db`. `import_status` and `fitatu_recipe_id` live in
it and are the record of what is already in Fitatu — losing them means losing
the duplicate protection, and the database is the only copy.

The `-wal` and `-shm` files beside it are transient and gitignored. If the `-wal`
file is large, checkpoint it first so the commit holds the whole database:

```bash
uv run python -c "import sqlite3; sqlite3.connect('state/library.db').execute('PRAGMA wal_checkpoint(TRUNCATE)')"
```
