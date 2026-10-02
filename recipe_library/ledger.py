"""The ledger: the single source of truth for what has been processed.

Two independent gates, which is the point of keeping them separate:

  * fetching is gated on whether the video id is in the ledger at all. The
    playlist listing gives us ids and nothing else, so a content hash cannot
    possibly gate this step.
  * re-extraction is gated on `description_sha256`. We only know the
    description after fetching, which is exactly when this gate applies.

Conflating the two was a real bug in the first draft of the design.
"""

import hashlib
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from .paths import LEDGER


def description_hash(description: str) -> str:
    return hashlib.sha256(description.encode("utf-8")).hexdigest()


def load(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Return the ledger's `videos` mapping, empty when there is no ledger yet."""
    path = path or LEDGER
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("videos") or {}


def save(videos: dict[str, dict[str, Any]], path: Path | None = None) -> None:
    path = path or LEDGER
    path.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(
        {"videos": dict(sorted(videos.items()))},
        allow_unicode=True,
        sort_keys=False,
        width=100,
    )
    path.write_text(body, encoding="utf-8")


def needs_fetch(video_id: str, videos: dict[str, dict], *, refresh: bool = False) -> bool:
    """Gate for stage 1. Id presence only — see the module docstring."""
    if refresh:
        return True
    return video_id not in videos


def needs_extraction(video_id: str, videos: dict[str, dict], current_hash: str) -> bool:
    """Gate for stage 2: never extracted, or the description changed since."""
    entry = videos.get(video_id)
    if entry is None:
        return True
    if entry.get("status") == "unavailable":
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
    status: str = "fetched",
) -> None:
    entry = videos.setdefault(video_id, {})
    entry.update(
        {
            "title": title,
            "fetched_at": date.today().isoformat(),
            "description_sha256": description_sha256,
            "subtitles": subtitles,
            "status": status,
        }
    )
    entry.setdefault("extracted_at", None)
    entry.setdefault("recipes", [])


def record_unavailable(videos: dict[str, dict], video_id: str, reason: str) -> None:
    videos.setdefault(video_id, {}).update(
        {
            "fetched_at": date.today().isoformat(),
            "status": "unavailable",
            "note": reason,
        }
    )
