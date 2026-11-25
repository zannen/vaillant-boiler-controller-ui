"""One-off import of the files older versions wrote: the logger CSV, its
summaries, change log and boiler instructions, and schedule.json.

Each file is imported once; a meta row "import:<absolute path>" records that.
The small files are imported at startup, each in one transaction. The logger
CSV can hold months of data, so CsvImporter imports it in the background,
recording its progress as JSON in the same meta row so that it can carry on
after a restart."""

import csv
import datetime
import json
import os

from .constants import SUMMARY_COLUMNS, TIERS
from .db import IDENTIFIER, get_meta, set_meta, transaction
from .samples import number
from .schedule import Schedule
from .schedule_rules import validate_schedule
from .util import log


def import_key(path):
    return f"import:{os.path.abspath(path)}"


def parse_ms(text):
    """Epoch ms of a local ISO time, or None."""
    try:
        return int(datetime.datetime.fromisoformat(text).timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def records(f, offset=0, header=None):
    """(columns, row, offset of the next line) for each data row of a CSV
    file this program wrote, read from a binary file at offset. Header lines
    ("time,...", written at each restart) switch the columns. Crash debris is
    skipped: NUL bytes split a line into fragments, and rows that don't match
    their header are dropped."""
    for raw in f:
        offset += len(raw)
        for piece in raw.split(b"\0"):
            line = piece.decode("utf-8", "replace").strip("\r\n")
            if not line.strip():
                continue
            try:
                row = next(csv.reader([line]))
            except (csv.Error, StopIteration):
                continue
            if row[0] == "time":
                header = row
            elif header is not None and len(row) == len(header):
                yield header, row, offset


def summary_paths(csv_path):
    """Tier name -> the summary CSV older versions kept for it."""
    base = os.path.splitext(csv_path)[0]
    return {name: f"{base}.{name}.csv" for name, _, _ in TIERS}


def commands_path(csv_path):
    return os.path.splitext(csv_path)[0] + ".commands.csv"


def import_changes(conn, path):
    rows = []
    with open(path, "rb") as f:
        for raw in f:
            line = raw.replace(b"\0", b"").decode("utf-8", "replace")
            try:
                when, name, rest = line.strip("\r\n").split(": ", 2)
                old, new = rest.rsplit(" -> ", 1)
            except ValueError:
                continue
            ms = parse_ms(when)
            if ms is not None:
                rows.append((ms, when, name, old, new))
    conn.executemany(
        "INSERT INTO changes (ms, time, name, old, new) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def import_commands(conn, path):
    rows = []
    with open(path, "rb") as f:
        for header, row, _ in records(f):
            r = dict(zip(header, row))
            try:
                ms = int(r["ms"])
            except (KeyError, ValueError):
                continue
            if r.get("state") not in ("on", "off"):
                continue
            rows.append(
                (
                    ms,
                    r["state"],
                    int(r.get("ok") == "1"),
                    r.get("result", ""),
                    r.get("source") or "schedule",
                    number(r.get("flow")),
                    number(r.get("hwc")),
                )
            )
    conn.executemany(
        "INSERT INTO commands (ms, state, ok, result, source, flow_temp, "
        "hwc_temp) VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def import_summaries(conn, path, tier):
    cols = ", ".join(f'"{c}"' for c in SUMMARY_COLUMNS)
    marks = ", ".join("?" * (len(SUMMARY_COLUMNS) + 2))
    rows = []
    with open(path, "rb") as f:
        for header, row, _ in records(f):
            r = dict(zip(header, row))
            try:
                ms = int(r["ms"])
            except (KeyError, ValueError):
                continue
            values = []
            for c in SUMMARY_COLUMNS:
                try:
                    values.append(float(r[c]))
                except (KeyError, ValueError):
                    values.append(None)
            rows.append((tier, ms, *values))
    conn.executemany(
        f"INSERT OR IGNORE INTO summaries (tier, ms, {cols}) "
        f"VALUES ({marks})",
        rows,
    )
    return len(rows)


def import_schedule(conn, path, temps):
    if Schedule.read(conn) is not None:
        return "kept the schedule already in the database"
    with open(path, encoding="utf-8") as f:
        saved = json.load(f)
    checked, err = validate_schedule(saved)
    if err:
        raise ValueError(err)
    Schedule.write(
        conn,
        {
            **temps,
            **checked,
            "updatedBy": saved.get("updatedBy"),
            "updatedAt": saved.get("updatedAt"),
        },
    )
    return "imported the schedule"


def import_old_files(conn, csv_path, changes_path, schedule_path, temps):
    """Import the small files that exist and haven't been imported, and set
    up the background import of the logger CSV. Returns True if that import
    has work to do."""
    small = [(schedule_path, lambda p: import_schedule(conn, p, temps))]
    small.append((changes_path, lambda p: import_changes(conn, p)))
    small.append((commands_path(csv_path), lambda p: import_commands(conn, p)))
    # summary files that never existed are built from the logger CSV
    backfill = []
    for tier, path in summary_paths(csv_path).items():
        if os.path.exists(path):
            small.append(
                (path, lambda p, t=tier: import_summaries(conn, p, t))
            )
        else:
            backfill.append(tier)
    for path, do_import in small:
        if not os.path.exists(path) or get_meta(conn, import_key(path)):
            continue
        log(f"importing {path} (once only)")
        try:
            with transaction(conn):
                result = do_import(path)
                set_meta(conn, import_key(path), "done")
        except (OSError, ValueError) as e:
            raise SystemExit(f"cannot import {path}: {e}") from e
        log(
            f"{path}: {result}"
            if isinstance(result, str)
            else f"{path}: imported {result} rows"
        )
    if not os.path.exists(csv_path):
        return False
    state = get_meta(conn, import_key(csv_path))
    if state is None:
        state = json.dumps(
            {"offset": 0, "header": None, "backfill": backfill, "done": False}
        )
        with transaction(conn):
            set_meta(conn, import_key(csv_path), state)
    return not json.loads(state)["done"]


def sample_columns(header):
    """Indexes and names of the value columns in a logger CSV header that can
    be database columns."""
    return [
        (i, name)
        for i, name in enumerate(header)
        if i > 0 and IDENTIFIER.match(name)
    ]
