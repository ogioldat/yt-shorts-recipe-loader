"""The one-shot YAML-to-SQLite migration.

Built against a miniature of the real tree, because the decisions that matter
are the non-mechanical ones and each has a wrong answer that looks plausible:

* dropping unverified products per *entry* rather than per *product id*, which
  would discard a product that another name vouches for;
* dropping the query aliases along with the unverified products, which would
  throw away the only translation knowledge the resolver has;
* carrying the single overloaded `status` into one column, which would leave
  the fetch state unknowable for every video extraction had touched.
"""

import json

import pytest
import yaml

from recipe_library import migrate_yaml
from recipe_library.db import fresh
from recipe_library.store import products as products_store
from recipe_library.store import recipes as recipes_store
from recipe_library.store import videos as videos_store


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A miniature of the YAML tree, wired in place of the real one."""
    raw = tmp_path / "raw"
    recipes = tmp_path / "recipes"
    state = tmp_path / "state"
    for directory in (raw, recipes, state):
        directory.mkdir()

    # Two videos: one complete, one listed but never fetched.
    (raw / "vid1").mkdir()
    (raw / "vid1" / "meta.json").write_text(
        json.dumps(
            {
                "id": "vid1",
                "title": "Placki",
                "description": "500g ziemniaków",
                "channel": "Kuchnia",
                "channel_id": "UC1",
                "uploader": "Kuchnia",
                "webpage_url": "https://www.youtube.com/watch?v=vid1",
                "shorts_url": "https://www.youtube.com/shorts/vid1",
                "duration": 40,
                "upload_date": "20260101",
                "original_lang": "pl",
                "caption_track": "pl-orig",
                "manual_sub_langs": [],
                "has_auto_captions": True,
            }
        ),
        encoding="utf-8",
    )
    (raw / "vid1" / "subs.pl.txt").write_text("mąka i sól\n", encoding="utf-8")
    # The timed original is present and must not be carried over.
    (raw / "vid1" / "subs.pl.vtt").write_text("WEBVTT\n\nmąka\n", encoding="utf-8")

    (state / "processed.yaml").write_text(
        yaml.safe_dump(
            {
                "videos": {
                    "vid1": {
                        "title": "Placki",
                        "fetched_at": "2026-10-02",
                        "description_sha256": "abc",
                        "subtitles": ["subs.pl.txt", "subs.pl.vtt"],
                        # An extraction verdict, in the field fetch also wrote.
                        "status": "needs_review",
                        "extracted_at": "2026-10-02",
                        "recipes": ["placki"],
                    },
                    "vid2": {
                        "title": "Never fetched",
                        "fetched_at": None,
                        "description_sha256": None,
                        "subtitles": [],
                        "status": "no_recipe_found",
                        "extracted_at": "2026-10-02",
                        "recipes": [],
                    },
                }
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    (recipes / "placki.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "placki",
                "title": "Placki ziemniaczane",
                "language": "pl",
                "servings": 4,
                "ingredients": [
                    {"name": "ziemniaki", "quantity": 500, "unit": "g"},
                    {"name": "cebula", "quantity": None, "unit": None},
                    {"name": "mąka", "quantity": 2, "unit": "tbsp"},
                ],
                "source": {
                    "video_id": "vid1",
                    "url": "https://www.youtube.com/shorts/vid1",
                    "channel": "Kuchnia",
                    "recipe_index": 1,
                    "extracted_from": ["title", "description"],
                },
                "extracted_at": "2026-10-02",
                "import_status": "ok",
                "fitatu_recipe_id": "555",
                "fitatu_account_id": "acct-1",
                "imported_at": "2026-10-02",
                "previous_imports": [
                    {"fitatu_recipe_id": "111", "fitatu_account_id": "acct-0"}
                ],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    measures = [{"measure_id": "1", "measure_name": "g", "weight_g": 1, "kcal": 0.8}]
    (state / "fitatu_products.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "products": {
                    # Verified.
                    "ziemniaki": {
                        "fitatu_product_id": "10",
                        "display_name": "Ziemniaki",
                        "brand": None,
                        "source": "public",
                        "verified": True,
                        "default_measure": measures[0],
                        "measures": measures,
                        "query": "ziemniaki",
                        "resolved_at": "2026-10-02",
                    },
                    # Not verified: dropped, and its name becomes pending.
                    "cebula": {
                        "fitatu_product_id": "20",
                        "display_name": "Cebula",
                        "brand": None,
                        "source": "public",
                        "verified": False,
                        "default_measure": measures[0],
                        "measures": measures,
                        "query": "cebula biała",
                        "resolved_at": "2026-10-02",
                    },
                    # Same product id as `ziemniaki`, recorded before the flag
                    # was checked. The product is verified; this entry is stale.
                    "potatoes": {
                        "fitatu_product_id": "10",
                        "display_name": "Ziemniaki",
                        "brand": None,
                        "source": "public",
                        "verified": None,
                        "default_measure": measures[0],
                        "measures": measures,
                        "query": "ziemniaki",
                        "resolved_at": "2026-10-02",
                    },
                    "mąka": {
                        "fitatu_product_id": "30",
                        "display_name": "Mąka pszenna",
                        "brand": None,
                        "source": "public",
                        "verified": True,
                        "default_measure": measures[0],
                        "measures": measures,
                        "query": "mąka pszenna",
                        "resolved_at": "2026-10-02",
                    },
                },
                "unresolved": {"allspice": {"reason": "no_match", "checked_at": "2026-10-02"}},
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    for name, value in (
        ("LEGACY_RAW", raw),
        ("LEGACY_RECIPES", recipes),
        ("LEGACY_LEDGER", state / "processed.yaml"),
        ("LEGACY_PRODUCTS", state / "fitatu_products.yaml"),
    ):
        monkeypatch.setattr(migrate_yaml, name, value)
    return tmp_path


@pytest.fixture
def migrated(tree, conn):
    result = migrate_yaml.run(conn, allowed_langs=["pl", "en"])
    return conn, result


# ---------------------------------------------------------------- videos ----


def test_metadata_and_subtitles_come_across(migrated):
    conn, _ = migrated
    meta = videos_store.meta(conn, "vid1")
    assert meta["title"] == "Placki"
    assert meta["caption_track"] == "pl-orig"
    assert videos_store.subtitles(conn, "vid1") == {"pl": "mąka i sól"}


def test_the_timed_original_is_not_migrated(migrated):
    # 456 KB of timing data against 56 KB of the prose derived from it, and
    # nothing reads the timings.
    conn, _ = migrated
    assert videos_store.subtitle_langs(conn, "vid1") == {"pl"}
    assert conn.execute("SELECT COUNT(*) FROM video_subtitles").fetchone()[0] == 1


def test_the_extraction_verdict_lands_in_its_own_column(migrated):
    conn, _ = migrated
    row = conn.execute("SELECT * FROM videos WHERE video_id = 'vid1'").fetchone()
    assert row["extract_status"] == "needs_review"
    # And the fetch state is recomputed from what is actually stored, which the
    # single overloaded `status` field could not express at all.
    assert row["fetch_status"] == "complete"


def test_a_video_that_was_never_fetched_is_pending(migrated):
    conn, _ = migrated
    row = conn.execute("SELECT * FROM videos WHERE video_id = 'vid2'").fetchone()
    assert row["fetch_status"] == "pending"
    assert row["extract_status"] == "no_recipe_found"
    assert videos_store.meta(conn, "vid2") is None


# --------------------------------------------------------------- recipes ----


def test_a_recipe_comes_across_whole(migrated):
    conn, _ = migrated
    data = recipes_store.get(conn, "placki")
    assert data["title"] == "Placki ziemniaczane"
    assert data["servings"] == 4
    assert data["source"]["extracted_from"] == ["title", "description"]
    assert [i["name"] for i in data["ingredients"]] == ["ziemniaki", "cebula", "mąka"]


def test_a_null_quantity_stays_null(migrated):
    conn, _ = migrated
    cebula = recipes_store.get(conn, "placki")["ingredients"][1]
    assert cebula["quantity"] is None
    assert cebula["unit"] is None


def test_the_import_record_comes_across(migrated):
    conn, _ = migrated
    data = recipes_store.get(conn, "placki")
    assert data["import_status"] == "ok"
    assert data["fitatu_recipe_id"] == "555"
    assert data["previous_imports"][0]["fitatu_recipe_id"] == "111"


# -------------------------------------------------------------- products ----


def test_only_verified_products_come_across(migrated):
    conn, result = migrated
    stored = {row["product_id"] for row in conn.execute("SELECT product_id FROM products")}
    assert stored == {"10", "30"}
    assert result["products"]["products_skipped_unverified"] == 1


def test_verification_is_judged_per_product_not_per_entry(migrated):
    # `potatoes` carries verified: None for product 10, which `ziemniaki`
    # vouches for. The id is the product; the entry was merely written before
    # the flag was checked.
    conn, _ = migrated
    assert products_store.mapping(conn, "potatoes")["product_id"] == "10"
    assert sorted(products_store.get(conn, "10")["names"]) == ["potatoes", "ziemniaki"]


def test_a_dropped_name_becomes_pending_rather_than_wrong(migrated):
    conn, result = migrated
    assert products_store.mapping(conn, "cebula") is None
    assert "cebula" in [r["name"] for r in products_store.pending_names(conn)]
    assert result["products"]["dropped_names"] == ["cebula"]


def test_the_ingredient_simply_has_no_product(migrated):
    # A blank, not a guess: the whole point of dropping it.
    conn, _ = migrated
    cebula = recipes_store.get(conn, "placki")["ingredients"][1]
    assert cebula["fitatu_product_id"] is None


def test_aliases_survive_even_for_a_dropped_product(migrated):
    # The alias records that `cebula` is found by searching `cebula biała`.
    # That is translation knowledge, independent of the product's verification,
    # and it is what gives the dropped name a chance of resolving again.
    conn, _ = migrated
    assert products_store.aliases(conn, "cebula") == [
        {"query": "cebula biała", "locale": None}
    ]


def test_an_alias_identical_to_the_name_is_not_recorded(migrated):
    conn, _ = migrated
    assert products_store.aliases(conn, "ziemniaki") == []


def test_seeded_mappings_are_marked_as_seeded(migrated):
    # They were chosen by a human reading candidates, which is different
    # evidence from a score, and the score is left null to say so.
    conn, _ = migrated
    mapping = products_store.mapping(conn, "ziemniaki")
    assert mapping["method"] == "seeded"
    assert mapping["match_score"] is None


def test_recorded_misses_come_across(migrated):
    conn, _ = migrated
    assert products_store.failure(conn, "allspice")["reason"] == "no_match"


def test_measures_come_across(migrated):
    conn, _ = migrated
    product = products_store.get(conn, "10")
    assert product["default_measure"]["measure_id"] == "1"
    assert product["default_measure"]["weight_g"] == 1


# ------------------------------------------------------------ idempotence ----


def test_running_it_twice_changes_nothing(tree, conn):
    first = migrate_yaml.run(conn, allowed_langs=["pl", "en"])
    counts = _counts(conn)
    second = migrate_yaml.run(conn, allowed_langs=["pl", "en"])
    assert _counts(conn) == counts
    assert first["recipes"]["recipes"] == second["recipes"]["recipes"]


def _counts(conn):
    tables = (
        "videos",
        "video_subtitles",
        "recipes",
        "ingredients",
        "recipe_imports",
        "products",
        "product_measures",
        "ingredient_products",
        "query_aliases",
        "ingredient_resolution_failures",
    )
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}


def test_the_migrated_database_passes_its_own_integrity_checks(migrated):
    conn, _ = migrated
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_an_absent_yaml_tree_is_not_an_error(conn, tmp_path, monkeypatch):
    # A fresh checkout has nothing to migrate, and that must not be a failure.
    for name in ("LEGACY_RAW", "LEGACY_RECIPES"):
        monkeypatch.setattr(migrate_yaml, name, tmp_path / "missing")
    for name in ("LEGACY_LEDGER", "LEGACY_PRODUCTS"):
        monkeypatch.setattr(migrate_yaml, name, tmp_path / "missing.yaml")
    result = migrate_yaml.run(conn, allowed_langs=["pl"])
    assert result["recipes"]["recipes"] == 0
    assert result["videos"]["videos"] == 0


def test_an_invalid_recipe_is_reported_but_still_migrated(tree, conn):
    # These are the recipes as they exist. Silently dropping one would lose
    # hand-curated work; the schema's CHECKs still refuse anything unstorable.
    (tree / "recipes" / "broken.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "broken",
                "title": "Broken",
                "ingredients": [{"name": "x", "quantity": None, "unit": None}],
                "source": {"video_id": "vid1", "recipe_index": 7},
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    result = migrate_yaml.run(conn, allowed_langs=["pl"])
    assert "broken.yaml" in [name for name, _ in result["recipes"]["invalid"]]
    assert recipes_store.get(conn, "broken") is not None
    assert result["recipes"]["failed"] == []
