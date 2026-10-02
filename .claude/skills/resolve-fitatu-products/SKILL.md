---
name: resolve-fitatu-products
description: Use when mapping recipe ingredient names to Fitatu product ids via the fitatu MCP server - fills state/fitatu_products.yaml so that recipe creation can run deterministically. Covers querying search_food in batches, picking between candidates, English ingredients in a Polish catalogue, and when to give up on a name.
---

# Resolving ingredient names to Fitatu product ids

You are the matching step, and the only step here that needs judgment. Everything
after it is deterministic: `products apply` writes the ids into the recipe files
and recipe creation reads them straight off the ingredients.

Resolve **once per distinct ingredient name**, not once per recipe. The cache
makes an ingredient used in thirty recipes cost one lookup.

## Before you start

The `fitatu` MCP server must be connected. It validates its environment at
startup and will not boot until `mcp/fitatu/.env` holds real credentials. If
`search_food` is unavailable, stop and say so — do not guess ids.

```bash
uv run python -m recipe_library.products pending   # what needs resolving
uv run python -m recipe_library.products status    # coverage summary
```

`pending` lists the most widely used names first, so working top-down maximises
the number of recipes fixed per lookup.

## Querying

`search_food` takes an **array** of queries and returns results grouped by
`queryIndex`, so batch roughly 10–20 names per call rather than one at a time.

```
search_food(queries: ["cebula", "masło", ...], limit: 5)
```

Useful parameters: `limit` (default 5 per query per source), `includeDetails`
(default false) and `detailsLimit`. Set `includeDetails: true` only when the
default measure is unusable and you need the full `measures` list.

### English ingredients in a Polish catalogue

This matters: 48 of the 70 recipes are English, giving 319 English ingredient
names against 177 Polish ones, with only 4 shared. Fitatu is a Polish product and
`search_food` defaults to `locale: pl_PL`, so `self-rising flour` will not match
as written.

Send **both the original and a Polish equivalent in the same call** — the tool's
own documentation recommends submitting plausible variants together, and extra
queries are nearly free:

```
queries: ["self-rising flour", "mąka samorosnąca", "ground beef", "mięso mielone wołowe"]
```

When a Polish phrase is what found the product, record it with `--query` so the
match stays auditable.

The server **does not infer brand or retailer aliases**. If an ingredient might
appear under a producer or private-label name, supply those variants yourself.

## Choosing between candidates

Each query returns two separate lists, `userItems` (your own Fitatu products) and
`publicItems` (the public catalogue), in Fitatu's order. They are **not merged or
deduplicated** — judge them together.

Prefer, in roughly this order:

1. **The generic raw ingredient** over a branded or prepared product. Recipes say
   `cebula`, not `Cebula Biedronka`. Pick the branded item only when the recipe
   names a brand.
2. **`verified: true`**, which is Fitatu's own quality signal.
3. **A gram-based measure**, because recipe quantities are mostly in grams and
   millilitres. A product whose only measure is `opakowanie` forces a conversion
   you cannot make safely.

Watch for these:

- **Raw versus cooked matters enormously.** `pierś z kurczaka` raw and grilled
  differ by a lot per 100 g. Recipes list raw weights, so prefer raw.
- **US fat ratios have no Polish equivalent.** `93/7 ground beef` will only match
  a generic `mięso mielone wołowe`. Record it, and note the imprecision — do not
  invent a product that encodes the ratio.
- **Do not settle for a near-miss.** `jogurt grecki 0%` is not `jogurt grecki`;
  the calorie difference is the whole point of tracking.

## Recording the result

Never hand-edit the cache. Use the CLI, which timestamps entries and keeps the
file sorted:

```bash
uv run python -m recipe_library.products record \
  --name "self-rising flour" --query "mąka samorosnąca" \
  --product-id 12345 --display-name "Mąka samorosnąca" \
  --source public --verified \
  --measure-id g --measure-name gram --weight-g 1 --kcal 3.6
```

Copy `productId` and `measureId` exactly as `search_food` returned them. A
candidate carries **either** `productId` **or** `recipeId`, never both; this cache
is for products, so skip recipe candidates.

When nothing is an acceptable match:

```bash
uv run python -m recipe_library.products unresolved \
  --name "sos tahini domowy" --reason "no_match"
```

Useful reasons: `no_match`, `ambiguous`, `compound` (an ingredient that is itself
a recipe), `not_a_food` (`woda`, `sól do gotowania`). Unresolved names are not
retried on later runs unless `--retry-unresolved` is passed, so a known miss does
not come back every time.

**An unresolved name is a perfectly good outcome.** A wrong product id is far
worse than a blank, because it silently corrupts the nutrition data this whole
library exists to produce. If you are unsure between two candidates, record it
`ambiguous` and ask.

## Finishing

```bash
uv run python -m recipe_library.products apply   # writes ids into recipes/*.yaml
uv run python -m recipe_library.validate         # confirm the files stay valid
```

`apply` is deterministic and idempotent: rerunning changes nothing, and an
ingredient the cache does not know keeps whatever it already had rather than
being cleared.
