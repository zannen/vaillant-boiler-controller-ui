import collections
import csv
import datetime
import os
import threading

from .csv_files import needs_header, tail_lines
from .util import epoch_ms


class CommandLog:
    """Every SetMode instruction sent to the boiler: appended to
    <csv name>.commands.csv and the last KEEP_DAYS kept in memory for the
    chart, as [epoch ms, "on"|"off", 1 if accepted else 0, source]. source is
    "schedule", or "override:<user>" for the override button."""

    HEADER = ["time", "ms", "state", "ok", "result", "source"]
    KEEP_DAYS = 31

    def __init__(self, path, hub, writer):
        self.path, self.hub, self.writer = path, hub, writer
        self.lock = threading.Lock()
        self.rows = collections.deque(maxlen=60000)

    def load(self):
        if not os.path.exists(self.path):
            return 0
        cutoff = epoch_ms() - self.KEEP_DAYS * 86400000
        lines, _ = tail_lines(self.path, self.rows.maxlen * 120)
        rows = []
        for r in csv.reader(lines):
            if (
                len(r) != len(self.HEADER)
                or r[0] == "time"
                or r[2] not in ("on", "off")
            ):
                continue
            try:
                ms = int(r[1])
            except ValueError:
                continue
            if ms >= cutoff:
                rows.append([ms, r[2], int(r[3] == "1"), r[5] or "schedule"])
        rows.sort(key=lambda r: r[0])
        with self.lock:
            self.rows.extend(rows)
        return len(rows)

    def add(self, state, ok, result, source):
        ms = epoch_ms()
        when = datetime.datetime.fromtimestamp(ms / 1000).isoformat(
            timespec="milliseconds"
        )
        if needs_header(self.path) and not self.rows:
            self.writer.csv_row(self.HEADER, self.path)
        self.writer.csv_row(
            [when, ms, state, int(ok), result, source], self.path
        )
        row = [ms, state, int(ok), source]
        with self.lock:
            self.rows.append(row)
            cutoff = ms - self.KEEP_DAYS * 86400000
            while self.rows and self.rows[0][0] < cutoff:
                self.rows.popleft()
        self.hub.broadcast({"type": "command", "row": row})

    def get(self):
        with self.lock:
            return list(self.rows)
