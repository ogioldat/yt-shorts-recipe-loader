"""Stage 1: fetch playlist metadata and subtitles to disk. No LLM involved.

Uses the yt_dlp Python API (the PyPI package) rather than shelling out to its
console script, so options are passed as data and errors arrive as exceptions.

Design notes grounded in measurements against real videos, not assumptions:

  * yt-dlp's full metadata dump is ~620KB per video across 64 top-level keys,
    almost all of it format lists, thumbnails and signed URLs that rotate on
    every fetch. We never persist it; `prune_metadata` keeps the ~3KB that
    extraction actually reads.
  * YouTube extraction now requires a JavaScript runtime. `js_runtimes` already
    contains deno by default; node is added because this machine has it. The
    signature and n-challenge warnings yt-dlp may still emit only affect media
    format URLs, which never matter here because we never download video.
  * Subtitle requests get HTTP 429 under load, so calls are retried with
    exponential backoff and a 429 never aborts the batch.
  * Manual subtitles are a separate, better track from auto-captions, so both
    are requested and which ones arrived is recorded as provenance.
"""

import argparse
import json
import shutil
import sys
import time
from typing import Any, Callable, TypeVar

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError, ExtractorError

from . import ledger
from .paths import load_config, meta_path, video_dir

T = TypeVar("T")

META_KEYS = (
    "id", "title", "description", "channel", "channel_id",
    "uploader", "webpage_url", "duration", "upload_date",
)

RETRYABLE = ("429", "too many requests", "temporarily unavailable", "timed out")
MAX_ATTEMPTS = 4
BACKOFF_BASE = 5  # seconds, doubled each attempt


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


def prune_metadata(full: dict) -> dict:
    """Reduce yt-dlp's ~620KB info dict to the fields extraction needs.

    Everything dropped is bulk and signed URLs that rotate on every fetch;
    persisting them would churn the committed tree for no informational gain.
    """
    video_id = full.get("id") or ""
    meta = {k: full.get(k) for k in META_KEYS}
    meta["shorts_url"] = f"https://www.youtube.com/shorts/{video_id}"
    meta["manual_sub_langs"] = sorted((full.get("subtitles") or {}).keys())
    meta["has_auto_captions"] = bool(full.get("automatic_captions"))
    return meta


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
    video_id: str, sub_langs: list[str], cookies_from_browser: str | None = None
) -> list[str]:
    """Write available subtitles into the video's raw dir; return filenames.

    `download=True` combined with `skip_download` is what makes yt-dlp write
    subtitle files without fetching any media.
    """
    out_dir = video_dir(video_id)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Clear stale subtitles first. The return value is built by globbing this
    # directory, so leftovers from an earlier run would be reported as though
    # this call had fetched them -- and stage 2 builds its `extracted_from`
    # provenance from that list. Matters on --refresh, after sub_langs changes,
    # and after a throttled attempt leaves a partial file behind.
    for stale in out_dir.glob("subs.*.vtt"):
        stale.unlink()

    url = f"https://www.youtube.com/watch?v={video_id}"

    opts = base_opts(cookies_from_browser) | {
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": list(sub_langs),
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
        # Absent or throttled subtitles are not fatal: the description alone is
        # often enough to extract a recipe.
        print(f"  no subtitles for {video_id}: {exc}", file=sys.stderr)

    return sorted(p.name for p in out_dir.glob("subs.*.vtt"))


def fetch_one(
    video_id: str,
    sub_langs: list[str],
    videos: dict,
    cookies_from_browser: str | None = None,
) -> bool:
    """Fetch one video. True on success, False when unavailable."""
    meta = fetch_metadata(video_id)
    if meta is None:
        ledger.record_unavailable(videos, video_id, "metadata extraction failed")
        print(f"  unavailable: {video_id}")
        return False

    video_dir(video_id).mkdir(parents=True, exist_ok=True)
    meta_path(video_id).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    subtitles = fetch_subtitles(video_id, sub_langs, cookies_from_browser)
    ledger.record_fetch(
        videos,
        video_id,
        title=meta.get("title") or "",
        description_sha256=ledger.description_hash(meta.get("description") or ""),
        subtitles=subtitles,
    )
    print(f"  fetched: {meta.get('title')!r} ({len(subtitles)} subtitle file(s))")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch recipe shorts metadata and subtitles."
    )
    parser.add_argument("--limit", type=int, help="fetch at most this many new videos")
    parser.add_argument(
        "--refresh", action="store_true", help="re-fetch videos already in the ledger"
    )
    parser.add_argument(
        "--video", action="append", help="fetch specific video id(s) instead of the playlist"
    )
    parser.add_argument(
        "--cookies-from-browser",
        help="reuse a browser's YouTube session for subtitle requests "
        "(chrome, firefox, safari, edge, brave); overrides config.yaml",
    )
    args = parser.parse_args(argv)

    videos = ledger.load()

    if args.video:
        # Explicit ids bypass config, so the playlist need not be set yet.
        ids = args.video
        sub_langs = ["pl", "en"]
        cookies = args.cookies_from_browser
    else:
        config = load_config()
        sub_langs = config["sub_langs"]
        cookies = args.cookies_from_browser or config["cookies_from_browser"]
        print(f"Listing playlist: {config['playlist_url']}")
        ids = list_playlist(config["playlist_url"])
        print(f"Playlist has {len(ids)} video(s)")

    todo = [v for v in ids if ledger.needs_fetch(v, videos, refresh=args.refresh)]
    if args.limit:
        todo = todo[: args.limit]

    if not todo:
        print("Nothing new to fetch.")
        return 0

    print(f"Fetching {len(todo)} video(s)")
    failures = 0
    try:
        for i, video_id in enumerate(todo, 1):
            print(f"[{i}/{len(todo)}] {video_id}")
            try:
                if not fetch_one(video_id, sub_langs, videos, cookies):
                    failures += 1
            except Exception as exc:  # one bad video must never abort the batch
                failures += 1
                print(f"  error on {video_id}: {exc}", file=sys.stderr)
    finally:
        # Persist progress even on interrupt, so a run is always resumable.
        ledger.save(videos)

    print(f"Done. {len(todo) - failures} fetched, {failures} failed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
