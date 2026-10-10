"""The HTTP server over the recipe library.

`app.py` builds the FastAPI application; the route modules are grouped by
subject and hold no logic of their own -- they validate input, call `store/` or
`resolve/`, and shape the response. Anything worth testing lives below them and
is tested without a request in the way.

    uv run uvicorn recipe_library.api:app --reload
"""

from .app import app, create_app

__all__ = ["app", "create_app"]
