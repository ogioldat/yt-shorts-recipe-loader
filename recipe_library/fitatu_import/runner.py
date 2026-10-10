"""Importing recipes into Fitatu, and recording the outcome on each recipe.

An import is now two phases, and the first one is new. It used to happen in a
model session, days earlier, through the Fitatu MCP server:

1. **Resolve.** Every ingredient name in the recipe is matched to a Fitatu
   product -- from the database if it is already mapped, otherwise by searching
   Fitatu live, discarding everything unverified, and scoring what remains
   (`resolve/`). The answer is stored, so the next recipe using that name costs
   nothing.
2. **Import.** The resolved products' measures convert each quantity
   (`payload.py`), and `create_recipe` posts the result.

Every recipe carries an `import_status`:

* `not_imported` -- the default, including when it has never been attempted
* `ok` -- created in Fitatu, with `fitatu_recipe_id` recorded
* `error` -- attempted and failed, with `import_error` explaining why

`POST /recipes` is **not idempotent**: sending the same recipe twice creates two
Fitatu recipes. So `ok` is a hard gate -- a recipe already imported is never
sent again unless `--force` is passed. That is the whole reason this status
exists, and why the schema requires the id alongside the status.

**A recipe imports whole or not at all.** An ingredient that will not resolve
fails the recipe, with the name in `import_error`. A recipe in Fitatu missing
three of its eight ingredients would silently under-report calories, which is
the one failure nobody notices. `--allow-partial` overrides this and warns.
"""

import sqlite3
from datetime import date
from typing import Any

from ..resolve.resolver import options_from_config, resolve_recipe
from ..store import products as products_store
from ..store import recipes as recipes_store
from .payload import build_recipe_kwargs

STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_NOT_IMPORTED = "not_imported"
IMPORT_STATUSES = (STATUS_OK, STATUS_ERROR, STATUS_NOT_IMPORTED)

ID_FIELDS = ("id", "recipeId", "recipe_id")


def recipe_status(data: dict) -> str:
    """The recipe's import status, defaulting to not_imported when absent."""
    status = data.get("import_status")
    return status if status in IMPORT_STATUSES else STATUS_NOT_IMPORTED


def extract_recipe_id(response: dict) -> str | None:
    """Pull Fitatu's new recipe id out of the create response."""
    if not isinstance(response, dict):
        return None
    for field in ID_FIELDS:
        value = response.get(field)
        if value not in (None, ""):
            return str(value)
    return None


def resolve_names(
    conn: sqlite3.Connection, names: list[str]
) -> tuple[list[str], list[str]]:
    """Map recipe ids to themselves, keeping the caller's order.

    Returns `(known, unknown)`. A name may be given with or without the `.yaml`
    suffix, since that is how recipes were identified before the move and how
    they still appear in older import logs.
    """
    existing = set(recipes_store.ids(conn))
    known, unknown = [], []
    for name in names:
        stem = name[:-5] if name.endswith(".yaml") else name
        if stem not in existing:
            unknown.append(name)
        elif stem not in known:
            known.append(stem)
    return known, unknown


def select_pending(
    conn: sqlite3.Connection,
    *,
    include_errors: bool = True,
    force: bool = False,
    names: list[str] | None = None,
) -> list[str]:
    """Recipe ids worth attempting.

    Already-imported recipes are excluded unless `force`, because creating them
    again would duplicate them in Fitatu. Naming recipes narrows the list to
    those, but does not lift that gate: a named recipe already imported is still
    skipped, because the duplicate it would create is the same duplicate.
    """
    if names is not None:
        candidates, _ = resolve_names(conn, names)
    else:
        candidates = recipes_store.ids(conn)

    if force:
        return list(candidates)

    rows = {
        row["id"]: row
        for row in conn.execute(
            "SELECT id, import_status, fitatu_recipe_id FROM recipes"
        )
    }

    pending = []
    for recipe_id in candidates:
        row = rows.get(recipe_id)
        if row is None:
            continue
        # A recorded Fitatu id outranks the status. Editing `import_status` back
        # to not_imported is an easy way to ask for 52 duplicates, and the id is
        # the evidence that the recipe is already there.
        if row["fitatu_recipe_id"]:
            continue
        status = row["import_status"]
        if status == STATUS_NOT_IMPORTED:
            pending.append(recipe_id)
        elif status == STATUS_ERROR and include_errors:
            pending.append(recipe_id)
    return pending


def _record_error(conn: sqlite3.Connection, recipe_id: str, reason: str) -> dict:
    recipes_store.set_import_result(
        conn,
        recipe_id,
        status=STATUS_ERROR,
        import_error=reason,
        imported_at=date.today().isoformat(),
    )
    return {"status": STATUS_ERROR, "detail": reason, "warnings": []}


def import_one(
    conn: sqlite3.Connection,
    recipe_id: str,
    client,
    *,
    shared: bool = False,
    allow_partial: bool = False,
    dry_run: bool = False,
    tags: list[str] | None = None,
    account: str | None = None,
    resolve: bool = True,
    retry_failed: bool = False,
    resolve_options: dict[str, Any] | None = None,
) -> dict:
    """Resolve, then import, one recipe. Records the outcome on it.

    Returns `{"status", "detail", "warnings"}`. `client` may be None when
    `dry_run`, in which case resolution consults only what is already stored.
    """
    data = recipes_store.get(conn, recipe_id)
    if data is None:
        return {"status": STATUS_ERROR, "detail": "no such recipe", "warnings": []}

    warnings: list[str] = []

    if resolve:
        outcomes = resolve_recipe(
            conn,
            client,
            recipe_id,
            retry_failed=retry_failed,
            **(resolve_options or {}),
        )
        newly = [o for o in outcomes if o.status == "resolved"]
        if newly:
            warnings.append(
                f"resolved {len(newly)} new ingredient name(s) against verified "
                "Fitatu products"
            )
        unresolved = [o for o in outcomes if not o.ok]
        if unresolved and not allow_partial:
            reason = "unresolved ingredient(s): " + "; ".join(
                f"{o.name} ({o.reason})" for o in unresolved
            )
            if dry_run:
                return {"status": STATUS_ERROR, "detail": reason, "warnings": warnings}
            result = _record_error(conn, recipe_id, reason)
            result["warnings"] = warnings
            return result
        if unresolved:
            warnings.append(
                f"{len(unresolved)} ingredient(s) have no verified Fitatu product "
                "and will be omitted (--allow-partial)"
            )
        # Re-read: resolution may have mapped names, which changes the
        # ingredients' product ids through the ingredients_resolved view.
        data = recipes_store.get(conn, recipe_id) or data

    products = products_store.cache(conn)
    built = build_recipe_kwargs(
        data, products, shared=shared, allow_partial=allow_partial, tags=tags
    )
    warnings.extend(built.warnings)

    if not built.ok:
        reason = "; ".join(str(p) for p in built.problems)
        if dry_run:
            return {"status": STATUS_ERROR, "detail": reason, "warnings": warnings}
        result = _record_error(conn, recipe_id, reason)
        result["warnings"] = warnings
        return result

    if dry_run:
        return {
            "status": "would_import",
            "detail": f"{len(built.kwargs['items'])} ingredient(s)",
            "warnings": warnings,
        }

    try:
        response = client.create_recipe(**built.kwargs)
    except Exception as exc:  # the client raises its own error types
        result = _record_error(conn, recipe_id, f"{type(exc).__name__}: {exc}")
        result["warnings"] = warnings
        return result

    new_id = extract_recipe_id(response)
    if new_id is None:
        # The recipe may well have been created, so refuse to retry it blindly:
        # a retry would duplicate it. Flag it for a human instead.
        result = _record_error(
            conn,
            recipe_id,
            f"created but Fitatu returned no recipe id: {response!r}",
        )
        result["warnings"] = warnings
        return result

    recipes_store.set_import_result(
        conn,
        recipe_id,
        status=STATUS_OK,
        fitatu_recipe_id=new_id,
        fitatu_account_id=account,
        imported_at=date.today().isoformat(),
    )
    return {"status": STATUS_OK, "detail": new_id, "warnings": warnings}


def summarise(conn: sqlite3.Connection) -> dict[str, int]:
    counts = {status: 0 for status in IMPORT_STATUSES}
    for row in conn.execute(
        "SELECT import_status, COUNT(*) AS n FROM recipes GROUP BY import_status"
    ):
        counts[row["import_status"]] = row["n"]
    return counts


def run(
    conn: sqlite3.Connection,
    *,
    offset: int | None = None,
    limit: int | None = None,
    shared: bool = False,
    allow_partial: bool = False,
    dry_run: bool = False,
    force: bool = False,
    include_errors: bool = True,
    tags: list[str] | None = None,
    names: list[str] | None = None,
    resolve: bool = True,
    retry_failed: bool = False,
    config: dict | None = None,
) -> dict[str, int]:
    """Import a window of the pending recipes. Returns a tally by outcome.

    `offset` and `limit` slice the **pending** list, not the whole collection.
    That list shrinks as recipes succeed, so an offset is only stable within one
    run, or across repeated `--dry-run` calls which change nothing. Walking a
    real import in batches therefore means keeping `--offset 0` and raising
    `--limit`, not advancing the offset -- otherwise the second batch skips
    recipes the first one already removed from the list.

    `names` imports just those recipes, by id. An unknown name is refused before
    anything is sent, because a typo that silently imported nothing would look
    exactly like a recipe that had already been imported.
    """
    if offset is not None and offset < 0:
        raise SystemExit(f"--offset must be zero or greater, got {offset}")
    if limit is not None and limit < 1:
        raise SystemExit(f"--limit must be at least 1, got {limit}")

    if names:
        _, unknown = resolve_names(conn, names)
        if unknown:
            raise SystemExit(
                "No recipe with id: " + ", ".join(unknown) + "\n"
                "Pass the recipe id, as `products status` and the API report it."
            )

    pending = select_pending(
        conn, include_errors=include_errors, force=force, names=names
    )
    total = len(pending)

    if names and not pending:
        # Named but not pending means already imported: say so, rather than
        # letting it read as "nothing to do".
        print(
            f"Nothing to import: {', '.join(names)} already has import_status "
            f"'ok'. Re-sending would duplicate it in Fitatu; --force does that "
            f"deliberately."
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
        print(f"Importing by name: {', '.join(pending)}")
    elif start or limit:
        end = start + len(pending)
        print(f"Pending: {total} recipe(s); taking {start}-{end - 1}")

    client = None
    account = None
    if not dry_run:
        from .client import account_id, connect

        client = connect()
        account = account_id(client)
        print(f"Importing as Fitatu account {account or 'unknown'}")

    resolve_options = options_from_config(config)
    print(f"Importing {len(pending)} recipe(s)")
    failed: list[str] = []
    for i, recipe_id in enumerate(pending, 1):
        print(f"[{i}/{len(pending)}] {recipe_id}")
        result = import_one(
            conn,
            recipe_id,
            client,
            shared=shared,
            allow_partial=allow_partial,
            dry_run=dry_run,
            tags=tags,
            account=account,
            resolve=resolve,
            retry_failed=retry_failed,
            resolve_options=resolve_options,
        )
        tally[result["status"]] = tally.get(result["status"], 0) + 1
        for warning in result["warnings"]:
            print(f"  warning: {warning}")
        marker = "ok" if result["status"] == STATUS_OK else result["status"]
        print(f"  {marker}: {result['detail']}")
        if result["status"] == STATUS_ERROR:
            failed.append(recipe_id)

    if failed and not allow_partial:
        _report_failures(conn, failed)

    return tally


def _report_failures(conn: sqlite3.Connection, recipe_ids: list[str]) -> None:
    """Split the failures into the ones worth acting on and the ones that are not.

    A long list of identical "no quantity stated" lines reads like a broken
    tool, when it is the opposite: the source video never said how much, and the
    import refused to invent it. Unresolved names are a third, different group:
    those are fixable with a query alias or a manual mapping. All three need
    different answers, so they are counted separately.
    """
    products = products_store.cache(conn)
    unresolvable, salvageable, hopeless = [], [], []

    for recipe_id in recipe_ids:
        data = recipes_store.get(conn, recipe_id)
        if data is None:
            continue
        ingredients = [i for i in data.get("ingredients") or [] if i.get("name")]
        total = len(ingredients)
        missing = [i["name"] for i in ingredients if not i.get("fitatu_product_id")]
        if missing:
            unresolvable.append((recipe_id, len(missing), total))
            continue
        built = build_recipe_kwargs(data, products, allow_partial=True)
        if built.ok:
            salvageable.append((recipe_id, len(built.kwargs["items"]), total))
        else:
            hopeless.append((recipe_id, total))

    if unresolvable:
        print(
            f"\n{len(unresolvable)} recipe(s) have ingredients with no verified "
            "Fitatu product. Add a search phrase for the name, or map it by hand:"
        )
        for recipe_id, missing, total in unresolvable:
            print(f"  {recipe_id}  ({missing}/{total} ingredients unresolved)")
        print(
            "  uv run python -m recipe_library.products failures\n"
            "  uv run python -m recipe_library.products alias --name NAME --query PHRASE"
        )
    if salvageable:
        print(
            f"\n{len(salvageable)} recipe(s) would import with --allow-partial, "
            "keeping the ingredients that convert:"
        )
        for recipe_id, kept, total in salvageable:
            print(f"  {recipe_id}  ({kept}/{total} ingredients)")
    if hopeless:
        print(
            f"\n{len(hopeless)} recipe(s) cannot be imported at all: the source "
            "video stated no quantity for any ingredient, and Fitatu needs at "
            "least one item. Filling those numbers in is the one thing this "
            "library will not do."
        )
        for recipe_id, total in hopeless:
            print(f"  {recipe_id}  (0/{total} ingredients convert)")
