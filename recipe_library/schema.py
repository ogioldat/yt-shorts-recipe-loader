"""Validation for recipe YAML files.

Extraction is done by a language model reading messy video descriptions, so
these checks are the main guard against silently malformed output. The rule
that matters most: a quantity is a number or it is absent. Never a guess.
"""

from typing import Any

VALID_UNITS = {"g", "ml", "piece", "tbsp", "tsp", "cup", "pinch"}
VALID_STATUSES = {"ok", "no_recipe_found", "needs_review", "unavailable"}
VALID_SOURCES = {"title", "description", "subtitles"}

REQUIRED_TOP = ("id", "title", "language", "ingredients", "source", "extracted_at")
REQUIRED_SOURCE = ("video_id", "url", "recipe_index")


def validate_recipe(data: Any, *, expected_id: str | None = None) -> list[str]:
    """Return a list of human-readable problems. Empty list means valid."""
    errors: list[str] = []

    if not isinstance(data, dict):
        return ["top level is not a mapping"]

    for field in REQUIRED_TOP:
        if data.get(field) in (None, "", []):
            errors.append(f"missing required field: {field}")

    if expected_id is not None and data.get("id") != expected_id:
        errors.append(f"id {data.get('id')!r} does not match filename {expected_id!r}")

    if (lang := data.get("language")) is not None and lang not in {"pl", "en"}:
        errors.append(f"language must be 'pl' or 'en', got {lang!r}")

    servings = data.get("servings")
    if servings is not None and (not isinstance(servings, int) or servings <= 0):
        errors.append(f"servings must be a positive integer or null, got {servings!r}")

    errors.extend(_validate_ingredients(data.get("ingredients")))
    errors.extend(_validate_source(data.get("source")))
    return errors


def _validate_ingredients(ingredients: Any) -> list[str]:
    errors: list[str] = []
    if ingredients is None:
        return errors
    if not isinstance(ingredients, list) or not ingredients:
        return ["ingredients must be a non-empty list"]

    for i, ing in enumerate(ingredients):
        where = f"ingredients[{i}]"
        if not isinstance(ing, dict):
            errors.append(f"{where} is not a mapping")
            continue
        if not ing.get("name"):
            errors.append(f"{where} missing name")

        qty = ing.get("quantity")
        if qty is not None and (isinstance(qty, bool) or not isinstance(qty, (int, float))):
            errors.append(f"{where} quantity must be a number or null, got {qty!r}")
        if isinstance(qty, (int, float)) and not isinstance(qty, bool) and qty <= 0:
            errors.append(f"{where} quantity must be positive, got {qty!r}")

        unit = ing.get("unit")
        if unit is not None and unit not in VALID_UNITS:
            errors.append(f"{where} unit {unit!r} not in {sorted(VALID_UNITS)}")
        if qty is not None and unit is None:
            errors.append(f"{where} has a quantity but no unit")

    return errors


def _validate_source(source: Any) -> list[str]:
    errors: list[str] = []
    if source is None:
        return errors
    if not isinstance(source, dict):
        return ["source must be a mapping"]

    for field in REQUIRED_SOURCE:
        if source.get(field) in (None, ""):
            errors.append(f"source missing required field: {field}")

    idx = source.get("recipe_index")
    if idx is not None and (not isinstance(idx, int) or isinstance(idx, bool) or idx < 1):
        errors.append(f"source.recipe_index must be an integer >= 1, got {idx!r}")

    url = source.get("url")
    if url is not None and not str(url).startswith("https://"):
        errors.append(f"source.url must be an https URL, got {url!r}")

    extracted_from = source.get("extracted_from")
    if extracted_from is not None:
        if not isinstance(extracted_from, list) or not extracted_from:
            errors.append("source.extracted_from must be a non-empty list")
        else:
            bad = set(extracted_from) - VALID_SOURCES
            if bad:
                errors.append(f"source.extracted_from has unknown values: {sorted(bad)}")

    return errors
