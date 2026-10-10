"""Video endpoints: the ledger, the raw source text, the extraction verdict.

Read-only except for that verdict. Fetching stays a CLI command: a full pass
takes many minutes, makes dozens of throttled requests to YouTube with
exponential backoff, and must stay resumable across an interrupt. That is a
batch job, not a request.

The source text matters because it is the provenance for every recipe --
YouTube descriptions get edited after upload, and this is the copy the
extraction actually read.
"""

from typing import Any, Literal

from fastapi import APIRouter, Body, HTTPException, Query

from ..ledger import needs_extraction
from ..store import videos as store
from .deps import Conn
from .schemas import ExtractionIn

router = APIRouter(prefix="/videos", tags=["videos"])


@router.get("")
def list_videos(
    conn: Conn,
    fetch_status: Literal["pending", "partial", "complete", "unavailable"] | None = None,
    extract_status: Literal["ok", "no_recipe_found", "needs_review", "unavailable"]
    | None = None,
    limit: int | None = Query(default=None, ge=1),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """The ledger. Descriptions are omitted here; they are long."""
    videos = store.listing(
        conn,
        fetch_status=fetch_status,
        extract_status=extract_status,
        limit=limit,
        offset=offset,
    )
    return {"count": len(videos), "videos": videos, "totals": store.stats(conn)}


@router.get("/pending-extraction")
def pending_extraction(conn: Conn) -> dict[str, Any]:
    """Videos stage 2 should read: never extracted, or edited since.

    Gated on `description_sha256`, which is the right gate here and the wrong
    one for fetching: when a playlist is listed we have ids and nothing else, so
    the description is unknown at that point.
    """
    ledger = store.ledger(conn)
    out = []
    for video_id, entry in ledger.items():
        if entry.get("fetch_status") == "pending":
            continue
        if needs_extraction(video_id, ledger, entry.get("description_sha256") or ""):
            out.append(
                {
                    "video_id": video_id,
                    "title": entry.get("title"),
                    "extract_status": entry.get("extract_status"),
                    "extracted_at": entry.get("extracted_at"),
                    "subtitles": entry.get("subtitles"),
                }
            )
    return {"count": len(out), "videos": out}


@router.post("/{video_id}/extraction")
def set_extraction(
    video_id: str, conn: Conn, body: ExtractionIn = Body(...)
) -> dict[str, Any]:
    """Record what stage 2 concluded about this video.

    Writing a recipe already marks its video extracted, so this is for the
    verdicts that produce no recipe -- `no_recipe_found` for a video whose
    ingredients only ever appeared as on-screen text, `needs_review` for an
    extraction that was completed but is doubted.
    """
    if store.get(conn, video_id) is None:
        raise HTTPException(status_code=404, detail=f"no video {video_id!r}")
    store.set_extraction(conn, video_id, status=body.status, note=body.note)
    conn.commit()
    return store.get(conn, video_id)


@router.get("/{video_id}")
def get_video(video_id: str, conn: Conn) -> dict[str, Any]:
    video = store.get(conn, video_id)
    if video is None:
        raise HTTPException(status_code=404, detail=f"no video {video_id!r}")
    return video


@router.get("/{video_id}/subtitles")
def get_subtitles(video_id: str, conn: Conn) -> dict[str, Any]:
    """The stored caption text, by language.

    Plain prose, not VTT: auto-caption timing is a rolling window of repeated
    lines with a timestamp on every word, and nothing here reads the timings.
    """
    if store.get(conn, video_id) is None:
        raise HTTPException(status_code=404, detail=f"no video {video_id!r}")
    return {"video_id": video_id, "subtitles": store.subtitles(conn, video_id)}


@router.get("/{video_id}/source")
def get_source_text(video_id: str, conn: Conn) -> dict[str, Any]:
    """Everything stage 2 reads for one video: title, description, captions."""
    meta = store.meta(conn, video_id)
    if meta is None:
        raise HTTPException(
            status_code=404,
            detail=f"no fetched metadata for {video_id!r}; run the fetch CLI first",
        )
    return {
        "video_id": video_id,
        "title": meta.get("title"),
        "description": meta.get("description"),
        "channel": meta.get("channel"),
        "url": meta.get("shorts_url") or meta.get("webpage_url"),
        "original_lang": meta.get("original_lang"),
        "subtitles": store.subtitles(conn, video_id),
    }
