"""The matcher that replaced a model's judgement about which product is which.

A wrong product silently corrupts the nutrition data this library exists to
produce, and nothing downstream would catch it. So these tests are mostly about
what the matcher *refuses*: unverified candidates, weak matches, ambiguous
ones, and words that merely start the same way.
"""

import pytest

from recipe_library.resolve.scoring import (
    Candidate,
    pick,
    rank,
    score,
    token_similarity,
    tokens,
    verified_only,
)


def cand(product_id, name, *, verified=True, brand=None):
    return Candidate(
        product_id=product_id, name=name, verified=verified, brand=brand
    )


# ---------------------------------------------------------------- tokens ----


def test_tokens_keeps_polish_diacritics():
    # maka and mąka are different words, and the catalogue spells them right.
    assert tokens("Mąka Pszenna") == ["mąka", "pszenna"]


def test_tokens_drops_stopwords_in_both_languages():
    assert tokens("pierś z kurczaka") == ["pierś", "kurczaka"]
    assert tokens("breast of chicken") == ["breast", "chicken"]


def test_tokens_keeps_stopwords_when_that_is_all_there_is():
    # Scoring a name against an empty token list would always return zero.
    assert tokens("z") == ["z"]


def test_tokens_splits_on_punctuation_and_keeps_numbers():
    assert tokens("93/7 ground beef") == ["93", "7", "ground", "beef"]


# ------------------------------------------------------ token similarity ----


@pytest.mark.parametrize(
    "a,b",
    [
        ("mielona", "mielone"),  # Polish adjective agreement
        ("kurczak", "kurczaka"),  # genitive
        ("pomidor", "pomidory"),  # plural
    ],
)
def test_inflected_forms_count_as_the_same_word(a, b):
    assert token_similarity(a, b) > 0


@pytest.mark.parametrize(
    "a,b",
    [
        ("chicken", "chickpea"),  # five shared letters, different foods
        ("flour", "flower"),
        ("ser", "serek"),  # too short to compare by prefix
        ("masło", "maślanka"),  # butter vs buttermilk
    ],
)
def test_words_that_merely_start_alike_do_not_match(a, b):
    assert token_similarity(a, b) == 0.0


def test_exact_match_outranks_inflected_match():
    assert token_similarity("kurczak", "kurczak") > token_similarity(
        "kurczak", "kurczaka"
    )


# ----------------------------------------------------------------- score ----


def test_exact_name_scores_one():
    assert score("mąka pszenna", "Mąka pszenna") == pytest.approx(1.0)


def test_extra_words_in_the_candidate_cost_precision():
    # "Wołowina chuda mielona" is a good match for "wołowina mielona", but not
    # a perfect one, and the difference is what lets a better entry win.
    full = score("wołowina mielona", "Wołowina mielona")
    partial = score("wołowina mielona", "Wołowina chuda mielona")
    assert 0 < partial < full


def test_recall_alone_is_not_enough():
    # Pure recall would match "chicken" to anything containing it.
    assert score("chicken", "chicken and leek soup with cream and potato") < 0.62


def test_unrelated_names_score_zero():
    assert score("chicken breast", "Mąka pszenna") == 0.0


def test_empty_inputs_score_zero():
    assert score("", "Mąka") == 0.0
    assert score("mąka", None) == 0.0


def test_branding_is_a_tiebreak_not_a_veto():
    plain = score("jogurt grecki", "Jogurt grecki")
    branded = score("jogurt grecki", "Jogurt grecki", brand="Piątnica")
    assert branded < plain
    # Still comfortably matched: branded products are often the right answer.
    assert branded > 0.9


# ----------------------------------------------------------- verified_only ----


def test_only_a_true_boolean_counts_as_verified():
    candidates = [
        cand("1", "a", verified=True),
        cand("2", "b", verified=False),
        cand("3", "c", verified=None),
    ]
    assert [c.product_id for c in verified_only(candidates)] == ["1"]


# ------------------------------------------------------------------ rank ----


def test_ranking_is_deterministic_for_equal_scores():
    # The answer gets cached and reused across the whole collection, so the
    # same inputs must always produce the same winner.
    a = [cand("2", "Mąka"), cand("1", "Mąka")]
    assert [c.product_id for c, _ in rank("mąka", a)] == ["1", "2"]
    assert [c.product_id for c, _ in rank("mąka", list(reversed(a)))] == ["1", "2"]


def test_unbranded_wins_a_tie():
    ranked = rank(
        "mąka pszenna",
        [cand("1", "Mąka pszenna", brand="Lubella"), cand("2", "Mąka pszenna")],
    )
    assert ranked[0][0].product_id == "2"


# ------------------------------------------------------------------ pick ----


def test_pick_takes_a_clear_verified_winner():
    decision = pick(
        "mąka pszenna",
        [cand("1", "Mąka pszenna"), cand("2", "Cukier biały")],
    )
    assert decision.ok
    assert decision.product_id == "1"
    assert decision.score == pytest.approx(1.0)


def test_pick_refuses_when_nothing_is_verified():
    decision = pick("mąka pszenna", [cand("1", "Mąka pszenna", verified=False)])
    assert not decision.ok
    assert "verified" in decision.reason
    # The rejected candidate is still reported, so the reason is auditable.
    assert decision.rejected()[0]["product_id"] == "1"


def test_pick_refuses_a_weak_best_match():
    decision = pick("spray oil", [cand("1", "Oliwa z oliwek extra vergine")])
    assert not decision.ok
    assert "threshold" in decision.reason


def test_pick_refuses_two_different_products_scoring_alike():
    # The catalogue is full of near-duplicates. Taking the first by coin-flip is
    # exactly how a wrong product gets in.
    decision = pick(
        "mąka pszenna",
        [cand("1", "Mąka pszenna"), cand("2", "Mąka pszenna")],
    )
    assert not decision.ok
    assert "ambiguous" in decision.reason


def test_two_names_for_one_product_are_not_ambiguous():
    # Same id twice is agreement, not a conflict.
    decision = pick(
        "mąka pszenna",
        [cand("7", "Mąka pszenna"), cand("7", "Mąka pszenna typ 500")],
    )
    assert decision.ok
    assert decision.product_id == "7"


def test_a_clear_gap_resolves_near_duplicates():
    decision = pick(
        "mąka pszenna",
        [cand("1", "Mąka pszenna"), cand("2", "Mąka ziemniaczana")],
    )
    assert decision.ok
    assert decision.product_id == "1"


def test_pick_with_no_candidates_explains_itself():
    decision = pick("anything", [])
    assert not decision.ok
    assert "no candidates" in decision.reason


def test_thresholds_are_adjustable():
    weak = [cand("1", "Oliwa z oliwek")]
    assert not pick("spray oil", weak).ok
    assert pick("spray oil", weak, min_score=0.0, min_gap=0.0).ok


def test_rejected_list_is_capped():
    many = [cand(str(i), f"Mąka {i}") for i in range(20)]
    assert len(pick("mąka", many).rejected()) <= 5
