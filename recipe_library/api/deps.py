"""Shared request plumbing: the database connection and the Fitatu client.

A connection per request, closed when the request ends. SQLite connections are
not safe to share across threads and FastAPI runs sync handlers in a
threadpool, so a module-level connection would be a bug waiting for the second
concurrent request. Opening one costs microseconds against a local file, and
WAL mode means a reader never blocks on the writer.

The Fitatu client is the opposite: logging in costs a round trip and real
credentials, so it is created only for the endpoints that actually talk to
Fitatu, and never at import time.
"""

import sqlite3
from typing import Iterator

from fastapi import Depends, HTTPException
from typing_extensions import Annotated

from ..db import connect, check_ready

# Overridden in tests via `app.dependency_overrides`, which is why the database
# path is reached through a function rather than read at import time.
_DB_OVERRIDE: str | None = None


def set_database(path: str | None) -> None:
    """Point the application at a specific database file."""
    global _DB_OVERRIDE
    _DB_OVERRIDE = path


def get_conn() -> Iterator[sqlite3.Connection]:
    conn = connect(_DB_OVERRIDE, create=False)
    try:
        check_ready(conn)
        yield conn
    finally:
        conn.close()


Conn = Annotated[sqlite3.Connection, Depends(get_conn)]


def fitatu_client():
    """A logged-in Fitatu client, or a 503 explaining what is missing.

    Credentials come from the environment or the gitignored `mcp/fitatu/.env`.
    A missing credential is a configuration problem, not a bad request, and
    `connect()` raises SystemExit for it -- which would otherwise shut down the
    server rather than fail the request.
    """
    from ..fitatu_import.client import account_id, connect as fitatu_connect

    try:
        client = fitatu_connect()
    except SystemExit as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail=f"Fitatu login failed: {type(exc).__name__}: {exc}"
        ) from exc
    return client, account_id(client)
