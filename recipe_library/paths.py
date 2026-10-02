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
    config.setdefault("sub_langs", ["pl", "en"])
    config.setdefault("cookies_from_browser", None)
    return config
