---
name: resolve-fitatu-products
description: Use when ingredient names will not match a verified Fitatu product - reviews what the automatic resolver rejected and fixes it with a search-phrase alias or a hand-recorded mapping. Covers English ingredients in a Polish catalogue, why ambiguity is refused, and when to leave a name unresolved.
---

# Fixing ingredient names the resolver could not match

**Resolution itself is no longer your job.** It runs programmatically as the
first phase of every import: search Fitatu, discard everything unverified, score
what remains, and take the winner only if it is clearly the winner
(`recipe_library/resolve/`). You are here for the leftovers.

```bash
uv run python -m recipe_library.products status     # coverage
uv run python -m recipe_library.products pending    # names with no product yet
uv run python -m recipe_library.products failures   # names the matcher refused
```

`failures` is the interesting one: it shows each name, why it was refused, and
the candidates it rejected with their scores and verification flags.

## Read the reason before doing anything

The four refusals mean different things and only two are yours to fix.

| Reason | What it means | What to do |
|---|---|---|
| `none of the N candidate(s) is verified by Fitatu` | Fitatu has the product but has not verified its nutrition data | **Nothing.** This is the rule working. Leave it unresolved. |
| `no candidates returned by Fitatu` | The phrase found nothing in a Polish catalogue | Add an alias (below) |
| `best verified candidate ... below the threshold` | Something was found but does not look like this ingredient | Add an alias; the query is probably in the wrong language |
| `ambiguous: ... different products scoring within ...` | Two genuinely different products look equally good | Read both and map by hand, or leave it |

## Aliases: the usual fix

Fitatu's catalogue is Polish and most of these recipes are not. 50 of 72
recipes are English, and an English ingredient name mostly does not appear in a
Polish catalogue at all. An alias records the phrase that *does* find it:

```bash
uv run python -m recipe_library.products alias \
  --name "self-rising flour" --query "mąka z proszkiem do pieczenia"
```

That teaches the resolver permanently, clears the recorded failure, and the
next import retries the name by itself.

Write the Polish phrase a Polish shopper would search for, not a literal
translation. The 405 aliases already seeded from previous resolutions are worth
skimming for the house style:

```bash
uv run python -m recipe_library.products aliases | head -40
```

## Mapping by hand

When an alias will not do it — usually the ambiguous case, where you have read
both candidates and know which one is meant:

```bash
uv run python -m recipe_library.products map --name "spray oil" --product-id 116722413
```

**The product must already be stored, and only verified products are ever
stored.** So this overrides the matcher, never the verification rule. If the
product is not stored, resolve it first:

```bash
uv run python -m recipe_library.products resolve --name "spray oil"
```

## When to stop

**A blank is better than a wrong id.** An unresolved name is a normal outcome,
not a task left undone. The recipes it appears in will refuse to import and say
which name blocked them, which is the honest result; a wrong product silently
corrupts the nutrition data this library exists to produce, and nothing
downstream would catch it.

So: leave a name unresolved when the only candidates are unverified, when you
cannot tell which of two products is meant, or when the ingredient is not really
a product (`seasonings`, `sauce of choice`). Do not reach for the
nearest-looking match to clear the list.

## Checking your work

```bash
uv run python -m recipe_library.products resolve --limit 20   # work the pending list
uv run python -m recipe_library.fitatu_import --dry-run       # what still blocks an import
```

The dry run groups its failures: recipes blocked by an unresolved name, recipes
that would import partially, and recipes where the video stated no quantity for
anything. Only the first group is yours.
