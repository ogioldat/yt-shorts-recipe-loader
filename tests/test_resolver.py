"""Resolution: search, verify, score, persist. No network; see FakeFitatu."""

import pytest

from conftest import FakeFitatu, details, search_row
from recipe_library.resolve import resolver
from recipe_library.store import products as products_store


# ------------------------------------------------------ reading Fitatu rows ----


def test_candidate_reads_fitatus_own_field_names():
    # Rows identify the product by foodId, not id or productId.
    candidate = resolver.to_candidate(search_row("42", "Mąka"))
    assert candidate.product_id == "42"
    assert candidate.name == "Mąka"
    assert candidate.verified is True


def test_a_missing_verified_flag_is_not_a_pass():
    candidate = resolver.to_candidate(search_row("42", "Mąka", verified=None))
    assert candidate.verified is None


def test_a_non_boolean_verified_flag_is_not_a_pass():
    # A truthy string must not be read as verification.
    candidate = resolver.to_candidate({"foodId": "1", "name": "x", "verified": "yes"})
    assert candidate.verified is None


def test_recipes_in_the_search_results_are_skipped():
    # Search returns Fitatu recipes alongside products; a recipe cannot be an
    # item inside another recipe.
    assert resolver.to_candidate(search_row("1", "Naleśniki", row_type="RECIPE")) is None


def test_rows_without_an_id_are_skipped():
    assert resolver.to_candidate({"name": "Mąka"}) is None
    assert resolver.to_candidate("not a dict") is None


# ------------------------------------------------------------- measures ----


def test_measures_come_from_simple_measures_with_weights():
    measures = resolver.measures_from_details(details("1", energy=110))
    by_id = {m["measure_id"]: m for m in measures}
    assert by_id["1"]["weight_g"] == 1
    assert by_id["39"]["weight_g"] == 100
    assert by_id["39"]["kcal"] == pytest.approx(110.0)


def test_weightless_measures_are_kept_anyway():
    # A measure with no weight is still a valid measureId. payload.py refuses to
    # convert against it rather than guessing, which is the behaviour we want.
    measures = resolver.measures_from_details(
        {"simpleMeasures": [], "measures": [{"id": "77", "name": "szklanka"}]}
    )
    assert measures == [
        {"measure_id": "77", "measure_name": "szklanka", "weight_g": None, "kcal": None}
    ]


def test_simple_measures_win_over_the_bare_list():
    measures = resolver.measures_from_details(
        {
            "simpleMeasures": [{"id": "1", "name": "g", "weight": 1}],
            "measures": [{"id": "1", "name": "gram"}],
        }
    )
    assert measures[0]["weight_g"] == 1


def test_initial_measure_is_listed_first():
    measures = resolver.measures_from_details(
        details("1", initial={"id": "39", "weight": 100})
    )
    assert measures[0]["measure_id"] == "39"


def test_garbage_details_yield_no_measures():
    assert resolver.measures_from_details(None) == []
    assert resolver.measures_from_details({}) == []


# ------------------------------------------------------------ resolve_name ----


def test_a_clear_verified_match_is_stored_with_its_measures(conn):
    client = FakeFitatu({"mąka pszenna": [search_row("24", "Mąka pszenna")]})
    outcome = resolver.resolve_name(conn, client, "Mąka Pszenna")

    assert outcome.status == "resolved"
    assert outcome.product_id == "24"
    stored = products_store.get(conn, "24")
    assert stored["verified"] is True
    assert len(stored["measures"]) == 2
    assert products_store.mapping(conn, "mąka pszenna")["method"] == "auto"


def test_an_unverified_candidate_is_never_stored(conn):
    client = FakeFitatu({"mąka": [search_row("24", "Mąka", verified=False)]})
    outcome = resolver.resolve_name(conn, client, "mąka")

    assert outcome.status == "unresolved"
    assert products_store.get(conn, "24") is None
    assert products_store.mapping(conn, "mąka") is None
    # Recorded, so the same search is not repeated every run.
    assert "verified" in products_store.failure(conn, "mąka")["reason"]


def test_the_rejected_candidates_are_recorded(conn):
    client = FakeFitatu({"mąka": [search_row("24", "Mąka", verified=False)]})
    resolver.resolve_name(conn, client, "mąka")
    candidates = products_store.failure(conn, "mąka")["candidates"]
    assert candidates[0]["product_id"] == "24"
    assert candidates[0]["verified"] is False


def test_a_second_lookup_of_the_same_name_costs_no_search(conn):
    client = FakeFitatu({"salt": [search_row("5", "Salt")]})
    resolver.resolve_name(conn, client, "salt")
    searches = len(client.searches)

    outcome = resolver.resolve_name(conn, client, "SALT")
    assert outcome.status == "cached"
    assert outcome.product_id == "5"
    assert len(client.searches) == searches


def test_refresh_searches_again(conn):
    client = FakeFitatu({"salt": [search_row("5", "Salt")]})
    resolver.resolve_name(conn, client, "salt")
    resolver.resolve_name(conn, client, "salt", refresh=True)
    assert len(client.searches) == 2


def test_without_a_client_only_the_database_is_consulted(conn):
    outcome = resolver.resolve_name(conn, None, "salt")
    assert outcome.status == "unresolved"
    assert "no Fitatu client" in outcome.reason
    # A dry run must not record a verdict it did not actually reach.
    assert products_store.failure(conn, "salt") is None


def test_an_alias_is_tried_when_the_name_itself_finds_nothing(conn):
    # The catalogue is Polish; this is how an English name gets found at all.
    products_store.add_alias(conn, "self-rising flour", "mąka z proszkiem")
    client = FakeFitatu({"mąka z proszkiem": [search_row("24", "Mąka z proszkiem")]})

    outcome = resolver.resolve_name(conn, client, "self-rising flour")
    assert outcome.status == "resolved"
    assert outcome.query == "mąka z proszkiem"
    assert [s["phrase"] for s in client.searches][0] == "self-rising flour"


def test_the_locale_list_is_tried_in_order(conn):
    client = FakeFitatu({})
    resolver.resolve_name(conn, client, "salt", locales=["pl_PL", None])
    assert [s["locale"] for s in client.searches] == ["pl_PL", None]


def test_an_alias_with_its_own_locale_is_tried_only_there(conn):
    products_store.add_alias(conn, "flour", "mąka", locale="pl_PL")
    client = FakeFitatu({})
    resolver.resolve_name(conn, client, "flour", locales=["en_GB", None])
    alias_calls = [s for s in client.searches if s["phrase"] == "mąka"]
    assert [s["locale"] for s in alias_calls] == ["pl_PL"]


def test_a_search_failure_is_not_recorded_as_a_verdict_on_the_name(conn):
    # Being unable to reach Fitatu says nothing about whether the name exists.
    client = FakeFitatu({})
    client.search_error = RuntimeError("429 Too Many Requests")

    outcome = resolver.resolve_name(conn, client, "salt")
    assert outcome.status == "unresolved"
    assert "Fitatu search failed" in outcome.reason
    assert products_store.failure(conn, "salt") is None


def test_a_product_with_no_usable_measure_is_refused(conn):
    # Without a measure there is no way to send a quantity, and inventing one
    # would corrupt the nutrition data.
    client = FakeFitatu(
        {"mąka": [search_row("24", "Mąka")]},
        details_by_id={"24": {"simpleMeasures": [], "measures": []}},
    )
    outcome = resolver.resolve_name(conn, client, "mąka")
    assert outcome.status == "unresolved"
    assert "no usable measures" in outcome.reason
    assert products_store.get(conn, "24") is None


def test_an_empty_name_resolves_to_nothing(conn):
    outcome = resolver.resolve_name(conn, FakeFitatu({}), "   ")
    assert outcome.status == "unresolved"
    assert "empty" in outcome.reason


# ---------------------------------------------------------- resolve_recipe ----


def test_resolving_a_recipe_covers_every_name(conn, recipe):
    client = FakeFitatu(
        {
            "chicken breast": [search_row("11", "Chicken breast")],
            "salt": [search_row("12", "Salt")],
        }
    )
    outcomes = resolver.resolve_recipe(conn, client, "test-recipe")
    assert {o.name for o in outcomes} == {"chicken breast", "salt"}
    assert all(o.ok for o in outcomes)


def test_a_known_miss_is_not_searched_again(conn, recipe):
    products_store.record_failure(conn, "salt", "no_match")
    client = FakeFitatu({"chicken breast": [search_row("11", "Chicken breast")]})

    outcomes = resolver.resolve_recipe(conn, client, "test-recipe")
    by_name = {o.name: o for o in outcomes}
    assert "previously unresolved" in by_name["salt"].reason
    assert "salt" not in [s["phrase"] for s in client.searches]


def test_retry_failed_searches_a_known_miss_again(conn, recipe):
    products_store.record_failure(conn, "salt", "no_match")
    client = FakeFitatu({"salt": [search_row("12", "Salt")]})

    outcomes = resolver.resolve_recipe(conn, client, "test-recipe", retry_failed=True)
    by_name = {o.name: o for o in outcomes}
    assert by_name["salt"].ok
    assert products_store.failure(conn, "salt") is None


def test_an_ingredient_resolves_once_for_the_whole_collection(conn, video):
    from conftest import make_recipe
    from recipe_library.store import recipes as recipes_store

    for i in (1, 2, 3):
        recipes_store.save(
            conn,
            make_recipe(
                f"recipe-{i}",
                ingredients=[{"name": "salt", "quantity": None, "unit": None}],
                source={"video_id": "vid1", "recipe_index": i},
            ),
        )
    client = FakeFitatu({"salt": [search_row("12", "Salt")]})
    for i in (1, 2, 3):
        resolver.resolve_recipe(conn, client, f"recipe-{i}")

    assert len([s for s in client.searches if s["phrase"] == "salt"]) == 1


# ----------------------------------------------------------------- config ----


def test_options_come_from_config_with_defaults_filled_in():
    options = resolver.options_from_config({"match_min_score": 0.8})
    assert options["min_score"] == 0.8
    assert options["locales"] == list(resolver.DEFAULT_LOCALES)


def test_empty_locale_strings_in_config_become_the_account_default():
    options = resolver.options_from_config({"search_locales": ["pl_PL", ""]})
    assert options["locales"] == ["pl_PL", None]
