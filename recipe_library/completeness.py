"""What counts as a fully fetched video, and what is still missing.

Stage 1 is additive and resumable: a run never deletes what an earlier run
fetched, and it only requests what is actually absent. That needs a definition of
"complete" precise enough to drive a retry loop, which is what this provides.

**Only the original caption track counts.** YouTube offers auto-captions in ~157
languages for a typical short, but exactly one of them is the real speech
recognition output -- marked with an `-orig` suffix, as in `pl-orig`. Every other
entry is machine-translated from it. Asking for `pl` on an English video returns
a Polish translation of English speech, which is a measurably worse source for
ingredient names than the English original.

A video is complete when `meta.json` exists and its original track is on disk, or
when it has no captions at all. That last clause is what keeps the retry loop
terminating.
"""

from pathlib import Path

SUBTITLE_SUFFIXES = (".txt", ".vtt")


def base_lang(code: str | None) -> str | None:
    """Reduce a caption code to its bare language: en-US and en-orig -> en."""
    if not code:
        return None
    return code.split("-", 1)[0]


def wanted_lang(meta: dict, allowed: list[str] | None = None) -> str | None:
    """The one language we want for this video, or None if there is nothing.

    `allowed` is an optional allowlist of original languages. A video whose
    original language is outside it needs no subtitles -- useful for skipping
    languages you cannot read rather than hoarding translations.
    """
    if not meta.get("caption_track"):
        return None
    lang = base_lang(meta.get("original_lang"))
    if lang is None:
        return None
    if allowed and lang not in allowed:
        return None
    return lang


def subtitle_langs_on_disk(out_dir: Path) -> set[str]:
    """Languages already present as subtitle files, in either format."""
    langs: set[str] = set()
    if not out_dir.is_dir():
        return langs
    for path in out_dir.iterdir():
        # subs.pl.txt -> pl
        if not path.name.startswith("subs.") or path.suffix not in SUBTITLE_SUFFIXES:
            continue
        parts = path.name.split(".")
        if len(parts) >= 3:
            langs.add(base_lang(parts[1]))
    return langs


def missing_lang(out_dir: Path, meta: dict, allowed: list[str] | None = None) -> str | None:
    """The original language still absent from disk, or None."""
    lang = wanted_lang(meta, allowed)
    if lang is None:
        return None
    return None if lang in subtitle_langs_on_disk(out_dir) else lang


def is_complete(out_dir: Path, meta: dict | None, allowed: list[str] | None = None) -> bool:
    """True when nothing further can be fetched for this video."""
    if meta is None:
        return False
    return missing_lang(out_dir, meta, allowed) is None


def describe_missing(
    out_dir: Path, meta: dict | None, allowed: list[str] | None = None
) -> str:
    """A short human-readable account of what is still absent."""
    if meta is None:
        return "metadata"
    lang = missing_lang(out_dir, meta, allowed)
    return f"original subtitles ({lang})" if lang else "nothing"
