"""The database itself: pragmas, and the invariants expressed as constraints.

Two rules are enforced by the schema rather than by convention, because a
convention is something a future call site forgets. These tests go straight at
the constraints, bypassing the store, to prove the database refuses the write
even when the Python does not.
"""

import sqlite3

import pytest

from recipe_library import db as db_module


def test_foreign_keys_are_enforced(conn):
    # Off by default in SQLite, and per connection: without this every
    # REFERENCES clause in schema.sql is decoration.
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO ingredient_products (name, product_id, method) "
            "VALUES ('x', 'no-such-product', 'auto')"
        )


def test_schema_is_versioned(conn):
    assert db_module.version(conn) == db_module.SCHEMA_VERSION


def test_applying_the_schema_twice_is_harmless(conn):
    db_module.apply_schema(conn)
    db_module.apply_schema(conn)
    assert db_module.version(conn) == db_module.SCHEMA_VERSION


def test_a_file_database_uses_wal(db_path):
    conn = db_module.connect(db_path)
    try:
        # Needed so the HTTP server can read while a fetch or import writes.
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        conn.close()


def test_opening_a_missing_database_without_create_is_refused(tmp_path):
    with pytest.raises(SystemExit):
        db_module.connect(tmp_path / "nope.db", create=False)


def test_reading_an_empty_database_fails_with_an_actionable_message(tmp_path):
    path = tmp_path / "empty.db"
    sqlite3.connect(path).close()
    conn = db_module.connect(path)
    try:
        with pytest.raises(SystemExit) as exc:
            db_module.check_ready(conn)
        assert "migrate_yaml" in str(exc.value)
    finally:
        conn.close()


# ------------------------------------------------- the verified constraint ----


def test_an_unverified_product_cannot_be_stored_at_all(conn):
    # The point of the CHECK: no code path, present or future, can store one.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO products (product_id, display_name, verified) "
            "VALUES ('1', 'Dubious', 0)"
        )


def test_a_product_cannot_be_flipped_to_unverified(conn, product):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE products SET verified = 0 WHERE product_id = ?", (product,))


# --------------------------------------------- the import_status constraint ----


def test_an_ok_import_status_requires_a_fitatu_recipe_id(conn, video):
    # Fitatu's create is not idempotent, and that id is the only evidence
    # distinguishing an imported recipe from a duplicate waiting to happen.
    conn.execute(
        "INSERT INTO recipes (id, title, import_status) VALUES ('r', 'R', 'not_imported')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE recipes SET import_status = 'ok' WHERE id = 'r'")


def test_an_empty_fitatu_recipe_id_does_not_satisfy_it(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO recipes (id, title, import_status, fitatu_recipe_id) "
            "VALUES ('r', 'R', 'ok', '')"
        )


# ------------------------------------------- the quantity/unit constraint ----


def test_a_quantity_without_a_unit_is_refused(conn, recipe):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO ingredients (recipe_id, position, name, normalized_name, "
            "quantity, unit) VALUES ('test-recipe', 99, 'x', 'x', 5, NULL)"
        )


def test_a_null_quantity_is_always_allowed(conn, recipe):
    # Null means the video stated no amount. It is the normal case: a third of
    # the collection's ingredients have no quantity.
    conn.execute(
        "INSERT INTO ingredients (recipe_id, position, name, normalized_name, "
        "quantity, unit) VALUES ('test-recipe', 99, 'x', 'x', NULL, NULL)"
    )


def test_a_zero_or_negative_quantity_is_refused(conn, recipe):
    for bad in (0, -5):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO ingredients (recipe_id, position, name, "
                "normalized_name, quantity, unit) "
                "VALUES ('test-recipe', 98, 'x', 'x', ?, 'g')",
                (bad,),
            )


def test_an_unknown_unit_is_refused(conn, recipe):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO ingredients (recipe_id, position, name, normalized_name, "
            "quantity, unit) VALUES ('test-recipe', 97, 'x', 'x', 1, 'handful')"
        )


# ---------------------------------------------------------------- cascades ----


def test_deleting_a_recipe_takes_its_ingredients(conn, recipe):
    conn.execute("DELETE FROM recipes WHERE id = 'test-recipe'")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM ingredients").fetchone()[0] == 0


def test_deleting_a_video_leaves_its_recipes(conn, recipe):
    # A recipe outlives the video row: the extraction is the valuable artefact
    # and the provenance is recorded on it, not only in the join.
    conn.execute("DELETE FROM videos WHERE video_id = 'vid1'")
    conn.commit()
    row = conn.execute("SELECT video_id FROM recipes WHERE id = 'test-recipe'").fetchone()
    assert row["video_id"] is None


def test_one_video_cannot_have_two_recipes_in_the_same_slot(conn, recipe):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO recipes (id, title, video_id, recipe_index) "
            "VALUES ('other', 'Other', 'vid1', 1)"
        )


def test_recipes_without_a_video_do_not_collide(conn):
    # The unique index is partial, so several video-less recipes are fine.
    conn.execute("INSERT INTO recipes (id, title, recipe_index) VALUES ('a', 'A', 1)")
    conn.execute("INSERT INTO recipes (id, title, recipe_index) VALUES ('b', 'B', 1)")


# ------------------------------------------------------------ transaction ----


def test_a_failed_transaction_leaves_nothing_behind(conn):
    with pytest.raises(RuntimeError):
        with db_module.transaction(conn):
            conn.execute(
                "INSERT INTO products (product_id, display_name) VALUES ('9', 'X')"
            )
            raise RuntimeError("boom")
    assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0
