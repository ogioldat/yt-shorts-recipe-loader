"""CLI: `uv run python -m recipe_library.fitatu_import`."""

import argparse
import sys

from .runner import run, summarise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import recipes into Fitatu.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="build payloads and report problems without contacting Fitatu",
    )
    parser.add_argument(
        "--offset",
        type=int,
        help="skip this many pending recipes first. Note the pending list shrinks "
        "as recipes succeed, so an offset is only stable within one run or across "
        "dry runs",
    )
    parser.add_argument("--limit", type=int, help="import at most this many recipes")
    parser.add_argument(
        "--shared", action="store_true", help="make recipes public (default private)"
    )
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="import a recipe even when some ingredients cannot be converted; "
        "Fitatu will then under-report it",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-send recipes already imported. Fitatu's create is not idempotent, "
        "so this DUPLICATES them",
    )
    parser.add_argument(
        "--skip-errors",
        action="store_true",
        help="do not retry recipes whose last attempt failed",
    )
    parser.add_argument(
        "--tag",
        action="append",
        dest="tags",
        help="tag to set on each imported recipe; repeatable. Fitatu's tag list is "
        "a fixed vocabulary, so a custom tag is also appended to the description",
    )
    parser.add_argument("--status", action="store_true", help="show the tally and exit")
    args = parser.parse_args(argv)

    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    if args.status:
        for status, count in summarise().items():
            print(f"{status:14} {count}")
        return 0

    tally = run(
        offset=args.offset,
        limit=args.limit,
        shared=args.shared,
        allow_partial=args.allow_partial,
        dry_run=args.dry_run,
        force=args.force,
        include_errors=not args.skip_errors,
        tags=args.tags,
    )
    print(
        "\nDone. "
        + ", ".join(f"{count} {status}" for status, count in tally.items() if count)
        or "\nDone."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
