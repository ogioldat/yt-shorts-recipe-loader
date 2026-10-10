"""Name normalisation and the `products` CLI.

The mapping tables themselves are tested in test_store.py; this covers the
normalisation that every key depends on, and the command line a human uses to
inspect and correct the mapping.
"""

import pytest

from conftest import make_recipe, make_video
from recipe_library import products as cli
from recipe_library.names import normalize_name
from recipe_library.store import products as store
from recipe_library.store import recipes as recipes_store


# -------------------------------------------------------- normalize_name ----


def test_case_and_spacing_are_normalised():
    assert normalize_name("  Pierś   z  Kurczaka ") == "pierś z kurczaka"


def test_polish_diacritics_are_preserved():
    # The key doubles as the search_food query, and the catalogue spells them
    # correctly: maka and mąka are different words.
    assert normalize_name("Mąka Pszenna") == "mąka pszenna"


def test_empty_name_normalises_to_empty():
    assert normalize_name("") == ""
    assert normalize_name(None) == ""
    assert normalize_name("   ") == ""


def test_newlines_and_tabs_count_as_spacing():
    assert normalize_name("olive\toil\n") == "olive oil"


def test_products_module_still_exports_it():
    # store/ and resolve/ import it from `names`, but `payload.py` and anything
    # written against the old module path reach it here.
    assert cli.normalize_name("  X ") == "x"


# -------------------------------------------------------------------- cli ----


@pytest.fixture
def library(db_path):
    """A database with one recipe, one product and one recorded miss."""
    from recipe_library.db import connect

    conn = connect(db_path)
    try:
        make_video(conn, "vid1")
        recipes_store.save(conn, make_recipe())
        store.save(
            conn,
            product_id="111",
            display_name="Chicken breast",
            source="public",
            verified=True,
            measures=[{"measure_id": "1", "measure_name": "g", "weight_g": 1}],
        )
        store.record_failure(conn, "salt", "no_match")
        conn.commit()
    finally:
        conn.close()
    return str(db_path)


def run(library, argv):
    return cli.main(["--db", library, *argv])


def test_status_reports_coverage(library, capsys):
    assert run(library, ["status"]) == 0
    out = capsys.readouterr().out
    assert "products" in out
    assert "pending names" in out


def test_pending_lists_unmapped_names(library, capsys):
    assert run(library, ["pending"]) == 0
    out = capsys.readouterr().out
    assert "chicken breast" in out
    # Recorded misses are skipped, so a known miss does not come back each run.
    assert "salt" not in out


def test_pending_can_include_known_misses(library, capsys):
    run(library, ["pending", "--retry-failed"])
    assert "salt" in capsys.readouterr().out


def test_failures_explain_themselves(library, capsys):
    assert run(library, ["failures"]) == 0
    out = capsys.readouterr().out
    assert "salt" in out
    assert "no_match" in out


def test_mapping_by_hand_requires_a_stored_product(library, capsys):
    # The product table holds verified products only, so this is an escape
    # hatch for the matcher rather than for the verification rule.
    assert run(library, ["map", "--name", "salt", "--product-id", "999"]) == 1
    assert "verified" in capsys.readouterr().err


def test_mapping_by_hand_works_for_a_stored_product(library, capsys):
    assert run(library, ["map", "--name", "chicken breast", "--product-id", "111"]) == 0
    capsys.readouterr()  # discard the mapping confirmation
    assert run(library, ["pending"]) == 0
    assert "Nothing pending" in capsys.readouterr().out


def test_a_manual_mapping_is_recorded_as_manual(library):
    from recipe_library.db import connect

    run(library, ["map", "--name", "chicken breast", "--product-id", "111"])
    conn = connect(library)
    try:
        assert store.mapping(conn, "chicken breast")["method"] == "manual"
    finally:
        conn.close()


def test_unmapping_reports_whether_there_was_anything(library, capsys):
    run(library, ["map", "--name", "chicken breast", "--product-id", "111"])
    run(library, ["unmap", "--name", "chicken breast"])
    assert "Removed" in capsys.readouterr().out
    run(library, ["unmap", "--name", "chicken breast"])
    assert "No mapping" in capsys.readouterr().out


def test_an_alias_is_recorded_and_clears_the_miss(library, capsys):
    assert run(library, ["alias", "--name", "salt", "--query", "sól biała"]) == 0
    run(library, ["aliases"])
    assert "sól biała" in capsys.readouterr().out
    run(library, ["failures"])
    assert "No recorded resolution failures" in capsys.readouterr().out


def test_resolve_dry_run_contacts_nothing(library, capsys):
    assert run(library, ["resolve", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "chicken breast" in out
    assert "no Fitatu client" in out


def test_there_is_no_apply_command():
    # `apply` used to copy resolved ids into 72 YAML files and had to be re-run
    # whenever the mapping grew. It is a join now: nothing to run, nothing to
    # forget. See the ingredients_resolved view in schema.sql.
    with pytest.raises(SystemExit):
        cli.main(["apply"])
