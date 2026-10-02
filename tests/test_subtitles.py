"""Tests for subtitle -> plain text conversion.

AUTO_CAPTIONS mirrors the real shape of YouTube's auto-generated VTT: a rolling
two-line window, each cue repeating the previous completed line, with per-word
inline timestamps wrapped in <c> spans.
"""

from recipe_library.subtitles import vtt_file_to_text_file, vtt_to_text

AUTO_CAPTIONS = """WEBVTT
Kind: captions
Language: pl

00:00:00.030 --> 00:00:02.270 align:start position:0%

dzisiaj<00:00:00.439><c> zrobimy</c><00:00:00.800><c> placki</c>

00:00:02.270 --> 00:00:02.280 align:start position:0%
dzisiaj zrobimy placki


00:00:02.280 --> 00:00:04.500 align:start position:0%
dzisiaj zrobimy placki
ziemniaczane<00:00:02.800><c> potrzebujemy</c>

00:00:04.500 --> 00:00:04.510 align:start position:0%
ziemniaczane potrzebujemy


00:00:04.510 --> 00:00:07.000 align:start position:0%
ziemniaczane potrzebujemy
500<00:00:05.000><c> gram</c><00:00:05.400><c> ziemniak&amp;oacute;w</c>
"""

MANUAL_CAPTIONS = """WEBVTT

1
00:00:01.000 --> 00:00:03.000
500 g ziemniaków

2
00:00:03.000 --> 00:00:05.000
1 cebula
"""


def test_no_timestamps_survive():
    text = vtt_to_text(AUTO_CAPTIONS)
    assert "-->" not in text
    assert "00:00" not in text
    assert "<" not in text and ">" not in text


def test_rolling_duplicates_are_collapsed():
    text = vtt_to_text(AUTO_CAPTIONS)
    # The phrase appears in four cues but is one utterance.
    assert text.count("dzisiaj zrobimy placki") == 1
    assert text.count("ziemniaczane potrzebujemy") == 1


def test_spoken_content_is_preserved_in_order():
    text = vtt_to_text(AUTO_CAPTIONS)
    assert text.splitlines() == [
        "dzisiaj zrobimy placki",
        "ziemniaczane potrzebujemy",
        "500 gram ziemniaków",
    ]


def test_html_entities_are_decoded():
    # &amp;oacute; is a double-encoded ó, which YouTube does emit.
    assert "ziemniaków" in vtt_to_text(AUTO_CAPTIONS)


def test_headers_and_metadata_are_dropped():
    text = vtt_to_text(AUTO_CAPTIONS)
    assert "WEBVTT" not in text
    assert "Kind" not in text
    assert "Language" not in text


def test_manual_captions_with_numeric_cue_ids():
    # Numeric cue identifiers are not dialogue and must not become text.
    assert vtt_to_text(MANUAL_CAPTIONS).splitlines() == ["500 g ziemniaków", "1 cebula"]


def test_quantities_and_units_survive_intact():
    # The whole point of the pipeline: numbers must not be mangled.
    text = vtt_to_text(MANUAL_CAPTIONS)
    assert "500 g" in text
    assert "1 cebula" in text


def test_repetition_later_in_the_video_is_kept():
    # Only adjacent duplication is a rolling-window artefact. Genuine repetition
    # separated by other speech is real and must survive.
    vtt = """WEBVTT

00:00:01.000 --> 00:00:02.000
dodaj sól

00:00:02.000 --> 00:00:03.000
wymieszaj

00:00:03.000 --> 00:00:04.000
dodaj sól
"""
    assert vtt_to_text(vtt).splitlines() == ["dodaj sól", "wymieszaj", "dodaj sól"]


def test_empty_and_header_only_input_yields_empty_text():
    assert vtt_to_text("") == ""
    assert vtt_to_text("WEBVTT\n\n") == ""


def test_comma_decimal_timings_are_recognised():
    # SRT-style comma decimals appear after some conversions.
    vtt = "WEBVTT\n\n00:00:01,000 --> 00:00:02,000\nmąka\n"
    assert vtt_to_text(vtt) == "mąka"


def test_file_conversion_writes_txt_and_removes_vtt_by_default(tmp_path):
    vtt = tmp_path / "subs.pl.vtt"
    vtt.write_text(MANUAL_CAPTIONS, encoding="utf-8")
    txt = vtt_file_to_text_file(vtt)
    assert txt.name == "subs.pl.txt"
    assert "500 g ziemniaków" in txt.read_text(encoding="utf-8")
    assert not vtt.exists()


def test_file_conversion_can_keep_the_vtt(tmp_path):
    vtt = tmp_path / "subs.pl.vtt"
    vtt.write_text(MANUAL_CAPTIONS, encoding="utf-8")
    vtt_file_to_text_file(vtt, keep_vtt=True)
    assert vtt.exists()
    assert (tmp_path / "subs.pl.txt").exists()


def test_literal_ampersand_is_not_over_decoded():
    # "&amp;amp;" means a literal "&amp;" on screen. Bounded unescaping must not
    # chew through meaningful text while fixing double-encoded diacritics.
    vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nsalt &amp;amp; pepper\n"
    assert "&" in vtt_to_text(vtt)


def test_numeric_entities_are_decoded():
    vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nziemniak&#243;w\n"
    assert vtt_to_text(vtt) == "ziemniaków"
