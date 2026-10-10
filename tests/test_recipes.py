"""The read-side facade and the MCP server's listing.

`recipes.py` is what the MCP server and anything else wanting "the collection"
in one call go through. The SQL it delegates to is tested in test_store.py, so
this covers the facade's own job: opening a connection, and the shape the MCP
tool hands back.
"""

import pytest

from conftest import make_recipe, make_video, source_for
from recipe_library import recipes as facade
from recipe_library.db import connect
from recipe_library.store import products as products_store
from recipe_library.store import recipes as recipes_store


@pytest.fixture
def library(db_path, monkeypatch):
    """Three recipes across both languages, one with a product mapped."""
    conn = connect(db_path)
    try:
        make_video(conn, "vid1")
        recipes_store.save(conn, make_recipe("placki", language="pl"))
        recipes_store.save(
            conn, make_recipe("pancakes", language="en", source=source_for(recipe_index=2))
        )
        recipes_store.save(
            conn,
            make_recipe(
                "imported",
                language="en",
                source=source_for(recipe_index=3),
                import_status="ok",
                fitatu_recipe_id="555",
            ),
        )
        products_store.save(
            conn,
            product_id="111",
            display_name="Chicken breast",
            verified=True,
            measures=[{"measure_id": "1", "measure_name": "g", "weight_g": 1}],
        )
        products_store.map_name(conn, "chicken breast", "111")
        conn.commit()
    finally:
        conn.close()

    # The facade opens its own connection, so point the default at this one.
    monkeypatch.setattr("recipe_library.paths.DB", db_path)
    monkeypatch.setattr("recipe_library.db.DB", db_path)
    return db_path


def test_recipes_are_ordered_by_id(library):
    assert [r["id"] for r in facade.list_recipes()] == [
        "imported",
        "pancakes",
        "placki",
    ]


def test_an_empty_collection_lists_nothing(db_path, monkeypatch):
    monkeypatch.setattr("recipe_library.db.DB", db_path)
    assert facade.list_recipes() == []
    assert facade.collection_stats()["recipes"] == 0


def test_ingredients_are_omitted_by_default(library):
    assert "ingredients" not in facade.list_recipes()[0]
    assert "ingredients" in facade.list_recipes(include_ingredients=True)[0]


def test_counts_describe_the_ingredient_list(library):
    row = next(r for r in facade.list_recipes() if r["id"] == "placki")
    assert row["ingredient_count"] == 2
    # One of the two ingredients has no stated quantity.
    assert row["unquantified_count"] == 1
    assert row["mapped_count"] == 1


def test_language_filter(library):
    assert [r["id"] for r in facade.list_recipes(language="pl")] == ["placki"]


def test_import_status_filter(library):
    assert [r["id"] for r in facade.list_recipes(import_status="ok")] == ["imported"]
    assert [r["id"] for r in facade.list_recipes(import_status="not_imported")] == [
        "pancakes",
        "placki",
    ]


def test_limit_caps_the_listing(library):
    assert len(facade.list_recipes(limit=2)) == 2


def test_a_recipe_id_is_surfaced_only_when_there_is_one(library):
    rows = {r["id"]: r for r in facade.list_recipes()}
    assert rows["imported"]["fitatu_recipe_id"] == "555"
    assert "fitatu_recipe_id" not in rows["placki"]


def test_source_is_included_without_empty_keys(library):
    source = facade.list_recipes()[0]["source"]
    assert source["video_id"] == "vid1"
    assert None not in source.values()


def test_getting_one_recipe_returns_the_whole_document(library):
    data = facade.get_recipe("placki")
    assert data["title"] == "Test Recipe"
    assert len(data["ingredients"]) == 2


def test_getting_a_missing_recipe_returns_none(library):
    assert facade.get_recipe("nope") is None


def test_stats_summarise_the_collection(library):
    stats = facade.collection_stats()
    assert stats["recipes"] == 3
    assert stats["ingredients"] == 6
    assert stats["by_language"] == {"en": 2, "pl": 1}
    assert stats["by_import_status"] == {"not_imported": 2, "ok": 1}


# ------------------------------------------------------------ mcp server ----


def test_the_mcp_tool_returns_a_listing_with_totals(library):
    from recipe_library import mcp_server

    result = mcp_server.list_tool()
    assert result["count"] == 3
    assert result["collection"]["recipes"] == 3
    assert "ingredients" not in result["recipes"][0]


def test_the_mcp_tool_passes_its_filters_through(library):
    from recipe_library import mcp_server

    result = mcp_server.list_tool(language="pl", include_ingredients=True)
    assert result["count"] == 1
    assert result["recipes"][0]["id"] == "placki"
    assert len(result["recipes"][0]["ingredients"]) == 2
