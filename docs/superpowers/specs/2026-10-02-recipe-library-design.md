# Recipe Library — Design

**Date:** 2026-10-02
**Status:** Approved (design); implementation plan not yet written

## Purpose

Turn a personal YouTube Shorts collection into a durable, structured recipe
library stored as YAML in this repository, and connect that library to Fitatu
for nutrition tracking.

The collection is a source of cooking inspiration saved over time. Today it is
only watchable: there is no way to search it, see what a recipe needs before
shopping, or get nutrition data from it. The library makes the collection
queryable and machine-readable.

**Success looks like:** running one command ingests new videos from the
playlist; a second produces reviewed YAML recipes; the Fitatu MCP server is
registered with Claude Code and authenticated against the user's account.

## Confirmed constraints

These were established in conversation, not assumed:

| Constraint | Value |
|---|---|
| Collection type | A **public** YouTube playlist — readable anonymously, no cookies or API key |
| Languages | Polish and English, mixed within one playlist |
| Extraction engine | **Claude Code in-session** (skill + slash command), not a standalone API-calling script |
| Analysis depth | **Text only**: title, description, auto-generated subtitles. No video frame reading |
| Recipe content | Title and ingredients only. **No preparation steps** |
| Videos per recipe | One video may yield **several** recipes |
| Fitatu scope | **Setup and registration only** — no recipe pushing in this phase |

### Known unknowns

- **The playlist URL is not yet supplied.** Required before Stage 1 can run.
  Stored in `config.yaml`, not hardcoded.

## Environment (verified on this machine)

- `node` 22.18.0 — exactly the Fitatu MCP's `>=22.18.0` floor, via Volta
- `uv` 0.8.14, `uvx`, `python3` 3.11.7, `ffmpeg`, `docker`, `gh`
- `yt-dlp` **not installed** — obtained per-run via `uv`'s inline script deps
- No MCP servers currently registered; no YouTube skill present

## Architecture

Two deliberately decoupled stages.

```
YouTube playlist (public)
        │
        │  Stage 1: fetch — deterministic, scripted, no LLM
        ▼
   raw/<video_id>/{info.json, subs.pl.vtt, subs.en.vtt}   (committed)
        │
        │  Stage 2: extract — Claude Code in-session, judgment
        ▼
   recipes/<recipe-id>.yaml  +  state/processed.yaml
        │
        │  Phase 2 (out of scope here)
        ▼
   Fitatu (via MCP: search_food, create_recipe)
```

**Why decoupled.** Fetching is cheap, deterministic and high-volume; doing it
inside a Claude session would waste context on mechanical work. Extraction is
irreducibly judgment-based. Separating them means a full playlist fetch costs
nothing in tokens, and extraction can be re-run or corrected offline without
re-hitting YouTube — which matters because YouTube throttles repeat scraping.

### Stage 1 — fetch

`scripts/fetch_playlist.py`, a `uv run --script` file with PEP 723 inline
dependencies (`yt-dlp`). No virtualenv to manage and no global install.

1. List playlist entries: `yt-dlp --flat-playlist --print "%(id)s"`.
2. Diff against `state/processed.yaml`; skip videos already fetched whose
   description hash is unchanged.
3. For each new or changed video, with `--skip-download`:
   - `--write-info-json` → `raw/<id>/info.json` (title, description, channel, url, duration)
   - `--write-auto-subs --sub-langs pl,en --convert-subs vtt` → `raw/<id>/subs.<lang>.vtt`
4. Record `fetched_at` and `description_sha256` in the ledger.

Subtitles are best-effort: a video with none is still fetched and still
extractable from its description alone.

`raw/` **is committed.** It is small text, it is the provenance record for every
recipe, and YouTube descriptions get edited after upload — the original is worth
more than the saved bytes.

### Stage 2 — extract

A skill (`.claude/skills/extract-recipes/SKILL.md`) invoked by a slash command
(`.claude/commands/extract-recipes.md`). For each video not yet extracted:

1. Read `info.json` and any `.vtt` files.
2. Identify how many distinct recipes the video contains.
3. For each, emit `recipes/<recipe-id>.yaml` per the schema below.
4. Update the ledger with `extracted_at`, the recipe ids, and a status.

The title is a valid source for the recipe name — some videos name the dish only
in the title.

## Data model

### Recipe — `recipes/<recipe-id>.yaml`

One file per recipe, because the unit of output is a recipe, not a video.

```yaml
id: kurczak-curry-kokosowe        # slug; equals the filename stem
title: Kurczak w sosie curry z mlekiem kokosowym
title_en: Chicken in coconut curry sauce   # optional, for browsing
language: pl                      # pl | en — the source language
servings: 4                       # null when not stated
ingredients:
  - name: pierś z kurczaka
    quantity: 500
    unit: g
  - name: mleko kokosowe
    quantity: 400
    unit: ml
  - name: cebula
    quantity: 1
    unit: piece                   # "1 onion" — no mass was given
  - name: curry w proszku
    quantity: null
    unit: null
    note: do smaku                # genuinely unquantified
source:
  video_id: dQw4w9WgXcQ
  url: https://www.youtube.com/shorts/dQw4w9WgXcQ
  channel: Some Channel
  recipe_index: 1                 # 1-based; disambiguates multi-recipe videos
  extracted_from: [description, subtitles]   # any of: title, description, subtitles
extracted_at: 2026-10-02
```

**Field rules:**

- `unit` ∈ {`g`, `ml`, `piece`, `tbsp`, `tsp`, `cup`, `pinch`} or `null`.
  `piece` is explicit so that "1 onion" is distinguishable from "unknown amount".
- `quantity` is a number or `null`. Never a string, never a range — if a source
  gives a range, take the lower bound and record the original in `note`.
- `id` is a slug of the title. On collision, append `-2`, `-3`.
- `extracted_from` records provenance, so a thin extraction can be spotted later.

**Never invent quantities.** If a short does not state an amount, `quantity`
stays `null`. A plausible-looking guess is the one failure mode that would go
unnoticed and would silently corrupt the nutrition data this library feeds into
Fitatu.

**Ingredient names stay in the source language.** Fitatu is a Polish product;
`search_food` will match `pierś z kurczaka` far better than an English
translation. `title_en` exists for human browsing, not for lookup.

### Ledger — `state/processed.yaml`

```yaml
videos:
  dQw4w9WgXcQ:
    fetched_at: 2026-10-02
    extracted_at: 2026-10-02
    description_sha256: a1b2c3...
    recipes: [kurczak-curry-kokosowe, salatka-z-feta]
    status: ok                    # ok | no_recipe_found | needs_review | unavailable
```

`description_sha256` decides re-processing: a changed description marks the
video for re-extraction. `no_recipe_found` is the manual-review pile — videos
where title, description and subtitles all came back dry. `needs_review` marks
extractions the operator flagged as doubtful.

The ledger is the single source of truth for "what is done", so both stages are
idempotent and safely re-runnable as the playlist grows.

## Fitatu MCP integration

Setup and registration only.

1. Clone `https://github.com/AndekQR/fitatu-mcp-unofficial` to `vendor/fitatu-mcp`
   (gitignored), **pinned to a reviewed commit** rather than tracking `main`.
2. `npm install && npm run build`.
3. Create `vendor/fitatu-mcp/.env`, mode `600`, containing `FITATU_EMAIL` and
   `FITATU_PASSWORD`.
4. Register over **stdio** — the server's own documented configuration, and it
   avoids a long-running HTTP service:

```json
{
  "mcpServers": {
    "fitatu": {
      "command": "node",
      "args": [
        "--env-file=/abs/path/vendor/fitatu-mcp/.env",
        "/abs/path/vendor/fitatu-mcp/dist/index.js"
      ],
      "env": { "MCP_TRANSPORT": "stdio" }
    }
  }
}
```

5. Verify by calling `get_current_user` and confirming the account resolves.

**Credentials never enter `.mcp.json`**, which is committed. They live only in
the gitignored `.env`, referenced by absolute path.

**Risk, stated once:** this is unofficial third-party code that holds the user's
real Fitatu password in plaintext and logs into their live account. Pinning to a
reviewed commit is the mitigation. The server exposes ~20 tools including
`create_recipe` and `search_food`, which is what makes the phase-2 push viable.

## Repository layout

```
.
├── CLAUDE.md
├── .mcp.json                      # fitatu registration, no secrets
├── .gitignore                     # vendor/, .env, __pycache__
├── config.yaml                    # playlist URL, sub languages
├── docs/superpowers/specs/
├── scripts/fetch_playlist.py      # uv inline-deps script
├── .claude/skills/extract-recipes/SKILL.md
├── .claude/commands/extract-recipes.md
├── tests/
├── raw/<video_id>/                # committed provenance
├── recipes/<recipe-id>.yaml
├── state/processed.yaml
└── vendor/fitatu-mcp/             # gitignored clone
```

## Error handling

| Condition | Behaviour |
|---|---|
| Playlist URL missing from `config.yaml` | Fail immediately with a clear message |
| yt-dlp throttled or blocked by YouTube | Fail loudly, do not write a partial ledger entry; the run is resumable |
| Video unavailable or removed | Record `status: unavailable`, continue the batch |
| No auto-subtitles for a video | Not an error — proceed with title and description |
| No ingredients found anywhere | `status: no_recipe_found`; write no recipe file |
| Recipe id collides | Append numeric suffix |
| Malformed existing YAML | Schema test fails; never silently rewritten |

A failure on one video must never abort the batch or corrupt the ledger.

## Testing

pytest, run via `uv`.

- **Schema conformance** over all `recipes/*.yaml`: valid YAML, required fields
  present, `quantity` numeric-or-null, `unit` within the enum, `source.url`
  present. This is the main guard against extraction mistakes, since extraction
  is done by a model rather than by testable code.
- **Fetch stage** against one known public short: asserts `info.json` lands and
  is parseable. Network-dependent, so marked and skippable.
- **Ledger** round-trip and idempotency: a second fetch of unchanged videos is a
  no-op.
- **Slug generation**: Polish diacritics (ł, ż, ś, ę) produce clean ASCII slugs,
  and collisions get suffixed.

## Risk to retire first

**Auto-subtitle availability on Polish Shorts is unverified.** YouTube's
auto-caption coverage for Shorts is uneven, and yt-dlp is periodically
rate-limited. Step one of implementation is running the fetcher against 2–3 real
videos from the actual playlist and inspecting what comes back.

If subtitles are largely absent, descriptions carry the entire load. That
materially weakens the pipeline and would justify revisiting frame reading —
which was explicitly scoped out, and should be reconsidered deliberately rather
than discovered after the pipeline is built.

## Implementation order

1. **Validate yt-dlp** against the real playlist — retire the subtitle risk
2. **Fetch stage** + ledger + tests
3. **Schema + validation test + extract skill/command**; iterate the schema
   against ~5 real videos before committing to it
4. **Full playlist pass**; triage the `no_recipe_found` pile
5. **Fitatu MCP** setup, registration, connection verified

Step 5 is independent of 1–4 and can be pulled forward if an early working
result is preferred.

## Out of scope

Preparation steps; pushing recipes to Fitatu; frame or image analysis;
non-playlist sources; a web or mobile UI; scheduled unattended runs.
