"""The `recipes-library` MCP server.

Exposes this repository's recipe collection over MCP so a client can browse it
without querying the database itself. Read-only: every statement it issues is a
SELECT.

This is the narrow, read-only view. The FastAPI server in `api/` is the one
that also writes -- extraction, product resolution and Fitatu import all go
through it.

Run it with:

    uv run python -m recipe_library.mcp_server

The listing logic lives in `recipes.py` and `store/recipes.py`, so it is tested
directly without a protocol in the way.
"""

import sys
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from .recipes import collection_stats, list_recipes

server = MCPServer(
    name="recipes-library",
    version="0.1.0",
    instructions=(
        "A personal recipe collection extracted from YouTube Shorts. Each recipe "
        "holds a title and ingredients only, never preparation steps. A null "
        "ingredient quantity means the source video did not state one and is "
        "never a placeholder for a guess. An ingredient with no "
        "fitatu_product_id has not been matched to a verified Fitatu product; "
        "only verified products are ever stored."
    ),
)


@server.tool(
    name="list",
    title="List recipes",
    description=(
        "Lists every recipe in the collection, ordered by id. Returns a summary per "
        "recipe: id, title, language, servings, how many ingredients it has, how many "
        "of those lack a quantity, how many are mapped to a Fitatu product, its import "
        "status, and the source video. Ingredients are omitted unless "
        "include_ingredients is true, because the collection holds hundreds of them. "
        "Optionally filter by language or import_status."
    ),
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    ),
)
def list_tool(
    include_ingredients: bool = False,
    language: str | None = None,
    import_status: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """List all recipes in the collection.

    Args:
        include_ingredients: include each recipe's full ingredient list.
        language: keep only this source language, `pl` or `en`.
        import_status: keep only this Fitatu import status -- `ok`, `error` or
            `not_imported`.
        limit: return at most this many recipes.
    """
    recipes = list_recipes(
        include_ingredients=include_ingredients,
        language=language,
        import_status=import_status,
        limit=limit,
    )
    return {
        "count": len(recipes),
        "recipes": recipes,
        "collection": collection_stats(),
    }


def main() -> int:
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
