import json
import shutil

import pytest
from yt_dlp.utils import DownloadError

from recipe_library import fetch as fetch_module
from recipe_library.fetch import (
    META_KEYS,
    _is_retryable,
    _with_backoff,
    base_opts,
    pick_caption_track,
    prune_metadata,
)

# Shaped like a real yt-dlp dump: a few useful keys buried in bulk that must go.
FULL_DUMP = {
    "id": "abc123",
    "title": "Najlepsze placki ziemniaczane",
    "description": "Składniki:\n500g ziemniaków\n1 cebula",
    "channel": "Kuchnia",
    "channel_id": "UC123",
    "uploader": "Kuchnia",
    "webpage_url": "https://www.youtube.com/watch?v=abc123",
    "duration": 48,
    "upload_date": "20260901",
    "language": "pl",
    "subtitles": {},
    "automatic_captions": {
        lang: [{"url": "x"}] for lang in ("pl-orig", "pl", "en", "de", "fr")
    },
    "formats": [{"url": "https://rotating.signed.url/" + "x" * 500} for _ in range(60)],
    "thumbnails": [{"url": "https://rotating.thumb/" + "y" * 200} for _ in range(30)],
}


def test_pruning_keeps_every_field_extraction_needs():
    meta = prune_metadata(FULL_DUMP)
    for key in META_KEYS:
        assert key in meta
    assert meta["description"] == FULL_DUMP["description"]
    assert meta["title"] == FULL_DUMP["title"]


def test_pruning_drops_the_bulk():
    meta = prune_metadata(FULL_DUMP)
    assert "formats" not in meta
    assert "thumbnails" not in meta


def test_pruning_is_a_large_size_reduction():
    full = len(json.dumps(FULL_DUMP))
    pruned = len(json.dumps(prune_metadata(FULL_DUMP), ensure_ascii=False))
    assert pruned < full / 10


def test_subtitle_availability_is_summarised_not_inlined():
    meta = prune_metadata(FULL_DUMP)
    assert meta["has_auto_captions"] is True
    # The ~157 translated languages are not listed: only the original is fetched.
    assert "auto_sub_langs" not in meta


def test_shorts_url_is_derived_from_the_id():
    assert prune_metadata(FULL_DUMP)["shorts_url"] == "https://www.youtube.com/shorts/abc123"


def test_pruning_tolerates_a_dump_missing_subtitle_keys():
    meta = prune_metadata({"id": "x", "title": "T"})
    assert meta["manual_sub_langs"] == []
    assert meta["has_auto_captions"] is False


def test_node_is_offered_as_a_js_runtime_alongside_deno():
    # YouTube extraction is deprecated without a JS runtime, and deno is the
    # only one enabled by default.
    runtimes = base_opts()["js_runtimes"]
    assert "deno" in runtimes
    if shutil.which("node"):
        assert "node" in runtimes


def test_media_download_is_always_skipped():
    # Subtitle writing needs download=True, so skip_download is the only thing
    # preventing a full video fetch.
    assert base_opts()["skip_download"] is True


@pytest.mark.parametrize(
    "message",
    ["HTTP Error 429: Too Many Requests", "too many requests", "connection timed out"],
)
def test_throttling_errors_are_retryable(message):
    assert _is_retryable(DownloadError(message)) is True


@pytest.mark.parametrize("message", ["This video is unavailable", "Private video"])
def test_permanent_errors_are_not_retried(message):
    assert _is_retryable(DownloadError(message)) is False


def test_backoff_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(fetch_module.time, "sleep", lambda _: None)
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise DownloadError("HTTP Error 429: Too Many Requests")
        return "ok"

    assert _with_backoff("test", flaky) == "ok"
    assert len(attempts) == 3


def test_backoff_gives_up_after_max_attempts(monkeypatch):
    monkeypatch.setattr(fetch_module.time, "sleep", lambda _: None)
    attempts = []

    def always_throttled():
        attempts.append(1)
        raise DownloadError("HTTP Error 429: Too Many Requests")

    with pytest.raises(DownloadError):
        _with_backoff("test", always_throttled)
    assert len(attempts) == fetch_module.MAX_ATTEMPTS


def test_permanent_error_is_raised_immediately_without_sleeping(monkeypatch):
    def fail_if_called(_):
        raise AssertionError("should not sleep on a permanent error")

    monkeypatch.setattr(fetch_module.time, "sleep", fail_if_called)
    with pytest.raises(DownloadError):
        _with_backoff("test", lambda: (_ for _ in ()).throw(DownloadError("Private video")))


def test_cookies_are_absent_by_default():
    # Public playlists need no session, and reading a browser's cookie store is
    # privacy-sensitive, so it must stay strictly opt-in.
    assert "cookiesfrombrowser" not in base_opts()


def test_cookies_use_the_four_tuple_yt_dlp_expects():
    opts = base_opts("chrome")
    assert opts["cookiesfrombrowser"] == ("chrome", None, None, None)


def test_existing_subtitle_files_are_never_deleted(tmp_path, monkeypatch):
    """A re-run must not destroy what an earlier run paid for.

    The return value is a before/after diff rather than a directory listing, so
    provenance stays honest without deleting anything.
    """
    out_dir = tmp_path / "vid"
    out_dir.mkdir()
    existing = out_dir / "subs.en.txt"
    existing.write_text("already fetched", encoding="utf-8")

    monkeypatch.setattr(fetch_module, "video_dir", lambda _: out_dir)
    monkeypatch.setattr(fetch_module.time, "sleep", lambda _: None)

    def throttled(*_args, **_kwargs):
        raise DownloadError("HTTP Error 429: Too Many Requests")

    monkeypatch.setattr(fetch_module, "_with_backoff", throttled)

    new_files = fetch_module.fetch_subtitles("vid", "pl-orig", "pl")
    assert new_files == []  # nothing arrived this call
    assert existing.read_text(encoding="utf-8") == "already fetched"


def test_orig_track_filename_is_normalised_to_the_bare_language(tmp_path, monkeypatch):
    """yt-dlp names the file after the track asked for, so pl-orig lands as
    subs.pl-orig.vtt. Stage 2 expects subs.pl.txt, so it is renamed."""
    out_dir = tmp_path / "vid"
    out_dir.mkdir()
    monkeypatch.setattr(fetch_module, "video_dir", lambda _: out_dir)

    def write_orig_file(_what, _call):
        (out_dir / "subs.pl-orig.vtt").write_text(
            "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nmąka\n", encoding="utf-8"
        )

    monkeypatch.setattr(fetch_module, "_with_backoff", write_orig_file)

    new_files = fetch_module.fetch_subtitles("vid", "pl-orig", "pl", None, "text")
    assert new_files == ["subs.pl.txt"]
    assert (out_dir / "subs.pl.txt").read_text(encoding="utf-8").strip() == "mąka"


def test_bare_track_needs_no_rename(tmp_path, monkeypatch):
    out_dir = tmp_path / "vid"
    out_dir.mkdir()
    monkeypatch.setattr(fetch_module, "video_dir", lambda _: out_dir)

    def write_file(_what, _call):
        (out_dir / "subs.pl.vtt").write_text(
            "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nsól\n", encoding="utf-8"
        )

    monkeypatch.setattr(fetch_module, "_with_backoff", write_file)
    assert fetch_module.fetch_subtitles("vid", "pl", "pl", None, "text") == ["subs.pl.txt"]


def test_original_track_is_the_orig_suffixed_one():
    # Exactly one auto-caption key is real ASR output; the rest are translations.
    assert pick_caption_track(FULL_DUMP) == ("pl", "pl-orig")


def test_metadata_records_the_original_track():
    meta = prune_metadata(FULL_DUMP)
    assert meta["original_lang"] == "pl"
    assert meta["caption_track"] == "pl-orig"


def test_english_video_resolves_to_english_not_polish():
    # The data-quality bug this prevents: storing a Polish machine translation
    # of English speech.
    dump = dict(
        FULL_DUMP,
        language="en-US",
        subtitles={},
        automatic_captions={k: [] for k in ("en-orig", "pl", "en", "de")},
    )
    assert pick_caption_track(dump) == ("en", "en-orig")


def test_human_written_captions_are_preferred_over_asr():
    dump = dict(FULL_DUMP, subtitles={"pl": []})
    assert pick_caption_track(dump) == ("pl", "pl")


def test_plain_language_key_is_used_when_no_orig_suffix_exists():
    # Older videos predate the -orig convention.
    dump = dict(FULL_DUMP, subtitles={}, automatic_captions={"pl": [], "en": []})
    assert pick_caption_track(dump) == ("pl", "pl")


def test_video_without_captions_has_no_track():
    dump = dict(FULL_DUMP, subtitles={}, automatic_captions={})
    assert pick_caption_track(dump) == ("pl", None)


def test_original_language_is_inferred_from_the_orig_key_when_absent():
    dump = dict(FULL_DUMP, language=None, subtitles={}, automatic_captions={"es-orig": []})
    assert pick_caption_track(dump) == ("es", "es-orig")


def test_metadata_from_an_older_schema_is_treated_as_absent(tmp_path, monkeypatch):
    """Self-healing upgrade path.

    Metadata predating original-track selection cannot say which track is the
    original. Treating it as valid would mark every old video complete and never
    fetch its subtitles, so it is reported absent and refetched instead.
    """
    out_dir = tmp_path / "vid"
    out_dir.mkdir()
    legacy = out_dir / "meta.json"
    legacy.write_text('{"id": "vid", "title": "T", "manual_sub_langs": []}', encoding="utf-8")
    monkeypatch.setattr(fetch_module, "meta_path", lambda _: legacy)

    assert fetch_module.read_meta("vid") is None


def test_current_metadata_is_returned(tmp_path, monkeypatch):
    out_dir = tmp_path / "vid"
    out_dir.mkdir()
    current = out_dir / "meta.json"
    current.write_text(
        '{"id": "vid", "original_lang": "pl", "caption_track": "pl-orig"}', encoding="utf-8"
    )
    monkeypatch.setattr(fetch_module, "meta_path", lambda _: current)

    assert fetch_module.read_meta("vid")["caption_track"] == "pl-orig"


def test_corrupt_metadata_is_treated_as_absent(tmp_path, monkeypatch):
    bad = tmp_path / "meta.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(fetch_module, "meta_path", lambda _: bad)
    assert fetch_module.read_meta("vid") is None
