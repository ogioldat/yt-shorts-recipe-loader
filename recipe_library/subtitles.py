"""Convert subtitle files into plain text.

YouTube auto-captions are the worst case and the common one. They arrive as a
rolling two-line window where each cue repeats the previous completed line and
appends new words, and every new word carries its own inline timestamp:

    00:00:02.280 --> 00:00:04.500 align:start position:0%
    dzisiaj zrobimy placki
    ziemniaczane<00:00:02.800><c> potrzebujemy</c>

Read literally that triples the text and buries it in markup. For extracting an
ingredient list every timestamp is noise, so the default output is plain prose:
timing lines dropped, inline tags stripped, rolling repeats collapsed.
"""

import html
import re

# A cue's timing line. The trailing settings (align, position) are optional, and
# comma decimals show up after some conversions.
TIMING = re.compile(r"^\d{2}:\d{2}:\d{2}[.,]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[.,]\d{3}")

# Inline per-word timestamps and the <c> colour spans wrapped around them.
INLINE_TAG = re.compile(r"<[^>]*>")

# Header and metadata lines that carry no dialogue.
METADATA = re.compile(r"^(WEBVTT|Kind:|Language:|NOTE\b|STYLE\b|REGION\b)")

# An entity still needing a decode pass, such as the &oacute; left behind after
# one pass over the double-encoded &amp;oacute; that YouTube actually emits.
ENTITY = re.compile(r"&(#\d+|#[xX][0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]{1,31});")

MAX_UNESCAPE_PASSES = 3

NBSP = " "


def _unescape(text: str) -> str:
    """Decode entities, repeating while the text is still entity-encoded.

    YouTube double-encodes non-ASCII characters in auto-captions, so a single
    pass turns "&amp;oacute;" into "&oacute;" and silently drops the Polish
    diacritic. Bounded, so a literal ampersand cannot cause runaway decoding.
    """
    for _ in range(MAX_UNESCAPE_PASSES):
        if not ENTITY.search(text):
            break
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    return text


def _clean_line(line: str) -> str:
    """Strip markup and entities from one cue line."""
    line = INLINE_TAG.sub("", line)
    line = _unescape(line)
    line = line.replace(NBSP, " ")
    return re.sub(r"\s+", " ", line).strip()


def vtt_to_text(vtt: str) -> str:
    """Return the spoken text from a VTT subtitle file, without timestamps.

    Consecutive duplicate lines are collapsed, which is what removes the rolling
    auto-caption window. A line repeated later in the video is kept: real speech
    does repeat, and only adjacent duplication is an artefact.
    """
    lines: list[str] = []

    for raw in vtt.splitlines():
        stripped = raw.strip()
        if not stripped or TIMING.match(stripped) or METADATA.match(stripped):
            continue
        # Bare numeric cue identifiers carry no dialogue.
        if stripped.isdigit():
            continue
        text = _clean_line(stripped)
        if not text:
            continue
        if lines and lines[-1] == text:
            continue
        lines.append(text)

    return "\n".join(lines)


def vtt_file_to_text_file(vtt_path, *, keep_vtt: bool = False):
    """Write `<stem>.txt` beside a `.vtt` file. Returns the text file's path.

    `vtt_path` is a pathlib.Path. With `keep_vtt` false the source is removed,
    since the text is what gets read and the timings are used nowhere.
    """
    text = vtt_to_text(vtt_path.read_text(encoding="utf-8"))
    # subs.pl.vtt -> subs.pl.txt
    txt_path = vtt_path.with_suffix(".txt")
    txt_path.write_text(text + "\n" if text else "", encoding="utf-8")
    if not keep_vtt:
        vtt_path.unlink()
    return txt_path
