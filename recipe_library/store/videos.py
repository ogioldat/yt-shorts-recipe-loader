"""The fetch ledger and the raw video text.

Replaces `state/processed.yaml` and `raw/<video_id>/`. The `.vtt` files are
deliberately not carried over: they are a rolling window of repeated lines with
a timestamp on every word, the timings are used nowhere, and the prose that
`subtitles.py` produces from them is both smaller and a better source for
ingredient names.

`ledger()` and `save_ledger()` keep the dict-of-dicts shape that `fetch.py`
works with. A fetch run is a batch that must stay resumable across an
interrupt, so it loads the working set once, mutates it as it goes, and flushes
it in a `finally`. That is the same contract as before; only the backing store
changed.
"""

import json
import sqlite3
from datetime import date
from typing import Any

from ..db import transaction

# The fields `raw/<id>/meta.json` held, in the order it held them.
META_COLUMNS = (
    "id", "title", "description", "channel", "channel_id",
    "uploader", "webpage_url", "duration", "upload_date",
)

FETCH_STATUSES = ("pending", "partial", "complete", "unavailable")
EXTRACT_STATUSES = ("ok", "no_recipe_found", "needs_review", "unavailable")


def _meta_from_row(row: sqlite3.Row) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "id": row["video_id"],
        "title": row["title"],
        "description": row["description"],
        "channel": row["channel"],
        "channel_id": row["channel_id"],
        "uploader": row["uploader"],
        "webpage_url": row["webpage_url"],
        "duration": row["duration"],
        "upload_date": row["upload_date"],
        "shorts_url": row["shorts_url"],
        "original_lang": row["original_lang"],
        "caption_track": row["caption_track"],
        "manual_sub_langs": json.loads(row["manual_sub_langs"] or "[]"),
        "has_auto_captions": bool(row["has_auto_captions"]),
    }
    return meta


def meta(conn: sqlite3.Connection, video_id: str) -> dict[str, Any] | None:
    """What `raw/<video_id>/meta.json` used to hold, or None.

    None also covers a video recorded only as a ledger stub -- listed in the
    playlist but never successfully extracted. `caption_track` cannot be known
    for such a row, and treating that as "no captions available" would mark the
    video complete and never fetch its subtitles.
    """
    row = conn.execute(
        "SELECT * FROM videos WHERE video_id = ?", (video_id,)
    ).fetchone()
    if row is None or row["fetched_at"] is None:
        return None
    return _meta_from_row(row)


def save_meta(conn: sqlite3.Connection, video_id: str, data: dict) -> None:
    """Upsert the pruned yt-dlp metadata for one video.

    The status columns and `extracted_at` are left alone: they belong to the
    ledger gates, not to metadata, and a metadata refresh must not silently
    un-extract a video.
    """
    from ..ledger import description_hash

    description = data.get("description") or ""
    conn.execute(
        """
        INSERT INTO videos (
            video_id, title, description, description_sha256, channel, channel_id,
            uploader, webpage_url, shorts_url, duration, upload_date,
            original_lang, caption_track, manual_sub_langs, has_auto_captions,
            fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (video_id) DO UPDATE SET
            title = excluded.title,
            description = excluded.description,
            description_sha256 = excluded.description_sha256,
            channel = excluded.channel,
            channel_id = excluded.channel_id,
            uploader = excluded.uploader,
            webpage_url = excluded.webpage_url,
            shorts_url = excluded.shorts_url,
            duration = excluded.duration,
            upload_date = excluded.upload_date,
            original_lang = excluded.original_lang,
            caption_track = excluded.caption_track,
            manual_sub_langs = excluded.manual_sub_langs,
            has_auto_captions = excluded.has_auto_captions,
            fetched_at = excluded.fetched_at
        """,
        (
            video_id,
            data.get("title") or "",
            description,
            description_hash(description),
            data.get("channel"),
            data.get("channel_id"),
            data.get("uploader"),
            data.get("webpage_url"),
            data.get("shorts_url"),
            data.get("duration"),
            data.get("upload_date"),
            data.get("original_lang"),
            data.get("caption_track"),
            json.dumps(data.get("manual_sub_langs") or [], ensure_ascii=False),
            1 if data.get("has_auto_captions") else 0,
            date.today().isoformat(),
        ),
    )


# ------------------------------------------------------------- subtitles ----


def subtitle_langs(conn: sqlite3.Connection, video_id: str) -> set[str]:
    """Languages already stored for this video."""
    rows = conn.execute(
        "SELECT lang FROM video_subtitles WHERE video_id = ?", (video_id,)
    ).fetchall()
    return {row["lang"] for row in rows}


def subtitles(conn: sqlite3.Connection, video_id: str) -> dict[str, str]:
    """Every stored subtitle track for a video, by language."""
    rows = conn.execute(
        "SELECT lang, content FROM video_subtitles WHERE video_id = ? ORDER BY lang",
        (video_id,),
    ).fetchall()
    return {row["lang"]: row["content"] for row in rows}


def save_subtitles(
    conn: sqlite3.Connection,
    video_id: str,
    lang: str,
    content: str,
    *,
    track: str | None = None,
) -> None:
    """Store one caption track as plain text.

    Empty content is not stored. A throttled subtitle request that produced
    nothing must leave the language genuinely missing, so the next pass retries
    it instead of treating a blank row as done.
    """
    if not (content or "").strip():
        return
    conn.execute(
        """
        INSERT INTO video_subtitles (video_id, lang, track, content, fetched_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (video_id, lang) DO UPDATE SET
            track = excluded.track,
            content = excluded.content,
            fetched_at = excluded.fetched_at
        """,
        (video_id, lang, track, content, date.today().isoformat()),
    )


# ---------------------------------------------------------------- ledger ----


def ledger(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """The whole ledger in the dict shape `fetch.py` expects."""
    videos: dict[str, dict[str, Any]] = {}
    subs: dict[str, list[str]] = {}
    for row in conn.execute(
        "SELECT video_id, lang FROM video_subtitles ORDER BY video_id, lang"
    ):
        subs.setdefault(row["video_id"], []).append(f"subs.{row['lang']}.txt")

    recipes_by_video: dict[str, list[str]] = {}
    for row in conn.execute(
        "SELECT video_id, id FROM recipes WHERE video_id IS NOT NULL "
        "ORDER BY video_id, recipe_index"
    ):
        recipes_by_video.setdefault(row["video_id"], []).append(row["id"])

    for row in conn.execute("SELECT * FROM videos ORDER BY video_id"):
        vid = row["video_id"]
        entry: dict[str, Any] = {
            "title": row["title"],
            "fetched_at": row["fetched_at"],
            "description_sha256": row["description_sha256"],
            "subtitles": subs.get(vid, []),
            "fetch_status": row["fetch_status"],
            "extract_status": row["extract_status"],
            "extracted_at": row["extracted_at"],
            "recipes": recipes_by_video.get(vid, []),
        }
        if row["note"]:
            entry["note"] = row["note"]
        videos[vid] = entry
    return videos


def save_ledger(conn: sqlite3.Connection, videos: dict[str, dict[str, Any]]) -> None:
    """Write back the gates a fetch run moves: statuses, timestamps, the hash.

    `subtitles` and `recipes` in the dict are projections of their own tables,
    so they are read from the ledger but never written back through it.

    `extract_status` is preserved when the entry does not carry one. A fetch
    pass knows nothing about extraction, and in the YAML version writing its own
    status into the single shared field is precisely what erased the extraction
    verdict for 13 videos.
    """
    with transaction(conn):
        for video_id, entry in videos.items():
            fetch_status = entry.get("fetch_status")
            if fetch_status not in FETCH_STATUSES:
                fetch_status = "pending"
            extract_status = entry.get("extract_status")
            if extract_status not in EXTRACT_STATUSES:
                extract_status = None
            conn.execute(
                """
                INSERT INTO videos (
                    video_id, title, description_sha256, fetch_status,
                    extract_status, note, fetched_at, extracted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (video_id) DO UPDATE SET
                    title = COALESCE(NULLIF(excluded.title, ''), videos.title),
                    description_sha256 = COALESCE(
                        excluded.description_sha256, videos.description_sha256),
                    fetch_status = excluded.fetch_status,
                    extract_status = COALESCE(
                        excluded.extract_status, videos.extract_status),
                    note = excluded.note,
                    fetched_at = COALESCE(excluded.fetched_at, videos.fetched_at),
                    extracted_at = COALESCE(
                        excluded.extracted_at, videos.extracted_at)
                """,
                (
                    video_id,
                    entry.get("title") or "",
                    entry.get("description_sha256"),
                    fetch_status,
                    extract_status,
                    entry.get("note"),
                    entry.get("fetched_at"),
                    entry.get("extracted_at"),
                ),
            )


def mark_extracted(
    conn: sqlite3.Connection, video_id: str, *, when: str | None = None
) -> None:
    """Record that stage 2 has read this video's text.

    Only the timestamp. `extract_status` is a verdict about what was found, and
    writing a recipe does not establish that the whole video was understood --
    a video may yield one recipe and still need review.
    """
    conn.execute(
        "UPDATE videos SET extracted_at = ? WHERE video_id = ?",
        (when or date.today().isoformat(), video_id),
    )


def set_extraction(
    conn: sqlite3.Connection,
    video_id: str,
    *,
    status: str,
    note: str | None = None,
    when: str | None = None,
) -> None:
    """Record stage 2's verdict on a video, and that it ran."""
    if status not in EXTRACT_STATUSES:
        raise ValueError(
            f"extract_status must be one of {list(EXTRACT_STATUSES)}, got {status!r}"
        )
    conn.execute(
        "UPDATE videos SET extract_status = ?, extracted_at = ?, "
        "note = COALESCE(?, note) WHERE video_id = ?",
        (status, when or date.today().isoformat(), note, video_id),
    )


def known_ids(conn: sqlite3.Connection) -> set[str]:
    return {row["video_id"] for row in conn.execute("SELECT video_id FROM videos")}


def get(conn: sqlite3.Connection, video_id: str) -> dict[str, Any] | None:
    """One video, metadata and ledger state together, for the API."""
    row = conn.execute(
        "SELECT * FROM videos WHERE video_id = ?", (video_id,)
    ).fetchone()
    if row is None:
        return None
    out = _meta_from_row(row)
    out.update(
        {
            "video_id": row["video_id"],
            "fetch_status": row["fetch_status"],
            "extract_status": row["extract_status"],
            "note": row["note"],
            "fetched_at": row["fetched_at"],
            "extracted_at": row["extracted_at"],
            "description_sha256": row["description_sha256"],
            "subtitle_langs": sorted(subtitle_langs(conn, video_id)),
            "recipes": [
                r["id"]
                for r in conn.execute(
                    "SELECT id FROM recipes WHERE video_id = ? ORDER BY recipe_index",
                    (video_id,),
                )
            ],
        }
    )
    return out


def listing(
    conn: sqlite3.Connection,
    *,
    fetch_status: str | None = None,
    extract_status: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Videos as a browsable summary. The description is omitted; it is long."""
    sql = [
        """
        SELECT v.video_id, v.title, v.channel, v.fetch_status, v.extract_status,
               v.original_lang, v.caption_track, v.fetched_at, v.extracted_at, v.note,
               LENGTH(v.description) AS description_chars,
               (SELECT COUNT(*) FROM video_subtitles s WHERE s.video_id = v.video_id)
                   AS subtitle_count,
               (SELECT COUNT(*) FROM recipes r WHERE r.video_id = v.video_id)
                   AS recipe_count
        FROM videos v
        """
    ]
    params: list[Any] = []
    where = []
    if fetch_status:
        where.append("v.fetch_status = ?")
        params.append(fetch_status)
    if extract_status:
        where.append("v.extract_status = ?")
        params.append(extract_status)
    if where:
        sql.append("WHERE " + " AND ".join(where))
    sql.append("ORDER BY v.video_id")
    if limit is not None:
        sql.append("LIMIT ? OFFSET ?")
        params.extend([limit, offset])
    rows = conn.execute("\n".join(sql), params).fetchall()
    return [dict(row) for row in rows]


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    by_fetch = {
        row["fetch_status"]: row["n"]
        for row in conn.execute(
            "SELECT fetch_status, COUNT(*) AS n FROM videos "
            "GROUP BY fetch_status ORDER BY fetch_status"
        )
    }
    by_extract = {
        (row["extract_status"] or "not_extracted"): row["n"]
        for row in conn.execute(
            "SELECT extract_status, COUNT(*) AS n FROM videos "
            "GROUP BY extract_status ORDER BY extract_status"
        )
    }
    totals = conn.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM videos) AS videos,
            (SELECT COUNT(*) FROM videos WHERE extracted_at IS NOT NULL) AS extracted,
            (SELECT COUNT(*) FROM video_subtitles) AS subtitle_tracks
        """
    ).fetchone()
    return {
        "videos": totals["videos"],
        "extracted": totals["extracted"],
        "subtitle_tracks": totals["subtitle_tracks"],
        "by_fetch_status": by_fetch,
        "by_extract_status": by_extract,
    }
