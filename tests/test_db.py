import os
import sqlite3

import pytest

from vaillant_boiler_ui import db, migrations
from vaillant_boiler_ui.constants import SUMMARY_COLUMNS, VALUES


def tables(conn):
    return {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def test_new_database_has_the_latest_schema(conn):
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(
        migrations.MIGRATIONS
    )
    assert {
        "samples",
        "summaries",
        "changes",
        "commands",
        "schedule",
        "schedule_periods",
        "meta",
    } <= tables(conn)
    assert db.columns(conn, "samples") == ["id", "ms", *VALUES]
    assert db.columns(conn, "summaries") == ["tier", "ms", *SUMMARY_COLUMNS]


def test_reopening_changes_nothing(db_path):
    db.open_database(db_path).close()
    conn = db.open_database(db_path)
    assert not [
        f for f in os.listdir(os.path.dirname(db_path)) if "before" in f
    ]
    conn.close()


def test_new_migration_runs_once_with_a_backup(db_path, monkeypatch):
    conn = db.open_database(db_path)
    conn.execute("INSERT INTO meta VALUES ('x', 'kept')")
    conn.close()
    steps = migrations.MIGRATIONS + [
        ["CREATE TABLE extra (id INTEGER PRIMARY KEY)"],
        lambda c: c.execute("INSERT INTO extra VALUES (7)"),
    ]
    monkeypatch.setattr(db, "MIGRATIONS", steps)
    conn = db.open_database(db_path)
    assert conn.execute("SELECT id FROM extra").fetchall() == [(7,)]
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(steps)
    backup = sqlite3.connect(f"{db_path}.before-v{len(steps)}")
    assert backup.execute("SELECT value FROM meta").fetchall() == [("kept",)]
    assert "extra" not in tables(backup)
    conn.close()
    conn = db.open_database(db_path)  # already applied: not run again
    assert conn.execute("SELECT id FROM extra").fetchall() == [(7,)]


def test_failed_migration_changes_nothing(db_path, monkeypatch):
    db.open_database(db_path).close()
    steps = migrations.MIGRATIONS + [
        ["CREATE TABLE extra (id INTEGER)", "NOT SQL"],
    ]
    monkeypatch.setattr(db, "MIGRATIONS", steps)
    with pytest.raises(sqlite3.Error):
        db.open_database(db_path)
    conn = sqlite3.connect(db_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(
        migrations.MIGRATIONS
    )
    assert "extra" not in tables(conn)


def test_database_from_a_newer_version_is_refused(db_path):
    db.open_database(db_path).close()
    conn = sqlite3.connect(db_path)
    conn.execute(f"PRAGMA user_version = {len(migrations.MIGRATIONS) + 1}")
    conn.close()
    with pytest.raises(RuntimeError, match="newer version"):
        db.open_database(db_path)


def test_a_file_that_is_not_a_database_is_refused(tmp_path):
    path = tmp_path / "boiler.csv"
    path.write_text("time,Flame\n" * 100)
    with pytest.raises(sqlite3.DatabaseError):
        db.open_database(str(path))


def test_ensure_columns_adds_missing_ones(conn):
    assert db.ensure_columns(
        conn, "samples", ["Flame", "NewValue"], "TEXT"
    ) == ["NewValue"]
    assert db.columns(conn, "samples")[-1] == "NewValue"


def test_quote_refuses_odd_names():
    with pytest.raises(ValueError):
        db.quote('a"; DROP TABLE samples; --')


def test_transaction_rolls_back_on_error(conn):
    with pytest.raises(ZeroDivisionError):
        with db.transaction(conn):
            db.set_meta(conn, "k", "v")
            _ = 1 / 0
    assert db.get_meta(conn, "k") is None
