"""The HTTP surface. No network: the Fitatu client is always a fake.

The guards get the most attention here, because the expensive mistakes are all
writes: a recipe imported twice becomes two Fitatu recipes, an invented
quantity becomes wrong nutrition data, and an unverified product becomes both.
"""

import pytest

from conftest import (
    FakeFitatu,
    make_importable_recipe,
    make_recipe,
    make_video,
    search_row,
    source_for,
)
from recipe_library.api import deps
from recipe_library.db import connect
from recipe_library.store import products as products_store
from recipe_library.store import recipes as recipes_store
from recipe_library.store import videos as videos_store


@pytest.fixture
def seeded(db_path):
    """A database with one video, one recipe and one verified product."""
    conn = connect(db_path)
    try:
        make_video(conn, "vid1")
        videos_store.save_subtitles(conn, "vid1", "en", "mix the chicken and salt")
        recipes_store.save(conn, make_recipe())
        # A second recipe whose every quantity can actually reach Fitatu. The
        # first one deliberately cannot: `salt` has no stated amount.
        recipes_store.save(
            conn, make_importable_recipe(source=source_for(recipe_index=2))
        )
        for product_id, name in (("111", "Chicken breast"), ("222", "Salt")):
            products_store.save(
                conn,
                product_id=product_id,
                display_name=name,
                source="public",
                verified=True,
                measures=[
                    {"measure_id": "1", "measure_name": "g", "weight_g": 1, "kcal": 1.1}
                ],
                default_measure_id="1",
            )
        conn.commit()
    finally:
        conn.close()
    return db_path


@pytest.fixture
def api(seeded):
    from fastapi.testclient import TestClient

    from recipe_library.api.app import create_app

    return TestClient(create_app(str(seeded)))


@pytest.fixture
def fitatu(monkeypatch):
    """Replace the Fitatu dependency with a fake, and hand it back."""
    client = FakeFitatu(
        {
            "chicken breast": [search_row("111", "Chicken breast")],
            "salt": [search_row("222", "Salt")],
        }
    )
    monkeypatch.setattr(deps, "fitatu_client", lambda: (client, "acct-1"))
    for module in ("routes_recipes", "routes_products"):
        monkeypatch.setattr(
            f"recipe_library.api.{module}.fitatu_client", lambda: (client, "acct-1")
        )
    return client


# ------------------------------------------------------------------- meta ----


def test_health_reports_the_schema_version(api):
    body = api.get("/health").json()
    assert body["status"] == "ok"
    assert body["schema_version"] == body["expected_schema_version"]


def test_stats_covers_all_three_subjects(api):
    body = api.get("/stats").json()
    assert body["recipes"]["recipes"] == 2
    assert body["products"]["products"] == 2
    assert body["videos"]["videos"] == 1


def test_an_unmigrated_database_is_reported_not_crashed(tmp_path):
    import sqlite3

    from fastapi.testclient import TestClient

    from recipe_library.api.app import create_app

    path = tmp_path / "empty.db"
    sqlite3.connect(path).close()
    with pytest.raises(SystemExit):
        TestClient(create_app(str(path))).get("/health")


# ---------------------------------------------------------------- reading ----


def test_listing_recipes_includes_collection_totals(api):
    body = api.get("/recipes").json()
    assert body["count"] == 2
    assert body["collection"]["ingredients"] == 4
    assert body["collection"]["unquantified_ingredients"] == 1
    assert "ingredients" not in body["recipes"][0]


def test_listing_can_include_ingredients(api):
    body = api.get("/recipes", params={"include_ingredients": True}).json()
    assert len(body["recipes"][0]["ingredients"]) == 2


def test_an_unknown_language_filter_is_rejected(api):
    assert api.get("/recipes", params={"language": "de"}).status_code == 422


def test_getting_one_recipe_resolves_its_products(api):
    body = api.get("/recipes/test-recipe").json()
    assert body["ingredients"][0]["name"] == "chicken breast"
    # Not mapped yet, so no product: a blank, not a guess.
    assert body["ingredients"][0]["fitatu_product_id"] is None


def test_a_missing_recipe_is_a_404(api):
    assert api.get("/recipes/nope").status_code == 404


def test_the_source_text_is_what_extraction_reads(api):
    body = api.get("/videos/vid1/source").json()
    assert body["description"] == "ingredients: chicken, salt"
    assert body["subtitles"]["en"] == "mix the chicken and salt"


def test_videos_can_be_filtered_by_either_status(api):
    assert api.get("/videos", params={"fetch_status": "complete"}).json()["count"] == 0
    assert api.get("/videos", params={"fetch_status": "pending"}).json()["count"] == 1


# ---------------------------------------------------------------- writing ----


def test_a_recipe_can_be_created_and_fetched_back(api):
    body = make_recipe("new-recipe", source=source_for(recipe_index=3))
    body.pop("id")
    response = api.put("/recipes/new-recipe", json=body)
    assert response.status_code == 200
    assert response.json()["created"] is True
    assert api.get("/recipes/new-recipe").json()["title"] == "Test Recipe"


def test_putting_the_same_id_twice_updates_rather_than_duplicating(api):
    body = make_recipe()
    body.pop("id")
    api.put("/recipes/test-recipe", json=body)
    response = api.put("/recipes/test-recipe", json=dict(body, title="Renamed"))
    assert response.json()["created"] is False
    assert api.get("/recipes/test-recipe").json()["title"] == "Renamed"


def test_a_quantity_with_no_unit_is_refused(api):
    body = make_recipe()
    body.pop("id")
    body["ingredients"] = [{"name": "flour", "quantity": 100}]
    response = api.put("/recipes/bad", json=body)
    assert response.status_code == 422
    assert "quantity but no unit" in str(response.json()["detail"])


def test_a_zero_quantity_is_refused(api):
    body = make_recipe()
    body.pop("id")
    body["ingredients"] = [{"name": "flour", "quantity": 0, "unit": "g"}]
    assert api.put("/recipes/bad", json=body).status_code == 422


def test_an_unknown_unit_is_refused(api):
    body = make_recipe()
    body.pop("id")
    body["ingredients"] = [{"name": "flour", "quantity": 1, "unit": "handful"}]
    assert api.put("/recipes/bad", json=body).status_code == 422


def test_a_recipe_with_no_ingredients_is_refused(api):
    body = make_recipe()
    body.pop("id")
    body["ingredients"] = []
    assert api.put("/recipes/bad", json=body).status_code == 422


def test_import_state_cannot_be_set_through_the_recipe_endpoint(api):
    # It is the record of what Fitatu did, and it is the duplicate gate.
    body = make_recipe()
    body.pop("id")
    body["import_status"] = "ok"
    body["fitatu_recipe_id"] = "12345"
    assert api.put("/recipes/test-recipe", json=body).status_code == 422


def test_re_extracting_keeps_the_recorded_import(api, fitatu):
    api.post("/recipes/importable/import")
    before = api.get("/recipes/importable").json()
    assert before["import_status"] == "ok"

    body = make_importable_recipe(source=source_for(recipe_index=2))
    body.pop("id")
    api.put("/recipes/importable", json=dict(body, title="Re-extracted"))

    after = api.get("/recipes/importable").json()
    assert after["title"] == "Re-extracted"
    # Resetting this would be a request for a duplicate in Fitatu.
    assert after["import_status"] == "ok"
    assert after["fitatu_recipe_id"] == before["fitatu_recipe_id"]


def test_a_body_id_that_contradicts_the_path_is_refused(api):
    body = make_recipe("something-else")
    assert api.put("/recipes/test-recipe", json=body).status_code == 400


def test_unknown_fields_are_refused(api):
    body = make_recipe()
    body.pop("id")
    assert api.put("/recipes/x", json=dict(body, colour="blue")).status_code == 422


def test_deleting_a_recipe_works_once(api):
    assert api.delete("/recipes/test-recipe").status_code == 204
    assert api.delete("/recipes/test-recipe").status_code == 404


# -------------------------------------------------------------- resolving ----


def test_resolving_a_recipe_stores_verified_products(api, fitatu):
    body = api.post("/recipes/test-recipe/resolve").json()
    assert body["resolved"] == 2
    assert body["ready_to_import"] is True
    assert api.get("/products/111").json()["names"] == ["chicken breast"]


def test_an_unverified_candidate_leaves_the_name_unresolved(api, monkeypatch):
    client = FakeFitatu(
        {
            "chicken breast": [search_row("111", "Chicken breast")],
            "salt": [search_row("999", "Salt", verified=False)],
        }
    )
    monkeypatch.setattr(
        "recipe_library.api.routes_recipes.fitatu_client", lambda: (client, "a")
    )
    body = api.post("/recipes/test-recipe/resolve").json()
    assert body["unresolved"] == 1
    assert body["ready_to_import"] is False
    assert api.get("/products/999").status_code == 404


def test_a_resolve_dry_run_never_contacts_fitatu(api, fitatu):
    body = api.post("/recipes/test-recipe/resolve", json={"dry_run": True}).json()
    assert body["unresolved"] == 2
    assert fitatu.searches == []


def test_resolving_a_missing_recipe_is_a_404(api, fitatu):
    assert api.post("/recipes/nope/resolve").status_code == 404


def test_a_single_name_can_be_resolved(api, fitatu):
    body = api.post("/products/resolve/salt").json()
    assert body["status"] == "resolved"
    assert body["product_id"] == "222"


# --------------------------------------------------------------- importing ----


def test_importing_resolves_first_then_creates(api, fitatu):
    body = api.post("/recipes/importable/import").json()
    assert body["status"] == "ok"
    assert body["detail"] == "999"
    assert len(fitatu.created) == 1
    assert body["recipe"]["fitatu_recipe_id"] == "999"
    assert body["recipe"]["fitatu_account_id"] == "acct-1"


def test_an_unresolved_ingredient_fails_the_whole_recipe(api, monkeypatch):
    # A Fitatu recipe missing ingredients under-reports calories with nothing
    # to show that it did.
    client = FakeFitatu({"chicken breast": [search_row("111", "Chicken breast")]})
    monkeypatch.setattr(
        "recipe_library.api.routes_recipes.fitatu_client", lambda: (client, "a")
    )
    body = api.post("/recipes/importable/import").json()
    assert body["status"] == "error"
    assert "salt" in body["detail"]
    assert client.created == []
    assert body["recipe"]["import_status"] == "error"


def test_a_recipe_already_imported_is_refused(api, fitatu):
    api.post("/recipes/importable/import")
    response = api.post("/recipes/importable/import")
    assert response.status_code == 409
    assert "duplicate" in response.json()["detail"]
    assert len(fitatu.created) == 1


def test_force_sends_it_again_deliberately(api, fitatu):
    api.post("/recipes/importable/import")
    response = api.post("/recipes/importable/import", json={"force": True})
    assert response.status_code == 200
    assert len(fitatu.created) == 2


def test_an_import_dry_run_creates_nothing(api, fitatu):
    body = api.post("/recipes/importable/import", json={"dry_run": True}).json()
    assert fitatu.created == []
    assert api.get("/recipes/importable").json()["import_status"] == "not_imported"


def test_a_fitatu_error_is_recorded_on_the_recipe(api, fitatu):
    fitatu.create_error = RuntimeError("500 Server Error")
    body = api.post("/recipes/importable/import").json()
    assert body["status"] == "error"
    assert "500 Server Error" in body["detail"]
    assert api.get("/recipes/importable").json()["import_status"] == "error"


def test_a_create_without_an_id_is_an_error_not_a_success(api, fitatu):
    # The recipe may well have been created, so a blind retry would duplicate
    # it. Flag it for a human instead.
    fitatu.created_id = None
    body = api.post("/recipes/importable/import").json()
    assert body["status"] == "error"
    assert "no recipe id" in body["detail"]


def test_the_import_queue_lists_what_is_pending(api, fitatu):
    assert api.get("/imports/pending").json()["pending"] == ["importable", "test-recipe"]
    api.post("/recipes/importable/import")
    body = api.get("/imports/pending").json()
    assert body["pending"] == ["test-recipe"]
    assert body["by_import_status"]["ok"] == 1


# ---------------------------------------------------------------- products ----


def test_there_is_no_endpoint_that_stores_a_product_directly(api):
    # Products arrive only through resolution, which is the one code path that
    # checks the verified flag.
    paths = api.get("/openapi.json").json()["paths"]
    writable = [
        path
        for path, methods in paths.items()
        if path in ("/products", "/products/{product_id}")
        and {"post", "put", "patch"} & set(methods)
    ]
    assert writable == []


def test_a_manual_mapping_requires_a_stored_product(api):
    response = api.post(
        "/products/mappings", json={"name": "salt", "product_id": "not-stored"}
    )
    assert response.status_code == 404
    assert "verified" in response.json()["detail"]


def test_a_manual_mapping_to_a_stored_product_works(api):
    response = api.post(
        "/products/mappings", json={"name": "chicken breast", "product_id": "111"}
    )
    assert response.status_code == 201
    assert (
        api.get("/recipes/test-recipe").json()["ingredients"][0]["fitatu_product_id"]
        == "111"
    )


def test_a_mapping_can_be_removed(api):
    api.post("/products/mappings", json={"name": "chicken breast", "product_id": "111"})
    assert api.delete("/products/mappings/chicken breast").status_code == 204
    assert api.delete("/products/mappings/chicken breast").status_code == 404


def test_pending_names_excludes_what_is_mapped(api):
    before = api.get("/products/pending").json()
    assert {n["name"] for n in before["names"]} == {"chicken breast", "salt"}
    api.post("/products/mappings", json={"name": "salt", "product_id": "222"})
    after = api.get("/products/pending").json()
    assert {n["name"] for n in after["names"]} == {"chicken breast"}


def test_pending_names_can_be_scoped_to_one_recipe(api):
    body = api.get("/products/pending", params={"recipe_id": "test-recipe"}).json()
    assert {n["name"] for n in body["names"]} == {"chicken breast", "salt"}


def test_adding_an_alias_clears_the_recorded_failure(api, monkeypatch):
    client = FakeFitatu({})
    monkeypatch.setattr(
        "recipe_library.api.routes_products.fitatu_client", lambda: (client, "a")
    )
    api.post("/products/resolve/self-rising flour")
    assert api.get("/products/failures").json()["count"] == 1

    api.post(
        "/products/aliases",
        json={"name": "self-rising flour", "query": "mąka z proszkiem"},
    )
    # There is something new to try, so the old verdict no longer stands.
    assert api.get("/products/failures").json()["count"] == 0
    aliases = api.get("/products/aliases").json()["aliases"]
    assert aliases[0]["query"] == "mąka z proszkiem"


def test_failures_report_what_was_rejected(api, monkeypatch):
    client = FakeFitatu({"salt": [search_row("999", "Salt", verified=False)]})
    monkeypatch.setattr(
        "recipe_library.api.routes_products.fitatu_client", lambda: (client, "a")
    )
    api.post("/products/resolve/salt")
    failure = api.get("/products/failures").json()["failures"][0]
    assert failure["candidates"][0]["verified"] is False


def test_a_missing_product_is_a_404(api):
    assert api.get("/products/nope").status_code == 404
