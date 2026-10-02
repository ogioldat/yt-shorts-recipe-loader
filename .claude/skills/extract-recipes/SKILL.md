---
name: extract-recipes
description: Use when turning fetched YouTube shorts into recipe YAML files - reads raw/<video_id>/ metadata and subtitles and writes recipes/<id>.yaml, updating the ledger. Covers multi-recipe videos, Polish and English sources, and the rule against inventing quantities.
---

# Extracting recipes from fetched shorts

Stage 1 has already put everything you need on disk. You are the extraction
engine: read the text, decide what the recipes are, write YAML.

## Inputs

For each video id:

- `raw/<video_id>/meta.json` — title, description, channel, url, and which
  subtitle languages were available
- `raw/<video_id>/subs.<lang>.vtt` — subtitles, when YouTube served any

All three text sources are legitimate: **the title alone sometimes names the
dish**, the description often carries the ingredient list verbatim, and the
subtitles carry whatever was spoken.

## Which videos to process

Read `state/processed.yaml`. A video needs extraction when it has no
`extracted_at`, or when its `description_sha256` differs from the hash of the
description now in `meta.json` (the creator edited it). Skip anything with
`status: unavailable`.

## The one rule that matters most

**Never invent a quantity.** If the source does not state an amount, write
`quantity: null` and `unit: null`, and put the original wording in `note`
(`do smaku`, `to taste`, `a handful`).

A plausible-looking guess is the only failure mode here that nobody would ever
notice. These numbers feed nutrition tracking in Fitatu, where a fabricated
"200 g" is worse than an honest blank.

Related judgements:

- A range (`2-3 ząbki czosnku`) takes the lower bound, with the original in `note`.
- `1 cebula` is `quantity: 1, unit: piece` — a real count, not a missing mass.
- Do not convert units. Record `2 tbsp` as `tbsp`, not as an estimated 30 ml.
- Do not add ingredients that "obviously" belong to the dish but are unstated.

## Multiple recipes in one video

A single short often shows several dishes. The unit of output is a **recipe**,
not a video. Each gets its own file and its own `recipe_index` (1-based, in the
order presented). Only split when the dishes are genuinely separate — a sauce
served with a main course is one recipe, not two.

## Language

Keep ingredient names in the **source language**. Fitatu is a Polish product and
its `search_food` matches `pierś z kurczaka` far better than any translation.
Set `language` to `pl` or `en` to match the source, and add an optional
`title_en` for your own browsing if the source is Polish.

## Output

Write `recipes/<id>.yaml`, where `<id>` is the slug of the title. Generate it
with the project's own slugifier so Polish diacritics are handled consistently:

```bash
uv run python -c "from recipe_library.slugs import slugify; print(slugify('Łosoś w śmietanie'))"
```

On a collision, append `-2`, `-3`.

```yaml
id: kurczak-curry-kokosowe
title: Kurczak w sosie curry z mlekiem kokosowym
title_en: Chicken in coconut curry sauce   # optional
language: pl
servings: 4                               # null when not stated
ingredients:
  - name: pierś z kurczaka
    quantity: 500
    unit: g
  - name: cebula
    quantity: 1
    unit: piece
  - name: curry w proszku
    quantity: null
    unit: null
    note: do smaku
source:
  video_id: dQw4w9WgXcQ
  url: https://www.youtube.com/shorts/dQw4w9WgXcQ
  channel: Some Channel
  recipe_index: 1
  extracted_from: [description, subtitles]
extracted_at: 2026-10-02
```

`unit` must be one of `g`, `ml`, `piece`, `tbsp`, `tsp`, `cup`, `pinch`, or null.
`extracted_from` lists which sources you actually used — `title`, `description`,
`subtitles` — so a thin extraction can be spotted later.

## When there is no recipe

If title, description and subtitles together yield no ingredients, write **no
recipe file**. Set the ledger entry's `status` to `no_recipe_found`. These are
the videos where the ingredients only ever appeared as on-screen text, which is
deliberately out of scope. Do not guess at them.

Use `status: needs_review` for an extraction you completed but doubt.

## Finishing each video

Update its ledger entry: `extracted_at` (today), `recipes` (the ids you wrote),
and `status`. Then verify:

```bash
uv run python -m recipe_library.validate
```

Fix anything it reports. A validation failure means the YAML is wrong, not that
the validator is wrong — it encodes the schema rules above.
