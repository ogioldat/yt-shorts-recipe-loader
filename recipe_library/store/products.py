"""Fitatu products, their measures, and the name-to-product mapping.

Replaces `state/fitatu_products.yaml`. `cache()` returns the same mapping that
file's `products` key held, so `fitatu_import/payload.py` and its measure
conversions are untouched by the move.

**Only verified products are stored.** `products.verified` is CHECKed to 1 in
the schema, so this is a property of the database rather than a filter each
call site has to remember. `save()` refuses an unverified product with a
readable message instead of letting the constraint surface as an IntegrityError.
"""

import json
import sqlite3
from datetime import date
from typing import Any

from ..db import transaction
from ..names import normalize_name


class UnverifiedProduct(ValueError):
    """Raised when something tries to store a product Fitatu has not verified."""


def _measures(conn: sqlite3.Connection, product_id: str) -> tuple[dict, list[dict]]:
    """The product's default measure and all of its measures, in cache shape."""
    rows = conn.execute(
        """
        SELECT measure_id, measure_name, weight_g, kcal, per_item, is_default
        FROM product_measures
        WHERE product_id = ?
        ORDER BY is_default DESC, CAST(measure_id AS INTEGER), measure_id
        """,
        (product_id,),
    ).fetchall()

    measures, default = [], {}
    for row in rows:
        measure = {
            "measure_id": row["measure_id"],
            "measure_name": row["measure_name"],
            "weight_g": row["weight_g"],
            "kcal": row["kcal"],
        }
        if row["per_item"]:
            measure["per_item"] = True
        measures.append(measure)
        if row["is_default"] and not default:
            default = dict(measure)
    if not default and measures:
        default = dict(measures[0])
    return default, measures


def get(conn: sqlite3.Connection, product_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM products WHERE product_id = ?", (str(product_id),)
    ).fetchone()
    if row is None:
        return None
    default, measures = _measures(conn, row["product_id"])
    return {
        "fitatu_product_id": row["product_id"],
        "display_name": row["display_name"],
        "brand": row["brand"],
        "source": row["source"],
        "verified": bool(row["verified"]),
        "default_measure": default,
        "measures": measures,
        "first_seen_at": row["first_seen_at"],
        "names": names_for(conn, row["product_id"]),
    }


def names_for(conn: sqlite3.Connection, product_id: str) -> list[str]:
    return [
        row["name"]
        for row in conn.execute(
            "SELECT name FROM ingredient_products WHERE product_id = ? ORDER BY name",
            (str(product_id),),
        )
    ]


def save(
    conn: sqlite3.Connection,
    *,
    product_id: str,
    display_name: str | None = None,
    brand: str | None = None,
    source: str | None = None,
    verified: bool | None = None,
    measures: list[dict] | None = None,
    default_measure_id: str | None = None,
) -> str:
    """Store a verified product and its measures. Returns the product id.

    Refuses anything not explicitly verified. The whole point of the constraint
    is that an unverified product never enters the library, so a caller passing
    `verified=None` because it did not look is refused just as firmly as one
    passing False.
    """
    product_id = str(product_id)
    if verified is not True:
        raise UnverifiedProduct(
            f"product {product_id} ({display_name or '?'}) has verified={verified!r}; "
            "only Fitatu-verified products are stored"
        )
    if source is not None and source not in ("user", "public"):
        raise ValueError(f"source must be 'user', 'public' or None, got {source!r}")

    measures = measures or []
    if default_measure_id is None and measures:
        default_measure_id = str(measures[0].get("measure_id") or "")

    with transaction(conn):
        conn.execute(
            """
            INSERT INTO products (
                product_id, display_name, brand, source, verified, first_seen_at
            ) VALUES (?, ?, ?, ?, 1, ?)
            ON CONFLICT (product_id) DO UPDATE SET
                display_name = COALESCE(excluded.display_name, products.display_name),
                brand = COALESCE(excluded.brand, products.brand),
                source = COALESCE(excluded.source, products.source)
            """,
            (product_id, display_name, brand, source, date.today().isoformat()),
        )
        for measure in measures:
            measure_id = str(measure.get("measure_id") or "").strip()
            if not measure_id:
                continue
            conn.execute(
                """
                INSERT INTO product_measures (
                    product_id, measure_id, measure_name, weight_g, kcal,
                    per_item, is_default
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (product_id, measure_id) DO UPDATE SET
                    measure_name = COALESCE(excluded.measure_name,
                                            product_measures.measure_name),
                    weight_g = COALESCE(excluded.weight_g, product_measures.weight_g),
                    kcal = COALESCE(excluded.kcal, product_measures.kcal),
                    per_item = MAX(excluded.per_item, product_measures.per_item),
                    is_default = excluded.is_default
                """,
                (
                    product_id,
                    measure_id,
                    measure.get("measure_name"),
                    measure.get("weight_g"),
                    measure.get("kcal"),
                    1 if measure.get("per_item") else 0,
                    1 if measure_id == str(default_measure_id or "") else 0,
                ),
            )
    return product_id


# ------------------------------------------------------ name -> product -----


def map_name(
    conn: sqlite3.Connection,
    name: str,
    product_id: str,
    *,
    query: str | None = None,
    match_score: float | None = None,
    method: str = "auto",
) -> str:
    """Point a normalised ingredient name at a product.

    Clears any recorded failure for that name: it has an answer now.
    """
    key = normalize_name(name)
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO ingredient_products (
                name, product_id, query, match_score, method, resolved_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (name) DO UPDATE SET
                product_id = excluded.product_id,
                query = excluded.query,
                match_score = excluded.match_score,
                method = excluded.method,
                resolved_at = excluded.resolved_at
            """,
            (
                key,
                str(product_id),
                normalize_name(query) if query else key,
                match_score,
                method,
                date.today().isoformat(),
            ),
        )
        conn.execute("DELETE FROM ingredient_resolution_failures WHERE name = ?", (key,))
    return key


def unmap_name(conn: sqlite3.Connection, name: str) -> bool:
    key = normalize_name(name)
    with transaction(conn):
        cur = conn.execute("DELETE FROM ingredient_products WHERE name = ?", (key,))
    return cur.rowcount > 0


def mapping(conn: sqlite3.Connection, name: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM ingredient_products WHERE name = ?", (normalize_name(name),)
    ).fetchone()
    return dict(row) if row else None


def record_failure(
    conn: sqlite3.Connection,
    name: str,
    reason: str,
    *,
    candidates: list[dict] | None = None,
) -> None:
    """Record that a name has no acceptable verified match.

    An unresolved name is a normal outcome, not an error: a blank is better than
    a wrong id. `candidates` keeps what was considered and rejected, so a later
    threshold change can be reviewed rather than guessed at. `attempts` counts
    up, which is what distinguishes "never found" from "tried repeatedly".
    """
    key = normalize_name(name)
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO ingredient_resolution_failures (
                name, reason, candidates, attempts, checked_at
            ) VALUES (?, ?, ?, 1, ?)
            ON CONFLICT (name) DO UPDATE SET
                reason = excluded.reason,
                candidates = excluded.candidates,
                attempts = ingredient_resolution_failures.attempts + 1,
                checked_at = excluded.checked_at
            """,
            (
                key,
                reason,
                json.dumps(candidates or [], ensure_ascii=False),
                date.today().isoformat(),
            ),
        )


def failure(conn: sqlite3.Connection, name: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM ingredient_resolution_failures WHERE name = ?",
        (normalize_name(name),),
    ).fetchone()
    if row is None:
        return None
    out = dict(row)
    out["candidates"] = json.loads(out.get("candidates") or "[]")
    return out


def failures(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM ingredient_resolution_failures ORDER BY name"
    ).fetchall()
    out = []
    for row in rows:
        entry = dict(row)
        entry["candidates"] = json.loads(entry.get("candidates") or "[]")
        out.append(entry)
    return out


def clear_failure(conn: sqlite3.Connection, name: str) -> bool:
    with transaction(conn):
        cur = conn.execute(
            "DELETE FROM ingredient_resolution_failures WHERE name = ?",
            (normalize_name(name),),
        )
    return cur.rowcount > 0


# ---------------------------------------------------------- query aliases ----


def aliases(conn: sqlite3.Connection, name: str) -> list[dict[str, Any]]:
    """Extra phrases to try in `search_food` for this ingredient name.

    The catalogue is Polish and most recipes are not, so an English ingredient
    is usually found by its Polish equivalent. This is where that equivalent
    lives: data, seeded from the phrases that previously worked, editable when a
    new name does not resolve.
    """
    rows = conn.execute(
        "SELECT query, locale FROM query_aliases WHERE name = ? ORDER BY query",
        (normalize_name(name),),
    ).fetchall()
    return [dict(row) for row in rows]


def add_alias(
    conn: sqlite3.Connection, name: str, query: str, *, locale: str | None = None
) -> None:
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO query_aliases (name, query, locale, added_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (name, query) DO UPDATE SET locale = excluded.locale
            """,
            (
                normalize_name(name),
                normalize_name(query),
                locale,
                date.today().isoformat(),
            ),
        )


def remove_alias(conn: sqlite3.Connection, name: str, query: str) -> bool:
    with transaction(conn):
        cur = conn.execute(
            "DELETE FROM query_aliases WHERE name = ? AND query = ?",
            (normalize_name(name), normalize_name(query)),
        )
    return cur.rowcount > 0


# ----------------------------------------------------------------- views ----


def cache(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """The mapping `fitatu_products.yaml` held: name -> product with measures.

    Built in three queries rather than one per name, because the import runner
    asks for the whole thing once per run.
    """
    products: dict[str, dict[str, Any]] = {}
    for row in conn.execute("SELECT * FROM products"):
        products[row["product_id"]] = {
            "fitatu_product_id": row["product_id"],
            "display_name": row["display_name"],
            "brand": row["brand"],
            "source": row["source"],
            "verified": bool(row["verified"]),
            "default_measure": {},
            "measures": [],
        }

    for row in conn.execute(
        """
        SELECT product_id, measure_id, measure_name, weight_g, kcal, per_item, is_default
        FROM product_measures
        ORDER BY product_id, is_default DESC, CAST(measure_id AS INTEGER), measure_id
        """
    ):
        entry = products.get(row["product_id"])
        if entry is None:
            continue
        measure = {
            "measure_id": row["measure_id"],
            "measure_name": row["measure_name"],
            "weight_g": row["weight_g"],
            "kcal": row["kcal"],
        }
        if row["per_item"]:
            measure["per_item"] = True
        entry["measures"].append(measure)
        if row["is_default"] and not entry["default_measure"]:
            entry["default_measure"] = dict(measure)

    for entry in products.values():
        if not entry["default_measure"] and entry["measures"]:
            entry["default_measure"] = dict(entry["measures"][0])

    out: dict[str, dict[str, Any]] = {}
    for row in conn.execute("SELECT * FROM ingredient_products"):
        entry = products.get(row["product_id"])
        if entry is None:
            continue
        out[row["name"]] = dict(
            entry,
            query=row["query"],
            match_score=row["match_score"],
            method=row["method"],
            resolved_at=row["resolved_at"],
        )
    return out


def usage(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Normalised ingredient name -> the recipe ids that use it."""
    out: dict[str, list[str]] = {}
    for row in conn.execute(
        "SELECT DISTINCT normalized_name, recipe_id FROM ingredients "
        "ORDER BY normalized_name, recipe_id"
    ):
        out.setdefault(row["normalized_name"], []).append(row["recipe_id"])
    return out


def pending_names(
    conn: sqlite3.Connection, *, retry_failed: bool = False, recipe_id: str | None = None
) -> list[dict[str, Any]]:
    """Ingredient names with no product yet, most widely used first.

    Names already recorded as having no acceptable match are skipped unless
    `retry_failed`, so a known miss does not come back every run.
    """
    sql = [
        """
        SELECT i.normalized_name AS name,
               COUNT(DISTINCT i.recipe_id) AS recipe_count,
               MIN(i.name) AS example
        FROM ingredients i
        LEFT JOIN ingredient_products m ON m.name = i.normalized_name
        WHERE i.product_id IS NULL AND m.name IS NULL
        """
    ]
    params: list[Any] = []
    if not retry_failed:
        sql.append(
            "AND i.normalized_name NOT IN "
            "(SELECT name FROM ingredient_resolution_failures)"
        )
    if recipe_id:
        sql.append("AND i.recipe_id = ?")
        params.append(recipe_id)
    sql.append("GROUP BY i.normalized_name ORDER BY recipe_count DESC, i.normalized_name")

    return [dict(row) for row in conn.execute("\n".join(sql), params)]


def listing(
    conn: sqlite3.Connection, *, limit: int | None = None, offset: int = 0
) -> list[dict[str, Any]]:
    sql = [
        """
        SELECT p.product_id, p.display_name, p.brand, p.source, p.first_seen_at,
               (SELECT COUNT(*) FROM product_measures m
                 WHERE m.product_id = p.product_id) AS measure_count,
               (SELECT COUNT(*) FROM ingredient_products n
                 WHERE n.product_id = p.product_id) AS name_count
        FROM products p
        ORDER BY p.display_name, p.product_id
        """
    ]
    params: list[Any] = []
    if limit is not None:
        sql.append("LIMIT ? OFFSET ?")
        params.extend([limit, offset])
    return [dict(row) for row in conn.execute("\n".join(sql), params)]


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    totals = conn.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM products) AS products,
            (SELECT COUNT(*) FROM product_measures) AS measures,
            (SELECT COUNT(*) FROM ingredient_products) AS mapped_names,
            (SELECT COUNT(*) FROM ingredient_resolution_failures) AS failed_names,
            (SELECT COUNT(*) FROM query_aliases) AS aliases,
            (SELECT COUNT(DISTINCT normalized_name) FROM ingredients) AS used_names
        """
    ).fetchone()
    by_method = {
        row["method"]: row["n"]
        for row in conn.execute(
            "SELECT method, COUNT(*) AS n FROM ingredient_products "
            "GROUP BY method ORDER BY method"
        )
    }
    return {
        "products": totals["products"],
        "measures": totals["measures"],
        "mapped_names": totals["mapped_names"],
        "failed_names": totals["failed_names"],
        "query_aliases": totals["aliases"],
        "distinct_ingredient_names": totals["used_names"],
        "pending_names": len(pending_names(conn)),
        "by_method": by_method,
    }
