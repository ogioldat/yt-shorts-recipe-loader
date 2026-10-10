"""Tests for importing recipes into Fitatu.

Two things here could do real damage and both are covered deliberately.

Fitatu's `POST /recipes` is **not idempotent**, so a recipe sent twice appears
twice. `import_status: not_imported` is the only thing preventing that.

And a recipe imported with some ingredients missing would silently under-report
calories — the one failure mode nobody notices. So conversion refuses to guess,
and a recipe imports whole or not at all.
"""

import pytest
import yaml

from recipe_library.fitatu_import.payload import (
    build_recipe_kwargs,
    measure_quantity,
)
from recipe_library.fitatu_import.runner import (
    STATUS_ERROR,
    read_recipe,
    STATUS_NOT_IMPORTED,
    STATUS_OK,
    extract_recipe_id,
    import_one,
    recipe_status,
    select_pending,
    summarise,
)

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


def write(path, data):
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


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


def test_already_imported_recipes_are_never_resent(tmp_path):
    # The core duplicate guard: Fitatu's create is not idempotent.
    write(tmp_path / "a.yaml", recipe([], import_status="ok", fitatu_recipe_id="9"))
    write(tmp_path / "b.yaml", recipe([]))
    assert [p.stem for p in select_pending(tmp_path)] == ["b"]


def test_force_resends_imported_recipes(tmp_path):
    write(tmp_path / "a.yaml", recipe([], import_status="ok", fitatu_recipe_id="9"))
    assert [p.stem for p in select_pending(tmp_path, force=True)] == ["a"]


def test_failed_recipes_are_retried_by_default(tmp_path):
    write(tmp_path / "a.yaml", recipe([], import_status="error", import_error="boom"))
    assert [p.stem for p in select_pending(tmp_path)] == ["a"]


def test_skipping_errors_excludes_them(tmp_path):
    write(tmp_path / "a.yaml", recipe([], import_status="error", import_error="boom"))
    assert select_pending(tmp_path, include_errors=False) == []


def test_summary_counts_every_status(tmp_path):
    write(tmp_path / "a.yaml", recipe([], import_status="ok", fitatu_recipe_id="1"))
    write(tmp_path / "b.yaml", recipe([], import_status="error"))
    write(tmp_path / "c.yaml", recipe([]))
    assert summarise(tmp_path) == {"ok": 1, "error": 1, "not_imported": 1}


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


def test_successful_import_records_ok_and_the_id(tmp_path):
    path = tmp_path / "a.yaml"
    write(path, recipe([{"name": "mąka", "quantity": 500, "unit": "g"}]))
    client = FakeClient()

    result = import_one(path, client, products(mąka=("111", GRAM)))

    assert result["status"] == STATUS_OK
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["import_status"] == "ok"
    assert data["fitatu_recipe_id"] == "4242"
    assert data["imported_at"]
    assert len(client.calls) == 1


def test_api_failure_records_error_with_the_reason(tmp_path):
    path = tmp_path / "a.yaml"
    write(path, recipe([{"name": "mąka", "quantity": 500, "unit": "g"}]))

    result = import_one(path, FakeClient(error=RuntimeError("503 upstream")), products(mąka=("111", GRAM)))

    assert result["status"] == STATUS_ERROR
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["import_status"] == "error"
    assert "503 upstream" in data["import_error"]
    assert "fitatu_recipe_id" not in data


def test_unconvertible_recipe_records_error_without_calling_fitatu(tmp_path):
    path = tmp_path / "a.yaml"
    write(path, recipe([{"name": "cebula", "quantity": 1, "unit": "piece"}]))
    client = FakeClient()

    result = import_one(path, client, products(cebula=("1", GRAM)))

    assert result["status"] == STATUS_ERROR
    assert client.calls == []  # never hit the network for a recipe we cannot build


def test_missing_id_in_response_is_an_error_not_a_silent_success(tmp_path):
    """The recipe may well have been created, so it must not be retried blindly.

    A blind retry would duplicate it; flagging it for a human will not.
    """
    path = tmp_path / "a.yaml"
    write(path, recipe([{"name": "mąka", "quantity": 500, "unit": "g"}]))

    result = import_one(path, FakeClient(response={"status": "created"}), products(mąka=("111", GRAM)))

    assert result["status"] == STATUS_ERROR
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert "no recipe id" in data["import_error"]


def test_dry_run_contacts_nothing_and_writes_nothing(tmp_path):
    path = tmp_path / "a.yaml"
    write(path, recipe([{"name": "mąka", "quantity": 500, "unit": "g"}]))
    before = path.read_text(encoding="utf-8")

    result = import_one(path, None, products(mąka=("111", GRAM)), dry_run=True)

    assert result["status"] == "would_import"
    assert path.read_text(encoding="utf-8") == before


def test_dry_run_does_not_record_errors_either(tmp_path):
    path = tmp_path / "a.yaml"
    write(path, recipe([{"name": "cebula", "quantity": 1, "unit": "piece"}]))
    before = path.read_text(encoding="utf-8")

    import_one(path, None, {}, dry_run=True)
    assert path.read_text(encoding="utf-8") == before


def test_reimport_after_failure_clears_the_old_error(tmp_path):
    path = tmp_path / "a.yaml"
    write(
        path,
        recipe(
            [{"name": "mąka", "quantity": 500, "unit": "g"}],
            import_status="error",
            import_error="old failure",
        ),
    )

    import_one(path, FakeClient(), products(mąka=("111", GRAM)))

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["import_status"] == "ok"
    assert "import_error" not in data


# --- response id extraction -------------------------------------------------


@pytest.mark.parametrize(
    "response,expected",
    [
        ({"id": 7}, "7"),
        ({"recipeId": "abc"}, "abc"),
        ({"recipe_id": 9}, "9"),
        ({"other": 1}, None),
        ({}, None),
        ("not a dict", None),
    ],
)
def test_recipe_id_extraction(response, expected):
    assert extract_recipe_id(response) == expected


# --- offset and limit windowing ---------------------------------------------


@pytest.fixture
def collection(tmp_path, monkeypatch):
    """Five pending recipes, with the network and the product cache stubbed out."""
    from recipe_library.fitatu_import import runner

    for name in ("a", "b", "c", "d", "e"):
        write(tmp_path / f"{name}.yaml", recipe([{"name": "mąka", "quantity": 100, "unit": "g"}]))

    monkeypatch.setattr(runner, "load_cache", lambda: {"products": products(mąka=("111", GRAM))})
    monkeypatch.setattr(runner, "RECIPES", tmp_path)
    return tmp_path


def imported(tmp_path):
    """Which recipes a run actually touched, in order."""
    return sorted(
        p.stem
        for p in tmp_path.glob("*.yaml")
        if yaml.safe_load(p.read_text(encoding="utf-8")).get("import_status") == "ok"
    )


def test_offset_skips_the_first_pending_recipes(collection):
    from recipe_library.fitatu_import.runner import run

    run(recipes_dir=collection, offset=2, dry_run=True)
    # A dry run writes nothing, so assert on the tally instead.
    tally = run(recipes_dir=collection, offset=2, dry_run=True)
    assert tally["would_import"] == 3  # c, d, e


def test_offset_and_limit_select_a_window(collection):
    from recipe_library.fitatu_import.runner import run

    tally = run(recipes_dir=collection, offset=1, limit=2, dry_run=True)
    assert tally["would_import"] == 2


def test_offset_zero_is_the_whole_list(collection):
    from recipe_library.fitatu_import.runner import run

    assert run(recipes_dir=collection, offset=0, dry_run=True)["would_import"] == 5


def test_offset_past_the_end_imports_nothing(collection, capsys):
    from recipe_library.fitatu_import.runner import run

    tally = run(recipes_dir=collection, offset=99, dry_run=True)
    assert tally["would_import"] == 0
    assert "past the end" in capsys.readouterr().out


def test_negative_offset_is_rejected(collection):
    from recipe_library.fitatu_import.runner import run

    with pytest.raises(SystemExit):
        run(recipes_dir=collection, offset=-1, dry_run=True)


def test_zero_limit_is_rejected(collection):
    # Silently importing nothing would look like success.
    from recipe_library.fitatu_import.runner import run

    with pytest.raises(SystemExit):
        run(recipes_dir=collection, limit=0, dry_run=True)


def test_offset_applies_to_pending_not_to_the_whole_collection(tmp_path, monkeypatch):
    """The subtlety worth pinning: already-imported recipes are not in the window.

    So --offset 1 skips the first *pending* recipe, not the first file on disk.
    """
    from recipe_library.fitatu_import import runner

    write(tmp_path / "a.yaml", recipe([], import_status="ok", fitatu_recipe_id="1"))
    for name in ("b", "c"):
        write(tmp_path / f"{name}.yaml", recipe([{"name": "mąka", "quantity": 100, "unit": "g"}]))

    monkeypatch.setattr(runner, "load_cache", lambda: {"products": products(mąka=("111", GRAM))})

    tally = runner.run(recipes_dir=tmp_path, offset=1, dry_run=True)
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


def test_naming_a_recipe_narrows_the_pending_list(tmp_path):
    from recipe_library.fitatu_import.runner import resolve_names

    write(tmp_path / "a.yaml", recipe([]))
    write(tmp_path / "b.yaml", recipe([]))
    write(tmp_path / "c.yaml", recipe([]))

    assert [p.stem for p in select_pending(tmp_path)] == ["a", "b", "c"]
    assert [p.stem for p in select_pending(tmp_path, names=["b"])] == ["b"]
    # the caller's order is kept, and the .yaml suffix is tolerated
    paths, unknown = resolve_names(["c.yaml", "a"], tmp_path)
    assert [p.stem for p in paths] == ["c", "a"]
    assert unknown == []


def test_naming_a_recipe_does_not_lift_the_duplicate_gate(tmp_path):
    # The point of `ok` is that re-sending creates a second Fitatu recipe. Asking
    # for one by name must not become a way around that.
    write(tmp_path / "done.yaml", recipe([], import_status=STATUS_OK))
    assert select_pending(tmp_path, names=["done"]) == []
    assert [p.stem for p in select_pending(tmp_path, names=["done"], force=True)] == ["done"]


def test_an_unknown_recipe_name_is_refused_before_anything_is_sent(tmp_path):
    from recipe_library.fitatu_import.runner import run

    write(tmp_path / "a.yaml", recipe([{"name": "mąka", "quantity": 1, "unit": "g"}]))
    with pytest.raises(SystemExit) as excinfo:
        run(recipes_dir=tmp_path, names=["typo"], dry_run=True)
    assert "typo" in str(excinfo.value)


def test_naming_an_already_imported_recipe_says_so(tmp_path, capsys):
    from recipe_library.fitatu_import.runner import run

    write(tmp_path / "done.yaml", recipe([], import_status=STATUS_OK))
    tally = run(recipes_dir=tmp_path, names=["done"], dry_run=True)
    assert tally == {STATUS_OK: 0, STATUS_ERROR: 0, "would_import": 0}
    out = capsys.readouterr().out
    assert "already has import_status 'ok'" in out
    assert "--force" in out


def test_one_named_recipe_builds_only_that_one(tmp_path, capsys):
    from recipe_library.fitatu_import.runner import run

    ing = [{"name": "mąka", "quantity": 100, "unit": "g"}]
    write(tmp_path / "wanted.yaml", recipe(ing))
    write(tmp_path / "other.yaml", recipe(ing))
    run(recipes_dir=tmp_path, names=["wanted"], dry_run=True)
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


def test_a_recorded_fitatu_id_outranks_a_hand_edited_status(tmp_path):
    # Setting import_status back to not_imported by hand is an easy way to ask
    # for a duplicate of every recipe already in Fitatu. The id is the evidence
    # that it is there, so it wins.
    write(
        tmp_path / "already.yaml",
        recipe([], import_status=STATUS_NOT_IMPORTED, fitatu_recipe_id="176264367"),
    )
    assert select_pending(tmp_path) == []
    assert select_pending(tmp_path, names=["already"]) == []
    # --force is still the deliberate way through
    assert [p.stem for p in select_pending(tmp_path, force=True)] == ["already"]


def test_an_error_with_an_id_is_not_retried_either(tmp_path):
    # `error` after a successful create -- Fitatu answered without an id, say --
    # must not be retried blindly, because the recipe may well be there.
    write(
        tmp_path / "half.yaml",
        recipe([], import_status=STATUS_ERROR, fitatu_recipe_id="999"),
    )
    assert select_pending(tmp_path) == []


def test_a_successful_import_records_which_account_it_landed_on(tmp_path):
    # The same library imported under two accounts leaves two disjoint sets, and
    # an id alone cannot tell them apart.
    class Client:
        def create_recipe(self, **kwargs):
            return {"id": "4242"}

    path = tmp_path / "r.yaml"
    write(path, recipe([{"name": "mąka", "quantity": 100, "unit": "g"}]))
    import_one(
        path,
        Client(),
        products(mąka=("1", GRAM)),
        account="56743546",
    )
    data = read_recipe(path)
    assert data["fitatu_recipe_id"] == "4242"
    assert data["fitatu_account_id"] == "56743546"
