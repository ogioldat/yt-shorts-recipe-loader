"""Reading the recipe collection. Pure, no I/O beyond the files themselves.

This is the read side of `recipes/`, kept separate from the MCP server that
exposes it so the listing can be tested without a protocol in the way.
"""

from pathlib import Path
from typing import Any

import yaml

from .paths import RECIPES

SUMMARY_FIELDS = ("id", "title", "title_en", "language", "servings")


def recipe_files(recipes_dir: Path | None = None) -> list[Path]:
    return sorted((recipes_dir or RECIPES).glob("*.yaml"))


def read_recipe(path: Path) -> dict | None:
    """Parse one recipe file, or None when it is unreadable."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError):
        return None
    return data if isinstance(data, dict) else None


def summarise(data: dict, *, include_ingredients: bool = False) -> dict[str, Any]:
    """Reduce a recipe to the fields a caller browsing the collection wants.

    Ingredients are omitted by default: 70 recipes hold 847 of them, which would
    bury the overview most callers are after.
    """
    summary: dict[str, Any] = {
        field: data.get(field) for field in SUMMARY_FIELDS if data.get(field) is not None
    }

    ingredients = data.get("ingredients") or []
    summary["ingredient_count"] = len(ingredients)
    summary["unquantified_count"] = sum(
        1 for i in ingredients if isinstance(i, dict) and i.get("quantity") is None
    )
    summary["mapped_count"] = sum(
        1 for i in ingredients if isinstance(i, dict) and i.get("fitatu_product_id")
    )

    # Absent means never attempted; see fitatu_import.runner.
    summary["import_status"] = data.get("import_status") or "not_imported"
    if data.get("fitatu_recipe_id"):
        summary["fitatu_recipe_id"] = data["fitatu_recipe_id"]

    source = data.get("source") or {}
    summary["source"] = {
        key: source.get(key)
        for key in ("video_id", "url", "channel", "recipe_index")
        if source.get(key) is not None
    }

    if include_ingredients:
        summary["ingredients"] = ingredients

    return summary


def list_recipes(
    recipes_dir: Path | None = None,
    *,
    include_ingredients: bool = False,
    language: str | None = None,
    import_status: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Every recipe in the collection, ordered by id.

    Unreadable files are skipped rather than raising: one corrupt file should not
    hide the other sixty-nine.
    """
    out = []
    for path in recipe_files(recipes_dir):
        data = read_recipe(path)
        if data is None:
            continue
        if language and data.get("language") != language:
            continue
        if import_status and (data.get("import_status") or "not_imported") != import_status:
            continue
        out.append(summarise(data, include_ingredients=include_ingredients))
        if limit and len(out) >= limit:
            break
    return out


def collection_stats(recipes_dir: Path | None = None) -> dict[str, Any]:
    """Counts worth knowing before reading 70 recipes one by one."""
    recipes = list_recipes(recipes_dir)
    languages: dict[str, int] = {}
    statuses: dict[str, int] = {}
    ingredients = 0
    for recipe in recipes:
        languages[recipe.get("language") or "?"] = (
            languages.get(recipe.get("language") or "?", 0) + 1
        )
        statuses[recipe["import_status"]] = statuses.get(recipe["import_status"], 0) + 1
        ingredients += recipe["ingredient_count"]
    return {
        "recipes": len(recipes),
        "ingredients": ingredients,
        "by_language": dict(sorted(languages.items())),
        "by_import_status": dict(sorted(statuses.items())),
    }
