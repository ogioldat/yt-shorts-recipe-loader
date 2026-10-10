"""Repository layout. One place that knows where things live."""

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.yaml"
STATE = ROOT / "state"
SCHEMA = Path(__file__).resolve().parent / "schema.sql"

# The library itself: ledger, raw video text, recipes and products.
# RECIPE_LIBRARY_DB overrides it, which is what the tests and a second checkout
# use instead of hardcoding this one.
DB = Path(os.environ.get("RECIPE_LIBRARY_DB") or STATE / "library.db")

# Where the YAML tree used to live. Still named because migrate_yaml.py reads
# it, and because `raw/` remains in git history as the provenance for every
# recipe extracted before the move.
LEGACY_RAW = ROOT / "raw"
LEGACY_RECIPES = ROOT / "recipes"
LEGACY_LEDGER = STATE / "processed.yaml"
LEGACY_PRODUCTS = STATE / "fitatu_products.yaml"


def load_config() -> dict:
    """Read config.yaml, failing clearly when the playlist is not yet set."""
    if not CONFIG.exists():
        raise SystemExit(f"Missing {CONFIG}. Copy the example and set playlist_url.")
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}
    if not config.get("playlist_url"):
        raise SystemExit(
            f"playlist_url is not set in {CONFIG}.\n"
            "Add the public YouTube playlist URL holding your recipe shorts."
        )
    # `sub_langs` was a request list; `original_langs` is an allowlist of
    # original languages, because only the original caption track is fetched.
    config.setdefault("original_langs", config.pop("sub_langs", None) or ["pl", "en"])
    config.setdefault("cookies_from_browser", None)
    config.setdefault("subtitle_output", "text")
    config.setdefault("fetch_until_complete", False)
    config.setdefault("max_passes", 20)
    config.setdefault("pass_delay_seconds", 60)
    config.setdefault("max_stalled_passes", 3)
    # Product resolution. `search_locales` is tried in order: the catalogue is
    # Polish, so pl_PL first, then whatever the account defaults to.
    config.setdefault("search_locales", ["pl_PL", None])
    config.setdefault("search_limit", 10)
    config.setdefault("match_min_score", 0.62)
    config.setdefault("match_min_gap", 0.08)
    return config
