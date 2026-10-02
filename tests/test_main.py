"""Tests that actually execute main(), including its argument wiring.

The cookie options were threaded through main() by editing the module, and
syntax-checking an edit does not prove the arguments are bound. These tests
exercise the real entry point with the network stubbed out.
"""

import pytest

from recipe_library import fetch as fetch_module
from recipe_library import ledger


@pytest.fixture
def stub_fetch(monkeypatch, tmp_path):
    """Record calls to fetch_one instead of hitting YouTube."""
    calls = []

    def fake_fetch_one(video_id, sub_langs, videos, cookies_from_browser=None):
        calls.append(
            {"video_id": video_id, "sub_langs": sub_langs, "cookies": cookies_from_browser}
        )
        ledger.record_fetch(
            videos, video_id, title="T", description_sha256="h", subtitles=[]
        )
        return True

    monkeypatch.setattr(fetch_module, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(fetch_module.ledger, "LEDGER", tmp_path / "processed.yaml")
    monkeypatch.setattr(
        fetch_module.ledger, "save", lambda videos, path=None: None
    )
    monkeypatch.setattr(fetch_module.ledger, "load", lambda path=None: {})
    return calls


def test_explicit_video_ids_bypass_config(stub_fetch):
    # Must not raise SystemExit from load_config: --video is the escape hatch
    # used before playlist_url is set.
    assert fetch_module.main(["--video", "abc123"]) == 0
    assert [c["video_id"] for c in stub_fetch] == ["abc123"]


def test_cookie_flag_reaches_fetch_one(stub_fetch):
    fetch_module.main(["--video", "abc", "--cookies-from-browser", "chrome"])
    assert stub_fetch[0]["cookies"] == "chrome"


def test_cookies_default_to_none(stub_fetch):
    fetch_module.main(["--video", "abc"])
    assert stub_fetch[0]["cookies"] is None


def test_limit_caps_the_batch(stub_fetch):
    fetch_module.main(["--video", "a", "--video", "b", "--video", "c", "--limit", "2"])
    assert len(stub_fetch) == 2


def test_multiple_video_flags_accumulate(stub_fetch):
    fetch_module.main(["--video", "a", "--video", "b"])
    assert [c["video_id"] for c in stub_fetch] == ["a", "b"]


def test_a_failing_video_does_not_abort_the_batch(monkeypatch, tmp_path):
    seen = []

    def exploding_fetch_one(video_id, sub_langs, videos, cookies_from_browser=None):
        seen.append(video_id)
        if video_id == "bad":
            raise RuntimeError("boom")
        return True

    monkeypatch.setattr(fetch_module, "fetch_one", exploding_fetch_one)
    monkeypatch.setattr(fetch_module.ledger, "save", lambda videos, path=None: None)
    monkeypatch.setattr(fetch_module.ledger, "load", lambda path=None: {})

    assert fetch_module.main(["--video", "good", "--video", "bad", "--video", "also-good"]) == 0
    assert seen == ["good", "bad", "also-good"]


def test_missing_playlist_url_fails_clearly(monkeypatch, tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("playlist_url: null\n", encoding="utf-8")
    monkeypatch.setattr(fetch_module.ledger, "load", lambda path=None: {})
    monkeypatch.setattr("recipe_library.paths.CONFIG", config)
    with pytest.raises(SystemExit) as exc:
        fetch_module.main([])
    assert "playlist_url" in str(exc.value)
