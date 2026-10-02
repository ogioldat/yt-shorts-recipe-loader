"""Recipe ids: ASCII slugs derived from titles, stable and collision-free.

Polish diacritics need an explicit map. Unicode decomposition handles most of
them (ą -> a) but not ł, which has no combining-mark decomposition and would
otherwise be dropped entirely, turning "łosoś" into "oso".
"""

import re
import unicodedata

POLISH_MAP = str.maketrans(
    {
        "ł": "l", "Ł": "l",
        "ą": "a", "Ą": "a",
        "ć": "c", "Ć": "c",
        "ę": "e", "Ę": "e",
        "ń": "n", "Ń": "n",
        "ó": "o", "Ó": "o",
        "ś": "s", "Ś": "s",
        "ź": "z", "Ź": "z",
        "ż": "z", "Ż": "z",
    }
)

MAX_SLUG_LEN = 60


def slugify(text: str) -> str:
    """Convert a recipe title into a lowercase ASCII slug."""
    text = text.translate(POLISH_MAP)
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    if len(text) > MAX_SLUG_LEN:
        text = text[:MAX_SLUG_LEN].rsplit("-", 1)[0].strip("-")
    return text or "recipe"


def unique_slug(base: str, taken: set[str]) -> str:
    """Return `base`, or `base-2`, `base-3`, ... if already taken.

    `taken` is not mutated; callers decide when a slug is really claimed.
    """
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"
