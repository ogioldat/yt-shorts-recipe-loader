"""Recipe endpoints: browse, write, resolve, import."""

from typing import Any, Literal

from fastapi import APIRouter, Body, HTTPException, Query, Response

from ..fitatu_import.runner import import_one, select_pending, summarise
from ..paths import load_config
from ..resolve.resolver import options_from_config, resolve_recipe
from ..schema import validate_recipe
from ..store import recipes as store
from ..store import videos as videos_store
from .deps import Conn, fitatu_client
from .schemas import ImportIn, RecipeIn, ResolveIn

router = APIRouter(prefix="/recipes", tags=["recipes"])

# A separate router, because "which recipes still need importing" is a question
# about the import queue rather than about any one recipe, and nesting it under
# /recipes/... would collide with the recipe-id route.
imports_router = APIRouter(prefix="/imports", tags=["imports"])


def _resolve_options() -> dict[str, Any]:
    try:
        config = load_config()
    except SystemExit:
        config = {}
    return options_from_config(config)


@router.get("")
def list_recipes(
    conn: Conn,
    include_ingredients: bool = False,
    language: Literal["pl", "en"] | None = None,
    import_status: Literal["ok", "error", "not_imported"] | None = None,
    limit: int | None = Query(default=None, ge=1),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """Every recipe as a summary, ordered by id.

    Ingredients are omitted unless asked for: the collection holds hundreds of
    them and they would bury the overview.
    """
    recipes = store.listing(
        conn,
        include_ingredients=include_ingredients,
        language=language,
        import_status=import_status,
        limit=limit,
        offset=offset,
    )
    return {"count": len(recipes), "recipes": recipes, "collection": store.stats(conn)}


@router.get("/{recipe_id}")
def get_recipe(recipe_id: str, conn: Conn) -> dict[str, Any]:
    data = store.get(conn, recipe_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"no recipe {recipe_id!r}")
    return data


@router.put("/{recipe_id}")
def put_recipe(recipe_id: str, body: RecipeIn, conn: Conn) -> dict[str, Any]:
    """Create or replace a recipe. This is what `/extract-recipes` writes to.

    Validated twice on purpose: `validate_recipe` catches what a constraint
    cannot express, and the schema's CHECKs catch everything else. A quantity
    without a unit is refused by both.
    """
    if body.id and body.id != recipe_id:
        raise HTTPException(
            status_code=400,
            detail=f"body id {body.id!r} does not match path id {recipe_id!r}",
        )

    existed = store.exists(conn, recipe_id)
    document = body.to_document(recipe_id)

    # Import state belongs to the importer, so a re-extraction keeps whatever
    # Fitatu already recorded instead of silently resetting it -- which would
    # drop the duplicate gate on an already-imported recipe.
    if existed:
        current = store.get(conn, recipe_id) or {}
        for field in (
            "import_status",
            "fitatu_recipe_id",
            "fitatu_account_id",
            "import_error",
            "import_note",
            "imported_at",
            "previous_imports",
        ):
            if field in current:
                document[field] = current[field]

    problems = validate_recipe(document, expected_id=recipe_id)
    if problems:
        raise HTTPException(status_code=422, detail=problems)

    try:
        store.save(conn, document)
    except Exception as exc:
        raise HTTPException(
            status_code=400, detail=f"{type(exc).__name__}: {exc}"
        ) from exc

    # A written recipe is evidence that stage 2 read this video, which is the
    # timestamp the re-extraction gate compares against. The verdict itself is
    # a separate call: one recipe does not mean the whole video was understood.
    video_id = (document.get("source") or {}).get("video_id")
    if video_id:
        videos_store.mark_extracted(conn, video_id)
        conn.commit()

    return {"id": recipe_id, "created": not existed, "recipe": store.get(conn, recipe_id)}


@router.delete("/{recipe_id}", status_code=204)
def delete_recipe(recipe_id: str, conn: Conn) -> Response:
    if not store.delete(conn, recipe_id):
        raise HTTPException(status_code=404, detail=f"no recipe {recipe_id!r}")
    return Response(status_code=204)


@router.post("/{recipe_id}/resolve")
def resolve(
    recipe_id: str,
    conn: Conn,
    body: ResolveIn = Body(default=ResolveIn()),
) -> dict[str, Any]:
    """Match this recipe's ingredient names to verified Fitatu products.

    Idempotent in effect: a name already mapped is returned from the database
    without a search. `dry_run` reports what is still missing without
    contacting Fitatu at all.
    """
    if not store.exists(conn, recipe_id):
        raise HTTPException(status_code=404, detail=f"no recipe {recipe_id!r}")

    client = None
    if not body.dry_run:
        client, _ = fitatu_client()

    outcomes = resolve_recipe(
        conn,
        client,
        recipe_id,
        retry_failed=body.retry_failed,
        refresh=body.refresh,
        **_resolve_options(),
    )
    unresolved = [o for o in outcomes if not o.ok]
    return {
        "recipe_id": recipe_id,
        "names": len(outcomes),
        "resolved": sum(1 for o in outcomes if o.ok),
        "unresolved": len(unresolved),
        "ready_to_import": not unresolved,
        "outcomes": [o.as_dict() for o in outcomes],
    }


@router.post("/{recipe_id}/import")
def import_recipe(
    recipe_id: str,
    conn: Conn,
    body: ImportIn = Body(default=ImportIn()),
) -> dict[str, Any]:
    """Resolve, then create this recipe in Fitatu.

    Two phases: every ingredient name is matched to a verified product first,
    and an unresolved name fails the whole recipe rather than being dropped --
    a Fitatu recipe missing three of eight ingredients under-reports calories
    with nothing to show that it did.

    Refuses a recipe already imported. Fitatu's create is not idempotent, so
    sending it again produces a second copy; `force` does that deliberately.
    """
    data = store.get(conn, recipe_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"no recipe {recipe_id!r}")

    if not body.force and not body.dry_run:
        pending = select_pending(conn, names=[recipe_id], force=False)
        if not pending:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{recipe_id} is already in Fitatu as recipe "
                    f"{data.get('fitatu_recipe_id')}. Re-sending would create a "
                    f"duplicate; pass force to do that deliberately."
                ),
            )

    client, account = (None, None)
    if not body.dry_run:
        client, account = fitatu_client()

    result = import_one(
        conn,
        recipe_id,
        client,
        shared=body.shared,
        allow_partial=body.allow_partial,
        dry_run=body.dry_run,
        tags=body.tags,
        account=account,
        resolve=body.resolve,
        retry_failed=body.retry_failed,
        resolve_options=_resolve_options(),
    )
    return {
        "recipe_id": recipe_id,
        "status": result["status"],
        "detail": result["detail"],
        "warnings": result["warnings"],
        "recipe": store.get(conn, recipe_id),
    }


@imports_router.get("/pending")
def pending(conn: Conn, include_errors: bool = True) -> dict[str, Any]:
    """Recipes worth an import attempt, and the tally by status."""
    return {
        "pending": select_pending(conn, include_errors=include_errors),
        "by_import_status": summarise(conn),
    }
