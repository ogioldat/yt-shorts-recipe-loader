from recipe_library import ledger


def test_load_missing_ledger_returns_empty(tmp_path):
    assert ledger.load(tmp_path / "nope.yaml") == {}


def test_save_and_load_round_trip(tmp_path):
    path = tmp_path / "processed.yaml"
    videos = {"abc": {"title": "Łosoś", "status": "ok"}}
    ledger.save(videos, path)
    assert ledger.load(path) == videos


def test_save_preserves_non_ascii_readably(tmp_path):
    path = tmp_path / "processed.yaml"
    ledger.save({"abc": {"title": "Łosoś w śmietanie"}}, path)
    assert "Łosoś w śmietanie" in path.read_text(encoding="utf-8")


def test_ledger_exposes_no_fetch_gate():
    # Fetch selection is driven by what is on disk (see select_pending and
    # completeness.py), never by ledger content. Guards against reintroducing a
    # description-hash gate on fetching, which cannot work: the playlist listing
    # yields ids alone.
    assert not hasattr(ledger, "needs_fetch")


def test_extraction_gate_uses_description_hash():
    videos = {"v": {"extracted_at": "2026-10-01", "description_sha256": "old"}}
    assert ledger.needs_extraction("v", videos, "old") is False
    assert ledger.needs_extraction("v", videos, "new") is True


def test_fetched_but_never_extracted_needs_extraction():
    videos = {"v": {"fetched_at": "2026-10-01", "extracted_at": None, "description_sha256": "h"}}
    assert ledger.needs_extraction("v", videos, "h") is True


def test_unavailable_videos_are_not_queued_for_extraction():
    videos = {"v": {"status": "unavailable"}}
    assert ledger.needs_extraction("v", videos, "whatever") is False


def test_description_hash_is_stable_and_sensitive():
    assert ledger.description_hash("a") == ledger.description_hash("a")
    assert ledger.description_hash("a") != ledger.description_hash("b")


def test_record_fetch_initialises_extraction_fields():
    videos = {}
    ledger.record_fetch(videos, "v", title="T", description_sha256="h", subtitles=["subs.pl.vtt"])
    assert videos["v"]["extracted_at"] is None
    assert videos["v"]["recipes"] == []
    assert videos["v"]["subtitles"] == ["subs.pl.vtt"]


def test_record_fetch_does_not_clobber_existing_extraction():
    videos = {"v": {"extracted_at": "2026-09-01", "recipes": ["placki"]}}
    ledger.record_fetch(videos, "v", title="T", description_sha256="h2", subtitles=[])
    assert videos["v"]["extracted_at"] == "2026-09-01"
    assert videos["v"]["recipes"] == ["placki"]
