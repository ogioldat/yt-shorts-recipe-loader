"""Importing recipes into Fitatu and recording the outcome on each recipe.

Every recipe carries an `import_status`:

* `not_imported` — the default, including when it has never been attempted
* `ok` — created in Fitatu, with `fitatu_recipe_id` recorded
* `error` — attempted and failed, with `import_error` explaining why

`POST /recipes` is **not idempotent**: sending the same recipe twice creates two
Fitatu recipes. So `ok` is a hard gate — a recipe already imported is never sent
again unless `--force` is passed. That is the whole reason this status exists.
"""

from datetime import date
from pathlib import Path

import yaml

from ..paths import RECIPES
from ..products import load_cache
from .payload import build_recipe_kwargs

STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_NOT_IMPORTED = "not_imported"
IMPORT_STATUSES = (STATUS_OK, STATUS_ERROR, STATUS_NOT_IMPORTED)

ID_FIELDS = ("id", "recipeId", "recipe_id")


def read_recipe(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def write_recipe(path: Path, data: dict) -> None:
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100),
        encoding="utf-8",
    )


def recipe_status(data: dict) -> str:
    """The recipe's import status, defaulting to not_imported when absent."""
    status = data.get("import_status")
    return status if status in IMPORT_STATUSES else STATUS_NOT_IMPORTED


def recipe_files(recipes_dir: Path | None = None) -> list[Path]:
    return sorted((recipes_dir or RECIPES).glob("*.yaml"))


def extract_recipe_id(response: dict) -> str | None:
    """Pull Fitatu's new recipe id out of the create response."""
    if not isinstance(response, dict):
        return None
    for field in ID_FIELDS:
        value = response.get(field)
        if value not in (None, ""):
            return str(value)
    return None


def _set_status(data: dict, status: str, **extra) -> None:
    data["import_status"] = status
    for key in ("fitatu_recipe_id", "import_error", "imported_at"):
        data.pop(key, None)
    data.update({k: v for k, v in extra.items() if v is not None})


def resolve_names(
    names: list[str], recipes_dir: Path | None = None
) -> tuple[list[Path], list[str]]:
    """Map recipe ids to their files, keeping the order the caller asked for.

    Returns `(paths, unknown)`. A name may be given with or without the `.yaml`
    suffix, since that is how it appears in the import log.
    """
    by_stem = {path.stem: path for path in recipe_files(recipes_dir)}
    paths, unknown = [], []
    for name in names:
        stem = name[:-5] if name.endswith(".yaml") else name
        path = by_stem.get(stem)
        if path is None:
            unknown.append(name)
        elif path not in paths:
            paths.append(path)
    return paths, unknown


def select_pending(
    recipes_dir: Path | None = None,
    *,
    include_errors: bool = True,
    force: bool = False,
    names: list[str] | None = None,
) -> list[Path]:
    """Recipes worth attempting.

    Already-imported recipes are excluded unless `force`, because creating them
    again would duplicate them in Fitatu. Naming recipes narrows the list to
    those, but does not lift that gate: a named recipe already imported is still
    skipped, because the duplicate it would create is the same duplicate.
    """
    candidates = recipe_files(recipes_dir)
    if names is not None:
        candidates, _ = resolve_names(names, recipes_dir)

    pending = []
    for path in candidates:
        status = recipe_status(read_recipe(path))
        if force:
            pending.append(path)
        elif status == STATUS_NOT_IMPORTED:
            pending.append(path)
        elif status == STATUS_ERROR and include_errors:
            pending.append(path)
    return pending


def import_one(
    path: Path,
    client,
    products: dict,
    *,
    shared: bool = False,
    allow_partial: bool = False,
    dry_run: bool = False,
    tags: list[str] | None = None,
) -> dict:
    """Import a single recipe and record the outcome on it.

    Returns `{"status", "detail"}`. `client` may be None when `dry_run`.
    """
    data = read_recipe(path)
    built = build_recipe_kwargs(
        data, products, shared=shared, allow_partial=allow_partial, tags=tags
    )

    if not built.ok:
        reason = "; ".join(str(p) for p in built.problems)
        if not dry_run:
            _set_status(
                data, STATUS_ERROR, import_error=reason, imported_at=date.today().isoformat()
            )
            write_recipe(path, data)
        return {"status": STATUS_ERROR, "detail": reason, "warnings": built.warnings}

    if dry_run:
        return {
            "status": "would_import",
            "detail": f"{len(built.kwargs['items'])} ingredient(s)",
            "warnings": built.warnings,
        }

    try:
        response = client.create_recipe(**built.kwargs)
    except Exception as exc:  # the client raises its own error types
        reason = f"{type(exc).__name__}: {exc}"
        _set_status(
            data, STATUS_ERROR, import_error=reason, imported_at=date.today().isoformat()
        )
        write_recipe(path, data)
        return {"status": STATUS_ERROR, "detail": reason, "warnings": built.warnings}

    recipe_id = extract_recipe_id(response)
    if recipe_id is None:
        # The recipe may well have been created, so refuse to retry it blindly:
        # a retry would duplicate it. Flag it for a human instead.
        reason = f"created but Fitatu returned no recipe id: {response!r}"
        _set_status(
            data, STATUS_ERROR, import_error=reason, imported_at=date.today().isoformat()
        )
        write_recipe(path, data)
        return {"status": STATUS_ERROR, "detail": reason, "warnings": built.warnings}

    _set_status(
        data,
        STATUS_OK,
        fitatu_recipe_id=recipe_id,
        imported_at=date.today().isoformat(),
    )
    write_recipe(path, data)
    return {"status": STATUS_OK, "detail": recipe_id, "warnings": built.warnings}


def summarise(recipes_dir: Path | None = None) -> dict[str, int]:
    counts = {status: 0 for status in IMPORT_STATUSES}
    for path in recipe_files(recipes_dir):
        counts[recipe_status(read_recipe(path))] += 1
    return counts


def run(
    *,
    recipes_dir: Path | None = None,
    offset: int | None = None,
    limit: int | None = None,
    shared: bool = False,
    allow_partial: bool = False,
    dry_run: bool = False,
    force: bool = False,
    include_errors: bool = True,
    tags: list[str] | None = None,
    names: list[str] | None = None,
) -> dict[str, int]:
    """Import a window of the pending recipes. Returns a tally by outcome.

    `offset` and `limit` slice the **pending** list, not the whole collection.
    That list shrinks as recipes succeed, so an offset is only stable within one
    run, or across repeated `--dry-run` calls which change nothing. Walking a real
    import in batches therefore means keeping `--offset 0` and raising `--limit`,
    not advancing the offset -- otherwise the second batch skips recipes the first
    one already removed from the list.

    `names` imports just those recipes, by id. An unknown name is refused before
    anything is sent, because a typo that silently imported nothing would look
    exactly like a recipe that had already been imported.
    """
    if offset is not None and offset < 0:
        raise SystemExit(f"--offset must be zero or greater, got {offset}")
    if limit is not None and limit < 1:
        raise SystemExit(f"--limit must be at least 1, got {limit}")

    if names:
        _, unknown = resolve_names(names, recipes_dir)
        if unknown:
            raise SystemExit(
                "No recipe file for: " + ", ".join(unknown) + "\n"
                "Pass the recipe id, which is the file name without .yaml."
            )

    products = (load_cache().get("products") or {})
    pending = select_pending(
        recipes_dir, include_errors=include_errors, force=force, names=names
    )
    total = len(pending)

    if names and not pending:
        # Named but not pending means already imported: say so, rather than
        # letting it read as "nothing to do".
        print(
            f"Nothing to import: {', '.join(names)} "
            f"already has import_status 'ok'. Re-sending would duplicate it in "
            f"Fitatu; --force does that deliberately."
        )
        return {STATUS_OK: 0, STATUS_ERROR: 0, "would_import": 0}

    start = offset or 0
    pending = pending[start:]
    if limit:
        pending = pending[:limit]

    tally = {STATUS_OK: 0, STATUS_ERROR: 0, "would_import": 0}
    if not pending:
        if total and start >= total:
            print(f"Offset {start} is past the end of {total} pending recipe(s).")
        else:
            print("Nothing to import.")
        return tally

    if names:
        print(f"Importing by name: {', '.join(p.stem for p in pending)}")
    elif start or limit:
        end = start + len(pending)
        print(f"Pending: {total} recipe(s); taking {start}-{end - 1}")

    client = None
    if not dry_run:
        from .client import connect

        client = connect()

    print(f"Importing {len(pending)} recipe(s)")
    for i, path in enumerate(pending, 1):
        print(f"[{i}/{len(pending)}] {path.stem}")
        result = import_one(
            path,
            client,
            products,
            shared=shared,
            allow_partial=allow_partial,
            dry_run=dry_run,
            tags=tags,
        )
        tally[result["status"]] = tally.get(result["status"], 0) + 1
        for warning in result["warnings"]:
            print(f"  warning: {warning}")
        marker = "ok" if result["status"] == STATUS_OK else result["status"]
        print(f"  {marker}: {result['detail']}")

    return tally
