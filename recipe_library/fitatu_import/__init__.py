"""Importing recipes from the library into Fitatu.

Uses the `fitatu-api` community HTTP client directly; no MCP server is involved,
for product resolution either. The whole pipeline -- search, verify, score,
convert, create -- runs through that one client.
"""

from .payload import BuildResult, Problem, build_recipe_kwargs, measure_quantity
from .runner import (
    IMPORT_STATUSES,
    STATUS_ERROR,
    STATUS_NOT_IMPORTED,
    STATUS_OK,
    extract_recipe_id,
    import_one,
    recipe_status,
    resolve_names,
    run,
    select_pending,
    summarise,
)

__all__ = [
    "BuildResult",
    "IMPORT_STATUSES",
    "Problem",
    "STATUS_ERROR",
    "STATUS_NOT_IMPORTED",
    "STATUS_OK",
    "build_recipe_kwargs",
    "extract_recipe_id",
    "import_one",
    "measure_quantity",
    "recipe_status",
    "resolve_names",
    "run",
    "select_pending",
    "summarise",
]
