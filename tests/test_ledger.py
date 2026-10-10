"""The ledger's two gates, and the two statuses that used to be one field."""

from conftest import make_video
from recipe_library import ledger


def test_an_empty_database_has_an_empty_ledger(conn):
    assert ledger.load(conn) == {}


def test_save_and_load_round_trip(conn):
    make_video(conn, "abc", title="Łosoś")
    ledger.save(conn, {"abc": {"title": "Łosoś", "fetch_status": "complete"}})
    entry = ledger.load(conn)["abc"]
    assert entry["title"] == "Łosoś"
    assert entry["fetch_status"] == "complete"


def test_non_ascii_survives_storage(conn):
    make_video(conn, "abc", title="Łosoś w śmietanie")
    assert ledger.load(conn)["abc"]["title"] == "Łosoś w śmietanie"


def test_ledger_exposes_no_fetch_gate():
    # Fetch selection is driven by what is stored (see fetch.select_pending and
    # completeness.py), never by ledger content. Guards against reintroducing a
    # description-hash gate on fetching, which cannot work: the playlist
    # listing yields ids alone.
    assert not hasattr(ledger, "needs_fetch")


# ----------------------------------------------------- the extraction gate ----


def test_extraction_gate_uses_description_hash():
    videos = {"v": {"extracted_at": "2026-10-01", "description_sha256": "old"}}
    assert ledger.needs_extraction("v", videos, "old") is False
    assert ledger.needs_extraction("v", videos, "new") is True


def test_fetched_but_never_extracted_needs_extraction():
    videos = {
        "v": {"fetched_at": "2026-10-01", "extracted_at": None, "description_sha256": "h"}
    }
    assert ledger.needs_extraction("v", videos, "h") is True


def test_an_unknown_video_needs_extraction():
    assert ledger.needs_extraction("v", {}, "h") is True


def test_unavailable_videos_are_not_queued_for_extraction():
    assert ledger.needs_extraction("v", {"v": {"extract_status": "unavailable"}}, "x") is False
    assert ledger.needs_extraction("v", {"v": {"fetch_status": "unavailable"}}, "x") is False


def test_description_hash_is_stable_and_sensitive():
    assert ledger.description_hash("a") == ledger.description_hash("a")
    assert ledger.description_hash("a") != ledger.description_hash("b")


# -------------------------------------------------------------- recording ----


def test_record_fetch_initialises_extraction_fields():
    videos = {}
    ledger.record_fetch(
        videos, "v", title="T", description_sha256="h", subtitles=["subs.pl.txt"]
    )
    assert videos["v"]["extract_status"] is None
    assert videos["v"]["extracted_at"] is None
    assert videos["v"]["recipes"] == []
    assert videos["v"]["subtitles"] == ["subs.pl.txt"]


def test_record_fetch_does_not_clobber_existing_extraction():
    videos = {"v": {"extracted_at": "2026-09-01", "recipes": ["placki"]}}
    ledger.record_fetch(videos, "v", title="T", description_sha256="h2", subtitles=[])
    assert videos["v"]["extracted_at"] == "2026-09-01"
    assert videos["v"]["recipes"] == ["placki"]


def test_fetch_and_extract_statuses_do_not_overwrite_each_other():
    # This is the bug the split exists to fix: one `status` field that fetch
    # wrote and extraction then overwrote, so neither could be queried.
    videos = {}
    ledger.record_extraction(videos, "v", status="needs_review", recipes=["a"])
    ledger.record_fetch(
        videos, "v", title="T", description_sha256="h", subtitles=[], status="partial"
    )
    assert videos["v"]["fetch_status"] == "partial"
    assert videos["v"]["extract_status"] == "needs_review"
    assert videos["v"]["recipes"] == ["a"]


def test_record_unavailable_is_terminal_for_both_gates():
    videos = {}
    ledger.record_unavailable(videos, "v", "metadata extraction failed")
    assert videos["v"]["fetch_status"] == "unavailable"
    assert videos["v"]["extract_status"] == "unavailable"
    assert videos["v"]["note"] == "metadata extraction failed"
    assert ledger.needs_extraction("v", videos, "h") is False
