"""Writing to the database while running, and loading it back at startup."""

import sqlite3
import time

import pytest

from vaillant_boiler_ui.aggregator import Aggregator
from vaillant_boiler_ui.command_log import CommandLog
from vaillant_boiler_ui.db import open_database
from vaillant_boiler_ui.db_writer import DbWriter
from vaillant_boiler_ui.history import (
    load_changes,
    load_history,
    load_summaries,
)
from vaillant_boiler_ui.hub import Hub
from vaillant_boiler_ui.schedule import Schedule
from vaillant_boiler_ui.util import epoch_ms

TEMPS = {"flowTemp": 75.0, "hwcTemp": 52.5}


@pytest.fixture
def db_writer(conn, db_path):  # conn: the database exists
    w = DbWriter(db_path, 3600)
    w.start()
    yield w
    w.stop()


def test_hub_poller_and_commands_rows_reach_the_database(
    conn, db_path, db_writer
):
    agg = Aggregator("1min", 1, 120)  # 1 ms buckets: each publish closes one
    hub = Hub(db_writer, 60, [agg])
    commands = CommandLog(hub, db_writer)
    start = epoch_ms() - 5 * 60_000
    for i, flow in enumerate(["50.0;ok", "50.5;ok", "52.0;ok"]):
        db_writer.insert(
            "samples",
            ["ms", "Flame", "FlowTemp"],
            [start + i * 1000, "on", flow],
        )
    hub.record("Flame", "off", "2026-11-25T10:00:00.000")
    hub.record("Flame", "on", "2026-11-25T10:00:01.000")
    hub.values["FlowTemp"] = "50;ok"
    hub.publish("t", None, True)
    time.sleep(0.005)
    hub.publish("t", None, True)
    commands.add("off", True, "done", "schedule", (60.0, 48.5))
    db_writer.stop()

    history = load_history(conn, 60)
    assert [r[0] for r in history] == [start, start + 1000, start + 2000]
    assert history[2][4] == pytest.approx(1.5)  # rise
    assert [(c["old"], c["new"]) for c in load_changes(conn)] == [
        ("off", "on")
    ]
    assert len(load_summaries(conn, agg)) == 1
    loaded = CommandLog(hub, db_writer)
    assert loaded.load(conn) == 1
    assert loaded.get()[0][1:] == ["off", 1, "schedule", 60.0, 48.5]


def test_rows_are_kept_while_the_database_cannot_be_written(conn, db_path):
    w = DbWriter(db_path, 3600)
    w.conn = open_database(db_path)
    w.insert("samples", ["ms", "Flame"], [1, "on"])
    blocker = sqlite3.connect(db_path, timeout=0)
    blocker.execute("BEGIN EXCLUSIVE")
    w.conn.execute("PRAGMA busy_timeout = 0")
    w.flush()  # locked: kept
    assert len(w.pending) == 1
    blocker.execute("ROLLBACK")
    w.insert("samples", ["ms", "Flame"], [2, "off"])
    w.flush()
    assert not w.pending
    assert conn.execute("SELECT ms, Flame FROM samples").fetchall() == [
        (1, "on"),
        (2, "off"),
    ]


def test_schedule_is_saved_and_loaded(conn, db_path):
    schedule = Schedule(conn, TEMPS)
    assert schedule.get()["enabled"] is False
    assert schedule.temps() == (75.0, 52.5)
    days = {"mon": [["06:30", "08:30"], ["17:00", "24:00"]], "sun": []}
    schedule.set({"enabled": True, "days": days, "hwcTemp": 50.0}, "alice")
    again = Schedule(open_database(db_path), TEMPS).get()
    assert again["enabled"] is True
    assert again["days"]["mon"] == days["mon"]
    assert again["days"]["tue"] == []
    assert (again["flowTemp"], again["hwcTemp"]) == (75.0, 50.0)
    assert again["updatedBy"] == "alice"


def test_bad_schedule_in_the_database_stops_the_program(conn):
    Schedule(conn, TEMPS).set({"enabled": True, "days": {}}, "alice")
    conn.execute(
        "INSERT INTO schedule_periods VALUES ('mon', '09:00', '08:00')"
    )
    with pytest.raises(SystemExit, match="bad"):
        Schedule(conn, TEMPS)
