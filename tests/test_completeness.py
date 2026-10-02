"""Tests for the completeness model that drives fetch selection and retries.

Two risks dominate here. First, a definition of "complete" that some videos can
never satisfy would make --until-complete loop forever. Second, treating a
machine-translated caption track as if it were the original would quietly degrade
every ingredient name downstream. Most of these tests pin down one or the other.
"""

from recipe_library.completeness import (
    base_lang,
    describe_missing,
    is_complete,
    missing_lang,
    subtitle_langs_on_disk,
    wanted_lang,
)

ALLOWED = ["pl", "en"]


def meta(lang=None, track=None):
    return {"original_lang": lang, "caption_track": track}


# --- language codes ---------------------------------------------------------


def test_regional_and_orig_suffixes_reduce_to_a_bare_language():
    assert base_lang("en-US") == "en"
    assert base_lang("pl-orig") == "pl"
    assert base_lang("pl") == "pl"


def test_absent_code_is_none():
    assert base_lang(None) is None
    assert base_lang("") is None


# --- which language we want -------------------------------------------------


def test_polish_video_wants_polish():
    assert wanted_lang(meta("pl", "pl-orig"), ALLOWED) == "pl"


def test_english_video_wants_english_not_the_allowlist_head():
    # The bug this guards: fetching "pl" for an English video, which returns a
    # Polish machine translation of English speech.
    assert wanted_lang(meta("en-US", "en-orig"), ALLOWED) == "en"


def test_video_with_no_caption_track_wants_nothing():
    assert wanted_lang(meta("pl", None), ALLOWED) is None


def test_original_language_outside_the_allowlist_is_skipped():
    assert wanted_lang(meta("de", "de-orig"), ALLOWED) is None


def test_empty_allowlist_accepts_any_original_language():
    assert wanted_lang(meta("de", "de-orig"), None) == "de"
    assert wanted_lang(meta("de", "de-orig"), []) == "de"


# --- what is on disk --------------------------------------------------------


def test_languages_are_read_from_filenames(tmp_path):
    (tmp_path / "subs.pl.txt").touch()
    (tmp_path / "subs.en.vtt").touch()
    assert subtitle_langs_on_disk(tmp_path) == {"pl", "en"}


def test_orig_suffixed_filenames_count_as_their_bare_language(tmp_path):
    (tmp_path / "subs.pl-orig.txt").touch()
    assert subtitle_langs_on_disk(tmp_path) == {"pl"}


def test_metadata_and_other_files_are_not_mistaken_for_subtitles(tmp_path):
    (tmp_path / "meta.json").touch()
    (tmp_path / "notes.txt").touch()
    assert subtitle_langs_on_disk(tmp_path) == set()


def test_missing_directory_is_not_an_error(tmp_path):
    assert subtitle_langs_on_disk(tmp_path / "nope") == set()


# --- completeness -----------------------------------------------------------


def test_absent_metadata_is_incomplete(tmp_path):
    assert is_complete(tmp_path, None, ALLOWED) is False


def test_polish_only_video_is_complete_with_polish_alone(tmp_path):
    # Many saved shorts carry one language. Requiring both would leave them
    # permanently incomplete and the retry loop endless.
    (tmp_path / "subs.pl.txt").touch()
    assert is_complete(tmp_path, meta("pl", "pl-orig"), ALLOWED) is True


def test_english_only_video_is_complete_with_english_alone(tmp_path):
    (tmp_path / "subs.en.txt").touch()
    assert is_complete(tmp_path, meta("en-US", "en-orig"), ALLOWED) is True


def test_english_video_holding_only_polish_is_incomplete(tmp_path):
    # A leftover Polish translation must not be mistaken for the original.
    (tmp_path / "subs.pl.txt").touch()
    assert missing_lang(tmp_path, meta("en-US", "en-orig"), ALLOWED) == "en"


def test_video_with_no_captions_at_all_is_complete(tmp_path):
    # Nothing further is fetchable, so this must terminate rather than retry.
    assert is_complete(tmp_path, meta("pl", None), ALLOWED) is True


def test_metadata_without_its_original_track_is_incomplete(tmp_path):
    assert is_complete(tmp_path, meta("pl", "pl-orig"), ALLOWED) is False


def test_either_output_format_satisfies_the_language(tmp_path):
    # subtitle_output may be vtt, text or both; any of them means we have it.
    (tmp_path / "subs.pl.vtt").touch()
    assert missing_lang(tmp_path, meta("pl", "pl-orig"), ALLOWED) is None


def test_skipped_language_counts_as_complete(tmp_path):
    # Nothing will ever be fetched for it, so it must not stay pending.
    assert is_complete(tmp_path, meta("de", "de-orig"), ALLOWED) is True


def test_describe_missing_names_the_language(tmp_path):
    assert "en" in describe_missing(tmp_path, meta("en-US", "en-orig"), ALLOWED)


def test_describe_missing_reports_absent_metadata(tmp_path):
    assert describe_missing(tmp_path, None, ALLOWED) == "metadata"


def test_describe_nothing_missing(tmp_path):
    (tmp_path / "subs.pl.txt").touch()
    assert describe_missing(tmp_path, meta("pl", "pl-orig"), ALLOWED) == "nothing"
