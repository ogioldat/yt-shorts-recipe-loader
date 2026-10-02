"""Tests for reading the recipe collection, which the recipes-library MCP serves."""

import yaml

from recipe_library.recipes import (
    collection_stats,
    list_recipes,
    read_recipe,
    summarise,
)


def write(recipes_dir, recipe_id, **overrides):
    recipes_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "id": recipe_id,
        "title": recipe_id.replace("-", " ").title(),
        "language": "pl",
        "servings": 2,
        "ingredients": [{"name": "mąka", "quantity": 100, "unit": "g"}],
        "source": {
            "video_id": "v1",
            "url": "https://www.youtube.com/shorts/v1",
            "channel": "Kanał",
            "recipe_index": 1,
        },
        "extracted_at": "2026-10-02",
    }
    data.update(overrides)
    (recipes_dir / f"{recipe_id}.yaml").write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


# --- reading -----------------------------------------------------------------


def test_unparseable_file_reads_as_none(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("ingredients: [unclosed", encoding="utf-8")
    assert read_recipe(bad) is None


def test_non_mapping_file_reads_as_none(tmp_path):
    path = tmp_path / "list.yaml"
    path.write_text("- just\n- a list\n", encoding="utf-8")
    assert read_recipe(path) is None


def test_one_corrupt_file_does_not_hide_the_others(tmp_path):
    write(tmp_path, "good")
    (tmp_path / "bad.yaml").write_text("{unclosed", encoding="utf-8")
    assert [r["id"] for r in list_recipes(tmp_path)] == ["good"]


# --- summarising -------------------------------------------------------------


def test_ingredients_are_omitted_by_default(tmp_path):
    # The collection holds hundreds of ingredients; listing them all would bury
    # the overview a caller is after.
    write(tmp_path, "a")
    assert "ingredients" not in list_recipes(tmp_path)[0]


def test_ingredients_can_be_requested(tmp_path):
    write(tmp_path, "a")
    recipes = list_recipes(tmp_path, include_ingredients=True)
    assert recipes[0]["ingredients"][0]["name"] == "mąka"


def test_counts_describe_the_ingredient_list():
    data = {
        "ingredients": [
            {"name": "mąka", "quantity": 100, "unit": "g", "fitatu_product_id": "1"},
            {"name": "sól", "quantity": None, "unit": None},
            {"name": "cebula", "quantity": 1, "unit": "piece"},
        ]
    }
    summary = summarise(data)
    assert summary["ingredient_count"] == 3
    assert summary["unquantified_count"] == 1
    assert summary["mapped_count"] == 1


def test_absent_import_status_reads_as_not_imported():
    assert summarise({})["import_status"] == "not_imported"


def test_import_status_and_recipe_id_are_surfaced():
    summary = summarise({"import_status": "ok", "fitatu_recipe_id": "42"})
    assert summary["import_status"] == "ok"
    assert summary["fitatu_recipe_id"] == "42"


def test_absent_recipe_id_is_left_out():
    assert "fitatu_recipe_id" not in summarise({})


def test_optional_fields_are_only_included_when_present():
    assert "title_en" not in summarise({"title": "T"})
    assert summarise({"title_en": "Chicken"})["title_en"] == "Chicken"


def test_source_is_included_without_empty_keys():
    summary = summarise({"source": {"url": "https://x", "channel": None}})
    assert summary["source"] == {"url": "https://x"}


def test_recipe_without_ingredients_counts_zero():
    assert summarise({})["ingredient_count"] == 0


# --- listing and filtering ---------------------------------------------------


def test_recipes_are_ordered_by_id(tmp_path):
    write(tmp_path, "zupa")
    write(tmp_path, "placki")
    assert [r["id"] for r in list_recipes(tmp_path)] == ["placki", "zupa"]


def test_empty_collection_lists_nothing(tmp_path):
    assert list_recipes(tmp_path) == []


def test_language_filter(tmp_path):
    write(tmp_path, "pl-one", language="pl")
    write(tmp_path, "en-one", language="en")
    assert [r["id"] for r in list_recipes(tmp_path, language="en")] == ["en-one"]


def test_import_status_filter_matches_absent_as_not_imported(tmp_path):
    write(tmp_path, "fresh")
    write(tmp_path, "done", import_status="ok", fitatu_recipe_id="1")
    assert [r["id"] for r in list_recipes(tmp_path, import_status="not_imported")] == ["fresh"]
    assert [r["id"] for r in list_recipes(tmp_path, import_status="ok")] == ["done"]


def test_limit_caps_the_listing(tmp_path):
    for name in ("a", "b", "c"):
        write(tmp_path, name)
    assert len(list_recipes(tmp_path, limit=2)) == 2


# --- stats -------------------------------------------------------------------


def test_stats_summarise_the_collection(tmp_path):
    write(tmp_path, "a", language="pl")
    write(tmp_path, "b", language="en", import_status="ok", fitatu_recipe_id="1")
    stats = collection_stats(tmp_path)
    assert stats["recipes"] == 2
    assert stats["ingredients"] == 2
    assert stats["by_language"] == {"en": 1, "pl": 1}
    assert stats["by_import_status"] == {"not_imported": 1, "ok": 1}


def test_stats_on_an_empty_collection(tmp_path):
    assert collection_stats(tmp_path) == {
        "recipes": 0,
        "ingredients": 0,
        "by_language": {},
        "by_import_status": {},
    }
