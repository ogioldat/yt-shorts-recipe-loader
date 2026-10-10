# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A personal recipe library built from a public YouTube Shorts playlist of cooking
videos, in Polish and English. Shorts are turned into structured YAML holding
**title and ingredients only** — no preparation steps. The library is then
usable from Fitatu for nutrition tracking via an MCP server.

## Commands

```bash
uv sync                                     # install dependencies
uv run pytest                               # whole suite; needs no network
uv run pytest tests/test_schema.py -q       # one file
uv run pytest -k "quantity" -q              # one topic
uv run python -m recipe_library.fetch       # stage 1: one pass over the playlist
uv run python -m recipe_library.fetch --until-complete   # keep going until done
uv run python -m recipe_library.validate    # check every recipes/*.yaml
```

Useful `fetch` flags: `--until-complete` (keep making passes until nothing is
missing), `--limit N` (videos per pass), `--video ID` (a specific video; bypasses
config entirely, so config defaults do not apply), `--refresh` (re-fetch metadata
already on disk), `--subtitle-output text|vtt|both`,
`--cookies-from-browser chrome` (see throttling below).

Always invoke Python through `uv run`. A bare `python3` on this machine resolves
to an unrelated project's virtualenv.

## Architecture

Two deliberately decoupled stages.

```
public playlist → [stage 1: fetch, scripted] → raw/<video_id>/
                → [stage 2: extract, Claude] → recipes/*.yaml
```

**Stage 1 — `recipe_library/fetch.py`.** Uses the `yt_dlp` Python API. Writes
`raw/<video_id>/meta.json` plus any `subs.<lang>.vtt`. Deterministic, no LLM.

**Stage 2 — `.claude/skills/extract-recipes/`.** Claude reads the raw text and
writes one YAML file per recipe. This is judgment work, which is why it is not
code. Driven by `/extract-recipes`.

Separating them means a full playlist fetch costs no context, and extraction can
be re-run or corrected without touching the network — which matters, because
YouTube throttles repeat scraping.

**`state/processed.yaml` is the ledger**, and `recipe_library/completeness.py`
decides what still needs fetching. Stage 1 is **additive**: it never deletes what
an earlier run fetched, re-reads an existing `meta.json` rather than refetching
it, and requests only what is actually absent. Re-running it is always safe.

## Invariants worth knowing before you change things

**Never invent a quantity.** If a video does not state an amount, `quantity`
stays `null`. These numbers feed nutrition tracking, where a fabricated value is
the one error nobody would ever catch. `recipe_library/schema.py` enforces the
shape; the honesty is on whoever extracts.

**The two ledger gates are not interchangeable.** Fetching is gated on video-id
presence; re-extraction is gated on `description_sha256`. The playlist listing
returns ids and nothing else, so a content hash *cannot* gate fetching — the
description is unknown at that point. An early draft conflated these; the tests
in `tests/test_ledger.py` exist to stop it coming back.

**Only ever fetch the original caption track.** YouTube lists auto-captions in
~157 languages per video, but exactly one is real speech recognition output —
the key suffixed `-orig`, as in `pl-orig`. Every other entry is machine-translated
from it. Requesting `pl` on an English video returns a Polish translation of
English speech; this actually happened, and the resulting file is a far worse
source for ingredient names than the English original. `pick_caption_track`
prefers human-written captions, then `-orig`, then a plainly-named track.

`original_langs` in `config.yaml` is therefore an **allowlist of languages you
can read**, not a request list. A Polish video yields Polish, an English video
yields English. Many saved shorts carry only one language, and completeness
accounts for that — requiring both would leave them permanently incomplete and
`--until-complete` would never terminate.

**Subtitles are stored as plain text by default.** Auto-caption VTT is a rolling
window of repeated lines with a timestamp on every word; `subtitles.py` strips
the timing, collapses the repeats and decodes YouTube's double-encoded entities
(a single decode pass turns `&amp;oacute;` into `&oacute;` and loses the Polish
diacritic). Timings are used nowhere, hence `subtitle_output: text`.

**Never persist yt-dlp's full info dict.** It is ~620 KB per video of format
lists and signed URLs that rotate on every fetch. `prune_metadata` keeps the
~3 KB that matters. `raw/**/*.info.json` is gitignored as a backstop.

**Ingredient names stay in the source language.** Fitatu is a Polish product;
`search_food` matches `pierś z kurczaka` far better than a translation.

**`raw/` is committed** — it is the provenance for every recipe, and YouTube
descriptions get edited after upload.

## Known constraint: subtitle throttling

YouTube serves metadata reliably but throttles its subtitle endpoint hard for
anonymous clients — persistent HTTP 429 even with exponential backoff. Fetch
treats missing subtitles as non-fatal and proceeds on title and description
alone, which is often enough. To lift the throttling, set
`cookies_from_browser` in `config.yaml` so yt-dlp reuses a real browser session.

YouTube extraction also now requires a JavaScript runtime; `fetch.py` offers node
alongside the default deno. Signature and n-challenge warnings are harmless here
because no media is ever downloaded.

## Fitatu product ids

The MCP server is used for exactly one thing: learning which Fitatu product
corresponds to an ingredient name. That mapping lives in
`state/fitatu_products.yaml`, keyed by the normalised ingredient name, so an
ingredient used in thirty recipes costs one lookup.

The split is deliberate. **Matching needs judgment** — `search_food` returns
separate user and public candidate lists in Fitatu's order, without merging or
deduplicating, and it does not infer brand aliases — so it happens in a session
via `/resolve-products`. **Everything after is deterministic**: `products apply`
writes `fitatu_product_id` onto each ingredient with no model involved, and
recipe creation reads the ids straight off the recipes.

```bash
uv run python -m recipe_library.products pending   # names still needing an id
uv run python -m recipe_library.products status    # coverage
uv run python -m recipe_library.products apply     # write ids into recipes/*.yaml
```

**The catalogue is Polish and most recipes are not.** 48 of 70 recipes are
English, giving 319 English ingredient names against 177 Polish ones with only 4
shared, while `search_food` defaults to `locale: pl_PL`. So resolution sends the
original *and* a Polish equivalent in one call, and the cache records which phrase
actually matched in `query`.

**A blank is better than a wrong id.** An unresolved name is a normal outcome; a
wrong product silently corrupts the nutrition data the library exists to produce.

## The recipes-library MCP server

`recipe_library/mcp_server.py` serves this repository's collection over MCP, so a
client can browse it without reading 70 YAML files. Read-only; it never writes to
`recipes/`. Registered in `.mcp.json` as `recipes-library`.

One tool, `list`: a summary per recipe (id, title, language, servings, ingredient
count, how many lack a quantity, how many are mapped to a Fitatu product, import
status, source video) plus collection-wide totals. Ingredients are omitted unless
`include_ingredients` is true, because the collection holds 847 of them. Filters:
`language`, `import_status`, `limit`.

```bash
uv run python -m recipe_library.mcp_server   # stdio
```

The listing lives in `recipe_library/recipes.py` and is tested directly, with no
protocol in the way. Note that **mcp 2.x renamed `FastMCP` to `MCPServer`** and
its model fields are snake_case (`read_only_hint`, `server_info`,
`structured_content`), not the camelCase of the wire format.

## Importing into Fitatu

`recipe_library/fitatu_import/` creates Fitatu recipes from `recipes/*.yaml`. It
uses the **`fitatu-api` HTTP client directly — not the MCP server**, whose only
job here is resolving product ids. The package is unofficial, absent from PyPI and
handles real credentials, so it is pinned to a reviewed commit in `pyproject.toml`.

```bash
uv run python -m recipe_library.fitatu_import --status    # tally
uv run python -m recipe_library.fitatu_import --dry-run   # build and report only
uv run python -m recipe_library.fitatu_import --limit 3   # import for real
```

Every recipe carries `import_status`: `not_imported` (the default, including when
absent), `ok` with a `fitatu_recipe_id`, or `error` with an `import_error`.

**`POST /recipes` is not idempotent** — the same body twice creates two Fitatu
recipes. `import_status: not_imported` is the only thing preventing duplicates, which is why
the schema requires `fitatu_recipe_id` alongside it and why `--force` is dangerous.

**Fitatu takes measures, not grams.** Each item is `{itemId, measureId,
measureQuantity}` counting units of that measure, so 500 g against a 150 g portion
becomes 3.333 portions, using grams-per-measure from the product cache. Counts and
spoonfuls (`piece`, `tbsp`, `tsp`, `cup`, `pinch`) have no mass and are **refused
rather than guessed**; `ml` converts only against a millilitre-based measure,
because ml→g needs a density nobody stated.

**A recipe imports whole or not at all.** A recipe in Fitatu missing three of its
eight ingredients would silently under-report calories — the one failure nobody
notices. `--allow-partial` overrides this and warns.

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

It validates its environment at startup, so it will not boot until `.env` holds
a real email and password. It exposes ~20 tools including `search_food` and
`create_recipe`, which is what would make a YAML→Fitatu push viable. That push
is **not** built: current scope is setup only.

## Out of scope

Preparation steps; pushing recipes to Fitatu; reading on-screen text from video
frames; non-playlist sources; any UI.
