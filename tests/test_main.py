"""Tests that actually execute main() and the pass loop, with no network.

The CLI options were threaded through main() by editing the module, and
syntax-checking an edit does not prove the arguments are bound. These tests
exercise the real entry point, against a throwaway database.
"""

import pytest

from recipe_library import fetch as fetch_module


@pytest.fixture
def stub(monkeypatch, tmp_path):
    """Record fetch_one calls instead of hitting YouTube.

    `pending` starts as every id and empties as videos are fetched, so the pass
    loop sees real progress and can terminate.

    `--db` points at a fresh file per test, so main() exercises its real
    database handling without touching the library.
    """
    calls = []
    state = {"pending": None, "progress_per_call": 1, "db": str(tmp_path / "library.db")}

    def fake_fetch_one(
        conn,
        video_id,
        allowed_langs,
        videos,
        cookies=None,
        subtitle_output="text",
        refresh=False,
    ):
        calls.append(
            {
                "video_id": video_id,
                "allowed_langs": allowed_langs,
                "cookies": cookies,
                "subtitle_output": subtitle_output,
                "refresh": refresh,
            }
        )
        progress = state["progress_per_call"]
        if progress and state["pending"] is not None:
            state["pending"].discard(video_id)
        return {"ok": True, "progress": progress}

    def fake_select_pending(conn, ids, videos, allowed_langs=None):
        if state["pending"] is None:
            state["pending"] = set(ids)
        return [i for i in ids if i in state["pending"]]

    monkeypatch.setattr(fetch_module, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(fetch_module, "select_pending", fake_select_pending)
    monkeypatch.setattr(fetch_module.time, "sleep", lambda _: None)
    return calls, state


def run_main(state, argv):
    """main() with the test database wired in."""
    return fetch_module.main(["--db", state["db"], *argv])


def test_explicit_video_ids_bypass_config(stub):
    # Must not raise SystemExit from load_config: --video is the escape hatch.
    calls, state = stub
    assert run_main(state, ["--video", "abc123"]) == 0
    assert [c["video_id"] for c in calls] == ["abc123"]


def test_cookie_flag_reaches_fetch_one(stub):
    calls, state = stub
    run_main(state, ["--video", "abc", "--cookies-from-browser", "chrome"])
    assert calls[0]["cookies"] == "chrome"


def test_cookies_default_to_none(stub):
    calls, state = stub
    run_main(state, ["--video", "abc"])
    assert calls[0]["cookies"] is None


def test_subtitle_output_defaults_to_text(stub):
    # Timestamps are noise for ingredient extraction, so plain text is default.
    calls, state = stub
    run_main(state, ["--video", "abc"])
    assert calls[0]["subtitle_output"] == "text"


def test_subtitle_output_flag_is_honoured(stub):
    calls, state = stub
    run_main(state, ["--video", "abc", "--subtitle-output", "both"])
    assert calls[0]["subtitle_output"] == "both"


def test_invalid_subtitle_output_is_rejected(stub):
    _, state = stub
    with pytest.raises(SystemExit):
        run_main(state, ["--video", "abc", "--subtitle-output", "srt"])


def test_limit_caps_the_batch(stub):
    calls, state = stub
    run_main(state, ["--video", "a", "--video", "b", "--video", "c", "--limit", "2"])
    assert len(calls) == 2


def test_multiple_video_flags_accumulate(stub):
    calls, state = stub
    run_main(state, ["--video", "a", "--video", "b"])
    assert [c["video_id"] for c in calls] == ["a", "b"]


def test_single_pass_by_default(stub):
    # Without --until-complete, one pass runs even if work remains.
    calls, state = stub
    state["progress_per_call"] = 0  # nothing ever completes
    run_main(state, ["--video", "a"])
    assert len(calls) == 1


def test_until_complete_retries_until_nothing_is_pending(stub):
    calls, state = stub
    state["pending"] = {"a", "b"}
    run_main(state, ["--video", "a", "--video", "b", "--until-complete", "--limit", "1"])
    # One video per pass, so it takes two passes to clear both.
    assert [c["video_id"] for c in calls] == ["a", "b"]


def test_until_complete_gives_up_after_repeated_stalls(stub):
    # A permanently throttled playlist must not loop forever.
    calls, state = stub
    state["progress_per_call"] = 0
    run_main(state, ["--video", "a", "--until-complete", "--pass-delay", "0"])
    assert len(calls) == fetch_module.DEFAULT_MAX_STALLED_PASSES


def test_max_passes_caps_the_loop(stub):
    calls, state = stub
    state["progress_per_call"] = 0
    fetch_module.main(
        ["--video", "a", "--until-complete", "--max-passes", "2", "--pass-delay", "0"]
    )
    assert len(calls) == 2


def test_refresh_only_applies_to_the_first_pass(stub):
    # Re-fetching metadata every pass would waste requests and risk throttling.
    calls, state = stub
    state["pending"] = {"a", "b"}
    fetch_module.main(
        ["--video", "a", "--video", "b", "--until-complete", "--limit", "1", "--refresh"]
    )
    assert calls[0]["refresh"] is True
    assert calls[-1]["refresh"] is False


def test_a_failing_video_does_not_abort_the_batch(monkeypatch, tmp_path):
    seen = []

    def exploding_fetch_one(conn, video_id, allowed_langs, videos, *args, **kwargs):
        seen.append(video_id)
        if video_id == "bad":
            raise RuntimeError("boom")
        return {"ok": True, "progress": 1}

    monkeypatch.setattr(fetch_module, "fetch_one", exploding_fetch_one)
    monkeypatch.setattr(
        fetch_module, "select_pending", lambda conn, ids, v, a=None: list(ids)
    )

    state = {"db": str(tmp_path / "library.db")}
    assert run_main(state, ["--video", "good", "--video", "bad", "--video", "also-good"]) == 0
    assert seen == ["good", "bad", "also-good"]


def test_missing_playlist_url_fails_clearly(monkeypatch, tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("playlist_url: null\n", encoding="utf-8")
    monkeypatch.setattr("recipe_library.paths.CONFIG", config)
    with pytest.raises(SystemExit) as exc:
        run_main({"db": str(tmp_path / "library.db")}, [])
    assert "playlist_url" in str(exc.value)


def test_a_first_fetch_creates_the_database(stub, tmp_path):
    # A fresh checkout must be able to fetch without a migration run first.
    _, state = stub
    state["db"] = str(tmp_path / "brand-new.db")
    assert run_main(state, ["--video", "abc"]) == 0
    assert (tmp_path / "brand-new.db").exists()
