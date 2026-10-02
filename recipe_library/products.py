"""Mapping ingredient names to Fitatu product ids, cached on disk.

The split here is deliberate. Choosing which Fitatu product corresponds to
"pierś z kurczaka" is judgment work: `search_food` returns separate user and
public candidate lists, in Fitatu's order, without merging or deduplicating, and
the server explicitly does not infer brand or retailer aliases. So resolution
happens once, in a Claude session, through the MCP server.

Everything after that is deterministic. The cache is the record of those
decisions, `apply_ids` writes them into the recipe files with no model involved,
and recipe creation can then read ids straight off the ingredients.

The cache is keyed by the normalised ingredient name, so the same ingredient
appearing in thirty recipes costs one lookup.
"""

import argparse
import re
import sys
from datetime import date
from pathlib import Path

import yaml

from .paths import RECIPES, STATE

CACHE = STATE / "fitatu_products.yaml"

CACHE_VERSION = 1


def normalize_name(name: str) -> str:
    """The cache key for an ingredient name.

    Case and spacing are noise; Polish diacritics are not, and are preserved
    because the key doubles as the `search_food` query.
    """
    return re.sub(r"\s+", " ", (name or "").strip()).lower()


# --- cache io ---------------------------------------------------------------


def load_cache(path: Path | None = None) -> dict:
    path = path or CACHE
    if not path.exists():
        return {"version": CACHE_VERSION, "products": {}, "unresolved": {}}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    data.setdefault("version", CACHE_VERSION)
    data.setdefault("products", {})
    data.setdefault("unresolved", {})
    return data


def save_cache(cache: dict, path: Path | None = None) -> None:
    path = path or CACHE
    path.parent.mkdir(parents=True, exist_ok=True)
    out = {
        "version": cache.get("version", CACHE_VERSION),
        "products": dict(sorted((cache.get("products") or {}).items())),
        "unresolved": dict(sorted((cache.get("unresolved") or {}).items())),
    }
    path.write_text(
        yaml.safe_dump(out, allow_unicode=True, sort_keys=False, width=100),
        encoding="utf-8",
    )


def record_product(
    cache: dict,
    name: str,
    *,
    product_id: str,
    display_name: str | None = None,
    brand: str | None = None,
    source: str | None = None,
    verified: bool | None = None,
    measure_id: str | None = None,
    measure_name: str | None = None,
    weight_g: float | None = None,
    kcal: float | None = None,
    measures: list[dict] | None = None,
    query: str | None = None,
) -> None:
    """Record a resolved match. `measure_id` is what add_meal_items needs.

    `query` is the phrase that actually found the product, which is not always
    the ingredient name: Fitatu's catalogue is Polish, so an English ingredient
    like "self-rising flour" is found by its Polish equivalent. Keeping it makes
    the match auditable.
    """
    key = normalize_name(name)
    cache.setdefault("products", {})[key] = {
        "fitatu_product_id": str(product_id),
        "display_name": display_name,
        "brand": brand,
        "source": source,
        "verified": verified,
        "default_measure": {
            "measure_id": measure_id,
            "measure_name": measure_name,
            "weight_g": weight_g,
            "kcal": kcal,
        },
        "measures": measures or [],
        "query": normalize_name(query) if query else key,
        "resolved_at": date.today().isoformat(),
    }
    (cache.get("unresolved") or {}).pop(key, None)


def record_unresolved(cache: dict, name: str, reason: str) -> None:
    """Record that a name has no acceptable match, so it is not retried blindly."""
    key = normalize_name(name)
    cache.setdefault("unresolved", {})[key] = {
        "reason": reason,
        "checked_at": date.today().isoformat(),
    }


# --- recipes ----------------------------------------------------------------


def _recipe_files(recipes_dir: Path | None = None) -> list[Path]:
    return sorted((recipes_dir or RECIPES).glob("*.yaml"))


def ingredient_names(recipes_dir: Path | None = None) -> dict[str, list[str]]:
    """Map normalised ingredient name -> recipe ids that use it."""
    usage: dict[str, list[str]] = {}
    for path in _recipe_files(recipes_dir):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for ing in data.get("ingredients") or []:
            if not isinstance(ing, dict) or not ing.get("name"):
                continue
            usage.setdefault(normalize_name(ing["name"]), []).append(path.stem)
    return usage


def pending_names(
    cache: dict, recipes_dir: Path | None = None, *, retry_unresolved: bool = False
) -> list[str]:
    """Ingredient names with no cache entry yet, most widely used first.

    Names previously recorded unresolved are skipped unless `retry_unresolved`,
    so a known miss does not come back every run.
    """
    usage = ingredient_names(recipes_dir)
    known = set(cache.get("products") or {})
    if not retry_unresolved:
        known |= set(cache.get("unresolved") or {})
    pending = [n for n in usage if n not in known]
    return sorted(pending, key=lambda n: (-len(usage[n]), n))


def apply_ids(
    cache: dict, recipes_dir: Path | None = None, *, dry_run: bool = False
) -> dict[str, int]:
    """Write `fitatu_product_id` onto every ingredient the cache can resolve.

    Deterministic and idempotent: no model involved, and rerunning changes
    nothing once applied. An ingredient the cache does not know keeps whatever it
    has rather than being cleared.
    """
    products = cache.get("products") or {}
    stats = {"recipes_changed": 0, "ingredients_set": 0, "ingredients_unresolved": 0}

    for path in _recipe_files(recipes_dir):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        changed = False
        for ing in data.get("ingredients") or []:
            if not isinstance(ing, dict) or not ing.get("name"):
                continue
            entry = products.get(normalize_name(ing["name"]))
            if entry is None:
                if not ing.get("fitatu_product_id"):
                    stats["ingredients_unresolved"] += 1
                continue
            product_id = entry["fitatu_product_id"]
            if ing.get("fitatu_product_id") != product_id:
                ing["fitatu_product_id"] = product_id
                changed = True
            stats["ingredients_set"] += 1
        if changed:
            stats["recipes_changed"] += 1
            if not dry_run:
                path.write_text(
                    yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100),
                    encoding="utf-8",
                )
    return stats


# --- cli --------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fitatu product id mapping.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_pending = sub.add_parser("pending", help="ingredient names still needing a Fitatu product id")
    p_pending.add_argument("--retry-unresolved", action="store_true")

    sub.add_parser("status", help="summarise coverage")

    p_apply = sub.add_parser("apply", help="write cached ids into recipes/*.yaml")
    p_apply.add_argument("--dry-run", action="store_true")

    # `record` exists so that resolution never hand-edits YAML: the session calls
    # search_food, then hands the chosen candidate's fields to this.
    p_rec = sub.add_parser("record", help="cache one resolved ingredient -> product match")
    p_rec.add_argument("--name", required=True, help="ingredient name as written in recipes")
    p_rec.add_argument("--product-id", required=True)
    p_rec.add_argument("--display-name")
    p_rec.add_argument("--brand")
    p_rec.add_argument("--source", choices=["user", "public"])
    p_rec.add_argument("--verified", action="store_true")
    p_rec.add_argument("--measure-id")
    p_rec.add_argument("--measure-name")
    p_rec.add_argument("--weight-g", type=float)
    p_rec.add_argument("--kcal", type=float)
    p_rec.add_argument(
        "--query", help="the phrase that found it, when different from --name"
    )

    p_un = sub.add_parser("unresolved", help="record that a name has no acceptable match")
    p_un.add_argument("--name", required=True)
    p_un.add_argument("--reason", default="no_match")

    args = parser.parse_args(argv)
    cache = load_cache()

    if args.command == "pending":
        pending = pending_names(cache, retry_unresolved=args.retry_unresolved)
        if not pending:
            print("Nothing pending.")
            return 0
        usage = ingredient_names()
        print(f"{len(pending)} ingredient name(s) need a Fitatu product id:\n")
        for name in pending:
            print(f"  {name}  ({len(usage[name])} recipe(s))")
        return 0

    if args.command == "status":
        usage = ingredient_names()
        products = cache.get("products") or {}
        unresolved = cache.get("unresolved") or {}
        print(f"distinct ingredient names : {len(usage)}")
        print(f"resolved to a product     : {len(set(usage) & set(products))}")
        print(f"recorded unresolved       : {len(set(usage) & set(unresolved))}")
        print(f"pending                   : {len(pending_names(cache))}")
        return 0

    if args.command == "record":
        record_product(
            cache,
            args.name,
            product_id=args.product_id,
            display_name=args.display_name,
            brand=args.brand,
            source=args.source,
            verified=args.verified or None,
            measure_id=args.measure_id,
            measure_name=args.measure_name,
            weight_g=args.weight_g,
            kcal=args.kcal,
            query=args.query,
        )
        save_cache(cache)
        print(f"Recorded {normalize_name(args.name)!r} -> product {args.product_id}")
        return 0

    if args.command == "unresolved":
        record_unresolved(cache, args.name, args.reason)
        save_cache(cache)
        print(f"Recorded {normalize_name(args.name)!r} as unresolved ({args.reason})")
        return 0

    stats = apply_ids(cache, dry_run=args.dry_run)
    prefix = "Would set" if args.dry_run else "Set"
    print(
        f"{prefix} ids on {stats['ingredients_set']} ingredient(s) "
        f"across {stats['recipes_changed']} recipe file(s); "
        f"{stats['ingredients_unresolved']} still unresolved."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
