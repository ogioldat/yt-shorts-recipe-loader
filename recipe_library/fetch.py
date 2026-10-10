"""Stage 1: fetch playlist metadata and subtitles into the database. No LLM.

Uses the yt_dlp Python API (the PyPI package) rather than shelling out to its
console script, so options are passed as data and errors arrive as exceptions.

The stage is **additive and resumable**. It never deletes what an earlier run
fetched, it re-reads stored metadata instead of refetching it, and it requests
only the subtitle languages actually missing. Re-running it is therefore always
safe and always cheap. With `fetch_until_complete` it keeps making passes until
every video has everything available for it.

yt-dlp can only write subtitles to files, so each fetch gets a throwaway
temporary directory; the text is read out of it, stored, and the directory goes
away. Nothing accumulates on disk, and `--subtitle-output vtt` now means "keep
the timed original in the database" rather than "leave a file behind".

Design notes grounded in measurements against real videos, not assumptions:

  * yt-dlp's full metadata dump is ~620KB per video across 64 top-level keys,
    almost all of it format lists, thumbnails and signed URLs that rotate on
    every fetch. We never persist it; `prune_metadata` keeps the ~3KB that
    extraction actually reads.
  * YouTube extraction now requires a JavaScript runtime. `js_runtimes` already
    contains deno by default; node is added because this machine has it. The
    signature and n-challenge warnings yt-dlp may still emit only affect media
    format URLs, which never matter here because no video is ever downloaded.
  * Subtitle requests get HTTP 429 under load, so calls are retried with
    exponential backoff and a 429 never aborts the batch.
  * Manual subtitles are a separate, better track from auto-captions, so both
    are requested and which ones arrived is recorded as provenance.
"""

import argparse
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, TypeVar

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError, ExtractorError

from . import completeness, ledger
from .completeness import base_lang
from .db import open_db
from .paths import load_config
from .store import videos as videos_store
from .subtitles import vtt_to_text

T = TypeVar("T")

META_KEYS = (
    "id", "title", "description", "channel", "channel_id",
    "uploader", "webpage_url", "duration", "upload_date",
)

RETRYABLE = ("429", "too many requests", "temporarily unavailable", "timed out")
SUBTITLE_OUTPUTS = ("text", "vtt", "both")
DEFAULT_SUBTITLE_OUTPUT = "text"

MAX_ATTEMPTS = 4
BACKOFF_BASE = 5  # seconds, doubled each attempt

# Rails for fetch_until_complete. Without them a playlist whose subtitles are
# permanently throttled would loop forever.
DEFAULT_MAX_PASSES = 20
DEFAULT_PASS_DELAY = 60
DEFAULT_MAX_STALLED_PASSES = 3


def _js_runtimes() -> dict[str, dict]:
    """Runtimes yt-dlp may use to solve YouTube's JS challenges."""
    runtimes = {"deno": {"path": None}}
    if shutil.which("node"):
        runtimes["node"] = {"path": None}
    return runtimes


def base_opts(cookies_from_browser: str | None = None) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "skip_download": True,
        "js_runtimes": _js_runtimes(),
    }
    if cookies_from_browser:
        # yt-dlp wants (browser, profile, keyring, container).
        opts["cookiesfrombrowser"] = (cookies_from_browser, None, None, None)
    return opts


def _is_retryable(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(token in message for token in RETRYABLE)


def _with_backoff(what: str, call: Callable[[], T]) -> T:
    """Run `call`, retrying only on throttling and transient errors."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return call()
        except (DownloadError, ExtractorError) as exc:
            if not _is_retryable(exc) or attempt == MAX_ATTEMPTS:
                raise
            delay = BACKOFF_BASE * (2 ** (attempt - 1))
            print(
                f"  {what}: throttled, retrying in {delay}s "
                f"(attempt {attempt}/{MAX_ATTEMPTS})",
                file=sys.stderr,
            )
            time.sleep(delay)
    raise AssertionError("unreachable")


ORIG_SUFFIX = "-orig"


def pick_caption_track(full: dict) -> tuple[str | None, str | None]:
    """Return (original_lang, caption_track) for a video.

    YouTube lists auto-captions in ~157 languages, but exactly one is the real
    speech recognition output: the key suffixed `-orig`. Everything else is
    machine-translated from it, so asking for `pl` on an English video yields a
    Polish translation of English speech.

    Preference order: a human-written manual track in the original language, then
    the `-orig` ASR track, then a plainly-named track in the original language
    for older videos that predate the suffix.
    """
    manual = sorted((full.get("subtitles") or {}).keys())
    auto = sorted((full.get("automatic_captions") or {}).keys())

    orig_keys = [k for k in auto if k.endswith(ORIG_SUFFIX)]
    lang = base_lang(full.get("language"))
    if lang is None and orig_keys:
        lang = base_lang(orig_keys[0])
    if lang is None:
        return None, None

    for key in manual:
        if base_lang(key) == lang:
            return lang, key
    for key in orig_keys:
        if base_lang(key) == lang:
            return lang, key
    if lang in auto:
        return lang, lang
    return lang, None


def prune_metadata(full: dict) -> dict:
    """Reduce yt-dlp's ~620KB info dict to the fields extraction needs.

    Everything dropped is bulk and signed URLs that rotate on every fetch;
    persisting them would churn the committed tree for no informational gain.
    The ~157 machine-translated caption languages are dropped too: only the
    original track is ever fetched, so listing the rest is noise.
    """
    video_id = full.get("id") or ""
    meta = {k: full.get(k) for k in META_KEYS}
    meta["shorts_url"] = f"https://www.youtube.com/shorts/{video_id}"

    original_lang, caption_track = pick_caption_track(full)
    meta["original_lang"] = original_lang
    meta["caption_track"] = caption_track
    meta["manual_sub_langs"] = sorted((full.get("subtitles") or {}).keys())
    meta["has_auto_captions"] = bool(full.get("automatic_captions"))
    return meta


def read_meta(conn, video_id: str) -> dict | None:
    """Return usable stored metadata, or None.

    None also covers a row that has no caption track recorded. Such a row cannot
    say which track is the original, and silently treating that as "no captions
    available" would mark the video complete and never fetch its subtitles.
    """
    data = videos_store.meta(conn, video_id)
    if not data or not data.get("original_lang"):
        return None
    return data


def list_playlist(playlist_url: str) -> list[str]:
    """Return video ids in playlist order.

    Flat extraction yields ids and little else, which is precisely why fetching
    cannot be gated on a description hash: the description is unknown here.
    """
    opts = base_opts() | {"extract_flat": "in_playlist"}

    def call() -> dict:
        with YoutubeDL(opts) as ydl:
            return ydl.extract_info(playlist_url, download=False)

    info = _with_backoff("playlist", call)
    entries = info.get("entries") or []
    return [e["id"] for e in entries if e and e.get("id")]


def fetch_metadata(video_id: str) -> dict | None:
    """Return pruned metadata, or None when the video cannot be extracted."""
    url = f"https://www.youtube.com/watch?v={video_id}"

    def call() -> dict:
        with YoutubeDL(base_opts()) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        return prune_metadata(_with_backoff("metadata", call))
    except (DownloadError, ExtractorError) as exc:
        print(f"  metadata failed for {video_id}: {exc}", file=sys.stderr)
        return None


def fetch_subtitles(
    video_id: str,
    track: str,
    lang: str,
    cookies_from_browser: str | None = None,
    subtitle_output: str = DEFAULT_SUBTITLE_OUTPUT,
) -> dict[str, str]:
    """Fetch one caption track; return what to store, by variant.

    Returns `{"text": ..., "vtt": ...}` with only the variants asked for, or an
    empty dict when nothing arrived -- throttled, or the video genuinely has no
    captions for that track.

    `track` is yt-dlp's key for the original captions (often `pl-orig`) and
    `lang` is the bare language it is stored under, so the result is keyed
    predictably whatever YouTube called the track.

    `download=True` combined with `skip_download` is what makes yt-dlp write
    subtitle files without fetching any media. It can only write files, so this
    writes into a temporary directory that is removed on the way out; the
    database is the only thing that keeps anything.
    """
    if subtitle_output not in SUBTITLE_OUTPUTS:
        raise SystemExit(
            f"subtitle_output must be one of {sorted(SUBTITLE_OUTPUTS)}, "
            f"got {subtitle_output!r}"
        )

    url = f"https://www.youtube.com/watch?v={video_id}"

    with tempfile.TemporaryDirectory(prefix=f"recipe-subs-{video_id}-") as scratch:
        out_dir = Path(scratch)
        opts = base_opts(cookies_from_browser) | {
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": [track],
            "sleep_interval_requests": 1,
            "outtmpl": {"default": "subs.%(ext)s"},
            "paths": {"home": str(out_dir)},
            "postprocessors": [
                {"key": "FFmpegSubtitlesConvertor", "format": "vtt", "when": "before_dl"}
            ],
        }

        def call() -> None:
            with YoutubeDL(opts) as ydl:
                ydl.extract_info(url, download=True)

        try:
            _with_backoff("subtitles", call)
        except (DownloadError, ExtractorError) as exc:
            # Absent or throttled subtitles are not fatal: the description alone
            # is often enough to extract a recipe.
            print(f"  no subtitles for {video_id}: {exc}", file=sys.stderr)
            return {}

        vtt_files = sorted(out_dir.glob("subs.*.vtt"))
        if not vtt_files:
            return {}

        raw = vtt_files[0].read_text(encoding="utf-8")

    out: dict[str, str] = {}
    if subtitle_output in ("text", "both"):
        # yt-dlp can only emit timed formats, so the plain-text conversion
        # happens here. Timings are noise for ingredient extraction, which is
        # why text is the default and the only thing normally kept.
        out["text"] = vtt_to_text(raw)
    if subtitle_output in ("vtt", "both"):
        out["vtt"] = raw
    return out


def fetch_one(
    conn,
    video_id: str,
    allowed_langs: list[str] | None,
    videos: dict,
    cookies_from_browser: str | None = None,
    subtitle_output: str = DEFAULT_SUBTITLE_OUTPUT,
    refresh: bool = False,
) -> dict:
    """Fetch whatever is missing for one video.

    Returns `{"ok": bool, "progress": int}`, where progress counts artifacts
    newly stored. Zero progress on a pending video means the fetch is blocked
    (throttled, or the content is genuinely unavailable).
    """
    progress = 0

    meta = None if refresh else read_meta(conn, video_id)
    if meta is None:
        meta = fetch_metadata(video_id)
        if meta is None:
            ledger.record_unavailable(videos, video_id, "metadata extraction failed")
            print(f"  unavailable: {video_id}")
            return {"ok": False, "progress": 0}
        videos_store.save_meta(conn, video_id, meta)
        conn.commit()
        progress += 1
        print(f"  metadata: {meta.get('title')!r}")

    held = videos_store.subtitle_langs(conn, video_id)
    missing = completeness.missing_lang(held, meta, allowed_langs)
    if missing:
        fetched = fetch_subtitles(
            video_id,
            meta["caption_track"],
            missing,
            cookies_from_browser,
            subtitle_output,
        )
        # Prefer the prose; the timed original is stored under its own language
        # key only when it is the one variant asked for.
        content = fetched.get("text") or fetched.get("vtt") or ""
        if content.strip():
            videos_store.save_subtitles(
                conn, video_id, missing, content, track=meta["caption_track"]
            )
            conn.commit()
            held = held | {missing}
            progress += 1
            print(f"  subtitles ({meta['caption_track']}): +{missing}")

    complete = completeness.is_complete(held, meta, allowed_langs)
    ledger.record_fetch(
        videos,
        video_id,
        title=meta.get("title") or "",
        description_sha256=ledger.description_hash(meta.get("description") or ""),
        subtitles=sorted(f"subs.{lang}.txt" for lang in held),
        status="complete" if complete else "partial",
    )
    if not complete:
        print(
            f"  still missing -> "
            f"{completeness.describe_missing(held, meta, allowed_langs)}"
        )
    return {"ok": True, "progress": progress}


def select_pending(
    conn, ids: list[str], videos: dict, allowed_langs: list[str] | None = None
) -> list[str]:
    """Videos that still have something fetchable.

    Driven by what is actually stored, never by the description hash -- that gate
    belongs to re-extraction, and the description is unknown when a playlist is
    listed. Videos recorded `unavailable` are terminal and excluded.
    """
    pending = []
    for video_id in ids:
        if (videos.get(video_id) or {}).get("fetch_status") == "unavailable":
            continue
        meta = read_meta(conn, video_id)
        held = videos_store.subtitle_langs(conn, video_id)
        if not completeness.is_complete(held, meta, allowed_langs):
            pending.append(video_id)
    return pending


def run_passes(
    conn,
    ids: list[str],
    videos: dict,
    *,
    allowed_langs: list[str] | None,
    cookies: str | None,
    subtitle_output: str,
    until_complete: bool,
    max_passes: int,
    pass_delay: int,
    max_stalled_passes: int,
    limit: int | None = None,
    refresh: bool = False,
) -> int:
    """Fetch until everything is complete, or until the rails stop us.

    `allowed_langs` is an optional allowlist of original languages; None accepts
    whatever language a video was actually spoken in.

    Returns the number of videos still incomplete. A pass that writes nothing is
    "stalled"; several consecutive stalls mean the block is not going to clear by
    waiting, so the loop gives up rather than spinning.
    """
    stalled = 0

    for pass_no in range(1, max_passes + 1):
        pending = select_pending(conn, ids, videos, allowed_langs)
        if refresh and pass_no == 1:
            pending = list(ids)
        if not pending:
            break

        batch = pending[:limit] if limit else pending
        label = f"Pass {pass_no}" if until_complete else "Fetching"
        print(f"{label}: {len(batch)} video(s) incomplete")

        progress = 0
        try:
            for i, video_id in enumerate(batch, 1):
                print(f"[{i}/{len(batch)}] {video_id}")
                try:
                    result = fetch_one(
                        conn,
                        video_id,
                        allowed_langs,
                        videos,
                        cookies,
                        subtitle_output,
                        refresh=refresh and pass_no == 1,
                    )
                    progress += result["progress"]
                except Exception as exc:  # one bad video must never abort a pass
                    print(f"  error on {video_id}: {exc}", file=sys.stderr)
        finally:
            # Persist progress even on interrupt, so a run is always resumable.
            ledger.save(conn, videos)

        if not until_complete:
            break

        if progress == 0:
            stalled += 1
            print(f"  no progress this pass ({stalled}/{max_stalled_passes})")
            if stalled >= max_stalled_passes:
                print("Giving up: repeated passes made no progress.")
                break
        else:
            stalled = 0

        if select_pending(conn, ids, videos, allowed_langs) and pass_no < max_passes:
            print(f"Waiting {pass_delay}s before the next pass")
            time.sleep(pass_delay)

    return len(select_pending(conn, ids, videos, allowed_langs))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch recipe shorts metadata and subtitles."
    )
    parser.add_argument("--db", help="use this database instead of the default")
    parser.add_argument("--limit", type=int, help="fetch at most this many videos per pass")
    parser.add_argument(
        "--refresh", action="store_true", help="re-fetch metadata even when it is on disk"
    )
    parser.add_argument(
        "--video", action="append", help="fetch specific video id(s) instead of the playlist"
    )
    parser.add_argument(
        "--until-complete",
        action="store_true",
        help="keep making passes until every video has everything available",
    )
    parser.add_argument("--max-passes", type=int, help="safety cap on passes")
    parser.add_argument("--pass-delay", type=int, help="seconds to wait between passes")
    parser.add_argument(
        "--subtitle-output",
        choices=SUBTITLE_OUTPUTS,
        help="text: plain prose with no timestamps (default); vtt: store the "
        "timed original instead; both. Overrides config.yaml",
    )
    parser.add_argument(
        "--cookies-from-browser",
        help="reuse a browser's YouTube session for subtitle requests "
        "(chrome, firefox, safari, edge, brave); overrides config.yaml",
    )
    args = parser.parse_args(argv)

    # A full --until-complete run takes many minutes. Block buffering would hide
    # all progress until it finished, making a working run look hung.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    if args.video:
        # Explicit ids bypass config, so the playlist need not be set yet.
        ids = args.video
        allowed_langs = None  # explicit ids: take whatever the original is
        cookies = args.cookies_from_browser
        sub_output = args.subtitle_output or DEFAULT_SUBTITLE_OUTPUT
        until_complete = args.until_complete
        max_passes = args.max_passes or DEFAULT_MAX_PASSES
        pass_delay = args.pass_delay if args.pass_delay is not None else DEFAULT_PASS_DELAY
        max_stalled = DEFAULT_MAX_STALLED_PASSES
    else:
        config = load_config()
        allowed_langs = config["original_langs"]
        cookies = args.cookies_from_browser or config["cookies_from_browser"]
        sub_output = args.subtitle_output or config["subtitle_output"]
        until_complete = args.until_complete or config["fetch_until_complete"]
        max_passes = args.max_passes or config["max_passes"]
        pass_delay = (
            args.pass_delay if args.pass_delay is not None else config["pass_delay_seconds"]
        )
        max_stalled = config["max_stalled_passes"]
        print(f"Listing playlist: {config['playlist_url']}")
        ids = list_playlist(config["playlist_url"])
        print(f"Playlist has {len(ids)} video(s)")

    # `create=True`: a first fetch into a fresh checkout should not need a
    # migration run before it can store anything.
    with open_db(args.db, create=True) as conn:
        videos = ledger.load(conn)
        remaining = run_passes(
            conn,
            ids,
            videos,
            allowed_langs=allowed_langs,
            cookies=cookies,
            subtitle_output=sub_output,
            until_complete=until_complete,
            max_passes=max_passes,
            pass_delay=pass_delay,
            max_stalled_passes=max_stalled,
            limit=args.limit,
            refresh=args.refresh,
        )

    complete = len(ids) - remaining
    print(f"Done. {complete}/{len(ids)} video(s) complete, {remaining} incomplete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
