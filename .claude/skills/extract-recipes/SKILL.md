---
name: extract-recipes
description: Use when turning fetched YouTube shorts into recipes - reads each video's stored title, description and subtitles from the library API and writes recipes back to it. Covers multi-recipe videos, Polish and English sources, and the rule against inventing quantities.
---

# Extracting recipes from fetched shorts

Stage 1 has already put everything you need in the database. You are the
extraction engine: read the text, decide what the recipes are, write them back.

## Before you start

The library lives in SQLite and is reached over HTTP. Start the server:

```bash
uv run uvicorn recipe_library.api:app --port 8000
```

Everything below assumes `http://127.0.0.1:8000`. If the server will not start,
say so rather than writing to the database directly — the API is where the
validation and the import-state guards live.

## Which videos to process

```bash
curl -s localhost:8000/videos/pending-extraction | jq
```

That is the authoritative list: videos never extracted, or whose creator edited
the description since (gated on `description_sha256`). It already excludes
videos that were never fetched and ones recorded unavailable.

## Inputs

For each video id:

```bash
curl -s localhost:8000/videos/<video_id>/source | jq
```

That returns the title, the description, the channel, the url, and `subtitles`
as a map of language to prose.

There is **one subtitle language per video: the language it was actually spoken
in.** Only the original caption track is fetched, never YouTube's machine
translations, so a Polish video has `pl` and an English one has `en`. An empty
`subtitles` map means YouTube served none, not that another language is hiding
somewhere.

All three text sources are legitimate: **the title alone sometimes names the
dish**, the description often carries the ingredient list verbatim, and the
subtitles carry whatever was spoken.

## The one rule that matters most

**Never invent a quantity.** If the source does not state an amount, write
`quantity: null` and `unit: null`, and put the original wording in `note`
(`do smaku`, `to taste`, `a handful`).

A plausible-looking guess is the only failure mode here that nobody would ever
notice. These numbers feed nutrition tracking in Fitatu, where a fabricated
"200 g" is worse than an honest blank. A third of this collection's ingredients
have no quantity, and that is the correct state for them.

Related judgements:

- A range (`2-3 ząbki czosnku`) takes the lower bound, with the original in `note`.
- `1 cebula` is `quantity: 1, unit: piece` — a real count, not a missing mass.
- Do not convert units. Record `2 tbsp` as `tbsp`, not as an estimated 30 ml.
- Do not add ingredients that "obviously" belong to the dish but are unstated.

## Multiple recipes in one video

A single short often shows several dishes. The unit of output is a **recipe**,
not a video. Each gets its own id and its own `recipe_index` (1-based, in the
order presented). Only split when the dishes are genuinely separate — a sauce
served with a main course is one recipe, not two.

## Language

Keep ingredient names in the **source language**. Fitatu is a Polish product and
its catalogue matches `pierś z kurczaka` far better than any translation. Set
`language` to `pl` or `en` to match the source, and add an optional `title_en`
for browsing if the source is Polish.

## Output

`PUT /recipes/<id>`, where `<id>` is the slug of the title. Generate it with the
project's own slugifier so Polish diacritics are handled consistently:

```bash
uv run python -c "from recipe_library.slugs import slugify; print(slugify('Łosoś w śmietanie'))"
```

On a collision, append `-2`, `-3`.

```bash
curl -s -X PUT localhost:8000/recipes/kurczak-curry-kokosowe \
  -H 'content-type: application/json' -d '{
  "title": "Kurczak w sosie curry z mlekiem kokosowym",
  "title_en": "Chicken in coconut curry sauce",
  "language": "pl",
  "servings": 4,
  "ingredients": [
    {"name": "pierś z kurczaka", "quantity": 500, "unit": "g"},
    {"name": "cebula", "quantity": 1, "unit": "piece"},
    {"name": "curry w proszku", "quantity": null, "unit": null, "note": "do smaku"}
  ],
  "source": {
    "video_id": "dQw4w9WgXcQ",
    "url": "https://www.youtube.com/shorts/dQw4w9WgXcQ",
    "channel": "Some Channel",
    "recipe_index": 1,
    "extracted_from": ["description", "subtitles"]
  },
  "extracted_at": "2026-10-02"
}'
```

`servings` is omitted or null when not stated. `unit` must be one of `g`, `ml`,
`piece`, `tbsp`, `tsp`, `cup`, `pinch`, or null. `extracted_from` lists which
sources you actually used — `title`, `description`, `subtitles` — so a thin
extraction can be spotted later.

**Do not send any `import_status`, `fitatu_recipe_id` or related field.** Those
belong to the importer and the endpoint refuses them. Re-extracting a recipe
already in Fitatu preserves its recorded import, which is what stops a
re-extraction turning into a duplicate.

**Do not set `fitatu_product_id` on an ingredient.** Products are matched
programmatically against verified Fitatu entries at import time; see
`/resolve-fitatu-products` for the cases that need a human.

A `422` means the recipe breaks a schema rule — the response body names which.
A quantity without a unit is the common one.

## When there is no recipe

If title, description and subtitles together yield no ingredients, write **no
recipe**. These are the videos where the ingredients only ever appeared as
on-screen text, which is deliberately out of scope. Do not guess at them.
Record the verdict on the video instead, so it stops coming back as pending:

```bash
curl -s -X POST localhost:8000/videos/<video_id>/extraction \
  -H 'content-type: application/json' -d '{"status": "no_recipe_found"}'
```

Use `needs_review` for an extraction you completed but doubt — write the recipe
*and* post that status.

Writing a recipe already records that you read its video, so the plain `ok` case
needs no extra call.

## Finishing up

```bash
uv run python -m recipe_library.validate
```

Fix anything it reports. A validation failure means the recipe is wrong, not
that the validator is wrong — it encodes the schema rules above.
