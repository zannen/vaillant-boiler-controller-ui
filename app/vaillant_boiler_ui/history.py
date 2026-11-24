"""Loading chart history, summaries and changes from disk at startup."""

import csv
import os

from .constants import CHANGE_HISTORY
from .csv_files import csv_samples, header_before, tail_lines
from .util import epoch_ms


def load_history(hub, csv_path):
    """Fill the chart history from the end of the CSV file; returns the number
    of samples loaded."""
    if not os.path.exists(csv_path):
        return 0
    lines, start = tail_lines(csv_path, hub.history_minutes * 60 * 400)
    header = None
    if not (lines and lines[0].startswith("time,")):
        header = header_before(csv_path, start)
    cutoff = epoch_ms() - hub.history_minutes * 60000
    rows = [s for s in csv_samples(lines, header) if s[0] >= cutoff]
    rows.sort(key=lambda r: r[0])  # the clock can jump at boot before NTP sync
    with hub.lock:
        hub.history.extend(rows)
    return len(rows)


def load_agg(agg):
    """Fill an aggregator from the end of its file; returns rows loaded, or
    None if there is no file."""
    if not os.path.exists(agg.path):
        return None
    wanted = agg.keep_minutes * 60000 // agg.bucket_ms + 10
    lines, start = tail_lines(agg.path, wanted * 600)
    header = None
    if not (lines and lines[0].startswith("time,")):
        header = header_before(agg.path, start)
    names = agg.header()[2:]
    rows = []
    for r in csv.reader(lines):
        if not r:
            continue
        if r[0] == "time":
            header = r
            continue
        if header is None or len(r) != len(header):
            continue
        cols = dict(zip(header, r))
        try:
            row = [int(cols["ms"])]
        except (KeyError, ValueError):
            continue
        for n in names:
            try:
                row.append(float(cols[n]))
            except (KeyError, ValueError):
                row.append(None)
        rows.append(row)
    rows.sort(key=lambda r: r[0])
    for row in rows:
        if not agg.rows or row[0] > agg.rows[-1][0]:
            agg.append(row)
    return len(agg.rows)


def backfill(aggs, csv_path, writer):
    """Build missing summary files from the whole logger CSV (done once).
    Returns samples read."""
    if not os.path.exists(csv_path):
        return 0
    cutoff = epoch_ms() - max(a.keep_minutes for a in aggs) * 60000
    count = 0
    with open(csv_path, "rb") as f:
        lines = (
            line.replace(b"\0", b"").decode("utf-8", "replace") for line in f
        )
        for sample in csv_samples(lines):
            count += 1
            if sample[0] < cutoff:
                continue
            for a in aggs:
                row = a.add(sample)
                if row:
                    writer.csv_row(a.file_row(row), a.path)
    return count


def load_changes(hub, changes_path):
    """Fill the change list from the end of the change log; returns the number
    of entries loaded."""
    if not os.path.exists(changes_path):
        return 0
    lines, _ = tail_lines(changes_path, CHANGE_HISTORY * 200)
    entries = []
    for line in lines[-CHANGE_HISTORY:]:
        try:
            when, name, rest = line.split(": ", 2)
            old, new = rest.rsplit(" -> ", 1)
        except ValueError:
            continue
        entries.append({"time": when, "name": name, "old": old, "new": new})
    with hub.lock:
        hub.changes.extend(entries)
    return len(entries)
