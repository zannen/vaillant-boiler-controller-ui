import datetime
import io
import json

from vaillant_boiler_ui.aggregator import Aggregator
from vaillant_boiler_ui.constants import TIERS
from vaillant_boiler_ui.csv_import import (
    import_key,
    import_old_files,
    records,
)
from vaillant_boiler_ui.csv_importer import CsvImporter
from vaillant_boiler_ui.db import get_meta
from vaillant_boiler_ui.history import load_changes, load_history
from vaillant_boiler_ui.hub import Hub
from vaillant_boiler_ui.schedule import Schedule

TEMPS = {"flowTemp": 75.0, "hwcTemp": 52.5}


def iso(ms):
    return datetime.datetime.fromtimestamp(ms / 1000).isoformat(
        timespec="milliseconds"
    )


def now_ms():
    return int(datetime.datetime.now().timestamp() * 1000)


def logger_csv(path, start_ms, n, step_ms=1000):
    """An old-style logger CSV: a restart with fewer columns, crash debris,
    then n rows a second apart. Returns the number of good rows."""
    lines = [
        "time,Flame,FlowTemp",
        f"{iso(start_ms - 5000)},off,40.0;ok",
        "\0\0\0\0",
        f"{iso(start_ms - 4000)},on",  # cut short by a crash
        "time,Flame,FlowTemp,OldValue",
    ]
    for i in range(n):
        lines.append(
            f"{iso(start_ms + i * step_ms)},on,{50 + i * 0.1:.1f};ok,7"
        )
    lines.append("not a time,on,50;ok,7")
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write("\n".join(lines) + "\n")
    return n + 1


def test_records_switches_header_and_skips_debris():
    data = b"time,a\n1,x\nbad,row,here\n\0\0time,a,b\n2,y,z\n3,w\0\0"
    got = [(h, r) for h, r, _ in records(io.BytesIO(data))]
    assert got == [
        (["time", "a"], ["1", "x"]),
        (["time", "a", "b"], ["2", "y", "z"]),
    ]


def write_small_files(tmp_path):
    (tmp_path / "boiler.changes.log").write_text(
        "2026-11-20T10:00:00.000: Flame: off -> on\n"
        "garbage\n"
        "2026-11-20T10:01:00.000: Statenumber: S04 -> S31\n"
    )
    (tmp_path / "boiler.commands.csv").write_text(
        "time,ms,state,ok,result,source\n"
        "2026-11-20T10:00:00.000,1795600000000,on,1,done,schedule\n"
        "time,ms,state,ok,result,source,flow,hwc\n"
        "2026-11-20T10:10:00.000,1795600600000,off,0,ERR: x,"
        "override:bob,60,48.5\n"
    )
    (tmp_path / "boiler.1min.csv").write_text(
        "time,ms,FlowTemp_mean,FlowTemp_min,FlowTemp_max\n"
        "2026-11-20T10:00:00,1795600000000,50.5,50,51\n"
    )
    (tmp_path / "schedule.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "days": {"mon": [["06:30", "08:30"]]},
                "flowTemp": 60,
                "updatedBy": "alice",
                "updatedAt": "2026-11-20T09:00:00.000",
            }
        )
    )


def import_all(conn, tmp_path):
    return import_old_files(
        conn,
        str(tmp_path / "boiler.csv"),
        str(tmp_path / "boiler.changes.log"),
        str(tmp_path / "schedule.json"),
        TEMPS,
    )


def test_small_files_are_imported_once(conn, tmp_path):
    write_small_files(tmp_path)
    assert import_all(conn, tmp_path) is False  # no logger CSV
    changes = load_changes(conn)
    assert [(c["name"], c["old"], c["new"]) for c in changes] == [
        ("Flame", "off", "on"),
        ("Statenumber", "S04", "S31"),
    ]
    assert conn.execute(
        "SELECT ms, state, ok, result, source, flow_temp, hwc_temp "
        "FROM commands ORDER BY ms"
    ).fetchall() == [
        (1795600000000, "on", 1, "done", "schedule", None, None),
        (1795600600000, "off", 0, "ERR: x", "override:bob", 60.0, 48.5),
    ]
    assert conn.execute(
        "SELECT tier, ms, FlowTemp_mean, FlowTemp_min, ReturnTemp_mean "
        "FROM summaries"
    ).fetchall() == [("1min", 1795600000000, 50.5, 50.0, None)]
    schedule = Schedule(conn, TEMPS).get()
    assert schedule["enabled"] is True
    assert schedule["days"]["mon"] == [["06:30", "08:30"]]
    assert (schedule["flowTemp"], schedule["hwcTemp"]) == (60.0, 52.5)
    assert schedule["updatedBy"] == "alice"

    import_all(conn, tmp_path)  # again: nothing is doubled
    assert len(load_changes(conn)) == 2
    assert conn.execute("SELECT COUNT(*) FROM commands").fetchone()[0] == 2


def test_saved_schedule_is_not_replaced_by_schedule_json(conn, tmp_path):
    write_small_files(tmp_path)
    Schedule(conn, TEMPS).set({"enabled": False, "days": {}}, "carol")
    import_all(conn, tmp_path)
    assert Schedule(conn, TEMPS).get()["updatedBy"] == "carol"


def make_hub(writer):
    aggs = [Aggregator(name, ms, keep) for name, ms, keep in TIERS]
    return Hub(writer, 60, aggs)


def test_logger_csv_is_imported_in_the_background(
    conn, db_path, tmp_path, writer
):
    # 30 minutes of samples 3 s apart: every tier finishes a bucket
    start = now_ms() - 35 * 60000
    expected = logger_csv(tmp_path / "boiler.csv", start, 600, 3000)
    assert import_all(conn, tmp_path) is True  # no summary files: backfill
    hub = make_hub(writer)
    hub.history.append([now_ms()] + [None] * 14)  # a live sample
    importer = CsvImporter(db_path, str(tmp_path / "boiler.csv"), hub)
    importer.CHUNK = 7
    importer.work()

    rows = conn.execute("SELECT ms, Flame, FlowTemp, OldValue FROM samples")
    rows = rows.fetchall()
    assert len(rows) == expected
    assert rows[0] == (start - 5000, "off", "40.0;ok", None)
    assert rows[-1] == (start + 599 * 3000, "on", "109.9;ok", "7")
    state = json.loads(
        get_meta(conn, import_key(str(tmp_path / "boiler.csv")))
    )
    assert state["done"] is True
    # summaries built for both tiers, and the hub reloaded
    tiers = dict(
        conn.execute("SELECT tier, COUNT(*) FROM summaries GROUP BY tier")
    )
    assert tiers["1min"] >= 29 and tiers["15min"] >= 1
    assert len(hub.history) == expected + 1
    assert hub.history[-1][1:] == [None] * 14  # the live one kept, last
    assert len(hub.aggs[0].rows) == tiers["1min"]
    # rise worked out from FlowTemp: 0.1 degrees every 3 s
    assert abs(hub.history[100][4] - 0.1 / 3) < 1e-3
    assert import_all(conn, tmp_path) is False  # done: not imported again


def test_logger_csv_import_carries_on_after_a_restart(
    conn, db_path, tmp_path, writer
):
    path = tmp_path / "boiler.csv"
    start = now_ms() - 30 * 60000
    expected = logger_csv(path, start, 50)
    import_all(conn, tmp_path)

    class Stop(Exception):
        pass

    saves = []
    real_save = CsvImporter.save

    def save_then_stop(*args):
        real_save(*args)
        saves.append(1)
        if len(saves) == 2:
            raise Stop

    importer = CsvImporter(db_path, str(path), make_hub(writer))
    importer.CHUNK = 10
    importer.save = save_then_stop
    try:
        importer.work()
    except Stop:
        pass
    assert conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 20
    importer = CsvImporter(db_path, str(path), make_hub(writer))
    importer.work()
    count, distinct = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT ms) FROM samples"
    ).fetchone()
    assert count == distinct == expected
    assert len(load_history(conn, 60)) == expected
