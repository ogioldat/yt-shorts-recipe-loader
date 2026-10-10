# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A personal recipe library built from a public YouTube Shorts playlist of cooking
videos, in Polish and English. Shorts are turned into structured recipes holding
**title and ingredients only** — no preparation steps. The library is one SQLite
database, served over HTTP, and pushed into Fitatu for nutrition tracking.

## Commands

```bash
uv sync                                     # install dependencies
uv run pytest                               # whole suite; needs no network
uv run pytest tests/test_scoring.py -q      # one file
uv run pytest -k "quantity" -q              # one topic

uv run uvicorn recipe_library.api:app --reload   # the HTTP server
uv run python -m recipe_library.fetch       # stage 1: one pass over the playlist
uv run python -m recipe_library.fetch --until-complete   # keep going until done
uv run python -m recipe_library.validate    # check every recipe in the database
```

Useful `fetch` flags: `--until-complete` (keep making passes until nothing is
missing), `--limit N` (videos per pass), `--video ID` (a specific video; bypasses
config entirely, so config defaults do not apply), `--refresh` (re-fetch metadata
already stored), `--subtitle-output text|vtt|both`,
`--cookies-from-browser chrome` (see throttling below).

Every command takes `--db PATH` to work against a database other than the
default. `RECIPE_LIBRARY_DB` does the same for everything, which is what the
tests use.

Always invoke Python through `uv run`. A bare `python3` on this machine resolves
to an unrelated project's virtualenv.

## Architecture

Four stages. The first is scripted, the second needs judgement, the last two are
deterministic.

```
public playlist → [1: fetch, scripted]  → videos + video_subtitles
                → [2: extract, Claude]  → recipes + ingredients
                → [3: resolve, scripted] → products + ingredient_products
                → [4: import, scripted]  → Fitatu
```

**Stage 1 — `recipe_library/fetch.py`.** Uses the `yt_dlp` Python API. Stores
the pruned metadata and the caption prose. Deterministic, no LLM. yt-dlp can
only write subtitles to files, so each fetch gets a throwaway temporary
directory; nothing accumulates on disk.

**Stage 2 — `.claude/skills/extract-recipes/`.** Claude reads each video's
stored text and writes recipes through the API. This is judgement work, which is
why it is not code. Driven by `/extract-recipes`.

**Stage 3 — `recipe_library/resolve/`.** Matches each ingredient name to a
verified Fitatu product. **This used to be stage 2's sibling and a model's job**;
it is now code, and the reasons it was ever a model's job are preserved in
`resolve/scoring.py`. `/resolve-fitatu-products` now handles only the leftovers.

**Stage 4 — `recipe_library/fitatu_import/`.** Converts quantities into Fitatu
measures and creates the recipes. Runs stage 3 as its first phase.

Separating 1 from 2 means a full playlist fetch costs no context, and extraction
can be re-run or corrected without touching the network — which matters, because
YouTube throttles repeat scraping.

## Everything is in `state/library.db`

One SQLite file, defined by `recipe_library/schema.sql`, reached through
`recipe_library/db.py` and the four modules in `recipe_library/store/`.

**It is committed, and it is the only copy** of 72 hand-curated extractions.
The `-wal` and `-shm` sidecars are gitignored; checkpoint the WAL before
committing so the `.db` holds everything:

```bash
uv run python -c "import sqlite3; sqlite3.connect('state/library.db').execute('PRAGMA wal_checkpoint(TRUNCATE)')"
```

It replaced a YAML tree: `state/processed.yaml` (the ledger),
`state/fitatu_products.yaml` (the product cache), `recipes/*.yaml` and
`raw/<video_id>/`. **Those paths are gone from the working tree but remain in git
history**, which is where to look for the state of anything before the move.
`recipe_library/migrate_yaml.py` is the one-shot migration; it is idempotent and
kept for reference, and `paths.py` still names the old locations for it.

**The store returns document shapes, not rows.** `videos.meta()` returns what
`raw/<id>/meta.json` held, `recipes.get()` returns what a `recipes/*.yaml` file
held, and `products.cache()` returns what the YAML cache's `products` key held.
That is deliberate: the pure logic — schema validation, the measure conversions
in `fitatu_import/payload.py`, the completeness rules — was written and tested
against those shapes, so the storage change never reached the parts where the
judgement lives.

Three pragmas are set on every connection because SQLite's defaults are wrong
here: `foreign_keys` is off by default and per connection (without it every
REFERENCES clause is decoration), `journal_mode = WAL` lets the server read
while a fetch or import writes, and `busy_timeout` makes a concurrent writer
wait instead of failing.

## Invariants worth knowing before you change things

**Never invent a quantity.** If a video does not state an amount, `quantity`
stays null. These numbers feed nutrition tracking, where a fabricated value is
the one error nobody would ever catch. A third of the collection's ingredients
(335 of 882) have no quantity, and that is correct for them. `schema.py` and a
CHECK constraint enforce the shape; the honesty is on whoever extracts.

**Only verified Fitatu products are stored.** `products.verified` is CHECKed to
1, so this is a property of the database rather than a filter each call site has
to remember — there is no code path, present or future, that can store an
unverified product. Resolution discards unverified candidates before scoring,
and there is deliberately no endpoint that writes a product directly.

**A blank is better than a wrong id.** An unresolved ingredient name is a normal
outcome. A wrong product silently corrupts the nutrition data the library exists
to produce, and nothing downstream would catch it. 117 names are currently
unresolved and that is a reported state, not a backlog to clear by reaching for
the nearest-looking match.

**The two ledger gates are not interchangeable.** Fetching is gated on what is
actually stored for a video; re-extraction is gated on `description_sha256`. The
playlist listing returns ids and nothing else, so a content hash *cannot* gate
fetching — the description is unknown at that point. An early draft conflated
these; the tests in `tests/test_ledger.py` exist to stop it coming back.

**`fetch_status` and `extract_status` are separate columns.** The YAML ledger had
one `status` that fetch wrote (`complete`, `partial`) and extraction then
overwrote (`ok`, `needs_review`, `no_recipe_found`), so after extraction ran the
fetch state was unknowable — it had been erased for 13 of 73 videos. Two gates,
two columns. `save_ledger` coalesces `extract_status`, so a fetch pass that
knows nothing about extraction cannot clobber it.

**An ingredient's product is a join, not a copied value.** `ingredients_resolved`
joins an ingredient's normalised name to `ingredient_products`. What used to be
`products apply` — a deterministic pass that copied ids into 72 files and had to
be re-run whenever the mapping grew — is now a view that cannot go stale. There
is no `apply` command any more, and `tests/test_products.py` asserts that.
`ingredients.product_id` exists only as a per-ingredient override.

**Only ever fetch the original caption track.** YouTube lists auto-captions in
~157 languages per video, but exactly one is real speech recognition output —
the key suffixed `-orig`, as in `pl-orig`. Every other entry is machine-translated
from it. Requesting `pl` on an English video returns a Polish translation of
English speech; this actually happened, and the result is a far worse source for
ingredient names than the English original. `pick_caption_track` prefers
human-written captions, then `-orig`, then a plainly-named track.

`original_langs` in `config.yaml` is therefore an **allowlist of languages you
can read**, not a request list. A Polish video yields Polish, an English video
yields English. Many saved shorts carry only one language, and completeness
accounts for that — requiring both would leave them permanently incomplete and
`--until-complete` would never terminate.

**Subtitles are stored as plain prose.** Auto-caption VTT is a rolling window of
repeated lines with a timestamp on every word; `subtitles.py` strips the timing,
collapses the repeats and decodes YouTube's double-encoded entities (a single
decode pass turns `&amp;oacute;` into `&oacute;` and loses the Polish diacritic).
Timings are used nowhere, so the `.vtt` form was not migrated: 456 KB of timing
data against the 56 KB of prose derived from it.

**Never persist yt-dlp's full info dict.** It is ~620 KB per video of format
lists and signed URLs that rotate on every fetch. `prune_metadata` keeps the
~3 KB that matters.

**Ingredient names stay in the source language.** Fitatu is a Polish product;
its catalogue matches `pierś z kurczaka` far better than a translation.

## Known constraint: subtitle throttling

YouTube serves metadata reliably but throttles its subtitle endpoint hard for
anonymous clients — persistent HTTP 429 even with exponential backoff. Fetch
treats missing subtitles as non-fatal and proceeds on title and description
alone, which is often enough. To lift the throttling, set
`cookies_from_browser` in `config.yaml` so yt-dlp reuses a real browser session.

YouTube extraction also now requires a JavaScript runtime; `fetch.py` offers node
alongside the default deno. Signature and n-challenge warnings are harmless here
because no media is ever downloaded.

## Product resolution

`recipe_library/resolve/` matches an ingredient name to a Fitatu product with no
model in the loop. Split in two:

* **`scoring.py` is pure.** Given a query and candidates it returns a decision
  and the reasoning, with no network and no database. This is where the
  judgement that used to happen in a model session now lives, so it is the
  part tested hardest (`tests/test_scoring.py`).
* **`resolver.py` wires it up**: build the queries, call `search_food`, discard
  everything unverified, score what remains, persist the answer.

Three rules do the work, and all three are about refusing rather than matching:

**Similarity is symmetric.** An F1 of how much of the query the candidate covers
against how much of the candidate the query asked for. Recall alone would match
"chicken" to "chicken and leek soup with cream"; precision alone would match it
to anything short.

**An ambiguous answer is no answer.** If the two best candidates are *different
products* with scores within `match_min_gap`, nothing is returned. The catalogue
is full of near-duplicates and picking the first by coin-flip is exactly how a
wrong product gets in. Two rows pointing at the same product id are agreement,
not conflict.

**Inflection is matched narrowly.** Tokens count as the same word only when they
agree to within their last character and are close in length. That matches
`mielona`/`mielone` and `kurczak`/`kurczaka` while keeping `chicken` and
`chickpea` apart, which a looser prefix rule does not.

**The catalogue is Polish and most recipes are not.** 50 of 72 recipes are
English, and `search_food` defaults to a Polish locale. A model used to supply
the Polish equivalent alongside the English name; that equivalent is now data,
in `query_aliases`, seeded with the 405 phrases that previously worked. **The
aliases were migrated in full, including those of the dropped unverified
products** — an alias records which phrase finds an ingredient in a Polish
catalogue, which is knowledge independent of whether the product it found was
verified, and it is what gives a dropped name a real chance of resolving again.

A new English name with no alias and no Polish cognate will not resolve. That is
reported, not papered over: the recipe fails to import with the name listed.

```bash
uv run python -m recipe_library.products status    # coverage
uv run python -m recipe_library.products pending   # names with no product yet
uv run python -m recipe_library.products failures  # names the matcher refused, and why
uv run python -m recipe_library.products resolve --limit 20
uv run python -m recipe_library.products alias --name NAME --query PHRASE
uv run python -m recipe_library.products map --name NAME --product-id ID
```

### The migration dropped 154 product references, deliberately

Switching to verified-only cost real coverage, and it is worth knowing the shape
of it. Of 247 distinct product ids in the old cache, **84 were never verified**.
They backed **154 of 862** ingredient→product mappings across **60 of 72
recipes, 44 of them already imported into Fitatu**. Mapped ingredients went from
862 to 708.

Nothing in Fitatu changed — those recipes are already posted — but they are no
longer reproducible from the library until their names re-resolve. Verification
was judged **per product id, not per cache entry**: an id vouched for by any
entry is verified, because the id *is* the product and an entry written before
the flag was checked says "unknown", not "unverified".

## The HTTP server

`recipe_library/api/` serves the library with FastAPI. Handlers are sync `def`,
not `async def`: `sqlite3` is a blocking driver, so an async handler would block
the event loop on every query, while Starlette runs sync handlers in a
threadpool. A connection is opened per request and closed with it — SQLite
connections are not safe across threads, and a module-level one would be a bug
waiting for the second concurrent request.

```
GET    /health  /stats
GET    /recipes  /recipes/{id}
PUT    /recipes/{id}                 # what /extract-recipes writes to
DELETE /recipes/{id}
POST   /recipes/{id}/resolve         # match its names to verified products
POST   /recipes/{id}/import          # resolve, then create in Fitatu
GET    /imports/pending
GET    /products  /products/{id}  /products/pending  /products/failures
GET    /products/aliases      POST /products/aliases      DELETE /products/aliases
GET    /products/mappings/{name}   POST /products/mappings   DELETE /products/mappings/{name}
POST   /products/resolve/{name}
GET    /videos  /videos/{id}  /videos/{id}/subtitles  /videos/{id}/source
GET    /videos/pending-extraction
POST   /videos/{id}/extraction       # the stage 2 verdict
```

**Fetching is not exposed.** A pass takes many minutes, makes dozens of
throttled requests with exponential backoff, and must stay resumable across an
interrupt. That is a batch job, not a request.

`PUT /recipes/{id}` refuses `import_status`, `fitatu_recipe_id` and friends, and
preserves whatever the importer already recorded. Resetting those on a
re-extraction would drop the duplicate gate on an already-imported recipe.

## The recipes-library MCP server

`recipe_library/mcp_server.py` serves the collection over MCP so a client can
browse it without querying the database. Read-only; every statement is a SELECT.
Registered in `.mcp.json` as `recipes-library`. One tool, `list`: a summary per
recipe plus collection-wide totals, with ingredients omitted unless
`include_ingredients` is true. Filters: `language`, `import_status`, `limit`.

The listing lives in `recipes.py` and `store/recipes.py` and is tested directly,
with no protocol in the way. Note that **mcp 2.x renamed `FastMCP` to
`MCPServer`** and its model fields are snake_case (`read_only_hint`,
`server_info`, `structured_content`), not the camelCase of the wire format.

## Importing into Fitatu

`recipe_library/fitatu_import/` creates Fitatu recipes from the library. It uses
the **`fitatu-api` HTTP client directly — no MCP server is involved**, for
product resolution either. The package is unofficial, absent from PyPI and
handles real credentials, so it is pinned to a reviewed commit in `pyproject.toml`.

```bash
uv run python -m recipe_library.fitatu_import --status    # tally
uv run python -m recipe_library.fitatu_import --dry-run   # build and report only
uv run python -m recipe_library.fitatu_import --limit 3   # import for real
```

A dry run contacts nothing and writes nothing, which means phase 1 can only
consult what is already mapped: it will tell you a name is unresolved but not
whether a live search would resolve it. Run `products resolve` first to find out.

Every recipe carries `import_status`: `not_imported` (the default), `ok` with a
`fitatu_recipe_id`, or `error` with an `import_error`.

**`POST /recipes` is not idempotent** — the same body twice creates two Fitatu
recipes. `import_status` is the only thing preventing duplicates, which is why a
CHECK constraint requires `fitatu_recipe_id` alongside `ok`, why a recorded id
outranks the status, and why `--force` is dangerous.

**Fitatu takes measures, not grams.** Each item is `{itemId, measureId,
measureQuantity}` counting units of that measure, so 500 g against a 150 g portion
becomes 3.333 portions, using grams-per-measure from the stored product. Counts and
spoonfuls (`piece`, `tbsp`, `tsp`, `cup`, `pinch`) have no mass and are **refused
rather than guessed**; `ml` converts only against a millilitre-based measure,
because ml→g needs a density nobody stated.

**A recipe imports whole or not at all.** A recipe in Fitatu missing three of its
eight ingredients would silently under-report calories — the one failure nobody
notices. An unresolved ingredient name fails the whole recipe, with the name in
`import_error`. `--allow-partial` overrides this and warns.

## Fitatu MCP

Installed under `mcp/fitatu/` by npm. **The server is not published to npm** —
only as a GitHub repo — so `mcp/fitatu/package.json` depends on it by git URL
pinned to an exact commit, and `package-lock.json` pins the rest of the tree.
Both are committed; `node_modules/` is not.

Upstream ships no prepared `dist/` and has no `prepare` script, so npm cannot
build it on install. A `postinstall` hook does that step. This is why the entry
point is `mcp/fitatu/node_modules/fitatu-mcp/dist/index.js`.

A clean `npm install` in `mcp/fitatu/` does rebuild that entry point — verified
from an empty `node_modules/`. But note the limit of the reproducibility claim:
the committed `package-lock.json` pins the outer tree, while the nested build
step resolves upstream's own devDependencies (vite, typescript) fresh each time
and is **not** pinned. An upstream toolchain break would surface here.

`.mcp.json` holds **absolute paths** for this machine. It is committed for
convenience, not portability; on another checkout the paths need regenerating or
the server fails silently.

It is **unofficial** third-party code that holds real account credentials in
plaintext and logs into a live account. Review upstream's diff before moving the
pin in `package.json`. Credentials live only in the gitignored
`mcp/fitatu/.env` (mode 600) and must never enter the committed `.mcp.json`.

**It is no longer on the critical path.** Product resolution used `search_food`
through it; that now runs through the `fitatu-api` client, so nothing in the
pipeline depends on this server. It remains installed for ad-hoc inspection of a
live account.

## Out of scope

Preparation steps; reading on-screen text from video frames; non-playlist
sources; any UI.
