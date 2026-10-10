"""Tests for importing recipes into Fitatu.

Two things here could do real damage and both are covered deliberately.

Fitatu's `POST /recipes` is **not idempotent**, so a recipe sent twice appears
twice. `import_status: not_imported` is the only thing preventing that.

And a recipe imported with some ingredients missing would silently under-report
calories — the one failure mode nobody notices. So conversion refuses to guess,
and a recipe imports whole or not at all.
"""

import pytest

from recipe_library.fitatu_import.payload import (
    build_recipe_kwargs,
    measure_quantity,
)
from recipe_library.fitatu_import.runner import (
    STATUS_ERROR,
    STATUS_NOT_IMPORTED,
    STATUS_OK,
    extract_recipe_id,
    import_one,
    recipe_status,
    resolve_names,
    select_pending,
    summarise,
)
from recipe_library.store import products as products_store
from recipe_library.store import recipes as recipes_store

GRAM = {"measure_id": "g", "measure_name": "gram", "weight_g": 1}
PORTION = {"measure_id": "p-1", "measure_name": "porcja", "weight_g": 150}
MILLILITRE = {"measure_id": "ml-1", "measure_name": "ml", "weight_g": 1.03}
NO_WEIGHT = {"measure_id": "x", "measure_name": "opakowanie"}


def products(**entries):
    """Build a product cache mapping like state/fitatu_products.yaml holds."""
    return {
        name: {"fitatu_product_id": spec[0], "default_measure": spec[1]}
        for name, spec in entries.items()
    }


def recipe(ingredients, **overrides):
    data = {
        "id": "test",
        "title": "Test",
        "language": "pl",
        "servings": 2,
        "ingredients": ingredients,
        "source": {
            "video_id": "v",
            "url": "https://www.youtube.com/shorts/v",
            "channel": "Kanał",
            "recipe_index": 1,
        },
        "extracted_at": "2026-10-02",
    }
    data.update(overrides)
    return data


# Recipes now live in the database, so a test "writes" one by saving it. The
# video row has to exist first: a recipe references it.
_INDEX = iter(range(1, 10_000))


def write(conn, recipe_id, data):
    conn.execute(
        "INSERT INTO videos (video_id, title) VALUES ('v', 'V') "
        "ON CONFLICT (video_id) DO NOTHING"
    )
    data = dict(data, id=recipe_id)
    data["source"] = dict(data["source"], recipe_index=next(_INDEX))
    recipes_store.save(conn, data)
    return recipe_id


def seed_products(conn, **entries):
    """Store verified products and map ingredient names to them.

    Every product stored here is verified, because the table will not hold
    anything else -- which is the point of the constraint.
    """
    for name, (product_id, measure) in entries.items():
        products_store.save(
            conn,
            product_id=product_id,
            display_name=name,
            verified=True,
            measures=[measure],
            default_measure_id=str(measure["measure_id"]),
        )
        products_store.map_name(conn, name, product_id, method="manual")


# --- quantity conversion ----------------------------------------------------


def test_grams_pass_through_a_gram_measure():
    assert measure_quantity(500, "g", GRAM) == (500.0, None)


def test_grams_convert_into_portion_units():
    # 500 g of a 150 g portion is 3.333 portions, which is what Fitatu wants.
    value, reason = measure_quantity(500, "g", PORTION)
    assert reason is None
    assert value == pytest.approx(3.333, abs=0.001)


def test_millilitres_pass_through_an_ml_measure():
    assert measure_quantity(400, "ml", MILLILITRE) == (400.0, None)


def test_millilitres_are_refused_for_a_gram_measure():
    # ml -> g needs a density nobody stated. Oil is not water.
    value, reason = measure_quantity(400, "ml", GRAM)
    assert value is None
    assert "millilitre" in reason


def test_missing_quantity_is_refused():
    value, reason = measure_quantity(None, None, GRAM)
    assert value is None
    assert "no quantity" in reason


@pytest.mark.parametrize("unit", ["piece", "tbsp", "tsp", "cup", "pinch"])
def test_countable_and_spoon_units_are_refused_against_a_mass(unit):
    # "1 onion" has no mass. Guessing one would corrupt the nutrition data. Such
    # a unit only converts into a measure that means the same thing, which a
    # gram is not.
    value, reason = measure_quantity(1, unit, GRAM)
    assert value is None
    assert reason


def test_measure_without_a_gram_weight_is_refused():
    value, reason = measure_quantity(500, "g", NO_WEIGHT)
    assert value is None
    assert "no gram weight" in reason


def test_measure_without_an_id_is_refused():
    value, reason = measure_quantity(500, "g", {"weight_g": 1})
    assert value is None
    assert "measure id" in reason


def test_zero_and_negative_quantities_are_refused():
    assert measure_quantity(0, "g", GRAM)[0] is None
    assert measure_quantity(-5, "g", GRAM)[0] is None


def test_quantity_rounding_to_zero_is_refused():
    # 0.0001 g of a 1000 g measure would round to 0, which Fitatu rejects.
    value, reason = measure_quantity(0.0001, "g", {"measure_id": "m", "weight_g": 1000})
    assert value is None
    assert "rounds to zero" in reason


# --- building the payload ---------------------------------------------------


def test_complete_recipe_builds_create_arguments():
    built = build_recipe_kwargs(
        recipe([{"name": "mąka", "quantity": 500, "unit": "g"}]),
        products(mąka=("111", GRAM)),
    )
    assert built.ok
    assert built.kwargs["items"] == [
        {"type": "PRODUCT", "itemId": "111", "measureId": "g", "measureQuantity": 500.0}
    ]
    assert built.kwargs["serving"] == "2"
    assert built.kwargs["shared"] is False


def test_unmapped_ingredient_blocks_the_whole_recipe():
    # Importing the rest would make Fitatu under-report the recipe silently.
    built = build_recipe_kwargs(
        recipe(
            [
                {"name": "mąka", "quantity": 500, "unit": "g"},
                {"name": "cebula", "quantity": 1, "unit": "piece"},
            ]
        ),
        products(mąka=("111", GRAM)),
    )
    assert not built.ok
    assert any("cebula" in str(p) for p in built.problems)


def test_allow_partial_imports_what_converts_and_warns_loudly():
    built = build_recipe_kwargs(
        recipe(
            [
                {"name": "mąka", "quantity": 500, "unit": "g"},
                {"name": "cebula", "quantity": 1, "unit": "piece"},
            ]
        ),
        products(mąka=("111", GRAM)),
        allow_partial=True,
    )
    assert built.ok
    assert len(built.kwargs["items"]) == 1
    assert any("under-report" in w for w in built.warnings)


def test_recipe_with_nothing_convertible_never_imports_even_partially():
    # A recipe with zero items is not a recipe; --allow-partial must not force it.
    built = build_recipe_kwargs(
        recipe([{"name": "cebula", "quantity": 1, "unit": "piece"}]),
        products(cebula=("1", GRAM)),
        allow_partial=True,
    )
    assert not built.ok


def test_missing_servings_becomes_one_portion_with_a_warning():
    built = build_recipe_kwargs(
        recipe([{"name": "mąka", "quantity": 500, "unit": "g"}], servings=None),
        products(mąka=("111", GRAM)),
    )
    assert built.kwargs["serving"] == "1"
    assert any("single portion" in w for w in built.warnings)


def test_description_carries_the_source_video():
    # This library stores no preparation steps, so the field carries provenance.
    built = build_recipe_kwargs(
        recipe([{"name": "mąka", "quantity": 500, "unit": "g"}]),
        products(mąka=("111", GRAM)),
    )
    assert "youtube.com/shorts/v" in built.kwargs["recipe_description"]


def test_untitled_recipe_is_refused():
    built = build_recipe_kwargs(
        recipe([{"name": "mąka", "quantity": 500, "unit": "g"}], title="  "),
        products(mąka=("111", GRAM)),
    )
    assert not built.ok


def test_ingredient_names_match_case_insensitively():
    built = build_recipe_kwargs(
        recipe([{"name": "  MĄKA  ", "quantity": 100, "unit": "g"}]),
        products(mąka=("111", GRAM)),
    )
    assert built.ok


# --- status and duplicate protection ---------------------------------------


def test_absent_status_reads_as_not_imported():
    assert recipe_status({}) == STATUS_NOT_IMPORTED


def test_unknown_status_reads_as_not_imported():
    assert recipe_status({"import_status": "nonsense"}) == STATUS_NOT_IMPORTED


def test_already_imported_recipes_are_never_resent(conn):
    # The core duplicate guard: Fitatu's create is not idempotent.
    write(conn, "a", recipe([], import_status="ok", fitatu_recipe_id="9"))
    write(conn, "b", recipe([]))
    assert select_pending(conn) == ["b"]


def test_a_recorded_id_outranks_the_status(conn):
    # Editing import_status back to not_imported is an easy way to ask for a
    # duplicate. The id is the evidence that the recipe is already there.
    write(conn, "a", recipe([], import_status="ok", fitatu_recipe_id="9"))
    conn.execute("UPDATE recipes SET import_status = 'not_imported' WHERE id = 'a'")
    conn.commit()
    assert select_pending(conn) == []


def test_force_resends_imported_recipes(conn):
    write(conn, "a", recipe([], import_status="ok", fitatu_recipe_id="9"))
    assert select_pending(conn, force=True) == ["a"]


def test_failed_recipes_are_retried_by_default(conn):
    write(conn, "a", recipe([], import_status="error", import_error="boom"))
    assert select_pending(conn) == ["a"]


def test_skipping_errors_excludes_them(conn):
    write(conn, "a", recipe([], import_status="error", import_error="boom"))
    assert select_pending(conn, include_errors=False) == []


def test_summary_counts_every_status(conn):
    write(conn, "a", recipe([], import_status="ok", fitatu_recipe_id="1"))
    write(conn, "b", recipe([], import_status="error"))
    write(conn, "c", recipe([]))
    assert summarise(conn) == {"ok": 1, "error": 1, "not_imported": 1}


def test_names_are_accepted_with_or_without_the_old_yaml_suffix(conn):
    write(conn, "a", recipe([]))
    assert resolve_names(conn, ["a", "a.yaml"]) == (["a"], [])
    assert resolve_names(conn, ["nope"]) == ([], ["nope"])


def test_naming_a_recipe_does_not_lift_the_duplicate_gate(conn):
    # The duplicate a named import would create is the same duplicate.
    write(conn, "a", recipe([], import_status="ok", fitatu_recipe_id="9"))
    assert select_pending(conn, names=["a"]) == []


# --- importing one recipe ---------------------------------------------------


class FakeClient:
    def __init__(self, response=None, error=None):
        self.response = response if response is not None else {"id": 4242}
        self.error = error
        self.calls = []

    def create_recipe(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


def test_successful_import_records_ok_and_the_id(conn):
    write(conn, "a", recipe([{"name": "mąka", "quantity": 500, "unit": "g"}]))
    seed_products(conn, mąka=("111", GRAM))
    client = FakeClient()

    result = import_one(conn, "a", client, resolve=False)

    assert result["status"] == STATUS_OK
    data = recipes_store.get(conn, "a")
    assert data["import_status"] == "ok"
    assert data["fitatu_recipe_id"] == "4242"
    assert data["imported_at"]
    assert len(client.calls) == 1


def test_api_failure_records_error_with_the_reason(conn):
    write(conn, "a", recipe([{"name": "mąka", "quantity": 500, "unit": "g"}]))
    seed_products(conn, mąka=("111", GRAM))

    result = import_one(
        conn, "a", FakeClient(error=RuntimeError("503 upstream")), resolve=False
    )

    assert result["status"] == STATUS_ERROR
    data = recipes_store.get(conn, "a")
    assert data["import_status"] == "error"
    assert "503 upstream" in data["import_error"]
    assert "fitatu_recipe_id" not in data


def test_unconvertible_recipe_records_error_without_calling_fitatu(conn):
    write(conn, "a", recipe([{"name": "mąka", "quantity": None, "unit": None}]))
    seed_products(conn, mąka=("111", GRAM))
    client = FakeClient()

    result = import_one(conn, "a", client, resolve=False)

    assert result["status"] == STATUS_ERROR
    assert client.calls == []
    assert recipes_store.get(conn, "a")["import_status"] == "error"


def test_missing_id_in_response_is_an_error_not_a_silent_success(conn):
    # The recipe may well have been created, so a blind retry would duplicate
    # it. Flag it for a human instead.
    write(conn, "a", recipe([{"name": "mąka", "quantity": 500, "unit": "g"}]))
    seed_products(conn, mąka=("111", GRAM))

    result = import_one(conn, "a", FakeClient(response={"ok": True}), resolve=False)

    assert result["status"] == STATUS_ERROR
    assert "no recipe id" in recipes_store.get(conn, "a")["import_error"]


def test_dry_run_contacts_nothing_and_writes_nothing(conn):
    write(conn, "a", recipe([{"name": "mąka", "quantity": 500, "unit": "g"}]))
    seed_products(conn, mąka=("111", GRAM))
    client = FakeClient()

    result = import_one(conn, "a", client, dry_run=True, resolve=False)

    assert result["status"] == "would_import"
    assert client.calls == []
    assert recipes_store.get(conn, "a")["import_status"] == "not_imported"


def test_dry_run_does_not_record_errors_either(conn):
    write(conn, "a", recipe([{"name": "mąka", "quantity": None, "unit": None}]))
    seed_products(conn, mąka=("111", GRAM))

    result = import_one(conn, "a", None, dry_run=True, resolve=False)

    assert result["status"] == STATUS_ERROR
    assert recipes_store.get(conn, "a")["import_status"] == "not_imported"


def test_reimport_after_failure_clears_the_old_error(conn):
    write(
        conn,
        "a",
        recipe(
            [{"name": "mąka", "quantity": 500, "unit": "g"}],
            import_status="error",
            import_error="old failure",
        ),
    )
    seed_products(conn, mąka=("111", GRAM))

    import_one(conn, "a", FakeClient(), resolve=False)

    data = recipes_store.get(conn, "a")
    assert data["import_status"] == "ok"
    assert "import_error" not in data


def test_importing_a_missing_recipe_is_an_error_not_a_crash(conn):
    result = import_one(conn, "nope", FakeClient(), resolve=False)
    assert result["status"] == STATUS_ERROR
    assert "no such recipe" in result["detail"]


# --- resolution is the first phase of an import -----------------------------


def test_an_unresolved_name_fails_the_recipe_before_anything_is_sent(conn):
    from conftest import FakeFitatu

    write(conn, "a", recipe([{"name": "mąka", "quantity": 500, "unit": "g"}]))
    client = FakeFitatu({})  # Fitatu finds nothing

    result = import_one(conn, "a", client, resolve=True)

    assert result["status"] == STATUS_ERROR
    assert "unresolved ingredient" in result["detail"]
    assert "mąka" in result["detail"]
    assert client.created == []


def test_an_unverified_candidate_does_not_get_a_recipe_imported(conn):
    from conftest import FakeFitatu, search_row

    write(conn, "a", recipe([{"name": "mąka", "quantity": 500, "unit": "g"}]))
    client = FakeFitatu({"mąka": [search_row("1", "Mąka", verified=False)]})

    result = import_one(conn, "a", client, resolve=True)

    assert result["status"] == STATUS_ERROR
    assert client.created == []
    assert products_store.get(conn, "1") is None


def test_resolution_feeds_straight_into_the_payload(conn):
    from conftest import FakeFitatu, search_row

    write(conn, "a", recipe([{"name": "mąka", "quantity": 500, "unit": "g"}]))
    client = FakeFitatu({"mąka": [search_row("24", "Mąka")]})

    result = import_one(conn, "a", client, resolve=True)

    assert result["status"] == STATUS_OK
    # 500 g against the 1 g measure the fake product carries.
    item = client.created[0]["items"][0]
    assert item["itemId"] == "24"
    assert item["measureQuantity"] == 500


# --- offset and limit windowing ---------------------------------------------


@pytest.fixture
def collection(conn):
    """Five pending recipes, with a product already mapped for each."""
    for name in ("a", "b", "c", "d", "e"):
        write(conn, name, recipe([{"name": "mąka", "quantity": 100, "unit": "g"}]))
    seed_products(conn, mąka=("111", GRAM))
    return conn


def test_offset_skips_the_first_pending_recipes(collection):
    from recipe_library.fitatu_import.runner import run

    tally = run(collection, offset=2, dry_run=True, resolve=False)
    assert tally["would_import"] == 3


def test_offset_and_limit_select_a_window(collection):
    from recipe_library.fitatu_import.runner import run

    tally = run(collection, offset=1, limit=2, dry_run=True, resolve=False)
    assert tally["would_import"] == 2


def test_offset_zero_is_the_whole_list(collection):
    from recipe_library.fitatu_import.runner import run

    tally = run(collection, offset=0, dry_run=True, resolve=False)
    assert tally["would_import"] == 5


def test_offset_past_the_end_imports_nothing(collection, capsys):
    from recipe_library.fitatu_import.runner import run

    tally = run(collection, offset=99, dry_run=True, resolve=False)
    assert tally["would_import"] == 0
    assert "past the end" in capsys.readouterr().out


def test_negative_offset_is_rejected(collection):
    from recipe_library.fitatu_import.runner import run

    with pytest.raises(SystemExit):
        run(collection, offset=-1, dry_run=True)


def test_zero_limit_is_rejected(collection):
    from recipe_library.fitatu_import.runner import run

    with pytest.raises(SystemExit):
        run(collection, limit=0, dry_run=True)


def test_an_unknown_recipe_name_is_refused_before_anything_is_sent(collection):
    # A typo that silently imported nothing would look exactly like a recipe
    # that had already been imported.
    from recipe_library.fitatu_import.runner import run

    with pytest.raises(SystemExit):
        run(collection, names=["nope"], dry_run=True)


def test_offset_applies_to_pending_not_to_the_whole_collection(conn):
    """The subtlety worth pinning: already-imported recipes are not in the window.

    So --offset 1 skips the first *pending* recipe, not the first recipe by id.
    """
    from recipe_library.fitatu_import.runner import run

    write(conn, "a", recipe([], import_status="ok", fitatu_recipe_id="1"))
    for name in ("b", "c"):
        write(conn, name, recipe([{"name": "mąka", "quantity": 100, "unit": "g"}]))
    seed_products(conn, mąka=("111", GRAM))

    tally = run(conn, offset=1, dry_run=True, resolve=False)
    assert tally["would_import"] == 1  # only c: a is done, b was skipped by the offset


# --- units that mean the same thing on both sides ---------------------------
#
# These are the conversions that are exact rather than guessed: a count into a
# per-item measure, and a spoon or cup into another spoon or cup. The weight per
# unit still comes from the product, so no density is ever assumed.

PIECE = {"measure_id": "46", "measure_name": "piece", "weight_g": 100}
CLOVE = {"measure_id": "25", "measure_name": "clove", "weight_g": 5}
TEASPOON = {"measure_id": "19", "measure_name": "teaspoon", "weight_g": 5}
TABLESPOON = {"measure_id": "20", "measure_name": "tablespoon", "weight_g": 10}
GLASS = {"measure_id": "32", "measure_name": "glass", "weight_g": 230}
PINCH = {"measure_id": "14", "measure_name": "pinch", "weight_g": 1}
PER_ITEM_PORTION = {
    "measure_id": "39",
    "measure_name": "portion",
    "weight_g": 57,
    "per_item": True,
}


@pytest.mark.parametrize("measure", [PIECE, CLOVE])
def test_a_count_carries_into_a_per_item_measure(measure):
    assert measure_quantity(3, "piece", measure) == (3.0, None)


def test_a_count_needs_the_measure_to_be_one_item():
    value, reason = measure_quantity(2, "piece", PORTION)
    assert value is None
    assert "not one item" in reason


def test_a_portion_can_be_declared_one_item():
    assert measure_quantity(14, "piece", PER_ITEM_PORTION) == (14.0, None)


def test_a_spoon_matches_the_same_spoon():
    assert measure_quantity(2, "tsp", TEASPOON) == (2.0, None)
    assert measure_quantity(1.5, "tbsp", TABLESPOON) == (1.5, None)


def test_spoon_sizes_are_never_scaled_into_each_other():
    # Fitatu's per-spoon weights are kitchen figures, not volumetric: the same
    # product can list a 10 g teaspoon and a 20 g tablespoon. Multiplying one
    # into the other would inject that inconsistency into the nutrition data.
    value, reason = measure_quantity(1, "tbsp", TEASPOON)
    assert value is None
    assert "resolve a product that has a tbsp" in reason

    value, reason = measure_quantity(1, "cup", TABLESPOON)
    assert value is None
    assert "resolve a product that has a cup" in reason


def test_a_cup_is_close_enough_to_a_polish_glass():
    assert measure_quantity(1, "cup", GLASS) == (0.96, None)


def test_a_spoon_will_not_become_a_mass():
    value, reason = measure_quantity(2, "tbsp", GRAM)
    assert value is None
    assert "resolve a product that has a tbsp" in reason


def test_a_pinch_only_matches_a_pinch():
    assert measure_quantity(1, "pinch", PINCH) == (1.0, None)
    value, reason = measure_quantity(1, "pinch", TEASPOON)
    assert value is None
    assert "no defined volume" in reason


# --- tag and preparation time -----------------------------------------------


def test_a_custom_tag_goes_into_the_description_not_the_tags_field():
    # Fitatu answers 500 for a tag outside its own vocabulary, so the field is
    # left alone and the description carries the marker instead.
    built = build_recipe_kwargs(
        recipe([{"name": "mleko", "quantity": 100, "unit": "g"}]),
        products(mleko=("1", GRAM)),
        tags=["AI-YT-shorts"],
    )
    assert built.ok
    assert "tags" not in built.kwargs
    assert "#AI-YT-shorts" in built.kwargs["recipe_description"]


def test_prep_time_is_passed_as_cooking_time():
    built = build_recipe_kwargs(
        recipe(
            [{"name": "mleko", "quantity": 100, "unit": "g"}], prep_time_minutes=25
        ),
        products(mleko=("1", GRAM)),
    )
    assert built.kwargs["cooking_time"] == 25


def test_a_nonsense_prep_time_is_dropped_with_a_warning():
    built = build_recipe_kwargs(
        recipe(
            [{"name": "mleko", "quantity": 100, "unit": "g"}], prep_time_minutes="soon"
        ),
        products(mleko=("1", GRAM)),
    )
    assert "cooking_time" not in built.kwargs
    assert any("prep_time_minutes" in w for w in built.warnings)


# --- picking the measure that matches the recipe ----------------------------


def test_the_measure_matching_the_recipe_unit_is_chosen():
    from recipe_library.fitatu_import.payload import select_measure

    entry = {
        "default_measure": {"measure_id": "20", "measure_name": "łyżka", "weight_g": 20},
        "measures": [
            {"measure_id": "20", "measure_name": "łyżka", "weight_g": 20, "kcal": 72.6},
            {"measure_id": "19", "measure_name": "łyżeczka", "weight_g": 10, "kcal": 36.3},
            {"measure_id": "1", "measure_name": "g", "weight_g": 1, "kcal": 3.63},
        ],
    }
    # The same ingredient, written three ways, resolves to three real measures
    # instead of being scaled from one of them.
    assert select_measure(entry, "tbsp")["measure_id"] == "20"
    assert select_measure(entry, "tsp")["measure_id"] == "19"
    assert select_measure(entry, "g")["measure_id"] == "1"


def test_measure_selection_falls_back_to_the_default():
    from recipe_library.fitatu_import.payload import select_measure

    entry = {"default_measure": {"measure_id": "2", "weight_g": 200}, "measures": []}
    assert select_measure(entry, "cup")["measure_id"] == "2"


def test_a_recipe_using_two_units_of_one_ingredient_still_converts():
    built = build_recipe_kwargs(
        recipe(
            [
                {"name": "czosnek w proszku", "quantity": 1, "unit": "tbsp"},
                {"name": "czosnek w proszku", "quantity": 2, "unit": "tsp"},
            ]
        ),
        {
            "czosnek w proszku": {
                "fitatu_product_id": "7",
                "default_measure": {"measure_id": "20", "weight_g": 20},
                "measures": [
                    {"measure_id": "20", "weight_g": 20},
                    {"measure_id": "19", "weight_g": 10},
                ],
            }
        },
    )
    assert built.ok
    assert [i["measureId"] for i in built.kwargs["items"]] == ["20", "19"]
    assert [i["measureQuantity"] for i in built.kwargs["items"]] == [1.0, 2.0]


# --- importing one named recipe ---------------------------------------------


def test_naming_a_recipe_narrows_the_pending_list(conn):
    write(conn, "a", recipe([]))
    write(conn, "b", recipe([]))
    write(conn, "c", recipe([]))

    assert select_pending(conn) == ["a", "b", "c"]
    assert select_pending(conn, names=["b"]) == ["b"]
    # The caller's order is kept, and the old .yaml suffix is tolerated.
    known, unknown = resolve_names(conn, ["c.yaml", "a"])
    assert known == ["c", "a"]
    assert unknown == []


def test_naming_an_already_imported_recipe_does_not_lift_the_gate(conn):
    # The point of `ok` is that re-sending creates a second Fitatu recipe.
    # Asking for one by name must not become a way around that.
    write(conn, "done", recipe([], import_status=STATUS_OK, fitatu_recipe_id="1"))
    assert select_pending(conn, names=["done"]) == []
    assert select_pending(conn, names=["done"], force=True) == ["done"]


def test_naming_an_already_imported_recipe_says_so(conn, capsys):
    from recipe_library.fitatu_import.runner import run

    write(conn, "done", recipe([], import_status=STATUS_OK, fitatu_recipe_id="1"))
    tally = run(conn, names=["done"], dry_run=True)
    assert tally == {STATUS_OK: 0, STATUS_ERROR: 0, "would_import": 0}
    out = capsys.readouterr().out
    assert "already has import_status 'ok'" in out
    assert "--force" in out


def test_one_named_recipe_builds_only_that_one(conn, capsys):
    from recipe_library.fitatu_import.runner import run

    ing = [{"name": "mąka", "quantity": 100, "unit": "g"}]
    write(conn, "wanted", recipe(ing))
    write(conn, "other", recipe(ing))
    seed_products(conn, mąka=("111", GRAM))

    run(conn, names=["wanted"], dry_run=True, resolve=False)
    out = capsys.readouterr().out
    assert "wanted" in out
    assert "other" not in out


# --- volume through millilitres ---------------------------------------------

ML = {"measure_id": "17", "measure_name": "ml", "weight_g": 1}


def test_spoons_and_cups_convert_into_a_millilitre_measure():
    # A tablespoon is 15 ml by definition, and a millilitre measure carries no
    # spoon convention, so this adds no assumption of its own.
    assert measure_quantity(1, "tbsp", ML) == (15.0, None)
    assert measure_quantity(2, "tsp", ML) == (10.0, None)
    assert measure_quantity(0.25, "cup", ML) == (60.0, None)


def test_a_recipe_in_millilitres_converts_into_a_glass():
    # 1 L against a 250 ml glass is 4 glasses; the product's own per-glass weight
    # then does the rest.
    assert measure_quantity(1000, "ml", GLASS) == (4.0, None)


def test_volume_still_refuses_a_mass_measure():
    value, reason = measure_quantity(1, "tbsp", GRAM)
    assert value is None
    assert "millilitre measure" in reason


def test_a_millilitre_measure_is_preferred_when_no_exact_one_exists():
    from recipe_library.fitatu_import.payload import select_measure

    entry = {
        "default_measure": {"measure_id": "39", "measure_name": "porcja", "weight_g": 100},
        "measures": [
            {"measure_id": "39", "weight_g": 100, "kcal": 824},
            {"measure_id": "1", "weight_g": 1, "kcal": 8.24},
            {"measure_id": "17", "weight_g": 1, "kcal": 8.24},
        ],
    }
    assert select_measure(entry, "tbsp")["measure_id"] == "17"


def test_a_recorded_fitatu_id_outranks_a_hand_edited_status(conn):
    # Setting import_status back to not_imported by hand is an easy way to ask
    # for a duplicate of every recipe already in Fitatu. The id is the evidence
    # that it is there, so it wins.
    write(
        conn,
        "already",
        recipe([], import_status=STATUS_NOT_IMPORTED, fitatu_recipe_id="176264367"),
    )
    assert select_pending(conn) == []
    assert select_pending(conn, names=["already"]) == []
    # --force is still the deliberate way through.
    assert select_pending(conn, force=True) == ["already"]


def test_an_error_with_an_id_is_not_retried_either(conn):
    # `error` after a successful create -- Fitatu answered without an id, say --
    # must not be retried blindly, because the recipe may well be there.
    write(conn, "half", recipe([], import_status=STATUS_ERROR, fitatu_recipe_id="999"))
    assert select_pending(conn) == []


def test_a_successful_import_records_which_account_it_landed_on(conn):
    # The same library imported under two accounts leaves two disjoint sets, and
    # an id alone cannot tell them apart.
    class Client:
        def create_recipe(self, **kwargs):
            return {"id": "4242"}

    write(conn, "r", recipe([{"name": "mąka", "quantity": 100, "unit": "g"}]))
    seed_products(conn, mąka=("1", GRAM))

    import_one(conn, "r", Client(), account="56743546", resolve=False)

    data = recipes_store.get(conn, "r")
    assert data["fitatu_recipe_id"] == "4242"
    assert data["fitatu_account_id"] == "56743546"


def test_a_second_import_under_another_account_files_the_first(conn):
    class Client:
        def __init__(self, new_id):
            self.new_id = new_id

        def create_recipe(self, **kwargs):
            return {"id": self.new_id}

    write(conn, "r", recipe([{"name": "mąka", "quantity": 100, "unit": "g"}]))
    seed_products(conn, mąka=("1", GRAM))

    # import_one itself has no duplicate gate -- that lives in select_pending
    # and in the API -- so this is what --force ends up doing.
    import_one(conn, "r", Client("100"), account="acct-1", resolve=False)
    import_one(conn, "r", Client("200"), account="acct-2", resolve=False)

    data = recipes_store.get(conn, "r")
    assert data["fitatu_recipe_id"] == "200"
    assert data["previous_imports"][0]["fitatu_recipe_id"] == "100"
    assert data["previous_imports"][0]["fitatu_account_id"] == "acct-1"
