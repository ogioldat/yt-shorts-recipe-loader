"""The store layer: round-tripping the document shapes, and the join.

The store hands back the same shapes the YAML files held, because the pure
logic -- schema validation, the measure conversions, the completeness rules --
was written and tested against those. These tests pin that contract down.
"""

import pytest

from conftest import make_recipe, make_video
from recipe_library.store import products as products_store
from recipe_library.store import recipes as recipes_store
from recipe_library.store import videos as videos_store


# ---------------------------------------------------------------- videos ----


def test_metadata_round_trips(conn):
    original = make_video(conn, "abc")
    stored = videos_store.meta(conn, "abc")
    for key in ("title", "description", "original_lang", "caption_track"):
        assert stored[key] == original[key]
    assert stored["has_auto_captions"] is True
    assert stored["manual_sub_langs"] == []


def test_metadata_is_none_for_a_video_never_fetched(conn):
    conn.execute("INSERT INTO videos (video_id, title) VALUES ('stub', 'T')")
    conn.commit()
    # A stub cannot say which caption track is the original, and calling that
    # "no captions" would mark it complete and never fetch its subtitles.
    assert videos_store.meta(conn, "stub") is None


def test_saving_metadata_records_the_description_hash(conn):
    make_video(conn, "abc", description="hello")
    from recipe_library.ledger import description_hash

    row = conn.execute(
        "SELECT description_sha256 FROM videos WHERE video_id = 'abc'"
    ).fetchone()
    assert row["description_sha256"] == description_hash("hello")


def test_refetching_metadata_does_not_un_extract_the_video(conn):
    make_video(conn, "abc")
    videos_store.mark_extracted(conn, "abc", when="2026-01-01")
    conn.commit()
    make_video(conn, "abc", title="Edited title")
    row = conn.execute("SELECT * FROM videos WHERE video_id = 'abc'").fetchone()
    assert row["title"] == "Edited title"
    assert row["extracted_at"] == "2026-01-01"


def test_subtitles_round_trip(conn, video):
    videos_store.save_subtitles(conn, "vid1", "en", "first line\nsecond line")
    conn.commit()
    assert videos_store.subtitles(conn, "vid1")["en"] == "first line\nsecond line"
    assert videos_store.subtitle_langs(conn, "vid1") == {"en"}


def test_empty_subtitles_are_not_stored(conn, video):
    # A throttled request that produced nothing must leave the language really
    # missing, so the next pass retries instead of seeing a blank as done.
    videos_store.save_subtitles(conn, "vid1", "en", "   \n ")
    conn.commit()
    assert videos_store.subtitle_langs(conn, "vid1") == set()


def test_the_ledger_projects_subtitles_and_recipes(conn, recipe):
    videos_store.save_subtitles(conn, "vid1", "en", "text")
    conn.commit()
    entry = videos_store.ledger(conn)["vid1"]
    assert entry["subtitles"] == ["subs.en.txt"]
    assert entry["recipes"] == ["test-recipe"]


def test_saving_the_ledger_keeps_the_extraction_verdict(conn, video):
    # The single-status YAML field let a fetch pass erase this. Two columns, and
    # save_ledger coalesces, so a fetch that knows nothing about extraction
    # cannot clobber it.
    videos_store.save_ledger(
        conn, {"vid1": {"fetch_status": "complete", "extract_status": "needs_review"}}
    )
    videos_store.save_ledger(conn, {"vid1": {"fetch_status": "partial"}})
    row = conn.execute("SELECT * FROM videos WHERE video_id = 'vid1'").fetchone()
    assert row["fetch_status"] == "partial"
    assert row["extract_status"] == "needs_review"


def test_an_unknown_status_falls_back_rather_than_raising(conn, video):
    videos_store.save_ledger(conn, {"vid1": {"fetch_status": "nonsense"}})
    row = conn.execute("SELECT fetch_status FROM videos WHERE video_id='vid1'").fetchone()
    assert row["fetch_status"] == "pending"


# --------------------------------------------------------------- recipes ----


def test_a_recipe_round_trips_through_the_document_shape(conn, video):
    original = make_recipe()
    recipes_store.save(conn, original)
    stored = recipes_store.get(conn, "test-recipe")

    assert stored["title"] == original["title"]
    assert stored["servings"] == 2
    assert stored["source"]["video_id"] == "vid1"
    assert stored["source"]["extracted_from"] == ["title", "description"]
    assert [i["name"] for i in stored["ingredients"]] == ["chicken breast", "salt"]


def test_ingredient_order_is_preserved(conn, video):
    names = ["first", "second", "third", "fourth"]
    recipes_store.save(
        conn,
        make_recipe(ingredients=[{"name": n, "quantity": None, "unit": None} for n in names]),
    )
    stored = recipes_store.get(conn, "test-recipe")
    assert [i["name"] for i in stored["ingredients"]] == names


def test_a_whole_gram_comes_back_as_an_integer(conn, recipe):
    # SQLite has no int/float distinction in a REAL column, and `300.0 g` reads
    # like a computed value rather than the number the video said.
    stored = recipes_store.get(conn, "test-recipe")
    assert stored["ingredients"][0]["quantity"] == 500
    assert isinstance(stored["ingredients"][0]["quantity"], int)


def test_a_null_quantity_survives_the_round_trip(conn, recipe):
    stored = recipes_store.get(conn, "test-recipe")
    assert stored["ingredients"][1]["quantity"] is None
    assert stored["ingredients"][1]["unit"] is None


def test_saving_replaces_ingredients_rather_than_merging(conn, recipe):
    # A positional merge across an edit that inserted one in the middle would
    # mis-associate everything after it.
    recipes_store.save(
        conn, make_recipe(ingredients=[{"name": "only one", "quantity": None, "unit": None}])
    )
    stored = recipes_store.get(conn, "test-recipe")
    assert [i["name"] for i in stored["ingredients"]] == ["only one"]


def test_saving_a_recipe_without_an_id_is_refused(conn):
    with pytest.raises(ValueError):
        recipes_store.save(conn, {"title": "No id"})


def test_previous_imports_round_trip(conn, video):
    recipes_store.save(
        conn,
        make_recipe(
            previous_imports=[
                {"fitatu_recipe_id": "100", "fitatu_account_id": "acct-1"}
            ]
        ),
    )
    stored = recipes_store.get(conn, "test-recipe")
    assert stored["previous_imports"][0]["fitatu_recipe_id"] == "100"


def test_a_successful_import_files_the_previous_one(conn, recipe):
    recipes_store.set_import_result(
        conn, "test-recipe", status="ok", fitatu_recipe_id="100", fitatu_account_id="a1"
    )
    recipes_store.set_import_result(
        conn, "test-recipe", status="ok", fitatu_recipe_id="200", fitatu_account_id="a2"
    )
    stored = recipes_store.get(conn, "test-recipe")
    assert stored["fitatu_recipe_id"] == "200"
    # The old id is the only record that the earlier Fitatu recipe exists.
    assert [p["fitatu_recipe_id"] for p in stored["previous_imports"]] == ["100"]


def test_an_error_clears_the_recipe_id(conn, recipe):
    recipes_store.set_import_result(
        conn, "test-recipe", status="error", import_error="boom"
    )
    stored = recipes_store.get(conn, "test-recipe")
    assert stored["import_status"] == "error"
    assert "fitatu_recipe_id" not in stored
    assert stored["import_error"] == "boom"


def test_setting_a_result_on_a_missing_recipe_raises(conn):
    with pytest.raises(KeyError):
        recipes_store.set_import_result(conn, "nope", status="error")


def test_listing_counts_quantities_and_mappings(conn, recipe, product):
    products_store.map_name(conn, "chicken breast", product)
    row = recipes_store.listing(conn)[0]
    assert row["ingredient_count"] == 2
    assert row["unquantified_count"] == 1
    assert row["mapped_count"] == 1


def test_listing_omits_ingredients_by_default(conn, recipe):
    assert "ingredients" not in recipes_store.listing(conn)[0]
    assert "ingredients" in recipes_store.listing(conn, include_ingredients=True)[0]


def test_listing_filters_and_paginates(conn, video):
    recipes_store.save(conn, make_recipe("a", language="pl", source={"video_id": "vid1", "recipe_index": 1}))
    recipes_store.save(conn, make_recipe("b", language="en", source={"video_id": "vid1", "recipe_index": 2}))
    assert [r["id"] for r in recipes_store.listing(conn, language="pl")] == ["a"]
    assert [r["id"] for r in recipes_store.listing(conn, limit=1, offset=1)] == ["b"]


# -------------------------------------------------------------- products ----


def test_storing_an_unverified_product_is_refused(conn):
    for flag in (False, None):
        with pytest.raises(products_store.UnverifiedProduct):
            products_store.save(conn, product_id="1", display_name="x", verified=flag)
    assert products_store.get(conn, "1") is None


def test_a_product_round_trips_with_its_measures(conn, product):
    stored = products_store.get(conn, product)
    assert stored["verified"] is True
    assert stored["default_measure"]["measure_id"] == "1"
    assert {m["measure_id"] for m in stored["measures"]} == {"1", "39"}


def test_an_unknown_source_is_refused(conn):
    with pytest.raises(ValueError):
        products_store.save(
            conn, product_id="1", verified=True, source="somewhere", measures=[]
        )


def test_mapping_a_name_clears_its_recorded_failure(conn, product):
    products_store.record_failure(conn, "chicken", "no_match")
    products_store.map_name(conn, "Chicken", product)
    assert products_store.failure(conn, "chicken") is None
    assert products_store.mapping(conn, "chicken")["product_id"] == product


def test_repeated_failures_are_counted(conn):
    products_store.record_failure(conn, "x", "no_match")
    products_store.record_failure(conn, "x", "ambiguous")
    failure = products_store.failure(conn, "x")
    assert failure["attempts"] == 2
    assert failure["reason"] == "ambiguous"


def test_the_cache_shape_matches_what_payload_expects(conn, product):
    products_store.map_name(conn, "chicken breast", product)
    entry = products_store.cache(conn)["chicken breast"]
    assert entry["fitatu_product_id"] == product
    assert entry["default_measure"]["weight_g"] == 1
    assert any(m["measure_id"] == "39" for m in entry["measures"])


def test_pending_names_are_ordered_by_how_widely_used_they_are(conn, video):
    recipes_store.save(
        conn,
        make_recipe(
            "a",
            ingredients=[
                {"name": "common", "quantity": None, "unit": None},
                {"name": "rare", "quantity": None, "unit": None},
            ],
            source={"video_id": "vid1", "recipe_index": 1},
        ),
    )
    recipes_store.save(
        conn,
        make_recipe(
            "b",
            ingredients=[{"name": "common", "quantity": None, "unit": None}],
            source={"video_id": "vid1", "recipe_index": 2},
        ),
    )
    assert [r["name"] for r in products_store.pending_names(conn)] == ["common", "rare"]


def test_pending_skips_recorded_failures_unless_asked(conn, recipe):
    products_store.record_failure(conn, "salt", "no_match")
    assert "salt" not in [r["name"] for r in products_store.pending_names(conn)]
    assert "salt" in [
        r["name"] for r in products_store.pending_names(conn, retry_failed=True)
    ]


def test_a_mapped_name_is_not_pending(conn, recipe, product):
    products_store.map_name(conn, "salt", product)
    assert "salt" not in [r["name"] for r in products_store.pending_names(conn)]


def test_aliases_round_trip_normalised(conn):
    products_store.add_alias(conn, "  Self-Rising FLOUR ", "Mąka z Proszkiem")
    assert products_store.aliases(conn, "self-rising flour") == [
        {"query": "mąka z proszkiem", "locale": None}
    ]


# -------------------------------------------------------------- the join ----


def test_an_ingredient_resolves_through_the_name_mapping(conn, recipe, product):
    # This is what `products apply` used to do by rewriting 72 files.
    products_store.map_name(conn, "chicken breast", product)
    stored = recipes_store.get(conn, "test-recipe")
    assert stored["ingredients"][0]["fitatu_product_id"] == product
    assert stored["ingredients"][1]["fitatu_product_id"] is None


def test_the_mapping_reaches_every_recipe_using_the_name(conn, video, product):
    for i in (1, 2):
        recipes_store.save(
            conn,
            make_recipe(
                f"r{i}",
                ingredients=[{"name": "Salt", "quantity": None, "unit": None}],
                source={"video_id": "vid1", "recipe_index": i},
            ),
        )
    products_store.map_name(conn, "salt", product)
    for i in (1, 2):
        stored = recipes_store.get(conn, f"r{i}")
        assert stored["ingredients"][0]["fitatu_product_id"] == product


def test_an_override_beats_the_name_mapping(conn, video, product):
    products_store.save(
        conn,
        product_id="222",
        display_name="Something else",
        verified=True,
        measures=[{"measure_id": "1", "measure_name": "g", "weight_g": 1}],
    )
    products_store.map_name(conn, "salt", product)
    recipes_store.save(
        conn,
        make_recipe(
            ingredients=[
                {
                    "name": "Salt",
                    "quantity": None,
                    "unit": None,
                    "product_id_override": "222",
                }
            ]
        ),
    )
    ingredient = recipes_store.get(conn, "test-recipe")["ingredients"][0]
    assert ingredient["fitatu_product_id"] == "222"
    assert ingredient["match"]["override"] is True


def test_unmapping_a_name_unresolves_the_ingredient(conn, recipe, product):
    products_store.map_name(conn, "chicken breast", product)
    products_store.unmap_name(conn, "chicken breast")
    stored = recipes_store.get(conn, "test-recipe")
    assert stored["ingredients"][0]["fitatu_product_id"] is None


def test_a_mapped_product_cannot_be_deleted_out_from_under_it(conn, recipe, product):
    import sqlite3

    products_store.map_name(conn, "chicken breast", product)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM products WHERE product_id = ?", (product,))
