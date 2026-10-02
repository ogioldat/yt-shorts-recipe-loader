from recipe_library.slugs import slugify, unique_slug


def test_polish_diacritics_become_ascii():
    assert slugify("Łosoś w sosie śmietanowym") == "losos-w-sosie-smietanowym"
    assert slugify("Zupa krem z cukinii i czosnku") == "zupa-krem-z-cukinii-i-czosnku"


def test_crossed_l_is_not_dropped():
    # Unicode decomposition alone turns "łosoś" into "oso"; the explicit map
    # is what keeps the word readable.
    assert slugify("łosoś") == "losos"


def test_punctuation_and_spacing_collapse():
    assert slugify("  Chicken & Rice (quick!) ") == "chicken-rice-quick"


def test_long_titles_truncate_on_a_word_boundary():
    slug = slugify("a" * 20 + " " + "b" * 20 + " " + "c" * 40)
    assert len(slug) <= 60
    assert not slug.endswith("-")


def test_empty_or_unmappable_title_still_yields_an_id():
    assert slugify("") == "recipe"
    assert slugify("???") == "recipe"


def test_unique_slug_suffixes_only_on_collision():
    assert unique_slug("placki", set()) == "placki"
    assert unique_slug("placki", {"placki"}) == "placki-2"
    assert unique_slug("placki", {"placki", "placki-2"}) == "placki-3"


def test_unique_slug_does_not_mutate_the_taken_set():
    taken = {"placki"}
    unique_slug("placki", taken)
    assert taken == {"placki"}
