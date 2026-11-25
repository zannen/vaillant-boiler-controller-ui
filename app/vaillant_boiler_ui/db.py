"""Opening the SQLite database, transactions and schema migrations."""

import contextlib
import os
import re
import sqlite3

from .constants import SUMMARY_COLUMNS, VALUES
from .migrations import MIGRATIONS
from .util import log

IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def connect(path):
    """A connection in autocommit mode: use transaction() to group writes.
    Waits up to a minute for another thread's write to finish."""
    conn = sqlite3.connect(
        path, timeout=60, isolation_level=None, check_same_thread=False
    )
    # WAL: readers don't block the writer. NORMAL: a power cut can lose the
    # last commits, but never corrupts the database.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextlib.contextmanager
def transaction(conn):
    """Run the block in one write transaction: all of it is saved, or none."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def quote(name):
    """A column name for SQL; only plain identifiers are allowed."""
    if not IDENTIFIER.match(name):
        raise ValueError(f"bad column name {name!r}")
    return f'"{name}"'


def columns(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({quote(table)})")]


def ensure_columns(conn, table, names, sql_type):
    """Add any of these columns the table lacks (e.g. a boiler value added to
    VALUES). Returns the names added."""
    have = set(columns(conn, table))
    added = [n for n in names if n not in have]
    for n in added:
        conn.execute(
            f"ALTER TABLE {quote(table)} ADD COLUMN {quote(n)} {sql_type}"
        )
    return added


def migrate(conn, path):
    """Bring the schema up to date. PRAGMA user_version holds the number of
    MIGRATIONS applied. Before changing an existing database it is copied to
    <path>.before-v<N>. Raises RuntimeError for a database made by a newer
    version of this program."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    latest = len(MIGRATIONS)
    if version > latest:
        raise RuntimeError(
            f"{path} has schema version {version}, but this program only "
            f"knows up to {latest}: it was made by a newer version"
        )
    if version == latest:
        return
    if version > 0:
        backup = f"{path}.before-v{latest}"
        if os.path.exists(backup):
            os.remove(backup)
        log(f"backing up {path} to {backup}")
        conn.execute("VACUUM INTO ?", (backup,))
    log(f"migrating {path} from schema version {version} to {latest}")
    with transaction(conn):
        for step in MIGRATIONS[version:]:
            if callable(step):
                step(conn)
            else:
                for statement in step:
                    conn.execute(statement)
        conn.execute(f"PRAGMA user_version = {latest:d}")


def get_meta(conn, key):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,))
    found = row.fetchone()
    return found[0] if found else None


def set_meta(conn, key, value):
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value)
    )


def open_database(path):
    """Connect, bring the schema up to date and add any new value columns.
    Raises sqlite3.Error (e.g. not a database) or RuntimeError."""
    conn = connect(path)
    migrate(conn, path)
    with transaction(conn):
        ensure_columns(conn, "samples", list(VALUES), "TEXT")
        ensure_columns(conn, "summaries", SUMMARY_COLUMNS, "REAL")
    return conn
