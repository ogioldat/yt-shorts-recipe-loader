"""Repository layout. One place that knows where things live."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.yaml"
RAW = ROOT / "raw"
RECIPES = ROOT / "recipes"
STATE = ROOT / "state"
LEDGER = STATE / "processed.yaml"


def video_dir(video_id: str) -> Path:
    return RAW / video_id


def meta_path(video_id: str) -> Path:
    return video_dir(video_id) / "meta.json"


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
    return config
