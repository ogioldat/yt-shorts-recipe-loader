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
uv run python -m recipe_library.fetch       # stage 1: fetch the playlist
uv run python -m recipe_library.validate    # check every recipes/*.yaml
./scripts/setup_fitatu.sh                   # npm-install and build the Fitatu MCP
```

Useful `fetch` flags: `--limit N` (fetch only N new videos), `--video ID`
(a specific video, bypasses config), `--refresh` (re-fetch known videos),
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

**`state/processed.yaml` is the ledger** and the single source of truth for what
is done. It makes both stages idempotent and resumable.

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
