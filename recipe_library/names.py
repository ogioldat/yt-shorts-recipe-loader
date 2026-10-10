"""Normalising ingredient names. One function, kept separate on purpose.

This is the key the product mapping is built on, so both the store and the
resolver need it, and putting it in either of them would make the other import
through it. It is pure and has no dependencies.
"""

import re


def normalize_name(name: str | None) -> str:
    """The lookup key for an ingredient name.

    Case and spacing are noise; Polish diacritics are not, and are preserved
    because the key doubles as the `search_food` query. `Pierś z Kurczaka` and
    `pierś z kurczaka` are one ingredient; `maka` and `mąka` are not.
    """
    return re.sub(r"\s+", " ", (name or "").strip()).lower()
