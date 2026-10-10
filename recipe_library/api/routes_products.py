"""Product endpoints: the verified catalogue, the name mapping, the aliases.

There is no endpoint that stores a product directly. Products arrive only
through resolution, which is the one code path that checks Fitatu's `verified`
flag, and the `products` table's CHECK constraint refuses anything else. Making
this writable would be the obvious place for an unverified product to get in.
"""

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query

from ..names import normalize_name
from ..paths import load_config
from ..resolve.resolver import options_from_config, resolve_name
from ..store import products as store
from .deps import Conn, fitatu_client
from .schemas import AliasIn, MappingIn, ResolveIn

router = APIRouter(prefix="/products", tags=["products"])


def _resolve_options() -> dict[str, Any]:
    try:
        config = load_config()
    except SystemExit:
        config = {}
    return options_from_config(config)


@router.get("")
def list_products(
    conn: Conn,
    limit: int | None = Query(default=None, ge=1),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """Every stored product. All of them are Fitatu-verified, by constraint."""
    products = store.listing(conn, limit=limit, offset=offset)
    return {"count": len(products), "products": products, "totals": store.stats(conn)}


@router.get("/pending")
def pending_names(
    conn: Conn, retry_failed: bool = False, recipe_id: str | None = None
) -> dict[str, Any]:
    """Ingredient names with no product yet, most widely used first."""
    names = store.pending_names(conn, retry_failed=retry_failed, recipe_id=recipe_id)
    return {"count": len(names), "names": names}


@router.get("/failures")
def failures(conn: Conn) -> dict[str, Any]:
    """Names with no acceptable verified match, and what was rejected.

    An unresolved name is a normal outcome, not a bug: a blank is better than a
    wrong id. The rejected candidates are kept so a threshold change can be
    reviewed rather than guessed at.
    """
    rows = store.failures(conn)
    return {"count": len(rows), "failures": rows}


@router.get("/aliases")
def aliases(conn: Conn) -> dict[str, Any]:
    """Every search phrase recorded for an ingredient name.

    This is the bilingual dictionary the resolver uses. Fitatu's catalogue is
    Polish and most of these recipes are not, so an English ingredient is
    usually found by its Polish equivalent, and that equivalent lives here.
    """
    rows = [
        dict(row)
        for row in conn.execute(
            "SELECT name, query, locale, added_at FROM query_aliases "
            "ORDER BY name, query"
        )
    ]
    return {"count": len(rows), "aliases": rows}


@router.post("/aliases", status_code=201)
def add_alias(body: AliasIn, conn: Conn) -> dict[str, Any]:
    """Teach the resolver another phrase for a name.

    Also clears any recorded failure for that name: there is something new to
    try, so the previous verdict no longer stands.
    """
    store.add_alias(conn, body.name, body.query, locale=body.locale)
    store.clear_failure(conn, body.name)
    name = normalize_name(body.name)
    return {"name": name, "aliases": store.aliases(conn, name)}


@router.delete("/aliases", status_code=204)
def remove_alias(conn: Conn, name: str, query: str) -> None:
    if not store.remove_alias(conn, name, query):
        raise HTTPException(
            status_code=404, detail=f"no alias {query!r} for {normalize_name(name)!r}"
        )


@router.get("/mappings/{name:path}")
def get_mapping(name: str, conn: Conn) -> dict[str, Any]:
    """What one ingredient name resolves to, with the product behind it."""
    key = normalize_name(name)
    mapping = store.mapping(conn, key)
    return {
        "name": key,
        "mapping": mapping,
        "product": store.get(conn, mapping["product_id"]) if mapping else None,
        "failure": store.failure(conn, key),
        "aliases": store.aliases(conn, key),
    }


@router.post("/mappings", status_code=201)
def put_mapping(body: MappingIn, conn: Conn) -> dict[str, Any]:
    """Record a name-to-product mapping by hand.

    The product has to be stored already, and only verified products are ever
    stored, so this overrides the matcher without touching the verification
    rule.
    """
    product = store.get(conn, body.product_id)
    if product is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"product {body.product_id} is not stored. Resolve it first; only "
                f"Fitatu-verified products can be stored."
            ),
        )
    name = store.map_name(
        conn, body.name, body.product_id, query=body.query, method="manual"
    )
    return {"name": name, "product": product}


@router.delete("/mappings/{name:path}", status_code=204)
def delete_mapping(name: str, conn: Conn) -> None:
    if not store.unmap_name(conn, name):
        raise HTTPException(
            status_code=404, detail=f"no mapping for {normalize_name(name)!r}"
        )


@router.post("/resolve/{name:path}")
def resolve_one(
    name: str,
    conn: Conn,
    body: ResolveIn = Body(default=ResolveIn()),
) -> dict[str, Any]:
    """Resolve a single ingredient name against verified Fitatu products."""
    client = None
    if not body.dry_run:
        client, _ = fitatu_client()
    outcome = resolve_name(
        conn, client, name, refresh=body.refresh, **_resolve_options()
    )
    return dict(outcome.as_dict(), candidates=outcome.candidates)


@router.get("/{product_id}")
def get_product(product_id: str, conn: Conn) -> dict[str, Any]:
    product = store.get(conn, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail=f"no product {product_id!r}")
    return product
