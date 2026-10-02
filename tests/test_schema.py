import copy

import pytest

from recipe_library.schema import validate_recipe

VALID = {
    "id": "kurczak-curry-kokosowe",
    "title": "Kurczak w sosie curry z mlekiem kokosowym",
    "language": "pl",
    "servings": 4,
    "ingredients": [
        {"name": "pierś z kurczaka", "quantity": 500, "unit": "g"},
        {"name": "cebula", "quantity": 1, "unit": "piece"},
        {"name": "curry w proszku", "quantity": None, "unit": None, "note": "do smaku"},
    ],
    "source": {
        "video_id": "dQw4w9WgXcQ",
        "url": "https://www.youtube.com/shorts/dQw4w9WgXcQ",
        "channel": "Some Channel",
        "recipe_index": 1,
        "extracted_from": ["description", "subtitles"],
    },
    "extracted_at": "2026-10-02",
}


def recipe(**overrides):
    data = copy.deepcopy(VALID)
    data.update(overrides)
    return data


def test_valid_recipe_has_no_errors():
    assert validate_recipe(VALID) == []


def test_unquantified_ingredient_is_valid():
    # "do smaku" / "to taste" is a real state, not a defect. Allowing it is
    # what makes the never-guess-a-quantity rule possible.
    data = recipe(ingredients=[{"name": "sól", "quantity": None, "unit": None}])
    assert validate_recipe(data) == []


def test_quantity_without_unit_is_rejected():
    data = recipe(ingredients=[{"name": "cebula", "quantity": 1, "unit": None}])
    assert any("no unit" in e for e in validate_recipe(data))


def test_string_quantity_is_rejected():
    # A model writing "500" or "about 500" instead of 500 is the likely failure.
    data = recipe(ingredients=[{"name": "mąka", "quantity": "500", "unit": "g"}])
    assert any("must be a number" in e for e in validate_recipe(data))


def test_boolean_quantity_is_rejected():
    data = recipe(ingredients=[{"name": "mąka", "quantity": True, "unit": "g"}])
    assert any("must be a number" in e for e in validate_recipe(data))


def test_unknown_unit_is_rejected():
    data = recipe(ingredients=[{"name": "mleko", "quantity": 1, "unit": "szklanka"}])
    assert any("not in" in e for e in validate_recipe(data))


def test_id_must_match_filename():
    errors = validate_recipe(VALID, expected_id="something-else")
    assert any("does not match filename" in e for e in errors)


@pytest.mark.parametrize("field", ["id", "title", "language", "ingredients", "source"])
def test_missing_required_fields_are_reported(field):
    data = recipe(**{field: None})
    assert any(field in e for e in validate_recipe(data))


def test_empty_ingredient_list_is_rejected():
    # A recipe with no ingredients is not a recipe; it should have been
    # recorded as no_recipe_found instead.
    assert validate_recipe(recipe(ingredients=[])) != []


def test_recipe_index_must_be_positive_integer():
    data = copy.deepcopy(VALID)
    data["source"]["recipe_index"] = 0
    assert any("recipe_index" in e for e in validate_recipe(data))


def test_non_https_source_url_is_rejected():
    data = copy.deepcopy(VALID)
    data["source"]["url"] = "http://youtube.com/shorts/x"
    assert any("https" in e for e in validate_recipe(data))


def test_unknown_provenance_value_is_rejected():
    data = copy.deepcopy(VALID)
    data["source"]["extracted_from"] = ["description", "vibes"]
    assert any("unknown values" in e for e in validate_recipe(data))


def test_non_mapping_input_is_reported_not_crashed():
    assert validate_recipe("not a recipe") == ["top level is not a mapping"]


def test_fitatu_product_id_is_optional():
    # Not yet mapped is a normal state, not an error.
    assert validate_recipe(VALID) == []


def test_valid_fitatu_product_id_is_accepted():
    data = recipe(
        ingredients=[{"name": "cebula", "quantity": 1, "unit": "piece", "fitatu_product_id": "123"}]
    )
    assert validate_recipe(data) == []


def test_non_string_fitatu_product_id_is_rejected():
    # Fitatu ids are opaque strings; a number here means something coerced them.
    data = recipe(ingredients=[{"name": "cebula", "quantity": 1, "unit": "piece", "fitatu_product_id": 123}])
    assert any("fitatu_product_id" in e for e in validate_recipe(data))


def test_blank_fitatu_product_id_is_rejected():
    data = recipe(ingredients=[{"name": "cebula", "quantity": 1, "unit": "piece", "fitatu_product_id": "  "}])
    assert any("fitatu_product_id" in e for e in validate_recipe(data))
