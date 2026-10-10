"""The SQLite connection, and applying the schema.

Plain `sqlite3` and plain SQL. The database is one file holding a few thousand
rows for a single user; an ORM and a migration framework would be more moving
parts than the thing they manage.

Three pragmas matter and are set on every connection, because SQLite's defaults
are wrong for this use:

  * `foreign_keys = ON` is off by default, per connection, which means every
    REFERENCES clause in schema.sql is decoration until this runs.
  * `journal_mode = WAL` lets the HTTP server read while a fetch or an import
    writes. Without it a long write blocks every reader.
  * `busy_timeout` makes a concurrent writer wait rather than raising
    "database is locked" immediately.
"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .paths import DB, SCHEMA

SCHEMA_VERSION = 1

BUSY_TIMEOUT_MS = 5000

# Tables that must exist for the database to be usable at all. Checked rather
# than assumed, so a truncated or hand-edited file fails with something more
# useful than a missing-column error ten frames deep.
CORE_TABLES = ("videos", "recipes", "ingredients", "products", "ingredient_products")


def connect(path: Path | str | None = None, *, create: bool = True) -> sqlite3.Connection:
    """Open a connection with the pragmas this schema depends on.

    `path` may be ":memory:", which is what the tests use. Passing
    `create=False` refuses to bring a missing database into existence, so a
    typo in a path cannot silently produce an empty library.
    """
    target = DB if path is None else path
    if target != ":memory:":
        target = Path(target)
        if not target.exists():
            if not create:
                raise SystemExit(
                    f"No database at {target}.\n"
                    "Run: uv run python -m recipe_library.migrate_yaml"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
        target = str(target)

    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    if target != ":memory:":
        # WAL is persistent, so this is a no-op after the first time.
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


def schema_sql() -> str:
    return SCHEMA.read_text(encoding="utf-8")


def apply_schema(conn: sqlite3.Connection) -> None:
    """Create everything the schema defines. Idempotent."""
    conn.executescript(schema_sql())
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    if row is None or row["v"] is None:
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) "
            "VALUES (?, datetime('now'))",
            (SCHEMA_VERSION,),
        )
    conn.commit()


def version(conn: sqlite3.Connection) -> int | None:
    """The applied schema version, or None when the schema is not there yet."""
    try:
        row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    except sqlite3.OperationalError:
        return None
    return row["v"] if row else None


def check_ready(conn: sqlite3.Connection) -> None:
    """Fail clearly when asked to read a database that holds no schema."""
    names = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    missing = [t for t in CORE_TABLES if t not in names]
    if missing:
        raise SystemExit(
            f"Database at {DB} is missing table(s): {', '.join(missing)}.\n"
            "Run: uv run python -m recipe_library.migrate_yaml"
        )


def fresh(path: Path | str | None = None) -> sqlite3.Connection:
    """A connection with the schema applied. The normal way in."""
    conn = connect(path)
    apply_schema(conn)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    """Commit on success, roll back on any exception.

    `sqlite3`'s own implicit transactions do not cover DDL or SELECT, and its
    context manager does not close over the "roll back a half-written recipe"
    case this is used for, so the behaviour is spelled out here.
    """
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


@contextmanager
def open_db(path: Path | str | None = None, *, create: bool = False):
    """Open, use, close. For CLI entry points and request handlers."""
    conn = connect(path, create=create)
    try:
        if create:
            apply_schema(conn)
        else:
            check_ready(conn)
        yield conn
    finally:
        conn.close()
