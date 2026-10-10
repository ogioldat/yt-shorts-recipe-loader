"""CLI for the ingredient-name to Fitatu-product mapping.

What changed when this moved into the database: **there is no `apply` command
any more.** It used to be a separate deterministic pass that copied resolved ids
into 72 YAML files, and it had to be re-run whenever the mapping grew. An
ingredient's product is now a join -- `ingredients_resolved` in schema.sql -- so
there is nothing to apply and nothing to forget to apply.

Resolution itself is no longer a judgement call made in a model session either.
`resolve` runs the scored matcher in `resolve/` against `search_food`, keeping
only products Fitatu has verified. `map` remains for the case the matcher
cannot handle: a name you know the answer to, recorded by hand.

    uv run python -m recipe_library.products status
    uv run python -m recipe_library.products pending
    uv run python -m recipe_library.products resolve --limit 20
    uv run python -m recipe_library.products map --name "salt" --product-id 5270094
    uv run python -m recipe_library.products alias --name "self-rising flour" \
        --query "mąka z proszkiem do pieczenia"
"""

import argparse
import sys

from .db import open_db
from .names import normalize_name
from .paths import load_config
from .store import products as store

__all__ = ["normalize_name", "main"]


def _resolution_options() -> dict:
    from .resolve.resolver import options_from_config

    try:
        config = load_config()
    except SystemExit:
        config = {}
    return options_from_config(config)


def _cmd_status(args) -> int:
    with open_db(args.db) as conn:
        stats = store.stats(conn)
    width = max(len(k) for k in stats if k != "by_method")
    for key, value in stats.items():
        if key == "by_method":
            continue
        print(f"{key.replace('_', ' '):{width}}  {value}")
    if stats["by_method"]:
        print("\nby how the mapping was made:")
        for method, count in stats["by_method"].items():
            print(f"  {method:8} {count}")
    return 0


def _cmd_pending(args) -> int:
    with open_db(args.db) as conn:
        pending = store.pending_names(conn, retry_failed=args.retry_failed)
    if not pending:
        print("Nothing pending.")
        return 0
    print(f"{len(pending)} ingredient name(s) need a Fitatu product id:\n")
    for row in pending:
        print(f"  {row['name']}  ({row['recipe_count']} recipe(s))")
    return 0


def _cmd_failures(args) -> int:
    with open_db(args.db) as conn:
        failures = store.failures(conn)
    if not failures:
        print("No recorded resolution failures.")
        return 0
    print(f"{len(failures)} name(s) with no acceptable verified match:\n")
    for row in failures:
        print(f"  {row['name']}")
        print(f"    {row['reason']}  (attempts: {row['attempts']})")
        for cand in (row["candidates"] or [])[:3]:
            flag = "verified" if cand.get("verified") else "UNVERIFIED"
            print(
                f"      rejected {cand.get('name')!r} [{flag}] "
                f"score {cand.get('score')}"
            )
    return 0


def _cmd_resolve(args) -> int:
    from .fitatu_import.client import connect as fitatu_connect
    from .resolve.resolver import resolve_name

    options = _resolution_options()
    with open_db(args.db) as conn:
        names = (
            [normalize_name(n) for n in args.names]
            if args.names
            else [r["name"] for r in store.pending_names(conn, retry_failed=args.retry_failed)]
        )
        if args.limit:
            names = names[: args.limit]
        if not names:
            print("Nothing to resolve.")
            return 0

        client = None if args.dry_run else fitatu_connect()
        print(f"Resolving {len(names)} name(s) against verified Fitatu products")
        resolved = unresolved = 0
        for i, name in enumerate(names, 1):
            outcome = resolve_name(
                conn, client, name, refresh=args.refresh, **options
            )
            marker = "ok " if outcome.ok else "-- "
            print(f"[{i}/{len(names)}] {marker}{name}")
            print(f"           {outcome.reason}")
            if outcome.ok:
                resolved += 1
            else:
                unresolved += 1

    print(f"\nDone. {resolved} resolved, {unresolved} unresolved.")
    return 0


def _cmd_map(args) -> int:
    """Record a mapping by hand.

    The product still has to be stored first, which means it still has to be
    verified: `store.map_name` references `products`, and that table will not
    hold an unverified row. So this is an escape hatch for the matcher, not for
    the verification rule.
    """
    with open_db(args.db) as conn:
        if store.get(conn, args.product_id) is None:
            print(
                f"Product {args.product_id} is not in the database. Resolve it "
                f"first, or add it with `resolve --name`; only verified Fitatu "
                f"products can be stored.",
                file=sys.stderr,
            )
            return 1
        key = store.map_name(
            conn, args.name, args.product_id, query=args.query, method="manual"
        )
    print(f"Mapped {key!r} -> product {args.product_id}")
    return 0


def _cmd_unmap(args) -> int:
    with open_db(args.db) as conn:
        removed = store.unmap_name(conn, args.name)
    print(
        f"Removed the mapping for {normalize_name(args.name)!r}"
        if removed
        else f"No mapping for {normalize_name(args.name)!r}"
    )
    return 0


def _cmd_alias(args) -> int:
    with open_db(args.db) as conn:
        store.add_alias(conn, args.name, args.query, locale=args.locale)
        store.clear_failure(conn, args.name)
    print(
        f"Alias added: {normalize_name(args.name)!r} will also be searched as "
        f"{normalize_name(args.query)!r}"
    )
    return 0


def _cmd_aliases(args) -> int:
    with open_db(args.db) as conn:
        rows = conn.execute(
            "SELECT name, query, locale FROM query_aliases ORDER BY name, query"
        ).fetchall()
    if not rows:
        print("No query aliases.")
        return 0
    print(f"{len(rows)} alias(es):\n")
    for row in rows:
        locale = f"  [{row['locale']}]" if row["locale"] else ""
        print(f"  {row['name']}  ->  {row['query']}{locale}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fitatu product id mapping.")
    parser.add_argument("--db", help="use this database instead of the default")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="summarise coverage").set_defaults(func=_cmd_status)

    p = sub.add_parser("pending", help="ingredient names still needing a product id")
    p.add_argument("--retry-failed", action="store_true")
    p.set_defaults(func=_cmd_pending)

    sub.add_parser("failures", help="names with no acceptable verified match").set_defaults(
        func=_cmd_failures
    )

    p = sub.add_parser("resolve", help="match pending names against verified products")
    p.add_argument("--name", action="append", dest="names", help="resolve just this name")
    p.add_argument("--limit", type=int, help="resolve at most this many names")
    p.add_argument("--retry-failed", action="store_true", help="include recorded misses")
    p.add_argument("--refresh", action="store_true", help="re-resolve names already mapped")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="report what is still unmapped without contacting Fitatu",
    )
    p.set_defaults(func=_cmd_resolve)

    p = sub.add_parser("map", help="record one name -> product mapping by hand")
    p.add_argument("--name", required=True)
    p.add_argument("--product-id", required=True)
    p.add_argument("--query", help="the phrase that found it, when different")
    p.set_defaults(func=_cmd_map)

    p = sub.add_parser("unmap", help="forget one name -> product mapping")
    p.add_argument("--name", required=True)
    p.set_defaults(func=_cmd_unmap)

    p = sub.add_parser("alias", help="add a search phrase for an ingredient name")
    p.add_argument("--name", required=True)
    p.add_argument("--query", required=True)
    p.add_argument("--locale", help="only search this phrase at this locale")
    p.set_defaults(func=_cmd_alias)

    sub.add_parser("aliases", help="list every search phrase").set_defaults(
        func=_cmd_aliases
    )

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
