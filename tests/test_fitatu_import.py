"""Tests for importing recipes into Fitatu.

Two things here could do real damage and both are covered deliberately.

Fitatu's `POST /recipes` is **not idempotent**, so a recipe sent twice appears
twice. `import_status: ok` is the only thing preventing that.

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
def test_countable_and_spoon_units_are_refused(unit):
    # "1 onion" has no mass. Guessing one would corrupt the nutrition data.
    value, reason = measure_quantity(1, unit, GRAM)
    assert value is None
    assert "no mass" in reason


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
