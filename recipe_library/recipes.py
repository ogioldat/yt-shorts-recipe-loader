"""Reading the recipe collection.

The read side of the library, kept separate from both the MCP server and the
HTTP API that expose it, so the listing can be tested without a protocol in the
way. That was true when this read 72 YAML files and it is still true now that
it reads SQLite; `store/recipes.py` holds the SQL and this holds the
connection handling, so callers that just want the collection get one call.
"""

from typing import Any

from .db import open_db
from .store import recipes as store


def list_recipes(
    *,
    db: str | None = None,
    include_ingredients: bool = False,
    language: str | None = None,
    import_status: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Every recipe in the collection, ordered by id."""
    with open_db(db) as conn:
        return store.listing(
            conn,
            include_ingredients=include_ingredients,
            language=language,
            import_status=import_status,
            limit=limit,
        )


def get_recipe(recipe_id: str, *, db: str | None = None) -> dict[str, Any] | None:
    with open_db(db) as conn:
        return store.get(conn, recipe_id)


def collection_stats(*, db: str | None = None) -> dict[str, Any]:
    """Counts worth knowing before reading the collection one recipe at a time."""
    with open_db(db) as conn:
        return store.stats(conn)
