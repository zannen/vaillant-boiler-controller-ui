#!/usr/bin/env python3
"""Poll ebusd for Vaillant BAI values, append them to a CSV file and serve a
live web UI.

Usage: vaillant-boiler-ui.py [CSV_PATH] --users HTPASSWD --roles ROLES
           [--port 8000] [--changes PATH] [--ebusd HOST:PORT]
           [--history-minutes 60] [--flush-seconds 30] [--secret-file PATH]
           [--token-hours 12] [--session-days 30]
Then open http://<pi-address>:8000/ in a browser on the local network (add
?minutes=N to choose the chart window, e.g.
http://<pi-address>:8000/?minutes=5).

Users: an htpasswd file with bcrypt hashes (htpasswd -B), plus a roles file in
Apache group-file format, one line per role (a user in several roles gets the
highest; a user in none cannot log in):
    admin: alice
    user: bob carol
    viewer: guest
Needs the bcrypt module (Raspberry Pi OS: sudo apt install python3-bcrypt).
"""

import argparse
import base64
import hashlib
import hmac
import collections
import csv
import datetime
import io
import json
import math
import os
import queue
import shutil
import signal
import socket
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import bcrypt
except ImportError:
    sys.exit(
        "vaillant-boiler-ui.py needs the bcrypt module: "
        "sudo apt install python3-bcrypt"
    )

# name -> minimum seconds between reads (0 = every pass)
VALUES = {
    "Flame": 0,
    "Statenumber": 0,
    "FanSpeed": 0,
    "IonisationVoltageLevel": 0,
    "HwcWaterflow": 0,
    "FlowTemp": 0,
    "Gasvalve": 0,
    "PumpPower": 0,
    "Ignitor": 2,
    "ReturnTemp": 2,
    "HwcTemp": 2,
    "PositionValveSet": 2,
    "HwcDemand": 2,
    "StorageTemp": 10,
    "WaterPressure": 30,
    "Currenterror": 30,
    "DeactivationsTemplimiter": 30,
    "DeactivationsIFC": 30,
    "CounterStartattempts1": 30,
}

# Numeric values only appear in the change list once they have moved this far
# from the last listed value. Everything else (on/off, status numbers,
# counters, error codes) is listed on every change.
THRESHOLDS = {
    "FlowTemp": 1.0,
    "ReturnTemp": 1.0,
    "HwcTemp": 1.0,
    "StorageTemp": 1.0,
    "HwcWaterflow": 0.5,
    "FanSpeed": 200,
    "WaterPressure": 0.05,
    "IonisationVoltageLevel": 10,
    "PumpPower": 10,
}

# Values kept as a time series for the charts; switches are stored as 1/0.
SAMPLE_KEYS = [
    "FlowTemp",
    "ReturnTemp",
    "HwcTemp",
    "rise",
    "FanSpeed",
    "HwcWaterflow",
    "PumpPower",
    "WaterPressure",
    "IonisationVoltageLevel",
    "Flame",
    "Gasvalve",
    "HwcDemand",
    "Statenumber",
    "PositionValveSet",
]
SWITCHES = {"Flame", "Gasvalve", "HwcDemand"}

CHANGE_HISTORY = 500
# samples further apart than this are not joined (and give no rise value)
GAP_MS = 15000

# Long chart windows use per-bucket summaries (mean/min/max of each sample
# value), kept in memory and appended to <csv name>.<tier>.csv:
# (name, bucket ms, minutes kept)
TIERS = [
    ("1min", 60_000, 2 * 1440),
    ("15min", 15 * 60_000, 31 * 1440),
]


def stamp():
    return datetime.datetime.now().isoformat(timespec="milliseconds")


def log(msg):
    """Print a timestamped message to stderr."""
    print(f"{datetime.datetime.now():%H:%M:%S} {msg}", file=sys.stderr)


def epoch_ms():
    return int(time.time() * 1000)


def number(raw):
    """First field of an ebusd value as a float, e.g. "51.06;ok" -> 51.06."""
    try:
        f = float(raw.split(";")[0])
    except (ValueError, AttributeError):
        return None
    return f if math.isfinite(f) else None


def display(name, raw):
    return raw.split(";")[0] if name in THRESHOLDS else raw


def significant(name, old, new):
    if old == new:
        return False
    step = THRESHOLDS.get(name)
    a, b = number(old), number(new)
    if step is None or a is None or b is None:
        return True
    return abs(b - a) >= step - 1e-9


def make_sample(ts, values, rise):
    """Chart row [epoch ms, *SAMPLE_KEYS values] from raw ebusd values."""
    row = [ts]
    for k in SAMPLE_KEYS:
        if k == "rise":
            row.append(None if rise is None else round(rise, 3))
        elif k in SWITCHES:
            raw = values.get(k)
            row.append(None if raw is None else int(raw in ("on", "yes")))
        else:
            row.append(number(values.get(k)))
    return row


def tail_lines(path, max_bytes):
    """Text lines from the last max_bytes of a file, and the byte offset they
    start at."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        start = max(0, f.tell() - max_bytes)
        f.seek(start)
        data = f.read()
    # a crash can leave runs of NUL bytes in a file; drop them
    lines = data.replace(b"\0", b"").decode("utf-8", "replace").splitlines()
    if start > 0 and lines:
        lines = lines[1:]  # probably a partial line
    return lines, start


def header_before(path, offset):
    """Columns of the last CSV header line ("time,...") before a byte offset,
    or None."""
    block, pos, carry = 65536, offset, b""
    with open(path, "rb") as f:
        while pos > 0:
            step = min(block, pos)
            pos -= step
            f.seek(pos)
            buf = (f.read(step) + carry).replace(
                b"\0", b"\n"
            )  # crash debris may precede a header
            i = buf.rfind(b"\ntime,")
            if i < 0 and pos == 0 and buf.startswith(b"time,"):
                i = -1
            elif i < 0:
                carry = buf[:1024]  # a header may straddle two blocks
                continue
            line = buf[i + 1 :].split(b"\n", 1)[0]
            return line.decode("utf-8", "replace").split(",")
    return None


def csv_samples(lines, header=None):
    """Chart samples from logger CSV lines; header lines (from restarts) switch
    the columns."""
    prev_flow = None  # (epoch ms, FlowTemp)
    for row in csv.reader(lines):
        if not row:
            continue
        if (
            row[0] == "time"
        ):  # the script was restarted here; columns may differ
            header, prev_flow = row, None
            continue
        if header is None or len(row) != len(header):
            continue
        try:
            ts = int(
                datetime.datetime.fromisoformat(row[0]).timestamp() * 1000
            )
        except ValueError:
            continue
        values = dict(zip(header, row))
        rise, flow = None, number(values.get("FlowTemp"))
        if flow is not None:
            if prev_flow and 200 < ts - prev_flow[0] < GAP_MS:
                rise = (flow - prev_flow[1]) / ((ts - prev_flow[0]) / 1000)
            prev_flow = (ts, flow)
        yield make_sample(ts, values, rise)


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


class Aggregator:
    """Summarises chart samples into fixed time buckets: [start ms, mean, min,
    max of each SAMPLE_KEYS value]."""

    def __init__(self, name, bucket_ms, keep_minutes, path):
        self.name, self.bucket_ms, self.keep_minutes, self.path = (
            name,
            bucket_ms,
            keep_minutes,
            path,
        )
        self.rows = collections.deque()
        self.start = None  # start of the bucket being filled
        self.acc = None

    def info(self):
        return {
            "name": self.name,
            "bucketMs": self.bucket_ms,
            "keepMinutes": self.keep_minutes,
        }

    def header(self):
        return ["time", "ms"] + [
            f"{k}_{s}" for k in SAMPLE_KEYS for s in ("mean", "min", "max")
        ]

    def file_row(self, row):
        when = datetime.datetime.fromtimestamp(row[0] / 1000).isoformat(
            timespec="seconds"
        )
        return [when, row[0]] + ["" if v is None else v for v in row[1:]]

    def add(self, sample):
        """Add a chart sample; returns the finished bucket row when a bucket
        closes."""
        start = sample[0] - sample[0] % self.bucket_ms
        # ignore samples for buckets already finished (clock stepped back, or a
        # restart mid-bucket)
        if (self.start is not None and start < self.start) or (
            self.rows and start <= self.rows[-1][0]
        ):
            return None
        done = None
        if start != self.start:
            if self.start is not None:
                done = self._finish()
            self.start = start
            self.acc = [
                [0.0, 0, None, None, None] for _ in SAMPLE_KEYS
            ]  # sum, count, min, max, last
        for a, v in zip(self.acc, sample[1:]):
            if v is None:
                continue
            a[0] += v
            a[1] += 1
            a[2] = v if a[2] is None else min(a[2], v)
            a[3] = v if a[3] is None else max(a[3], v)
            a[4] = v
        return done

    def _finish(self):
        row = [self.start]
        for k, (total, n, lo, hi, last) in zip(SAMPLE_KEYS, self.acc):
            if (
                k == "Statenumber"
            ):  # an average S-code means nothing; keep the last one
                row += [last, last, last]
            else:
                row += [None if n == 0 else round(total / n, 3), lo, hi]
        self.append(row)
        return row

    def append(self, row):
        self.rows.append(row)
        cutoff = row[0] - self.keep_minutes * 60000
        while self.rows and self.rows[0][0] < cutoff:
            self.rows.popleft()


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


def csv_is_clean(path, header):
    """True if the file starts with exactly this header, has no other header
    lines and no NUL bytes, and ends with a complete line. Reads the file in
    large blocks, so it's quick."""
    want = ",".join(header).encode()
    with open(path, "rb") as f:
        if f.readline().rstrip(b"\r\n") != want:
            return False
        carry = b""
        while True:
            block = f.read(1 << 20)
            if not block:
                break
            buf = carry + block
            if b"\0" in block or b"\ntime," in buf:
                return False
            carry = buf[-8:]
        f.seek(0, os.SEEK_END)
        f.seek(f.tell() - 1)
        return f.read(1) == b"\n"


def repair_csv(path, header, row_ok):
    """Clean up a CSV file written by this program, rewriting it only if
    needed: one header line (the current columns) at the top, no NUL bytes
    (left by crashes or power cuts), and no incomplete or unreadable rows. Rows
    written under an older header are converted to the current columns by name.
    Returns a summary string, or None if nothing changed."""
    tmp = path + ".repair"
    if os.path.exists(tmp):
        os.remove(
            tmp
        )  # left over from an interrupted repair; the original is intact
    if (
        not os.path.exists(path)
        or os.path.getsize(path) == 0
        or csv_is_clean(path, header)
    ):
        return None
    kept = dropped = headers = converted = nuls = 0
    cols = None  # the header the following rows were written under
    with open(path, "rb") as src, open(tmp, "w", newline="") as dst:
        w = csv.writer(dst)
        w.writerow(header)
        for raw in src:
            nuls += raw.count(b"\0")
            for piece in raw.split(
                b"\0"
            ):  # text either side of NULs is a fragment of a line
                line = piece.decode("utf-8", "replace").strip("\r\n")
                if not line.strip():
                    continue
                try:
                    row = next(csv.reader([line]))
                except (csv.Error, StopIteration):
                    dropped += 1
                    continue
                if row[0] == "time":
                    cols = row
                    headers += 1
                    continue
                if (
                    cols is None
                    or len(row) != len(cols)
                    or not row_ok(dict(zip(cols, row)))
                ):
                    dropped += 1
                    continue
                if cols != header:
                    values = dict(zip(cols, row))
                    row = [values.get(c, "") for c in header]
                    converted += 1
                w.writerow(row)
                kept += 1
        dst.flush()
        os.fsync(
            dst.fileno()
        )  # the card must hold the new file before it replaces the old one
    shutil.copymode(path, tmp)
    os.replace(tmp, path)
    dirfd = os.open(os.path.dirname(os.path.abspath(path)), os.O_RDONLY)
    try:
        os.fsync(dirfd)
    finally:
        os.close(dirfd)
    return (
        f"repaired {path}: kept {kept} rows, "
        f"removed {max(0, headers - 1)} extra header lines, "
        f"{nuls} NUL bytes and {dropped} incomplete or unreadable rows; "
        f"converted {converted} rows from older column layouts"
    )


def valid_time(value):
    try:
        datetime.datetime.fromisoformat(value)
        return True
    except (TypeError, ValueError):
        return False


def logger_row_ok(row):
    return valid_time(row.get("time"))


def summary_row_ok(row):
    if not valid_time(row.get("time")):
        return False
    try:
        int(row.get("ms"))
        return True
    except (TypeError, ValueError):
        return False


def needs_header(path):
    return not os.path.exists(path) or os.path.getsize(path) == 0


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


class Ebusd:
    def __init__(self, host, port):
        self.sock = socket.create_connection((host, port), timeout=10)
        self.f = self.sock.makefile("rw", newline="\n")

    def read(self, name):
        return self.command(f"read -f -c bai {name}")

    def write(self, name, value):
        """Write a message; ebusd answers "done" or "empty" on success,
        "ERR: ..." on failure."""
        return self.command(f"write -c bai {name} {value}")

    def close(self):
        self.sock.close()

    def command(self, line):
        self.f.write(line + "\n")
        self.f.flush()
        lines = []
        while True:  # ebusd ends each response with an empty line
            line = self.f.readline()
            if not line:
                raise ConnectionError("ebusd closed the connection")
            line = line.rstrip("\n")
            if not line:
                return " ".join(lines)
            lines.append(line)


# ------------------------------------------------------------ heating schedule

DAY_KEYS = [
    "mon",
    "tue",
    "wed",
    "thu",
    "fri",
    "sat",
    "sun",
]  # datetime.weekday() order
MAX_PERIODS = 10  # on/off pairs per day


def parse_hhmm(text, allow_2400=False):
    """Minutes since midnight for "HH:MM", or None. "24:00" (end of day) only
    if allowed."""
    if not isinstance(text, str) or len(text) != 5 or text[2] != ":":
        return None
    try:
        h, m = int(text[:2]), int(text[3:])
    except ValueError:
        return None
    if allow_2400 and (h, m) == (24, 0):
        return 1440
    return h * 60 + m if 0 <= h < 24 and 0 <= m < 60 else None


def hhmm(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def validate_schedule(obj):
    """Checked, normalised copy of a schedule from the web page, or (None,
    error message)."""
    if (
        not isinstance(obj, dict)
        or not isinstance(obj.get("enabled"), bool)
        or not isinstance(obj.get("days"), dict)
    ):
        return (
            None,
            'expected {"enabled": true|false, '
            '"days": {"mon": [["06:30", "08:30"], ...], ...}}',
        )
    unknown = set(obj["days"]) - set(DAY_KEYS)
    if unknown:
        return None, f"unknown day(s): {', '.join(sorted(unknown))}"
    days = {}
    for day in DAY_KEYS:
        periods = obj["days"].get(day, [])
        if not isinstance(periods, list) or len(periods) > MAX_PERIODS:
            return None, f"{day}: at most {MAX_PERIODS} on/off periods"
        spans = []
        for p in periods:
            if not (isinstance(p, list) and len(p) == 2):
                return None, f"{day}: each period must be [on, off]"
            on, off = parse_hhmm(p[0]), parse_hhmm(p[1], allow_2400=True)
            if on is None or off is None:
                return (
                    None,
                    f"{day}: times must be HH:MM (off may be 24:00), "
                    f"got {p[0]!r}-{p[1]!r}",
                )
            if on >= off:
                return (
                    None,
                    f"{day}: {p[0]}-{p[1]}: on must be before off "
                    "(use 24:00 for midnight)",
                )
            spans.append((on, off))
        spans.sort()
        for (a0, a1), (b0, b1) in zip(spans, spans[1:]):
            if b0 < a1:
                return (
                    None,
                    f"{day}: {hhmm(a0)}-{hhmm(a1)} "
                    f"overlaps {hhmm(b0)}-{hhmm(b1)}",
                )
        days[day] = [[hhmm(a), hhmm(b)] for a, b in spans]
    return {"enabled": obj["enabled"], "days": days}, None


class Schedule:
    """7-day heating schedule, kept in a JSON file and saved atomically."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.data = {
            "enabled": False,
            "days": {d: [] for d in DAY_KEYS},
            "updatedBy": None,
            "updatedAt": None,
        }
        if os.path.exists(path):
            try:
                with open(path) as f:
                    saved = json.load(f)
                checked, err = validate_schedule(saved)
                if err:
                    raise ValueError(err)
                self.data = {
                    **checked,
                    "updatedBy": saved.get("updatedBy"),
                    "updatedAt": saved.get("updatedAt"),
                }
            except (OSError, ValueError) as e:
                sys.exit(f"cannot use schedule {path}: {e}; fix or delete it")

    def get(self):
        with self.lock:
            return json.loads(json.dumps(self.data))

    def set(self, checked, user):
        data = {**checked, "updatedBy": user, "updatedAt": stamp()}
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
        with self.lock:
            self.data = data
        return self.get()

    def wanted(self, when):
        """True if heating should be allowed at this (local) time, False if
        not, None if disabled."""
        with self.lock:
            if not self.data["enabled"]:
                return None
            m = when.hour * 60 + when.minute
            return any(
                parse_hhmm(a) <= m < parse_hhmm(b, True)
                for a, b in self.data["days"][DAY_KEYS[when.weekday()]]
            )

    def next_change(self, when):
        """Local time at which wanted() next changes, or None (always on,
        always off, or disabled)."""
        now = self.wanted(when)
        if now is None:
            return None
        with self.lock:
            days = self.data["days"]
        midnight = when.replace(hour=0, minute=0, second=0, microsecond=0)
        edges = sorted(
            midnight + datetime.timedelta(days=d, minutes=parse_hhmm(t, True))
            for d in range(8)
            for p in days[DAY_KEYS[(when.weekday() + d) % 7]]
            for t in p
        )
        for t in edges:
            if t > when and self.wanted(t) != now:
                return t
        return None


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


class HeatingController(threading.Thread):
    """Applies the schedule by telling the boiler, over eBUS, to block or allow
    central heating.

    The block only lasts about 15 minutes after the last SetMode message, so
    the current state is re-sent on every 10-minute boundary of the clock
    (xx:00, xx:10, ...). It is also sent at once on startup and when the
    schedule is saved, and at the exact minute of each schedule on/off time
    (e.g. 07:31), then again on the following boundaries. A failed send is
    retried after RETRY_SECONDS. When the schedule is disabled, heating is
    allowed once, and then the boiler goes back to its own controls. The
    boiler's own timer still applies: the Pi can stop heating, but cannot start
    it while that timer is off.

    Override: one "heating on" is sent at once, whatever the schedule says,
    then nothing for OVERRIDE_SECONDS (the boiler holds it for about 15
    minutes); then the schedule state is sent and the normal 10-minute
    boundaries resume."""

    RESEND_SECONDS = 600  # boundary spacing; must divide an hour
    RETRY_SECONDS = 60
    # re-check at least this often, in case the clock is stepped
    MAX_SLEEP = 60
    # how long the boiler keeps a SetMode block without a repeat
    HOLD_SECONDS = 900
    OVERRIDE_SECONDS = 840

    def __init__(
        self, schedule, hub, commands, host, port, flow_temp, hwc_temp
    ):
        super().__init__(daemon=True)
        self.schedule, self.hub, self.commands, self.host, self.port = (
            schedule,
            hub,
            commands,
            host,
            port,
        )
        self.flow_temp, self.hwc_temp = flow_temp, hwc_temp
        self.wake = threading.Event()
        self.sent = None  # "on"/"off" last confirmed by ebusd, or None
        # local time of the next regular send (boundary or schedule change)
        self.due = None
        self.retry_at = None  # local time to retry after a failed send
        # {"time", "state", "ok", "result"} of the last attempt
        self.last = None
        # the same, for the last attempt the boiler accepted
        self.last_ok = None
        # {"until": local time, "by": user, "sent": bool} while overriding
        self.override = None
        self.requests = []  # override requests from the web, handled by step()
        self.lock = threading.Lock()

    def request_override(self, user):
        with self.lock:
            self.requests.append(user)
        self.wake.set()

    def setmode(self, off):
        return (
            f"auto;{self.flow_temp:g};{self.hwc_temp:g};-;{int(off)};0;0;0;0;0"
        )

    def send(self, state, source="schedule"):
        try:
            bus = Ebusd(self.host, self.port)
            try:
                result = bus.write("SetMode", self.setmode(state == "off"))
            finally:
                bus.close()
            ok = not result.startswith("ERR")
        except (OSError, ConnectionError) as e:
            result, ok = f"ERR: {e}", False
        self.last = {
            "time": stamp(),
            "state": state,
            "ok": ok,
            "result": result,
            "source": source,
        }
        self.commands.add(state, ok, result, source)
        if ok:
            self.last_ok = self.last
            if state != self.sent:
                self.hub.record("HeatingSchedule", state, stamp())
            self.sent = state
        else:
            log(f"SetMode heating {state} failed: {result}")
        return ok

    def next_slot(self, now):
        """The next 10-minute boundary after now, or the next schedule change
        if that comes first."""
        step = self.RESEND_SECONDS
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        secs = (now - midnight).total_seconds()
        boundary = midnight + datetime.timedelta(
            seconds=(secs // step + 1) * step
        )
        change = self.schedule.next_change(now)
        return min(boundary, change) if change else boundary

    def step(self, now=None):
        now = now or datetime.datetime.now()
        with self.lock:
            requests, self.requests = self.requests, []
        if requests:  # a new press restarts the override
            until = now + datetime.timedelta(seconds=self.OVERRIDE_SECONDS)
            self.override = {"until": until, "by": requests[-1], "sent": False}
            self.retry_at = None
            self.hub.record(
                "HeatingOverride",
                f"on until {until:%H:%M:%S} ({requests[-1]})",
                stamp(),
            )
        if self.override and now >= self.override["until"]:
            self.override = None
            # back to the schedule: send its state now
            self.due = self.retry_at = None
            self.hub.record("HeatingOverride", "ended", stamp())
        wanted = self.schedule.wanted(now)
        target = None if wanted is None else ("on" if wanted else "off")
        if self.override:
            o = self.override
            if not o["sent"] and (
                self.retry_at is None or now >= self.retry_at
            ):
                if self.send("on", f"override:{o['by']}"):
                    o["sent"], self.retry_at = True, None
                else:
                    self.retry_at = min(
                        now + datetime.timedelta(seconds=self.RETRY_SECONDS),
                        o["until"],
                    )
            self.due = o["until"]
        elif (
            target is None
        ):  # schedule disabled: if heating is blocked, release it once
            self.due = None
            if self.sent == "off" and (
                self.retry_at is None or now >= self.retry_at
            ):
                if self.send("on"):
                    self.sent, self.retry_at = None, None
                else:
                    self.retry_at = now + datetime.timedelta(
                        seconds=self.RETRY_SECONDS
                    )
            elif self.sent != "off":
                self.retry_at = None
        else:
            failed = self.retry_at is not None
            need = (
                self.due is None
                or now >= self.due
                or (failed and now >= self.retry_at)
                or (not failed and target != self.sent)
            )  # e.g. the schedule was just saved
            if need:
                ok = self.send(target)
                self.retry_at = (
                    None
                    if ok
                    else now + datetime.timedelta(seconds=self.RETRY_SECONDS)
                )
            self.due = self.next_slot(
                now
            )  # recomputed each time, so schedule edits take effect
        nxt = self.schedule.next_change(now)
        nxt_send = (
            min(t for t in (self.due, self.retry_at) if t)
            if (self.due or self.retry_at)
            else None
        )
        o = self.override
        if (
            o
            and nxt_send == o["until"]
            and wanted is None
            and self.sent != "off"
        ):
            # override ending with the schedule disabled: nothing more is sent
            nxt_send = None
        if nxt_send is None:
            nxt_state = None
        elif nxt_send == self.retry_at:
            nxt_state = "on" if o else target
        else:
            nxt_state = "on" if self.schedule.wanted(nxt_send) else "off"
        self.hub.set_control(
            {
                "enabled": wanted is not None,
                "target": target,
                "nextChange": (
                    nxt.isoformat(timespec="minutes") if nxt else None
                ),
                "nextSend": (
                    nxt_send.isoformat(timespec="seconds")
                    if nxt_send
                    else None
                ),
                "nextState": nxt_state,
                "override": (
                    None
                    if not o
                    else {
                        "until": o["until"].isoformat(timespec="seconds"),
                        "by": o["by"],
                        "sent": o["sent"],
                    }
                ),
                "lastSend": self.last,
                "lastOk": self.last_ok,
                "resendSeconds": self.RESEND_SECONDS,
            }
        )

    def sleep_seconds(self, now):
        times = [t for t in (self.due, self.retry_at) if t]
        if not times:
            return self.MAX_SLEEP
        # wake just after the due time; if woken early, the next step sees it
        # isn't due and waits again
        return max(
            0.01,
            min(self.MAX_SLEEP, (min(times) - now).total_seconds() + 0.02),
        )

    def run(self):
        while True:
            try:
                self.step()
            except Exception as e:  # never let the controller die quietly
                log(f"heating controller error: {e!r}")
            self.wake.wait(self.sleep_seconds(datetime.datetime.now()))
            self.wake.clear()


class FileWriter(threading.Thread):
    """Appends CSV rows and change-log lines in batches from its own thread, so
    a stalled SD card holds up only this thread, never the polling or the web
    UI."""

    # characters held per file while writes keep failing
    MAX_PENDING = 20_000_000

    def __init__(self, csv_path, changes_path, interval):
        super().__init__(daemon=True)
        self.csv_path, self.changes_path, self.interval = (
            csv_path,
            changes_path,
            interval,
        )
        self.q = queue.Queue()
        self.pending = {csv_path: "", changes_path: ""}
        self.stopping = threading.Event()

    def csv_row(self, row, path=None):
        self.q.put((path or self.csv_path, row))

    def change(self, line):
        self.q.put((self.changes_path, line))

    def run(self):
        while not self.stopping.wait(self.interval):
            self.flush()
        self.flush()

    def stop(self):
        """Write out everything queued so far (waits up to 30 s for a slow
        card)."""
        self.stopping.set()
        self.join(timeout=30)

    def flush(self):
        out = {}  # path -> StringIO of new text
        while True:
            try:
                path, item = self.q.get_nowait()
            except queue.Empty:
                break
            buf = out.setdefault(path, io.StringIO())
            if isinstance(item, str):
                buf.write(item)
            else:
                csv.writer(buf).writerow(item)
        for path, buf in out.items():
            self.pending[path] = self.pending.get(path, "") + buf.getvalue()
        for path, data in self.pending.items():
            if not data:
                continue
            try:
                with open(path, "a", newline="") as f:
                    f.write(data)
                self.pending[path] = ""
            except OSError as e:  # keep the data and try again next time
                log(f"cannot write {path}: {e}")
                if len(data) > self.MAX_PENDING:
                    self.pending[path] = data[-self.MAX_PENDING :]


class Hub:
    """Latest values, chart history and recent changes, shared between the
    poller and the web clients."""

    def __init__(self, writer, history_minutes, aggs):
        self.aggs = aggs
        self.lock = threading.Lock()
        self.clients = []
        self.values = {}
        self.listed = {}  # name -> raw value last written to the change list
        self.changes = collections.deque(maxlen=CHANGE_HISTORY)
        self.history = collections.deque()  # [epoch ms, *SAMPLE_KEYS values]
        self.history_minutes = history_minutes
        self.rise = None
        self.connected = False
        self.updated = None
        self.writer = writer
        self.control = None  # heating controller status

    def set_control(self, status):
        with self.lock:
            if status == self.control:
                return
            self.control = status
            self._broadcast({"type": "control", "control": status})

    def snapshot(self):  # caller holds the lock
        return {
            "type": "snapshot",
            "ts": epoch_ms(),
            "time": self.updated,
            "values": dict(self.values),
            "rise": self.rise,
            "connected": self.connected,
            "changes": list(self.changes),
            "sampleKeys": SAMPLE_KEYS,
            "historyMinutes": self.history_minutes,
            "history": list(self.history),
            "tiers": [a.info() for a in self.aggs],
            "control": self.control,
        }

    def agg_rows(self, name):
        with self.lock:
            for a in self.aggs:
                if a.name == name:
                    return {
                        "tier": name,
                        "bucketMs": a.bucket_ms,
                        "rows": list(a.rows),
                    }
        return None

    def subscribe(self):
        q = queue.Queue(maxsize=200)
        with self.lock:
            self.clients.append(q)
            return q, self.snapshot()

    def unsubscribe(self, q):
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)

    def broadcast(self, msg):
        with self.lock:
            self._broadcast(msg)

    def _broadcast(self, msg):  # caller holds the lock
        for q in list(self.clients):
            try:
                q.put_nowait(msg)
            except (
                queue.Full
            ):  # client too slow; drop it and let the browser reconnect
                q.dropped = True
                self.clients.remove(q)

    def record(self, name, raw, when):
        with self.lock:
            self.values[name] = raw
            old = self.listed.get(name)
            if old is None:
                self.listed[name] = raw
                return
            if not significant(name, old, raw):
                return
            self.listed[name] = raw
            entry = {
                "time": when,
                "name": name,
                "old": display(name, old),
                "new": display(name, raw),
            }
            self.changes.append(entry)
            self._broadcast({"type": "change", **entry})
        self.writer.change(
            f"{when}: {name}: {entry['old']} -> {entry['new']}\n"
        )

    def publish(self, when, rise, connected):
        ts = epoch_ms()
        with self.lock:
            self.updated, self.rise, self.connected = when, rise, connected
            sample = None
            if (
                connected
            ):  # no samples while disconnected, so the charts show a gap
                sample = make_sample(ts, self.values, rise)
                self.history.append(sample)
                cutoff = ts - self.history_minutes * 60000
                while self.history and self.history[0][0] < cutoff:
                    self.history.popleft()
                for a in self.aggs:
                    row = a.add(sample)
                    if row:
                        self.writer.csv_row(a.file_row(row), a.path)
                        self._broadcast(
                            {"type": "bucket", "tier": a.name, "row": row}
                        )
            self._broadcast(
                {
                    "type": "update",
                    "ts": ts,
                    "time": when,
                    "values": dict(self.values),
                    "rise": rise,
                    "connected": connected,
                    "sample": sample,
                }
            )


def poll(hub, writer, host, port):
    names = list(VALUES)
    last = {n: "" for n in names}
    last_read = {n: 0.0 for n in names}
    # (monotonic time, FlowTemp) of the previous FlowTemp read
    prev_flow = None
    rise = None  # FlowTemp change in degrees per second
    bus = None

    def read(n):
        nonlocal prev_flow, rise
        raw = bus.read(n)
        now = time.monotonic()
        last[n] = raw
        last_read[n] = now
        hub.record(n, raw, stamp())
        if n == "FlowTemp":
            t = number(raw)
            if t is not None:
                if prev_flow and now - prev_flow[0] > 0.2:
                    rise = (t - prev_flow[1]) / (now - prev_flow[0])
                prev_flow = (now, t)

    if needs_header(
        writer.csv_path
    ):  # existing files already start with it (see repair_csv)
        writer.csv_row(["time"] + names)
    while True:
        try:
            bus = bus or Ebusd(host, port)
            flame_before = last["Flame"]
            for n in names:
                if time.monotonic() - last_read[n] >= VALUES[n]:
                    read(n)
            # flame went out: refresh the fault counters straight away
            if flame_before == "on" and last["Flame"] != "on":
                for n in names:
                    if VALUES[n] >= 30:
                        read(n)
            now = stamp()
            writer.csv_row([now] + [last[n] for n in names])
            hub.publish(now, rise, True)
        except (OSError, ConnectionError) as e:
            log(f"ebusd error: {e}; retrying")
            hub.publish(stamp(), rise, False)
            bus = None
            time.sleep(5)


# -------------------------------------------------------------- authentication

# role -> permissions. Every API endpoint names the permission it needs.
ROLES = {
    "viewer": {"read"},
    "user": {"read", "control"},
    "admin": {"read", "control", "admin"},
}
ROLE_RANK = ["viewer", "user", "admin"]


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64url(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Auth:
    """Checks passwords against an htpasswd file, looks up roles, and
    issues/verifies HS256 JWTs.

    The user and role files are re-read whenever they change, so editing them
    needs no restart. Deleting a user or removing their role stops them
    refreshing; their current token still works until it expires (at most
    --token-hours)."""

    JWT_HEADER = b64url(
        json.dumps(
            {"alg": "HS256", "typ": "JWT"}, separators=(",", ":")
        ).encode()
    )
    # used for unknown users so a login takes as long whether or not the name
    # exists
    DUMMY_HASH = bcrypt.hashpw(b"x", bcrypt.gensalt(6))
    MAX_FAILURES, LOCKOUT_SECONDS = 5, 60

    def __init__(
        self,
        users_path,
        roles_path,
        secret_path,
        token_seconds,
        session_seconds,
    ):
        self.users_path, self.roles_path = users_path, roles_path
        self.token_seconds, self.session_seconds = (
            token_seconds,
            session_seconds,
        )
        self.lock = threading.Lock()
        self.users, self.roles, self.mtimes = {}, {}, None
        self.failures = {}  # client address -> (count, time of last failure)
        self.key = self._load_key(secret_path)
        self._reload()
        if not self.users:
            sys.exit(
                f"no usable bcrypt users in {users_path}; nobody could log in"
            )

    @staticmethod
    def _load_key(path):
        """Signing key, created on first run; kept so restarts don't log
        everyone out."""
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            with open(path, "rb") as f:
                key = f.read().strip()
            if len(key) < 32:
                sys.exit(
                    f"{path} is too short to be a signing key; "
                    "delete it to make a new one"
                )
            if os.stat(path).st_mode & 0o077:
                print(
                    f"warning: {path} is readable by other users; "
                    "chmod 600 it",
                    file=sys.stderr,
                )
            return key
        key = base64.b64encode(os.urandom(48))
        with os.fdopen(fd, "wb") as f:
            f.write(key + b"\n")
        print(f"created token signing key {path}", file=sys.stderr)
        return key

    def _reload(self):
        try:
            mtimes = (
                os.stat(self.users_path).st_mtime_ns,
                os.stat(self.roles_path).st_mtime_ns,
            )
        except OSError as e:
            print(f"cannot read user files: {e}", file=sys.stderr)
            return
        if mtimes == self.mtimes:
            return
        users = {}
        with open(self.users_path) as f:
            for n, line in enumerate(f, 1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                name, _, pw_hash = line.partition(":")
                pw_hash = pw_hash.split(":", 1)[0]
                if not pw_hash.startswith(("$2y$", "$2b$", "$2a$")):
                    print(
                        f"{self.users_path}:{n}: {name} skipped "
                        "(not a bcrypt hash; use htpasswd -B)",
                        file=sys.stderr,
                    )
                    continue
                users[name] = (
                    "$2b$" + pw_hash[4:]
                ).encode()  # $2y$ is the same algorithm as $2b$
        roles = {}
        with open(self.roles_path) as f:
            for n, line in enumerate(f, 1):
                line = line.split("#", 1)[0].strip()
                if not line:
                    continue
                role, _, members = line.partition(":")
                role = role.strip()
                if role not in ROLES:
                    print(
                        f"{self.roles_path}:{n}: "
                        f"unknown role {role!r} ignored",
                        file=sys.stderr,
                    )
                    continue
                for name in members.split():
                    if (
                        ROLE_RANK.index(role)
                        > ROLE_RANK.index(roles.get(name, "viewer"))
                        or name not in roles
                    ):
                        roles[name] = role
        for name in users:
            if name not in roles:
                print(
                    f"{name} has no role in {self.roles_path}, "
                    "so cannot log in",
                    file=sys.stderr,
                )
        self.users, self.roles, self.mtimes = users, roles, mtimes
        print(f"loaded {len(users)} users", file=sys.stderr)

    def role_of(self, name):
        with self.lock:
            self._reload()
            return self.roles.get(name) if name in self.users else None

    def login(self, name, password, client):
        """Returns (token info, None) or (None, (http code, message))."""
        now = time.time()
        with self.lock:
            count, last = self.failures.get(client, (0, 0))
            if (
                count >= self.MAX_FAILURES
                and now - last < self.LOCKOUT_SECONDS
            ):
                return None, (
                    429,
                    f"too many failed logins; wait {self.LOCKOUT_SECONDS} s",
                )
            self._reload()
            pw_hash = self.users.get(name)
            role = self.roles.get(name)
        try:
            ok = (
                bcrypt.checkpw(password.encode(), pw_hash or self.DUMMY_HASH)
                and pw_hash is not None
            )
        except ValueError:  # malformed hash in the file
            ok = False
        with self.lock:
            if not ok:
                self.failures[client] = (
                    count + 1 if now - last < self.LOCKOUT_SECONDS else 1,
                    now,
                )
                return None, (401, "wrong username or password")
            self.failures.pop(client, None)
        if role is None:
            return None, (403, "this user has no role; ask the admin")
        return self.issue(name, role, int(now)), None

    def issue(self, name, role, auth_time):
        now = int(time.time())
        exp = min(now + self.token_seconds, auth_time + self.session_seconds)
        claims = {
            "sub": name,
            "role": role,
            "iat": now,
            "exp": exp,
            "auth_time": auth_time,
        }
        body = (
            self.JWT_HEADER
            + "."
            + b64url(json.dumps(claims, separators=(",", ":")).encode())
        )
        sig = b64url(
            hmac.new(self.key, body.encode(), hashlib.sha256).digest()
        )
        return {
            "token": body + "." + sig,
            "expiresIn": exp - now,
            "user": name,
            "role": role,
        }

    def verify(self, token):
        """Claims of a valid, unexpired token, or None."""
        try:
            header, payload, sig = token.split(".")
            if (
                header != self.JWT_HEADER
            ):  # only ever accept our own HS256 header
                return None
            want = hmac.new(
                self.key, f"{header}.{payload}".encode(), hashlib.sha256
            ).digest()
            if not hmac.compare_digest(unb64url(sig), want):
                return None
            claims = json.loads(unb64url(payload))
        except (ValueError, TypeError):
            return None
        if not isinstance(claims, dict) or claims.get("role") not in ROLES:
            return None
        if time.time() >= claims.get("exp", 0):
            return None
        return claims

    def refresh(self, claims):
        """New token for the same login session, re-checking the user still
        exists and their role."""
        role = self.role_of(claims["sub"])
        if role is None:
            return None, (401, "user no longer allowed")
        if time.time() >= claims["auth_time"] + self.session_seconds:
            return None, (401, "session too old; log in again")
        return self.issue(claims["sub"], role, claims["auth_time"]), None


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "frame-ancestors 'none'"
    ),
}


STATIC_DIR = os.path.join(
    os.path.dirname(os.path.realpath(__file__)), "static"
)
# URL path -> file in the static directory. Page shells and assets only, no
# boiler data, so no token needed: the page checks the token itself (auth.js)
# and sends you to /login.
STATIC_FILES = {
    "/": "index.html",
    "/login": "login.html",
    "/auth.js": "auth.js",
    "/app.js": "app.js",
    "/app.css": "app.css",
    "/login.js": "login.js",
    "/login.css": "login.css",
}
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}


def load_static(static_dir):
    """URL path -> (content type, bytes) for every STATIC_FILES entry, read
    once at startup."""
    files = {}
    for url, name in STATIC_FILES.items():
        path = os.path.join(static_dir, name)
        try:
            with open(path, "rb") as f:
                files[url] = (
                    CONTENT_TYPES[os.path.splitext(name)[1]],
                    f.read(),
                )
        except OSError as e:
            sys.exit(f"cannot read web UI file {path}: {e}")
    return files


def serve(hub, auth, controller, static, bind, port):
    started = time.time()
    schedule = controller.schedule

    class Handler(BaseHTTPRequestHandler):
        # path -> (method name, permission needed); None = no token needed
        ROUTES = {
            **{("GET", url): ("_static", None) for url in static},
            ("GET", "/health"): ("_health", None),
            ("POST", "/api/login"): ("_login", None),
            ("POST", "/api/refresh"): ("_refresh", "read"),
            ("GET", "/api/whoami"): ("_whoami", "read"),
            ("GET", "/api/state"): ("_state", "read"),
            ("GET", "/api/agg"): ("_agg", "read"),
            ("GET", "/api/commands"): ("_commands", "read"),
            ("GET", "/api/events"): ("_events", "read"),
            ("GET", "/api/schedule"): ("_get_schedule", "read"),
            ("PUT", "/api/schedule"): ("_put_schedule", "admin"),
            ("POST", "/api/override"): ("_override", "control"),
        }

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("PUT")

        def _dispatch(self, method):
            path, _, self.query = self.path.partition("?")
            route = self.ROUTES.get((method, path))
            if route is None:
                allowed = any(p == path for _, p in self.ROUTES)
                self._json(
                    405 if allowed else 404,
                    {
                        "error": (
                            "method not allowed" if allowed else "not found"
                        )
                    },
                )
                return
            handler, permission = route
            self.claims = None
            if permission is not None:
                header = self.headers.get("Authorization", "")
                self.claims = (
                    auth.verify(header[7:])
                    if header.startswith("Bearer ")
                    else None
                )
                if self.claims is None:
                    self._json(
                        401,
                        {"error": "login required"},
                        {"WWW-Authenticate": "Bearer"},
                    )
                    return
                if permission not in ROLES[self.claims["role"]]:
                    self._json(
                        403,
                        {
                            "error": f"your role ({self.claims['role']}) "
                            "cannot do this"
                        },
                    )
                    return
            getattr(self, handler)()

        def _send(self, code, ctype, body, headers=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in {**SECURITY_HEADERS, **(headers or {})}.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code, obj, headers=None):
            self._send(
                code,
                "application/json",
                json.dumps(obj, separators=(",", ":")).encode(),
                headers,
            )

        def _body(self, limit=4096):
            try:
                n = int(self.headers.get("Content-Length", 0))
            except ValueError:
                n = 0
            if not 0 < n <= limit:
                return None
            try:
                obj = json.loads(self.rfile.read(n))
            except ValueError:
                return None
            return obj if isinstance(obj, dict) else None

        def _static(self):
            self._send(200, *static[self.path.partition("?")[0]])

        def _health(self):
            with hub.lock:
                connected, updated = hub.connected, hub.updated
            self._json(
                200,
                {
                    "status": "ok",
                    "uptime": int(time.time() - started),
                    "ebusd": "connected" if connected else "disconnected",
                    "lastUpdate": updated,
                },
            )

        def _login(self):
            body = self._body()
            name, password = (body or {}).get("username"), (body or {}).get(
                "password"
            )
            if (
                not isinstance(name, str)
                or not isinstance(password, str)
                or not name
                or len(password) > 200
            ):
                self._json(400, {"error": "username and password required"})
                return
            info, err = auth.login(name, password, self.client_address[0])
            if err:
                log(
                    f"login failed for {name!r} from "
                    f"{self.client_address[0]}: {err[1]}"
                )
                self._json(err[0], {"error": err[1]})
            else:
                self._json(200, info)

        def _refresh(self):
            info, err = auth.refresh(self.claims)
            (
                self._json(err[0], {"error": err[1]})
                if err
                else self._json(200, info)
            )

        def _whoami(self):
            c = self.claims
            self._json(
                200,
                {
                    "user": c["sub"],
                    "role": c["role"],
                    "permissions": sorted(ROLES[c["role"]]),
                    "expiresIn": int(c["exp"] - time.time()),
                },
            )

        def _schedule_reply(self, code=200):
            with hub.lock:
                status = hub.control
            self._json(
                code,
                {
                    **schedule.get(),
                    "maxPeriods": MAX_PERIODS,
                    "dayOrder": DAY_KEYS,
                    "status": status,
                },
            )

        def _get_schedule(self):
            self._schedule_reply()

        def _put_schedule(self):
            body = self._body(limit=32768)
            if body is None:
                self._json(400, {"error": "expected a JSON schedule"})
                return
            checked, err = validate_schedule(body)
            if err:
                self._json(400, {"error": err})
                return
            try:
                schedule.set(checked, self.claims["sub"])
            except OSError as e:
                self._json(500, {"error": f"cannot save schedule: {e}"})
                return
            log(
                f"schedule saved by {self.claims['sub']} "
                f"(enabled: {checked['enabled']})"
            )
            controller.wake.set()  # apply it now
            time.sleep(
                0.2
            )  # usually lets the status below reflect the new schedule
            self._schedule_reply()

        def _override(self):
            user = self.claims["sub"]
            log(f"heating override by {user}")
            controller.request_override(user)
            time.sleep(0.5)  # usually long enough for the send to finish
            self._schedule_reply()

        def _state(self):
            with hub.lock:
                snap = hub.snapshot()
            self._json(200, snap)

        def _commands(self):
            self._json(
                200,
                {
                    "rows": controller.commands.get(),
                    "holdSeconds": controller.HOLD_SECONDS,
                },
            )

        def _agg(self):
            tier = urllib.parse.parse_qs(self.query).get("tier", [""])[0]
            data = hub.agg_rows(tier)
            if data is None:
                self._json(404, {"error": "unknown tier"})
            else:
                self._json(200, data)

        def _push(self, msg):
            self.wfile.write(
                b"data: "
                + json.dumps(msg, separators=(",", ":")).encode()
                + b"\n\n"
            )
            self.wfile.flush()

        def _events(self):
            q, snap = hub.subscribe()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                for k, v in SECURITY_HEADERS.items():
                    self.send_header(k, v)
                self.end_headers()
                self._push(snap)
                # the token was checked when the stream opened; end it when the
                # token expires,
                # and the page reconnects with its refreshed token
                while (
                    not getattr(q, "dropped", False)
                    and time.time() < self.claims["exp"]
                ):
                    try:
                        self._push(q.get(timeout=5))
                    except (
                        queue.Empty
                    ):  # a real message, so the browser's watchdog sees it
                        self._push({"type": "ping"})
                self.close_connection = True
            except OSError:
                pass  # browser went away
            finally:
                hub.unsubscribe(q)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer((bind, port), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "csv",
        nargs="?",
        default=time.strftime("boiler-%Y%m%d-%H%M%S.csv"),
        help="CSV file to append to",
    )
    ap.add_argument(
        "--changes",
        help="change log file (default: CSV path with .changes.log)",
    )
    ap.add_argument(
        "--bind",
        default="0.0.0.0",
        help="web UI address (default: all interfaces)",
    )
    ap.add_argument(
        "--port", type=int, default=8000, help="web UI port (default: 8000)"
    )
    ap.add_argument(
        "--ebusd",
        default="127.0.0.1:8888",
        help="ebusd command port (default: 127.0.0.1:8888)",
    )
    ap.add_argument(
        "--history-minutes",
        type=int,
        default=60,
        help="chart history kept in memory, i.e. the longest chart window "
        "(default: 60)",
    )
    ap.add_argument(
        "--flush-seconds",
        type=float,
        default=30,
        help="how often to write the CSV and change log to disk (default: 30)",
    )
    ap.add_argument(
        "--users",
        required=True,
        help="htpasswd file with bcrypt passwords (htpasswd -B)",
    )
    ap.add_argument(
        "--roles",
        required=True,
        help="roles file: lines of 'admin|user|viewer: name name ...'",
    )
    ap.add_argument(
        "--secret-file",
        default="boiler-ui.secret",
        help="token signing key, created if missing "
        "(default: ./boiler-ui.secret)",
    )
    ap.add_argument(
        "--token-hours",
        type=float,
        default=12,
        help="token lifetime (default: 12)",
    )
    ap.add_argument(
        "--session-days",
        type=float,
        default=30,
        help="longest a login can be kept alive by refreshing (default: 30)",
    )
    ap.add_argument(
        "--schedule",
        help="heating schedule file (default: schedule.json next to the CSV)",
    )
    ap.add_argument(
        "--flow-temp",
        type=float,
        default=75,
        help="heating flow temperature sent with each SetMode, °C "
        "(default: 75)",
    )
    ap.add_argument(
        "--hwc-temp",
        type=float,
        default=52.5,
        help="hot water temperature sent with each SetMode, °C "
        "(default: 52.5)",
    )
    ap.add_argument(
        "--static-dir",
        default=STATIC_DIR,
        help="directory with the web UI's HTML, JS and CSS files "
        "(default: static next to this program)",
    )
    args = ap.parse_args()
    static = load_static(args.static_dir)
    changes = args.changes or os.path.splitext(args.csv)[0] + ".changes.log"
    host, _, port = args.ebusd.rpartition(":")
    auth = Auth(
        args.users,
        args.roles,
        args.secret_file,
        int(args.token_hours * 3600),
        int(args.session_days * 86400),
    )

    writer = FileWriter(args.csv, changes, args.flush_seconds)
    base = os.path.splitext(args.csv)[0]
    aggs = [
        Aggregator(name, ms, keep, f"{base}.{name}.csv")
        for name, ms, keep in TIERS
    ]
    for path, header, row_ok in (
        [(args.csv, ["time"] + list(VALUES), logger_row_ok)]
        + [(a.path, a.header(), summary_row_ok) for a in aggs]
        + [(f"{base}.commands.csv", CommandLog.HEADER, summary_row_ok)]
    ):
        t0 = time.monotonic()
        result = repair_csv(path, header, row_ok)
        if result:
            print(f"{result} ({time.monotonic() - t0:.0f} s)", file=sys.stderr)
    hub = Hub(writer, args.history_minutes, aggs)
    print(
        f"loaded {load_history(hub, args.csv)} chart samples from {args.csv} "
        f"and {load_changes(hub, changes)} changes from {changes}",
        file=sys.stderr,
    )
    missing = []
    for a in aggs:
        n = load_agg(a)
        if n is None:
            missing.append(a)
        else:
            print(
                f"loaded {n} {a.name} summaries from {a.path}", file=sys.stderr
            )
        if needs_header(a.path):
            writer.csv_row(a.header(), a.path)
    # first run with summaries: build them from the existing CSV (one-off, may
    # take a while)
    if missing:
        print(
            f"building {', '.join(a.path for a in missing)} "
            f"from {args.csv}...",
            file=sys.stderr,
        )
        t0 = time.monotonic()
        n = backfill(missing, args.csv, writer)
        print(
            f"read {n} samples in {time.monotonic() - t0:.0f} s; "
            + ", ".join(f"{len(a.rows)} {a.name} summaries" for a in missing),
            file=sys.stderr,
        )
    schedule = Schedule(
        args.schedule
        or os.path.join(
            os.path.dirname(os.path.abspath(args.csv)), "schedule.json"
        )
    )
    commands = CommandLog(f"{base}.commands.csv", hub, writer)
    print(
        f"loaded {commands.load()} boiler instructions from {commands.path}",
        file=sys.stderr,
    )
    controller = HeatingController(
        schedule, hub, commands, host, int(port), args.flow_temp, args.hwc_temp
    )
    writer.start()
    controller.start()
    serve(hub, auth, controller, static, args.bind, args.port)
    print(
        f"web UI on port {args.port}; logging to {args.csv} and {changes} "
        f"every {args.flush_seconds:g} s",
        file=sys.stderr,
    )

    def on_sigterm(
        signum, frame
    ):  # systemctl stop/restart: write out what's queued first
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, on_sigterm)
    try:
        poll(hub, writer, host, int(port))
    except KeyboardInterrupt:
        pass
    finally:
        writer.stop()


if __name__ == "__main__":
    main()
