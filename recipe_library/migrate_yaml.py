"""One-shot migration: the YAML tree and `raw/` into SQLite.

Run once:

    uv run python -m recipe_library.migrate_yaml

Idempotent, so running it twice is harmless -- every write is an upsert keyed on
the same natural key the YAML used.

Four decisions here are not mechanical, and each one is reported in the summary
rather than being done quietly:

**Only verified products come across.** Verification is judged per *product id*,
not per cache entry: the id is the product, and a name whose entry says
`verified: false` while another name's entry for the same id says true is a
stale record, not a second product. Entries pointing at a product no verified
record vouches for are dropped, and their ingredient names become pending --
the resolver re-attempts them live, against verified candidates only.

**Query aliases come across in full, verified or not.** An alias records that
`self-rising flour` was found by searching for `mąka z proszkiem`. That is
translation knowledge, independent of whether the product it found was
verified, and discarding it would throw away the only thing standing between
the resolver and an English name it cannot look up. This is what gives the
dropped names a real chance of re-resolving.

**The single `status` field becomes two.** `state/processed.yaml` had one
`status` that fetch wrote ('complete', 'partial') and extraction overwrote
('ok', 'needs_review', 'no_recipe_found'), so the fetch verdict was unknowable
afterwards. Extraction verdicts land in `extract_status`; `fetch_status` is
recomputed from what is actually stored, which is the gate it was always
supposed to be.

**`.vtt` files are not migrated.** 456 KB of timing data against 56 KB of the
prose derived from it, and nothing reads the timings.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

from . import completeness
from .db import apply_schema, connect, transaction
from .names import normalize_name
from .paths import (
    LEGACY_LEDGER,
    LEGACY_PRODUCTS,
    LEGACY_RAW,
    LEGACY_RECIPES,
    load_config,
)
from .schema import validate_recipe
from .store import products as products_store
from .store import recipes as recipes_store
from .store import videos as videos_store

EXTRACT_STATUSES = ("ok", "no_recipe_found", "needs_review", "unavailable")


def _read_yaml(path: Path) -> Any:
    if not path.exists():
        return None
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- videos ----


def _subtitle_files(video_id: str) -> dict[str, Path]:
    """The plain-text subtitle files for a video, by bare language."""
    out: dict[str, Path] = {}
    directory = LEGACY_RAW / video_id
    if not directory.is_dir():
        return out
    for path in sorted(directory.glob("subs.*.txt")):
        parts = path.name.split(".")
        if len(parts) >= 3:
            lang = completeness.base_lang(parts[1])
            if lang:
                out[lang] = path
    return out


def migrate_videos(conn, *, allowed_langs: list[str] | None) -> dict[str, int]:
    """The ledger plus everything under `raw/`."""
    ledger = (_read_yaml(LEGACY_LEDGER) or {}).get("videos") or {}
    stats = {
        "videos": 0,
        "metadata": 0,
        "subtitle_tracks": 0,
        "no_metadata": 0,
        "fetch_complete": 0,
        "fetch_partial": 0,
        "fetch_pending": 0,
        "fetch_unavailable": 0,
    }

    # Directories under raw/ that the ledger never recorded still hold real
    # provenance, so they are migrated too rather than silently skipped.
    raw_ids = {p.name for p in LEGACY_RAW.iterdir() if p.is_dir()} if LEGACY_RAW.is_dir() else set()
    video_ids = sorted(set(ledger) | raw_ids)

    for video_id in video_ids:
        entry = ledger.get(video_id) or {}
        old_status = entry.get("status")

        meta_path = LEGACY_RAW / video_id / "meta.json"
        meta = None
        if meta_path.exists():
            try:
                loaded = json.loads(meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                loaded = None
            if isinstance(loaded, dict):
                meta = loaded

        with transaction(conn):
            if meta is not None:
                videos_store.save_meta(conn, video_id, meta)
                stats["metadata"] += 1
            else:
                stats["no_metadata"] += 1
                # A ledger stub with no metadata still needs a row, or its
                # recipes would have nothing to reference.
                conn.execute(
                    "INSERT INTO videos (video_id, title) VALUES (?, ?) "
                    "ON CONFLICT (video_id) DO NOTHING",
                    (video_id, entry.get("title") or ""),
                )

            subtitles = _subtitle_files(video_id)
            for lang, path in subtitles.items():
                content = path.read_text(encoding="utf-8").strip()
                if not content:
                    continue
                videos_store.save_subtitles(
                    conn,
                    video_id,
                    lang,
                    content,
                    track=(meta or {}).get("caption_track"),
                )
                stats["subtitle_tracks"] += 1

            if old_status == "unavailable":
                fetch_status = "unavailable"
            elif meta is None:
                fetch_status = "partial" if subtitles else "pending"
            elif completeness.is_complete(subtitles, meta, allowed_langs):
                fetch_status = "complete"
            else:
                fetch_status = "partial"
            stats[f"fetch_{fetch_status}"] = stats.get(f"fetch_{fetch_status}", 0) + 1

            extract_status = old_status if old_status in EXTRACT_STATUSES else None

            conn.execute(
                """
                UPDATE videos SET
                    fetch_status = ?,
                    extract_status = ?,
                    note = COALESCE(?, note),
                    fetched_at = COALESCE(fetched_at, ?),
                    extracted_at = COALESCE(?, extracted_at),
                    description_sha256 = COALESCE(description_sha256, ?)
                WHERE video_id = ?
                """,
                (
                    fetch_status,
                    extract_status,
                    entry.get("note"),
                    entry.get("fetched_at"),
                    entry.get("extracted_at"),
                    entry.get("description_sha256"),
                    video_id,
                ),
            )
        stats["videos"] += 1

    return stats


# -------------------------------------------------------------- products ----


def _verified_product_ids(cache_products: dict) -> tuple[set[str], set[str]]:
    """Split product ids into verified and not, judging per id.

    A product id vouched for by any cache entry is verified. The same product is
    recorded under several ingredient names, and an entry written before the
    flag was checked says "unknown", not "unverified product".
    """
    verified: set[str] = set()
    seen: set[str] = set()
    for entry in cache_products.values():
        product_id = str(entry.get("fitatu_product_id") or "")
        if not product_id:
            continue
        seen.add(product_id)
        if entry.get("verified") is True:
            verified.add(product_id)
    return verified, seen - verified


def migrate_products(conn) -> dict[str, Any]:
    """The product cache: verified products, every alias, recorded misses."""
    cache = _read_yaml(LEGACY_PRODUCTS) or {}
    cache_products = cache.get("products") or {}
    unresolved = cache.get("unresolved") or {}

    verified_ids, unverified_ids = _verified_product_ids(cache_products)

    stats: dict[str, Any] = {
        "cache_entries": len(cache_products),
        "products_stored": 0,
        "products_skipped_unverified": len(unverified_ids),
        "names_mapped": 0,
        "names_dropped_unverified": 0,
        "aliases": 0,
        "failures": len(unresolved),
        "dropped_names": [],
    }

    stored: set[str] = set()
    for name, entry in sorted(cache_products.items()):
        key = normalize_name(name)
        product_id = str(entry.get("fitatu_product_id") or "")

        # The alias goes in regardless of the product's verification: it records
        # which phrase finds this ingredient in a Polish catalogue, which is
        # exactly what a dropped name needs in order to resolve again.
        query = normalize_name(entry.get("query") or "")
        if query and query != key:
            products_store.add_alias(conn, key, query)
            stats["aliases"] += 1

        if not product_id or product_id not in verified_ids:
            stats["names_dropped_unverified"] += 1
            stats["dropped_names"].append(key)
            continue

        if product_id not in stored:
            measures = [
                {
                    "measure_id": str(m.get("measure_id") or ""),
                    "measure_name": m.get("measure_name"),
                    "weight_g": m.get("weight_g"),
                    "kcal": m.get("kcal"),
                    "per_item": bool(m.get("per_item")),
                }
                for m in (entry.get("measures") or [])
                if m.get("measure_id")
            ]
            default = entry.get("default_measure") or {}
            default_id = str(default.get("measure_id") or "") or None
            if default_id and default_id not in {m["measure_id"] for m in measures}:
                measures.insert(
                    0,
                    {
                        "measure_id": default_id,
                        "measure_name": default.get("measure_name"),
                        "weight_g": default.get("weight_g"),
                        "kcal": default.get("kcal"),
                        "per_item": bool(default.get("per_item")),
                    },
                )
            products_store.save(
                conn,
                product_id=product_id,
                display_name=entry.get("display_name"),
                brand=entry.get("brand"),
                source=entry.get("source"),
                verified=True,
                measures=measures,
                default_measure_id=default_id,
            )
            stored.add(product_id)
            stats["products_stored"] += 1

        products_store.map_name(
            conn,
            key,
            product_id,
            query=query or key,
            # The old cache kept no score: these were chosen by a human reading
            # candidates, which is a different kind of evidence from a number.
            match_score=None,
            method="seeded",
        )
        stats["names_mapped"] += 1

    for name, entry in sorted(unresolved.items()):
        products_store.record_failure(
            conn, name, entry.get("reason") or "no_match", candidates=[]
        )

    return stats


# --------------------------------------------------------------- recipes ----


def migrate_recipes(conn) -> dict[str, Any]:
    """Every `recipes/*.yaml`, validated on the way in."""
    stats: dict[str, Any] = {
        "recipes": 0,
        "ingredients": 0,
        "invalid": [],
        "failed": [],
    }
    if not LEGACY_RECIPES.is_dir():
        return stats

    for path in sorted(LEGACY_RECIPES.glob("*.yaml")):
        data = _read_yaml(path)
        if not isinstance(data, dict):
            stats["invalid"].append((path.name, ["unparseable or not a mapping"]))
            continue

        problems = validate_recipe(data, expected_id=path.stem)
        if problems:
            # Reported, not refused. These are the recipes as they exist, and a
            # migration that silently dropped them would lose hand-curated work;
            # the schema's CHECK constraints still refuse anything unstorable.
            stats["invalid"].append((path.name, problems))

        try:
            recipes_store.save(conn, data)
        except Exception as exc:
            stats["failed"].append((path.name, f"{type(exc).__name__}: {exc}"))
            continue

        stats["recipes"] += 1
        stats["ingredients"] += len(data.get("ingredients") or [])

    return stats


# ------------------------------------------------------------------- cli ----


def run(conn, *, allowed_langs: list[str] | None) -> dict[str, Any]:
    return {
        "videos": migrate_videos(conn, allowed_langs=allowed_langs),
        # Products before recipes so an ingredient's override, if any, has a
        # product row to reference.
        "products": migrate_products(conn),
        "recipes": migrate_recipes(conn),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Migrate the YAML tree and raw/ into SQLite."
    )
    parser.add_argument("--db", help="write to this database instead of the default")
    parser.add_argument(
        "--list-dropped",
        action="store_true",
        help="print every ingredient name whose product was not verified",
    )
    args = parser.parse_args(argv)

    try:
        allowed_langs = load_config()["original_langs"]
    except SystemExit:
        # A missing playlist url must not stop a migration: it is only needed to
        # decide which original languages count as complete.
        allowed_langs = ["pl", "en"]

    conn = connect(args.db)
    apply_schema(conn)
    try:
        result = run(conn, allowed_langs=allowed_langs)
    finally:
        conn.close()

    videos, products, recipes = result["videos"], result["products"], result["recipes"]

    print("videos")
    print(f"  rows                  {videos['videos']}")
    print(f"  with metadata         {videos['metadata']}")
    print(f"  without metadata      {videos['no_metadata']}")
    print(f"  subtitle tracks       {videos['subtitle_tracks']}")
    print(f"  fetch complete        {videos['fetch_complete']}")
    print(f"  fetch partial         {videos['fetch_partial']}")
    print(f"  fetch pending         {videos['fetch_pending']}")
    print(f"  fetch unavailable     {videos['fetch_unavailable']}")

    print("\nproducts")
    print(f"  cache entries read    {products['cache_entries']}")
    print(f"  verified products     {products['products_stored']}")
    print(f"  unverified skipped    {products['products_skipped_unverified']}")
    print(f"  names mapped          {products['names_mapped']}")
    print(f"  names dropped         {products['names_dropped_unverified']}")
    print(f"  query aliases seeded  {products['aliases']}")
    print(f"  recorded misses       {products['failures']}")

    print("\nrecipes")
    print(f"  rows                  {recipes['recipes']}")
    print(f"  ingredients           {recipes['ingredients']}")

    if args.list_dropped and products["dropped_names"]:
        print(
            f"\n{len(products['dropped_names'])} ingredient name(s) lost their "
            "product because it is not verified:"
        )
        for name in products["dropped_names"]:
            print(f"  {name}")

    if recipes["invalid"]:
        print(f"\n{len(recipes['invalid'])} recipe(s) migrated with schema problems:")
        for name, problems in recipes["invalid"]:
            print(f"  {name}: {'; '.join(problems)}")

    if recipes["failed"]:
        print(f"\n{len(recipes['failed'])} recipe(s) could NOT be migrated:")
        for name, error in recipes["failed"]:
            print(f"  {name}: {error}")
        return 1

    print(
        "\nDone. The dropped names are now pending: they will be re-resolved "
        "against verified products on the next import."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
