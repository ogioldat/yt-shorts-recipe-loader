"""The ledger: the single source of truth for what has been processed.

Two independent gates, which is the point of keeping them separate:

  * fetching is gated on what is actually stored for a video, via
    `completeness.py`. The playlist listing gives us ids and nothing else, so a
    content hash cannot possibly gate this step.
  * re-extraction is gated on `description_sha256`. We only know the
    description after fetching, which is exactly when this gate applies.

Conflating the two was a real bug in the first draft of the design.

The gates now also have separate *status* fields. In the YAML ledger both
stages wrote one `status` key: fetch set `complete` or `partial`, extraction
then overwrote it with `ok`, `needs_review` or `no_recipe_found`. So a video's
fetch state was unknowable after extraction ran, and a re-fetch erased the
extraction verdict. `fetch_status` and `extract_status` are distinct columns
and distinct keys in the working dict.

The functions here are mostly pure dict manipulation. A fetch run loads the
working set once, mutates it as it goes, and flushes it in a `finally`, which
is what keeps a long run resumable across an interrupt.
"""

import hashlib
import sqlite3
from datetime import date
from typing import Any

from .store import videos as videos_store


def description_hash(description: str) -> str:
    return hashlib.sha256(description.encode("utf-8")).hexdigest()


def load(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """The ledger as a working dict, empty when nothing has been fetched."""
    return videos_store.ledger(conn)


def save(conn: sqlite3.Connection, videos: dict[str, dict[str, Any]]) -> None:
    videos_store.save_ledger(conn, videos)


def needs_extraction(video_id: str, videos: dict[str, dict], current_hash: str) -> bool:
    """Gate for stage 2: never extracted, or the description changed since."""
    entry = videos.get(video_id)
    if entry is None:
        return True
    if entry.get("extract_status") == "unavailable":
        return False
    if entry.get("fetch_status") == "unavailable":
        return False
    if not entry.get("extracted_at"):
        return True
    return entry.get("description_sha256") != current_hash


def record_fetch(
    videos: dict[str, dict],
    video_id: str,
    *,
    title: str,
    description_sha256: str,
    subtitles: list[str],
    status: str = "partial",
) -> None:
    """Record the outcome of fetching one video.

    `extract_status`, `extracted_at` and `recipes` are initialised but never
    overwritten: fetching a video again says nothing about whether its recipes
    have been extracted, and clobbering that was the old single-status bug.
    """
    entry = videos.setdefault(video_id, {})
    entry.update(
        {
            "title": title,
            "fetched_at": date.today().isoformat(),
            "description_sha256": description_sha256,
            "subtitles": subtitles,
            "fetch_status": status,
        }
    )
    entry.setdefault("extract_status", None)
    entry.setdefault("extracted_at", None)
    entry.setdefault("recipes", [])


def record_unavailable(videos: dict[str, dict], video_id: str, reason: str) -> None:
    """Record a video that cannot be extracted at all. Terminal for both gates."""
    videos.setdefault(video_id, {}).update(
        {
            "fetched_at": date.today().isoformat(),
            "fetch_status": "unavailable",
            "extract_status": "unavailable",
            "note": reason,
        }
    )


def record_extraction(
    videos: dict[str, dict],
    video_id: str,
    *,
    status: str = "ok",
    recipes: list[str] | None = None,
) -> None:
    """Record that stage 2 has read this video. Does not touch `fetch_status`."""
    entry = videos.setdefault(video_id, {})
    entry["extract_status"] = status
    entry["extracted_at"] = date.today().isoformat()
    if recipes is not None:
        entry["recipes"] = recipes
