"""The recipe collection.

Replaces `recipes/*.yaml`. `get()` returns and `save()` accepts the document
shape those files held, so `schema.validate_recipe` and
`fitatu_import.payload.build_recipe_kwargs` are unchanged by the move.

One thing genuinely changed rather than relocated: an ingredient's
`fitatu_product_id` is no longer stored on the ingredient. It is read from the
`ingredients_resolved` view, which joins the ingredient's normalised name to
the name-keyed product mapping. What used to be the `products apply` pass --
copying ids into 72 files, to be re-run whenever the mapping grew -- is now a
join that cannot go stale. An ingredient can still carry its own `product_id`
as an override, and the view prefers it when present.
"""

import json
import sqlite3
from typing import Any

from ..db import transaction
from ..names import normalize_name

SUMMARY_FIELDS = ("id", "title", "title_en", "language", "servings")

RECIPE_COLUMNS = (
    "id", "title", "title_en", "language", "servings", "video_id", "recipe_index",
    "source_url", "channel", "extracted_from", "extracted_at",
    "prep_time_minutes", "prep_time_source", "import_status",
    "fitatu_recipe_id", "fitatu_account_id", "import_error", "import_note",
    "imported_at",
)


def _ingredients(conn: sqlite3.Connection, recipe_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT name, quantity, unit, note, product_id, match_query, match_score,
               match_method, is_override
        FROM ingredients_resolved
        WHERE recipe_id = ?
        ORDER BY position
        """,
        (recipe_id,),
    ).fetchall()

    out = []
    for row in rows:
        ing: dict[str, Any] = {
            "name": row["name"],
            # Null means the source video stated no amount. Never a guess.
            "quantity": _number(row["quantity"]),
            "unit": row["unit"],
        }
        if row["note"] is not None:
            ing["note"] = row["note"]
        ing["fitatu_product_id"] = row["product_id"]
        if row["product_id"] is not None:
            ing["match"] = {
                "query": row["match_query"],
                "score": row["match_score"],
                "method": row["match_method"],
                "override": bool(row["is_override"]),
            }
        out.append(ing)
    return out


def _number(value: Any) -> Any:
    """Hand back an integral REAL as an int, which is how it was written.

    SQLite has no integer/float distinction in a REAL column, so 300 comes back
    as 300.0. The recipes were authored with whole grams and the Fitatu payload
    rounds anyway, but a quantity displayed as `300.0 g` reads like a computed
    value rather than the number the video actually said.
    """
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def get(conn: sqlite3.Connection, recipe_id: str) -> dict[str, Any] | None:
    """One recipe, in the shape a `recipes/*.yaml` file held."""
    row = conn.execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
    if row is None:
        return None

    data: dict[str, Any] = {
        "id": row["id"],
        "title": row["title"],
    }
    if row["title_en"]:
        data["title_en"] = row["title_en"]
    data["language"] = row["language"]
    data["servings"] = row["servings"]
    data["ingredients"] = _ingredients(conn, recipe_id)

    source: dict[str, Any] = {"video_id": row["video_id"]}
    if row["source_url"]:
        source["url"] = row["source_url"]
    if row["channel"]:
        source["channel"] = row["channel"]
    source["recipe_index"] = row["recipe_index"]
    extracted_from = json.loads(row["extracted_from"] or "[]")
    if extracted_from:
        source["extracted_from"] = extracted_from
    data["source"] = source

    data["extracted_at"] = row["extracted_at"]
    for field in ("prep_time_minutes", "prep_time_source"):
        if row[field] is not None:
            data[field] = row[field]

    previous = [
        {
            "fitatu_recipe_id": r["fitatu_recipe_id"],
            "fitatu_account_id": r["fitatu_account_id"],
            **({"imported_at": r["imported_at"]} if r["imported_at"] else {}),
        }
        for r in conn.execute(
            "SELECT fitatu_recipe_id, fitatu_account_id, imported_at "
            "FROM recipe_imports WHERE recipe_id = ? ORDER BY imported_at, fitatu_recipe_id",
            (recipe_id,),
        )
    ]
    data["import_status"] = row["import_status"]
    if previous:
        data["previous_imports"] = previous
    for field in (
        "fitatu_recipe_id",
        "fitatu_account_id",
        "import_error",
        "import_note",
        "imported_at",
    ):
        if row[field] is not None:
            data[field] = row[field]
    return data


def exists(conn: sqlite3.Connection, recipe_id: str) -> bool:
    return (
        conn.execute("SELECT 1 FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
        is not None
    )


def ids(conn: sqlite3.Connection) -> list[str]:
    return [row["id"] for row in conn.execute("SELECT id FROM recipes ORDER BY id")]


def save(conn: sqlite3.Connection, data: dict) -> str:
    """Upsert one recipe from the document shape. Returns its id.

    Ingredients are replaced wholesale rather than merged: they are an ordered
    list belonging to the recipe, and a positional merge across an edit that
    inserted one in the middle would quietly mis-associate the rest.

    Validation is the caller's job -- `schema.validate_recipe` -- but the
    schema's own CHECK constraints are the backstop, so a quantity without a
    unit or an `ok` status without a Fitatu id fails here regardless.
    """
    recipe_id = data.get("id")
    if not recipe_id:
        raise ValueError("recipe has no id")

    source = data.get("source") or {}
    values = {
        "id": recipe_id,
        "title": data.get("title") or "",
        "title_en": data.get("title_en"),
        "language": data.get("language"),
        "servings": data.get("servings"),
        "video_id": source.get("video_id"),
        "recipe_index": source.get("recipe_index") or 1,
        "source_url": source.get("url"),
        "channel": source.get("channel"),
        "extracted_from": json.dumps(
            source.get("extracted_from") or [], ensure_ascii=False
        ),
        "extracted_at": data.get("extracted_at"),
        "prep_time_minutes": data.get("prep_time_minutes"),
        "prep_time_source": data.get("prep_time_source"),
        "import_status": data.get("import_status") or "not_imported",
        "fitatu_recipe_id": data.get("fitatu_recipe_id"),
        "fitatu_account_id": data.get("fitatu_account_id"),
        "import_error": data.get("import_error"),
        "import_note": data.get("import_note"),
        "imported_at": data.get("imported_at"),
    }

    columns = ", ".join(RECIPE_COLUMNS)
    placeholders = ", ".join(f":{c}" for c in RECIPE_COLUMNS)
    updates = ", ".join(f"{c} = excluded.{c}" for c in RECIPE_COLUMNS if c != "id")

    with transaction(conn):
        conn.execute(
            f"INSERT INTO recipes ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT (id) DO UPDATE SET {updates}",
            values,
        )
        conn.execute("DELETE FROM ingredients WHERE recipe_id = ?", (recipe_id,))
        for position, ing in enumerate(data.get("ingredients") or [], 1):
            if not isinstance(ing, dict) or not ing.get("name"):
                continue
            conn.execute(
                """
                INSERT INTO ingredients (
                    recipe_id, position, name, normalized_name, quantity, unit,
                    note, product_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    recipe_id,
                    position,
                    ing["name"],
                    normalize_name(ing["name"]),
                    ing.get("quantity"),
                    ing.get("unit"),
                    ing.get("note"),
                    # Only an explicit override lands on the row. A plain
                    # `fitatu_product_id` carried over from the YAML era is the
                    # name mapping's job and is not duplicated here.
                    ing.get("product_id_override"),
                ),
            )

        for previous in data.get("previous_imports") or []:
            if not isinstance(previous, dict) or not previous.get("fitatu_recipe_id"):
                continue
            conn.execute(
                """
                INSERT INTO recipe_imports (
                    recipe_id, fitatu_recipe_id, fitatu_account_id, imported_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT (recipe_id, fitatu_recipe_id) DO NOTHING
                """,
                (
                    recipe_id,
                    str(previous["fitatu_recipe_id"]),
                    previous.get("fitatu_account_id"),
                    previous.get("imported_at"),
                ),
            )
    return recipe_id


def delete(conn: sqlite3.Connection, recipe_id: str) -> bool:
    with transaction(conn):
        cur = conn.execute("DELETE FROM recipes WHERE id = ?", (recipe_id,))
    return cur.rowcount > 0


def set_import_result(
    conn: sqlite3.Connection,
    recipe_id: str,
    *,
    status: str,
    fitatu_recipe_id: str | None = None,
    fitatu_account_id: str | None = None,
    import_error: str | None = None,
    imported_at: str | None = None,
) -> None:
    """Record the outcome of an import attempt.

    A successful import first files the previous one, if any, into
    `recipe_imports`. The same library imported under two accounts leaves two
    disjoint sets of Fitatu recipes, and the old id is the only record that the
    earlier one exists.
    """
    with transaction(conn):
        row = conn.execute(
            "SELECT fitatu_recipe_id, fitatu_account_id, imported_at "
            "FROM recipes WHERE id = ?",
            (recipe_id,),
        ).fetchone()
        if row is None:
            raise KeyError(recipe_id)

        if (
            status == "ok"
            and row["fitatu_recipe_id"]
            and row["fitatu_recipe_id"] != fitatu_recipe_id
        ):
            conn.execute(
                """
                INSERT INTO recipe_imports (
                    recipe_id, fitatu_recipe_id, fitatu_account_id, imported_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT (recipe_id, fitatu_recipe_id) DO NOTHING
                """,
                (
                    recipe_id,
                    row["fitatu_recipe_id"],
                    row["fitatu_account_id"],
                    row["imported_at"],
                ),
            )

        conn.execute(
            """
            UPDATE recipes SET
                import_status = ?,
                fitatu_recipe_id = ?,
                fitatu_account_id = ?,
                import_error = ?,
                imported_at = ?
            WHERE id = ?
            """,
            (
                status,
                fitatu_recipe_id,
                fitatu_account_id,
                import_error,
                imported_at,
                recipe_id,
            ),
        )


# -------------------------------------------------------------- listings ----


def summarise(conn: sqlite3.Connection, row: sqlite3.Row, *, include_ingredients: bool):
    summary: dict[str, Any] = {
        field: row[field] for field in SUMMARY_FIELDS if row[field] is not None
    }
    summary["ingredient_count"] = row["ingredient_count"]
    summary["unquantified_count"] = row["unquantified_count"]
    summary["mapped_count"] = row["mapped_count"]
    summary["import_status"] = row["import_status"]
    if row["fitatu_recipe_id"]:
        summary["fitatu_recipe_id"] = row["fitatu_recipe_id"]

    source = {
        "video_id": row["video_id"],
        "url": row["source_url"],
        "channel": row["channel"],
        "recipe_index": row["recipe_index"],
    }
    summary["source"] = {k: v for k, v in source.items() if v is not None}

    if include_ingredients:
        summary["ingredients"] = _ingredients(conn, row["id"])
    return summary


_LISTING_SQL = """
SELECT r.id, r.title, r.title_en, r.language, r.servings, r.import_status,
       r.fitatu_recipe_id, r.video_id, r.source_url, r.channel, r.recipe_index,
       (SELECT COUNT(*) FROM ingredients i WHERE i.recipe_id = r.id)
           AS ingredient_count,
       (SELECT COUNT(*) FROM ingredients i
         WHERE i.recipe_id = r.id AND i.quantity IS NULL)
           AS unquantified_count,
       (SELECT COUNT(*) FROM ingredients_resolved x
         WHERE x.recipe_id = r.id AND x.product_id IS NOT NULL)
           AS mapped_count
FROM recipes r
"""


def listing(
    conn: sqlite3.Connection,
    *,
    include_ingredients: bool = False,
    language: str | None = None,
    import_status: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Every recipe as a summary, ordered by id.

    Ingredients are omitted by default: the collection holds hundreds of them,
    which would bury the overview most callers are after.
    """
    sql = [_LISTING_SQL]
    where, params = [], []
    if language:
        where.append("r.language = ?")
        params.append(language)
    if import_status:
        where.append("r.import_status = ?")
        params.append(import_status)
    if where:
        sql.append("WHERE " + " AND ".join(where))
    sql.append("ORDER BY r.id")
    if limit is not None:
        sql.append("LIMIT ? OFFSET ?")
        params.extend([limit, offset])

    rows = conn.execute("\n".join(sql), params).fetchall()
    return [summarise(conn, row, include_ingredients=include_ingredients) for row in rows]


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    """Counts worth knowing before reading the collection one recipe at a time."""
    totals = conn.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM recipes) AS recipes,
            (SELECT COUNT(*) FROM ingredients) AS ingredients,
            (SELECT COUNT(*) FROM ingredients WHERE quantity IS NULL)
                AS unquantified,
            (SELECT COUNT(*) FROM ingredients_resolved WHERE product_id IS NOT NULL)
                AS mapped,
            (SELECT COUNT(DISTINCT normalized_name) FROM ingredients)
                AS distinct_names
        """
    ).fetchone()
    by_language = {
        (row["language"] or "?"): row["n"]
        for row in conn.execute(
            "SELECT language, COUNT(*) AS n FROM recipes GROUP BY language "
            "ORDER BY language"
        )
    }
    by_status = {
        row["import_status"]: row["n"]
        for row in conn.execute(
            "SELECT import_status, COUNT(*) AS n FROM recipes GROUP BY import_status "
            "ORDER BY import_status"
        )
    }
    return {
        "recipes": totals["recipes"],
        "ingredients": totals["ingredients"],
        "unquantified_ingredients": totals["unquantified"],
        "mapped_ingredients": totals["mapped"],
        "distinct_ingredient_names": totals["distinct_names"],
        "by_language": by_language,
        "by_import_status": by_status,
    }
