"""The FastAPI application.

Handlers are sync `def`, not `async def`. `sqlite3` is a blocking driver, so an
async handler would block the event loop for the duration of every query;
Starlette runs sync handlers in a threadpool instead, which is both correct here
and less machinery than an async SQLite layer would be for a one-file database.

    uv run uvicorn recipe_library.api:app --reload
    uv run python -m recipe_library.api.app --port 8000
"""

import sys
from typing import Any

from fastapi import FastAPI

from ..db import SCHEMA_VERSION
from ..paths import DB
from . import routes_products, routes_recipes, routes_videos
from .deps import Conn, set_database

DESCRIPTION = """
The recipe library: YouTube Shorts turned into structured recipes, and pushed
into Fitatu for nutrition tracking.

Two rules shape the whole API and are worth knowing before using it:

* **A null ingredient quantity means the source video stated no amount.** It is
  never a placeholder for a guess, and nothing here will fill one in.
* **Only Fitatu-verified products are stored.** Product resolution discards
  unverified candidates before scoring, and the `products` table constrains the
  flag, so an unverified product cannot be stored by any code path.

Fetching from YouTube is not exposed here. It is a long, throttled, resumable
batch job: `uv run python -m recipe_library.fetch`.
"""


def create_app(db: str | None = None) -> FastAPI:
    if db is not None:
        set_database(db)

    app = FastAPI(
        title="recipe-library",
        version="0.2.0",
        description=DESCRIPTION,
    )
    app.include_router(routes_recipes.router)
    app.include_router(routes_recipes.imports_router)
    app.include_router(routes_products.router)
    app.include_router(routes_videos.router)

    @app.get("/health", tags=["meta"])
    def health(conn: Conn) -> dict[str, Any]:
        """Confirms the database is readable and says which schema it holds."""
        row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        return {
            "status": "ok",
            "database": str(db or DB),
            "schema_version": row["v"] if row else None,
            "expected_schema_version": SCHEMA_VERSION,
        }

    @app.get("/stats", tags=["meta"])
    def stats(conn: Conn) -> dict[str, Any]:
        """Every count worth knowing, in one request."""
        from ..store import products as products_store
        from ..store import recipes as recipes_store
        from ..store import videos as videos_store

        return {
            "recipes": recipes_store.stats(conn),
            "products": products_store.stats(conn),
            "videos": videos_store.stats(conn),
        }

    return app


app = create_app()


def main(argv: list[str] | None = None) -> int:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="Serve the recipe library.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", help="serve this database instead of the default")
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args(argv)

    if args.db:
        set_database(args.db)

    uvicorn.run(
        "recipe_library.api.app:app" if args.reload else app,
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
