"""Tests for the Fitatu product id mapping.

The cache exists so that choosing a product happens once per ingredient name
rather than once per recipe, and so that everything downstream of that choice is
deterministic. These tests pin both properties.
"""

import yaml

from recipe_library.products import (
    apply_ids,
    ingredient_names,
    load_cache,
    normalize_name,
    pending_names,
    record_product,
    record_unresolved,
    save_cache,
)


def write_recipe(recipes_dir, recipe_id, ingredients):
    recipes_dir.mkdir(parents=True, exist_ok=True)
    (recipes_dir / f"{recipe_id}.yaml").write_text(
        yaml.safe_dump(
            {
                "id": recipe_id,
                "title": recipe_id,
                "language": "pl",
                "ingredients": ingredients,
                "source": {
                    "video_id": "v",
                    "url": "https://www.youtube.com/shorts/v",
                    "recipe_index": 1,
                },
                "extracted_at": "2026-10-02",
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )


# --- normalisation ----------------------------------------------------------


def test_case_and_spacing_are_normalised():
    assert normalize_name("  Pierś   z Kurczaka ") == "pierś z kurczaka"


def test_polish_diacritics_are_preserved():
    # The key doubles as the search_food query, and Fitatu's locale is pl_PL.
    assert normalize_name("Łosoś") == "łosoś"


def test_empty_name_normalises_to_empty():
    assert normalize_name("") == ""
    assert normalize_name(None) == ""


# --- cache io ---------------------------------------------------------------


def test_missing_cache_loads_as_empty(tmp_path):
    cache = load_cache(tmp_path / "nope.yaml")
    assert cache["products"] == {}
    assert cache["unresolved"] == {}


def test_cache_round_trip_preserves_polish(tmp_path):
    path = tmp_path / "c.yaml"
    cache = load_cache(path)
    record_product(cache, "Pierś z kurczaka", product_id="123", display_name="Pierś")
    save_cache(cache, path)
    assert "pierś z kurczaka" in path.read_text(encoding="utf-8")
    assert load_cache(path)["products"]["pierś z kurczaka"]["fitatu_product_id"] == "123"


def test_product_id_is_stored_as_a_string(tmp_path):
    # Fitatu ids are opaque; coercing to int would corrupt any non-numeric id.
    cache = load_cache(tmp_path / "c.yaml")
    record_product(cache, "cebula", product_id=4567)
    assert cache["products"]["cebula"]["fitatu_product_id"] == "4567"


def test_measure_details_are_kept_for_deterministic_creation(tmp_path):
    # add_meal_items needs a measureId, so it is cached next to the product id.
    cache = load_cache(tmp_path / "c.yaml")
    record_product(
        cache, "mleko", product_id="1", measure_id="m-9", measure_name="ml", weight_g=1.03
    )
    measure = cache["products"]["mleko"]["default_measure"]
    assert measure["measure_id"] == "m-9"
    assert measure["weight_g"] == 1.03


def test_resolving_clears_a_previous_unresolved_record(tmp_path):
    cache = load_cache(tmp_path / "c.yaml")
    record_unresolved(cache, "dziwny składnik", "no_match")
    record_product(cache, "dziwny składnik", product_id="77")
    assert "dziwny składnik" not in cache["unresolved"]
    assert "dziwny składnik" in cache["products"]


# --- pending ----------------------------------------------------------------


def test_each_distinct_name_is_pending_once(tmp_path):
    # The whole point of the cache: an ingredient in thirty recipes costs one
    # lookup, not thirty.
    recipes = tmp_path / "recipes"
    write_recipe(recipes, "a", [{"name": "cebula"}])
    write_recipe(recipes, "b", [{"name": "Cebula"}])
    cache = load_cache(tmp_path / "c.yaml")
    assert pending_names(cache, recipes) == ["cebula"]


def test_most_used_names_come_first(tmp_path):
    recipes = tmp_path / "recipes"
    write_recipe(recipes, "a", [{"name": "cebula"}, {"name": "masło"}])
    write_recipe(recipes, "b", [{"name": "cebula"}])
    cache = load_cache(tmp_path / "c.yaml")
    assert pending_names(cache, recipes)[0] == "cebula"


def test_resolved_names_are_not_pending(tmp_path):
    recipes = tmp_path / "recipes"
    write_recipe(recipes, "a", [{"name": "cebula"}])
    cache = load_cache(tmp_path / "c.yaml")
    record_product(cache, "cebula", product_id="1")
    assert pending_names(cache, recipes) == []


def test_known_misses_are_not_retried_by_default(tmp_path):
    recipes = tmp_path / "recipes"
    write_recipe(recipes, "a", [{"name": "coś dziwnego"}])
    cache = load_cache(tmp_path / "c.yaml")
    record_unresolved(cache, "coś dziwnego", "no_match")
    assert pending_names(cache, recipes) == []
    assert pending_names(cache, recipes, retry_unresolved=True) == ["coś dziwnego"]


def test_ingredient_usage_maps_to_recipe_ids(tmp_path):
    recipes = tmp_path / "recipes"
    write_recipe(recipes, "placki", [{"name": "cebula"}])
    write_recipe(recipes, "zupa", [{"name": "cebula"}])
    assert sorted(ingredient_names(recipes)["cebula"]) == ["placki", "zupa"]


# --- apply ------------------------------------------------------------------


def test_apply_writes_ids_into_recipe_files(tmp_path):
    recipes = tmp_path / "recipes"
    write_recipe(recipes, "a", [{"name": "cebula", "quantity": 1, "unit": "piece"}])
    cache = load_cache(tmp_path / "c.yaml")
    record_product(cache, "cebula", product_id="555")

    stats = apply_ids(cache, recipes)
    assert stats["ingredients_set"] == 1

    data = yaml.safe_load((recipes / "a.yaml").read_text(encoding="utf-8"))
    assert data["ingredients"][0]["fitatu_product_id"] == "555"


def test_apply_is_idempotent(tmp_path):
    recipes = tmp_path / "recipes"
    write_recipe(recipes, "a", [{"name": "cebula"}])
    cache = load_cache(tmp_path / "c.yaml")
    record_product(cache, "cebula", product_id="555")

    apply_ids(cache, recipes)
    before = (recipes / "a.yaml").read_text(encoding="utf-8")
    stats = apply_ids(cache, recipes)
    assert stats["recipes_changed"] == 0
    assert (recipes / "a.yaml").read_text(encoding="utf-8") == before


def test_apply_preserves_other_ingredient_fields(tmp_path):
    recipes = tmp_path / "recipes"
    write_recipe(
        recipes,
        "a",
        [{"name": "curry", "quantity": None, "unit": None, "note": "do smaku"}],
    )
    cache = load_cache(tmp_path / "c.yaml")
    record_product(cache, "curry", product_id="9")
    apply_ids(cache, recipes)

    ing = yaml.safe_load((recipes / "a.yaml").read_text(encoding="utf-8"))["ingredients"][0]
    assert ing["note"] == "do smaku"
    assert ing["quantity"] is None
    assert ing["fitatu_product_id"] == "9"


def test_unknown_ingredients_are_counted_not_cleared(tmp_path):
    recipes = tmp_path / "recipes"
    write_recipe(recipes, "a", [{"name": "cebula"}, {"name": "nieznane"}])
    cache = load_cache(tmp_path / "c.yaml")
    record_product(cache, "cebula", product_id="1")

    stats = apply_ids(cache, recipes)
    assert stats["ingredients_unresolved"] == 1
    ings = yaml.safe_load((recipes / "a.yaml").read_text(encoding="utf-8"))["ingredients"]
    assert "fitatu_product_id" not in ings[1]


def test_dry_run_changes_nothing_on_disk(tmp_path):
    recipes = tmp_path / "recipes"
    write_recipe(recipes, "a", [{"name": "cebula"}])
    cache = load_cache(tmp_path / "c.yaml")
    record_product(cache, "cebula", product_id="1")

    before = (recipes / "a.yaml").read_text(encoding="utf-8")
    stats = apply_ids(cache, recipes, dry_run=True)
    assert stats["ingredients_set"] == 1
    assert (recipes / "a.yaml").read_text(encoding="utf-8") == before


def test_matching_query_is_recorded_when_it_differs_from_the_name(tmp_path):
    """Fitatu's catalogue is Polish, so an English ingredient is usually found by
    its Polish equivalent. Keeping that phrase makes the match auditable."""
    cache = load_cache(tmp_path / "c.yaml")
    record_product(
        cache, "self-rising flour", product_id="1", query="mąka samorosnąca"
    )
    entry = cache["products"]["self-rising flour"]
    assert entry["query"] == "mąka samorosnąca"


def test_query_defaults_to_the_ingredient_name(tmp_path):
    cache = load_cache(tmp_path / "c.yaml")
    record_product(cache, "Cebula", product_id="1")
    assert cache["products"]["cebula"]["query"] == "cebula"
