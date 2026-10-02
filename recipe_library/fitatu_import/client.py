"""Authenticating against Fitatu.

Uses `fitatu-api`, an unofficial community client that talks to Fitatu's HTTP API
directly. No MCP server is involved in importing.

Credentials come from the environment, falling back to the gitignored
`mcp/fitatu/.env` so the same account details are not stored twice.
"""

import os
from pathlib import Path

from ..paths import ROOT

ENV_FILE = ROOT / "mcp" / "fitatu" / ".env"


def _read_env_file(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE reader. Not a full dotenv parser; this file is ours."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def credentials() -> tuple[str, str]:
    """Return (email, password), or exit with an actionable message."""
    email = os.environ.get("FITATU_EMAIL")
    password = os.environ.get("FITATU_PASSWORD")

    if not (email and password):
        from_file = _read_env_file(ENV_FILE)
        email = email or from_file.get("FITATU_EMAIL")
        password = password or from_file.get("FITATU_PASSWORD")

    if not (email and password):
        raise SystemExit(
            "Fitatu credentials not found.\n"
            f"Set FITATU_EMAIL and FITATU_PASSWORD in the environment, or in {ENV_FILE}."
        )
    return email, password


def connect():
    """Log in and return a ready `FitatuApiClient`.

    Imported lazily so that the pure payload logic, and its tests, never need the
    dependency or the network.
    """
    from fitatu_api import FitatuApiClient

    email, password = credentials()
    return FitatuApiClient.login(email, password)
