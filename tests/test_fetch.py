import json
import shutil

import pytest
from yt_dlp.utils import DownloadError

from recipe_library import fetch as fetch_module
from recipe_library.fetch import META_KEYS, _is_retryable, _with_backoff, base_opts, prune_metadata

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
    "subtitles": {"pl": [{"url": "https://signed.example/pl"}]},
    "automatic_captions": {lang: [{"url": "x"}] for lang in ("pl", "en", "de", "fr")},
    "formats": [{"url": "https://rotating.signed.url/" + "x" * 500} for _ in range(60)],
    "thumbnails": [{"url": "https://rotating.thumb/" + "y" * 200} for _ in range(30)],
}


def test_pruning_keeps_every_field_extraction_needs():
    meta = prune_metadata(FULL_DUMP)
    for key in META_KEYS:
        assert key in meta
    assert meta["description"] == FULL_DUMP["description"]
    assert meta["title"] == FULL_DUMP["title"]


def test_pruning_drops_the_bulk_and_rotating_urls():
    meta = prune_metadata(FULL_DUMP)
    assert "formats" not in meta
    assert "thumbnails" not in meta
    # Signed URLs rotate per fetch; keeping them would churn the committed tree.
    assert "automatic_captions" not in meta
    assert "subtitles" not in meta


def test_pruning_is_a_large_size_reduction():
    full = len(json.dumps(FULL_DUMP))
    pruned = len(json.dumps(prune_metadata(FULL_DUMP), ensure_ascii=False))
    assert pruned < full / 10


def test_subtitle_availability_is_summarised_not_inlined():
    meta = prune_metadata(FULL_DUMP)
    assert meta["manual_sub_langs"] == ["pl"]
    assert meta["has_auto_captions"] is True


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
