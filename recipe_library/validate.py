"""Validate every recipe in the database. Run after extraction.

The schema's CHECK constraints already refuse the structural errors -- a
quantity without a unit, an `ok` import status with no Fitatu recipe id, a unit
outside the vocabulary. This catches what a constraint cannot express: missing
required fields, an id that does not match, an `extracted_from` naming a source
that does not exist.

    uv run python -m recipe_library.validate
"""

import argparse
import sys

from .db import open_db
from .schema import validate_recipe
from .store import recipes as store


def validate_all(db: str | None = None) -> dict[str, list[str]]:
    """Map recipe id -> list of problems, for recipes that have problems."""
    problems: dict[str, list[str]] = {}
    with open_db(db) as conn:
        for recipe_id in store.ids(conn):
            data = store.get(conn, recipe_id)
            if data is None:
                problems[recipe_id] = ["recipe disappeared while reading"]
                continue
            errors = validate_recipe(data, expected_id=recipe_id)
            if errors:
                problems[recipe_id] = errors
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate every recipe.")
    parser.add_argument("--db", help="use this database instead of the default")
    args = parser.parse_args(argv)

    with open_db(args.db) as conn:
        total = len(store.ids(conn))
    problems = validate_all(args.db)

    if not problems:
        print(f"All {total} recipe(s) valid.")
        return 0
    for recipe_id, errors in problems.items():
        print(f"\n{recipe_id}:")
        for err in errors:
            print(f"  - {err}")
    print(f"\n{len(problems)} of {total} recipe(s) invalid.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
