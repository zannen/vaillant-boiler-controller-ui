#!/usr/bin/env python3
"""Poll ebusd for Vaillant BAI values, append them to a CSV file and serve a live web UI.

Usage: boiler-log-ui.py [CSV_PATH] --users HTPASSWD --roles ROLES [--port 8000] [--changes PATH]
                        [--ebusd HOST:PORT] [--history-minutes 60] [--flush-seconds 30]
                        [--secret-file PATH] [--token-hours 12] [--session-days 30]
Then open http://<pi-address>:8000/ in a browser on the local network
(add ?minutes=N to choose the chart window, e.g. http://<pi-address>:8000/?minutes=5).

Users: an htpasswd file with bcrypt hashes (htpasswd -B), plus a roles file in Apache group-file
format, one line per role (a user in several roles gets the highest; a user in none cannot log in):
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
    sys.exit("boiler-log-ui.py needs the bcrypt module: sudo apt install python3-bcrypt")

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
# from the last listed value. Everything else (on/off, status numbers, counters,
# error codes) is listed on every change.
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
    "FlowTemp", "ReturnTemp", "HwcTemp", "rise", "FanSpeed", "HwcWaterflow",
    "PumpPower", "WaterPressure", "IonisationVoltageLevel",
    "Flame", "Gasvalve", "HwcDemand", "Statenumber",
    "PositionValveSet",
]
SWITCHES = {"Flame", "Gasvalve", "HwcDemand"}

CHANGE_HISTORY = 500
GAP_MS = 15000  # samples further apart than this are not joined (and give no rise value)

# Long chart windows use per-bucket summaries (mean/min/max of each sample value),
# kept in memory and appended to <csv name>.<tier>.csv: (name, bucket ms, minutes kept)
TIERS = [
    ("1min", 60_000, 2 * 1440),
    ("15min", 15 * 60_000, 31 * 1440),
]


def stamp():
    return datetime.datetime.now().isoformat(timespec="milliseconds")


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
    """Text lines from the last max_bytes of a file, and the byte offset they start at."""
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
    """Columns of the last CSV header line ("time,...") before a byte offset, or None."""
    block, pos, carry = 65536, offset, b""
    with open(path, "rb") as f:
        while pos > 0:
            step = min(block, pos)
            pos -= step
            f.seek(pos)
            buf = (f.read(step) + carry).replace(b"\0", b"\n")  # crash debris may precede a header
            i = buf.rfind(b"\ntime,")
            if i < 0 and pos == 0 and buf.startswith(b"time,"):
                i = -1
            elif i < 0:
                carry = buf[:1024]  # a header may straddle two blocks
                continue
            line = buf[i + 1:].split(b"\n", 1)[0]
            return line.decode("utf-8", "replace").split(",")
    return None


def csv_samples(lines, header=None):
    """Chart samples from logger CSV lines; header lines (from restarts) switch the columns."""
    prev_flow = None  # (epoch ms, FlowTemp)
    for row in csv.reader(lines):
        if not row:
            continue
        if row[0] == "time":  # the script was restarted here; columns may differ
            header, prev_flow = row, None
            continue
        if header is None or len(row) != len(header):
            continue
        try:
            ts = int(datetime.datetime.fromisoformat(row[0]).timestamp() * 1000)
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
    """Fill the chart history from the end of the CSV file; returns the number of samples loaded."""
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
    """Summarises chart samples into fixed time buckets: [start ms, mean, min, max of each SAMPLE_KEYS value]."""

    def __init__(self, name, bucket_ms, keep_minutes, path):
        self.name, self.bucket_ms, self.keep_minutes, self.path = name, bucket_ms, keep_minutes, path
        self.rows = collections.deque()
        self.start = None  # start of the bucket being filled
        self.acc = None

    def info(self):
        return {"name": self.name, "bucketMs": self.bucket_ms, "keepMinutes": self.keep_minutes}

    def header(self):
        return ["time", "ms"] + [f"{k}_{s}" for k in SAMPLE_KEYS for s in ("mean", "min", "max")]

    def file_row(self, row):
        when = datetime.datetime.fromtimestamp(row[0] / 1000).isoformat(timespec="seconds")
        return [when, row[0]] + ["" if v is None else v for v in row[1:]]

    def add(self, sample):
        """Add a chart sample; returns the finished bucket row when a bucket closes."""
        start = sample[0] - sample[0] % self.bucket_ms
        # ignore samples for buckets already finished (clock stepped back, or a restart mid-bucket)
        if (self.start is not None and start < self.start) or (self.rows and start <= self.rows[-1][0]):
            return None
        done = None
        if start != self.start:
            if self.start is not None:
                done = self._finish()
            self.start = start
            self.acc = [[0.0, 0, None, None, None] for _ in SAMPLE_KEYS]  # sum, count, min, max, last
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
            if k == "Statenumber":  # an average S-code means nothing; keep the last one
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
    """Fill an aggregator from the end of its file; returns rows loaded, or None if there is no file."""
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
    """Build missing summary files from the whole logger CSV (done once). Returns samples read."""
    if not os.path.exists(csv_path):
        return 0
    cutoff = epoch_ms() - max(a.keep_minutes for a in aggs) * 60000
    count = 0
    with open(csv_path, "rb") as f:
        lines = (line.replace(b"\0", b"").decode("utf-8", "replace") for line in f)
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
    """True if the file starts with exactly this header, has no other header lines and no NUL
    bytes, and ends with a complete line. Reads the file in large blocks, so it's quick."""
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
    """Clean up a CSV file written by this program, rewriting it only if needed:
    one header line (the current columns) at the top, no NUL bytes (left by crashes or power
    cuts), and no incomplete or unreadable rows. Rows written under an older header are
    converted to the current columns by name. Returns a summary string, or None if nothing changed."""
    tmp = path + ".repair"
    if os.path.exists(tmp):
        os.remove(tmp)  # left over from an interrupted repair; the original is intact
    if not os.path.exists(path) or os.path.getsize(path) == 0 or csv_is_clean(path, header):
        return None
    kept = dropped = headers = converted = nuls = 0
    cols = None  # the header the following rows were written under
    with open(path, "rb") as src, open(tmp, "w", newline="") as dst:
        w = csv.writer(dst)
        w.writerow(header)
        for raw in src:
            nuls += raw.count(b"\0")
            for piece in raw.split(b"\0"):  # text either side of NULs is a fragment of a line
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
                if cols is None or len(row) != len(cols) or not row_ok(dict(zip(cols, row))):
                    dropped += 1
                    continue
                if cols != header:
                    values = dict(zip(cols, row))
                    row = [values.get(c, "") for c in header]
                    converted += 1
                w.writerow(row)
                kept += 1
        dst.flush()
        os.fsync(dst.fileno())  # the card must hold the new file before it replaces the old one
    shutil.copymode(path, tmp)
    os.replace(tmp, path)
    dirfd = os.open(os.path.dirname(os.path.abspath(path)), os.O_RDONLY)
    try:
        os.fsync(dirfd)
    finally:
        os.close(dirfd)
    return (f"repaired {path}: kept {kept} rows, removed {max(0, headers - 1)} extra header lines, "
            f"{nuls} NUL bytes and {dropped} incomplete or unreadable rows; "
            f"converted {converted} rows from older column layouts")


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
    """Fill the change list from the end of the change log; returns the number of entries loaded."""
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
        """Write a message; ebusd answers "done" or "empty" on success, "ERR: ..." on failure."""
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


# ---------------------------------------------------------------- heating schedule

DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]  # datetime.weekday() order
MAX_PERIODS = 10  # on/off pairs per day


def parse_hhmm(text, allow_2400=False):
    """Minutes since midnight for "HH:MM", or None. "24:00" (end of day) only if allowed."""
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
    """Checked, normalised copy of a schedule from the web page, or (None, error message)."""
    if not isinstance(obj, dict) or not isinstance(obj.get("enabled"), bool) or not isinstance(obj.get("days"), dict):
        return None, 'expected {"enabled": true|false, "days": {"mon": [["06:30", "08:30"], ...], ...}}'
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
                return None, f"{day}: times must be HH:MM (off may be 24:00), got {p[0]!r}-{p[1]!r}"
            if on >= off:
                return None, f"{day}: {p[0]}-{p[1]}: on must be before off (use 24:00 for midnight)"
            spans.append((on, off))
        spans.sort()
        for (a0, a1), (b0, b1) in zip(spans, spans[1:]):
            if b0 < a1:
                return None, f"{day}: {hhmm(a0)}-{hhmm(a1)} overlaps {hhmm(b0)}-{hhmm(b1)}"
        days[day] = [[hhmm(a), hhmm(b)] for a, b in spans]
    return {"enabled": obj["enabled"], "days": days}, None


class Schedule:
    """7-day heating schedule, kept in a JSON file and saved atomically."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.data = {"enabled": False, "days": {d: [] for d in DAY_KEYS}, "updatedBy": None, "updatedAt": None}
        if os.path.exists(path):
            try:
                with open(path) as f:
                    saved = json.load(f)
                checked, err = validate_schedule(saved)
                if err:
                    raise ValueError(err)
                self.data = {**checked, "updatedBy": saved.get("updatedBy"), "updatedAt": saved.get("updatedAt")}
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
        """True if heating should be allowed at this (local) time, False if not, None if disabled."""
        with self.lock:
            if not self.data["enabled"]:
                return None
            m = when.hour * 60 + when.minute
            return any(parse_hhmm(a) <= m < parse_hhmm(b, True) for a, b in self.data["days"][DAY_KEYS[when.weekday()]])

    def next_change(self, when):
        """Local time at which wanted() next changes, or None (always on, always off, or disabled)."""
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
    """Every SetMode instruction sent to the boiler: appended to <csv name>.commands.csv and the
    last KEEP_DAYS kept in memory for the chart, as [epoch ms, "on"|"off", 1 if accepted else 0, source].
    source is "schedule", or "override:<user>" for the override button."""

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
            if len(r) != len(self.HEADER) or r[0] == "time" or r[2] not in ("on", "off"):
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
        when = datetime.datetime.fromtimestamp(ms / 1000).isoformat(timespec="milliseconds")
        if needs_header(self.path) and not self.rows:
            self.writer.csv_row(self.HEADER, self.path)
        self.writer.csv_row([when, ms, state, int(ok), result, source], self.path)
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
    """Applies the schedule by telling the boiler, over eBUS, to block or allow central heating.

    The block only lasts about 15 minutes after the last SetMode message, so the current state is
    re-sent on every 10-minute boundary of the clock (xx:00, xx:10, ...). It is also sent at once
    on startup and when the schedule is saved, and at the exact minute of each schedule on/off time
    (e.g. 07:31), then again on the following boundaries. A failed send is retried after
    RETRY_SECONDS. When the schedule is disabled, heating is allowed once, and then the boiler goes
    back to its own controls. The boiler's own timer still applies: the Pi can stop heating, but
    cannot start it while that timer is off.

    Override: one "heating on" is sent at once, whatever the schedule says, then nothing for
    OVERRIDE_SECONDS (the boiler holds it for about 15 minutes); then the schedule state is sent
    and the normal 10-minute boundaries resume.
    """

    RESEND_SECONDS = 600  # boundary spacing; must divide an hour
    RETRY_SECONDS = 60
    MAX_SLEEP = 60  # re-check at least this often, in case the clock is stepped
    HOLD_SECONDS = 900  # how long the boiler keeps a SetMode block without a repeat
    OVERRIDE_SECONDS = 840

    def __init__(self, schedule, hub, commands, host, port, flow_temp, hwc_temp):
        super().__init__(daemon=True)
        self.schedule, self.hub, self.commands, self.host, self.port = schedule, hub, commands, host, port
        self.flow_temp, self.hwc_temp = flow_temp, hwc_temp
        self.wake = threading.Event()
        self.sent = None        # "on"/"off" last confirmed by ebusd, or None
        self.due = None         # local time of the next regular send (boundary or schedule change)
        self.retry_at = None    # local time to retry after a failed send
        self.last = None        # {"time", "state", "ok", "result"} of the last attempt
        self.last_ok = None     # the same, for the last attempt the boiler accepted
        self.override = None    # {"until": local time, "by": user, "sent": bool} while overriding
        self.requests = []      # override requests from the web, handled by step()
        self.lock = threading.Lock()

    def request_override(self, user):
        with self.lock:
            self.requests.append(user)
        self.wake.set()

    def setmode(self, off):
        return f"auto;{self.flow_temp:g};{self.hwc_temp:g};-;{int(off)};0;0;0;0;0"

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
        self.last = {"time": stamp(), "state": state, "ok": ok, "result": result, "source": source}
        self.commands.add(state, ok, result, source)
        if ok:
            self.last_ok = self.last
            if state != self.sent:
                self.hub.record("HeatingSchedule", state, stamp())
            self.sent = state
        else:
            print(f"{datetime.datetime.now():%H:%M:%S} SetMode heating {state} failed: {result}", file=sys.stderr)
        return ok

    def next_slot(self, now):
        """The next 10-minute boundary after now, or the next schedule change if that comes first."""
        step = self.RESEND_SECONDS
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        secs = (now - midnight).total_seconds()
        boundary = midnight + datetime.timedelta(seconds=(secs // step + 1) * step)
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
            self.hub.record("HeatingOverride", f"on until {until:%H:%M:%S} ({requests[-1]})", stamp())
        if self.override and now >= self.override["until"]:
            self.override = None
            self.due = self.retry_at = None  # back to the schedule: send its state now
            self.hub.record("HeatingOverride", "ended", stamp())
        wanted = self.schedule.wanted(now)
        target = None if wanted is None else ("on" if wanted else "off")
        if self.override:
            o = self.override
            if not o["sent"] and (self.retry_at is None or now >= self.retry_at):
                if self.send("on", f"override:{o['by']}"):
                    o["sent"], self.retry_at = True, None
                else:
                    self.retry_at = min(now + datetime.timedelta(seconds=self.RETRY_SECONDS), o["until"])
            self.due = o["until"]
        elif target is None:  # schedule disabled: if heating is blocked, release it once
            self.due = None
            if self.sent == "off" and (self.retry_at is None or now >= self.retry_at):
                if self.send("on"):
                    self.sent, self.retry_at = None, None
                else:
                    self.retry_at = now + datetime.timedelta(seconds=self.RETRY_SECONDS)
            elif self.sent != "off":
                self.retry_at = None
        else:
            failed = self.retry_at is not None
            need = (self.due is None or now >= self.due
                    or (failed and now >= self.retry_at)
                    or (not failed and target != self.sent))  # e.g. the schedule was just saved
            if need:
                ok = self.send(target)
                self.retry_at = None if ok else now + datetime.timedelta(seconds=self.RETRY_SECONDS)
            self.due = self.next_slot(now)  # recomputed each time, so schedule edits take effect
        nxt = self.schedule.next_change(now)
        nxt_send = min(t for t in (self.due, self.retry_at) if t) if (self.due or self.retry_at) else None
        o = self.override
        if o and nxt_send == o["until"] and wanted is None and self.sent != "off":
            nxt_send = None  # override ending with the schedule disabled: nothing more is sent
        if nxt_send is None:
            nxt_state = None
        elif nxt_send == self.retry_at:
            nxt_state = "on" if o else target
        else:
            nxt_state = "on" if self.schedule.wanted(nxt_send) else "off"
        self.hub.set_control({
            "enabled": wanted is not None,
            "target": target,
            "nextChange": nxt.isoformat(timespec="minutes") if nxt else None,
            "nextSend": nxt_send.isoformat(timespec="seconds") if nxt_send else None,
            "nextState": nxt_state,
            "override": None if not o else {"until": o["until"].isoformat(timespec="seconds"),
                                            "by": o["by"], "sent": o["sent"]},
            "lastSend": self.last,
            "lastOk": self.last_ok,
            "resendSeconds": self.RESEND_SECONDS,
        })

    def sleep_seconds(self, now):
        times = [t for t in (self.due, self.retry_at) if t]
        if not times:
            return self.MAX_SLEEP
        # wake just after the due time; if woken early, the next step sees it isn't due and waits again
        return max(0.01, min(self.MAX_SLEEP, (min(times) - now).total_seconds() + 0.02))

    def run(self):
        while True:
            try:
                self.step()
            except Exception as e:  # never let the controller die quietly
                print(f"{datetime.datetime.now():%H:%M:%S} heating controller error: {e!r}", file=sys.stderr)
            self.wake.wait(self.sleep_seconds(datetime.datetime.now()))
            self.wake.clear()


class FileWriter(threading.Thread):
    """Appends CSV rows and change-log lines in batches from its own thread,
    so a stalled SD card holds up only this thread, never the polling or the web UI."""

    MAX_PENDING = 20_000_000  # characters held per file while writes keep failing

    def __init__(self, csv_path, changes_path, interval):
        super().__init__(daemon=True)
        self.csv_path, self.changes_path, self.interval = csv_path, changes_path, interval
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
        """Write out everything queued so far (waits up to 30 s for a slow card)."""
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
                print(f"{datetime.datetime.now():%H:%M:%S} cannot write {path}: {e}", file=sys.stderr)
                if len(data) > self.MAX_PENDING:
                    self.pending[path] = data[-self.MAX_PENDING:]


class Hub:
    """Latest values, chart history and recent changes, shared between the poller and the web clients."""

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
                    return {"tier": name, "bucketMs": a.bucket_ms, "rows": list(a.rows)}
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
            except queue.Full:  # client too slow; drop it and let the browser reconnect
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
            entry = {"time": when, "name": name, "old": display(name, old), "new": display(name, raw)}
            self.changes.append(entry)
            self._broadcast({"type": "change", **entry})
        self.writer.change(f"{when}: {name}: {entry['old']} -> {entry['new']}\n")

    def publish(self, when, rise, connected):
        ts = epoch_ms()
        with self.lock:
            self.updated, self.rise, self.connected = when, rise, connected
            sample = None
            if connected:  # no samples while disconnected, so the charts show a gap
                sample = make_sample(ts, self.values, rise)
                self.history.append(sample)
                cutoff = ts - self.history_minutes * 60000
                while self.history and self.history[0][0] < cutoff:
                    self.history.popleft()
                for a in self.aggs:
                    row = a.add(sample)
                    if row:
                        self.writer.csv_row(a.file_row(row), a.path)
                        self._broadcast({"type": "bucket", "tier": a.name, "row": row})
            self._broadcast({
                "type": "update",
                "ts": ts,
                "time": when,
                "values": dict(self.values),
                "rise": rise,
                "connected": connected,
                "sample": sample,
            })


def poll(hub, writer, host, port):
    names = list(VALUES)
    last = {n: "" for n in names}
    last_read = {n: 0.0 for n in names}
    prev_flow = None  # (monotonic time, FlowTemp) of the previous FlowTemp read
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

    if needs_header(writer.csv_path):  # existing files already start with it (see repair_csv)
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
            print(f"{datetime.datetime.now():%H:%M:%S} ebusd error: {e}; retrying", file=sys.stderr)
            hub.publish(stamp(), rise, False)
            bus = None
            time.sleep(5)


# ---------------------------------------------------------------- authentication

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
    """Checks passwords against an htpasswd file, looks up roles, and issues/verifies HS256 JWTs.

    The user and role files are re-read whenever they change, so editing them needs no restart.
    Deleting a user or removing their role stops them refreshing; their current token still
    works until it expires (at most --token-hours).
    """

    JWT_HEADER = b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    # used for unknown users so a login takes as long whether or not the name exists
    DUMMY_HASH = bcrypt.hashpw(b"x", bcrypt.gensalt(6))
    MAX_FAILURES, LOCKOUT_SECONDS = 5, 60

    def __init__(self, users_path, roles_path, secret_path, token_seconds, session_seconds):
        self.users_path, self.roles_path = users_path, roles_path
        self.token_seconds, self.session_seconds = token_seconds, session_seconds
        self.lock = threading.Lock()
        self.users, self.roles, self.mtimes = {}, {}, None
        self.failures = {}  # client address -> (count, time of last failure)
        self.key = self._load_key(secret_path)
        self._reload()
        if not self.users:
            sys.exit(f"no usable bcrypt users in {users_path}; nobody could log in")

    @staticmethod
    def _load_key(path):
        """Signing key, created on first run; kept so restarts don't log everyone out."""
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            with open(path, "rb") as f:
                key = f.read().strip()
            if len(key) < 32:
                sys.exit(f"{path} is too short to be a signing key; delete it to make a new one")
            if os.stat(path).st_mode & 0o077:
                print(f"warning: {path} is readable by other users; chmod 600 it", file=sys.stderr)
            return key
        key = base64.b64encode(os.urandom(48))
        with os.fdopen(fd, "wb") as f:
            f.write(key + b"\n")
        print(f"created token signing key {path}", file=sys.stderr)
        return key

    def _reload(self):
        try:
            mtimes = (os.stat(self.users_path).st_mtime_ns, os.stat(self.roles_path).st_mtime_ns)
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
                    print(f"{self.users_path}:{n}: {name} skipped (not a bcrypt hash; use htpasswd -B)",
                          file=sys.stderr)
                    continue
                users[name] = ("$2b$" + pw_hash[4:]).encode()  # $2y$ is the same algorithm as $2b$
        roles = {}
        with open(self.roles_path) as f:
            for n, line in enumerate(f, 1):
                line = line.split("#", 1)[0].strip()
                if not line:
                    continue
                role, _, members = line.partition(":")
                role = role.strip()
                if role not in ROLES:
                    print(f"{self.roles_path}:{n}: unknown role {role!r} ignored", file=sys.stderr)
                    continue
                for name in members.split():
                    if ROLE_RANK.index(role) > ROLE_RANK.index(roles.get(name, "viewer")) or name not in roles:
                        roles[name] = role
        for name in users:
            if name not in roles:
                print(f"{name} has no role in {self.roles_path}, so cannot log in", file=sys.stderr)
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
            if count >= self.MAX_FAILURES and now - last < self.LOCKOUT_SECONDS:
                return None, (429, f"too many failed logins; wait {self.LOCKOUT_SECONDS} s")
            self._reload()
            pw_hash = self.users.get(name)
            role = self.roles.get(name)
        try:
            ok = bcrypt.checkpw(password.encode(), pw_hash or self.DUMMY_HASH) and pw_hash is not None
        except ValueError:  # malformed hash in the file
            ok = False
        with self.lock:
            if not ok:
                self.failures[client] = (count + 1 if now - last < self.LOCKOUT_SECONDS else 1, now)
                return None, (401, "wrong username or password")
            self.failures.pop(client, None)
        if role is None:
            return None, (403, "this user has no role; ask the admin")
        return self.issue(name, role, int(now)), None

    def issue(self, name, role, auth_time):
        now = int(time.time())
        exp = min(now + self.token_seconds, auth_time + self.session_seconds)
        claims = {"sub": name, "role": role, "iat": now, "exp": exp, "auth_time": auth_time}
        body = self.JWT_HEADER + "." + b64url(json.dumps(claims, separators=(",", ":")).encode())
        sig = b64url(hmac.new(self.key, body.encode(), hashlib.sha256).digest())
        return {"token": body + "." + sig, "expiresIn": exp - now, "user": name, "role": role}

    def verify(self, token):
        """Claims of a valid, unexpired token, or None."""
        try:
            header, payload, sig = token.split(".")
            if header != self.JWT_HEADER:  # only ever accept our own HS256 header
                return None
            want = hmac.new(self.key, f"{header}.{payload}".encode(), hashlib.sha256).digest()
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
        """New token for the same login session, re-checking the user still exists and their role."""
        role = self.role_of(claims["sub"])
        if role is None:
            return None, (401, "user no longer allowed")
        if time.time() >= claims["auth_time"] + self.session_seconds:
            return None, (401, "session too old; log in again")
        return self.issue(claims["sub"], role, claims["auth_time"]), None


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                               "style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'",
}


def serve(hub, auth, controller, bind, port):
    started = time.time()
    schedule = controller.schedule

    class Handler(BaseHTTPRequestHandler):
        # path -> (method name, permission needed); None = no token needed
        ROUTES = {
            ("GET", "/"): ("_page", None),           # static page shell, no boiler data; it checks the
            ("GET", "/login"): ("_login_page", None),  # token itself and sends you to /login
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
                self._json(405 if allowed else 404, {"error": "method not allowed" if allowed else "not found"})
                return
            handler, permission = route
            self.claims = None
            if permission is not None:
                header = self.headers.get("Authorization", "")
                self.claims = auth.verify(header[7:]) if header.startswith("Bearer ") else None
                if self.claims is None:
                    self._json(401, {"error": "login required"}, {"WWW-Authenticate": "Bearer"})
                    return
                if permission not in ROLES[self.claims["role"]]:
                    self._json(403, {"error": f"your role ({self.claims['role']}) cannot do this"})
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
            self._send(code, "application/json", json.dumps(obj, separators=(",", ":")).encode(), headers)

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

        def _page(self):
            self._send(200, "text/html; charset=utf-8", PAGE.encode())

        def _login_page(self):
            self._send(200, "text/html; charset=utf-8", LOGIN_PAGE.encode())

        def _health(self):
            with hub.lock:
                connected, updated = hub.connected, hub.updated
            self._json(200, {"status": "ok", "uptime": int(time.time() - started),
                             "ebusd": "connected" if connected else "disconnected",
                             "lastUpdate": updated})

        def _login(self):
            body = self._body()
            name, password = (body or {}).get("username"), (body or {}).get("password")
            if not isinstance(name, str) or not isinstance(password, str) or not name or len(password) > 200:
                self._json(400, {"error": "username and password required"})
                return
            info, err = auth.login(name, password, self.client_address[0])
            if err:
                print(f"{datetime.datetime.now():%H:%M:%S} login failed for {name!r} from "
                      f"{self.client_address[0]}: {err[1]}", file=sys.stderr)
                self._json(err[0], {"error": err[1]})
            else:
                self._json(200, info)

        def _refresh(self):
            info, err = auth.refresh(self.claims)
            self._json(err[0], {"error": err[1]}) if err else self._json(200, info)

        def _whoami(self):
            c = self.claims
            self._json(200, {"user": c["sub"], "role": c["role"], "permissions": sorted(ROLES[c["role"]]),
                             "expiresIn": int(c["exp"] - time.time())})

        def _schedule_reply(self, code=200):
            with hub.lock:
                status = hub.control
            self._json(code, {**schedule.get(), "maxPeriods": MAX_PERIODS, "dayOrder": DAY_KEYS,
                              "status": status})

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
            print(f"{datetime.datetime.now():%H:%M:%S} schedule saved by {self.claims['sub']} "
                  f"(enabled: {checked['enabled']})", file=sys.stderr)
            controller.wake.set()  # apply it now
            time.sleep(0.2)  # usually lets the status below reflect the new schedule
            self._schedule_reply()

        def _override(self):
            user = self.claims["sub"]
            print(f"{datetime.datetime.now():%H:%M:%S} heating override by {user}", file=sys.stderr)
            controller.request_override(user)
            time.sleep(0.5)  # usually long enough for the send to finish
            self._schedule_reply()

        def _state(self):
            with hub.lock:
                snap = hub.snapshot()
            self._json(200, snap)

        def _commands(self):
            self._json(200, {"rows": controller.commands.get(), "holdSeconds": controller.HOLD_SECONDS})

        def _agg(self):
            tier = urllib.parse.parse_qs(self.query).get("tier", [""])[0]
            data = hub.agg_rows(tier)
            if data is None:
                self._json(404, {"error": "unknown tier"})
            else:
                self._json(200, data)

        def _push(self, msg):
            self.wfile.write(b"data: " + json.dumps(msg, separators=(",", ":")).encode() + b"\n\n")
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
                # the token was checked when the stream opened; end it when the token expires,
                # and the page reconnects with its refreshed token
                while not getattr(q, "dropped", False) and time.time() < self.claims["exp"]:
                    try:
                        self._push(q.get(timeout=5))
                    except queue.Empty:  # a real message, so the browser's watchdog sees it
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
    ap.add_argument("csv", nargs="?", default=time.strftime("boiler-%Y%m%d-%H%M%S.csv"),
                    help="CSV file to append to")
    ap.add_argument("--changes", help="change log file (default: CSV path with .changes.log)")
    ap.add_argument("--bind", default="0.0.0.0", help="web UI address (default: all interfaces)")
    ap.add_argument("--port", type=int, default=8000, help="web UI port (default: 8000)")
    ap.add_argument("--ebusd", default="127.0.0.1:8888", help="ebusd command port (default: 127.0.0.1:8888)")
    ap.add_argument("--history-minutes", type=int, default=60,
                    help="chart history kept in memory, i.e. the longest chart window (default: 60)")
    ap.add_argument("--flush-seconds", type=float, default=30,
                    help="how often to write the CSV and change log to disk (default: 30)")
    ap.add_argument("--users", required=True, help="htpasswd file with bcrypt passwords (htpasswd -B)")
    ap.add_argument("--roles", required=True, help="roles file: lines of 'admin|user|viewer: name name ...'")
    ap.add_argument("--secret-file", default="boiler-ui.secret",
                    help="token signing key, created if missing (default: ./boiler-ui.secret)")
    ap.add_argument("--token-hours", type=float, default=12, help="token lifetime (default: 12)")
    ap.add_argument("--session-days", type=float, default=30,
                    help="longest a login can be kept alive by refreshing (default: 30)")
    ap.add_argument("--schedule", help="heating schedule file (default: schedule.json next to the CSV)")
    ap.add_argument("--flow-temp", type=float, default=75,
                    help="heating flow temperature sent with each SetMode, °C (default: 75)")
    ap.add_argument("--hwc-temp", type=float, default=52.5,
                    help="hot water temperature sent with each SetMode, °C (default: 52.5)")
    args = ap.parse_args()
    changes = args.changes or os.path.splitext(args.csv)[0] + ".changes.log"
    host, _, port = args.ebusd.rpartition(":")
    auth = Auth(args.users, args.roles, args.secret_file,
                int(args.token_hours * 3600), int(args.session_days * 86400))

    writer = FileWriter(args.csv, changes, args.flush_seconds)
    base = os.path.splitext(args.csv)[0]
    aggs = [Aggregator(name, ms, keep, f"{base}.{name}.csv") for name, ms, keep in TIERS]
    for path, header, row_ok in [(args.csv, ["time"] + list(VALUES), logger_row_ok)] + \
            [(a.path, a.header(), summary_row_ok) for a in aggs] + \
            [(f"{base}.commands.csv", CommandLog.HEADER, summary_row_ok)]:
        t0 = time.monotonic()
        result = repair_csv(path, header, row_ok)
        if result:
            print(f"{result} ({time.monotonic() - t0:.0f} s)", file=sys.stderr)
    hub = Hub(writer, args.history_minutes, aggs)
    print(f"loaded {load_history(hub, args.csv)} chart samples from {args.csv} "
          f"and {load_changes(hub, changes)} changes from {changes}", file=sys.stderr)
    missing = []
    for a in aggs:
        n = load_agg(a)
        if n is None:
            missing.append(a)
        else:
            print(f"loaded {n} {a.name} summaries from {a.path}", file=sys.stderr)
        if needs_header(a.path):
            writer.csv_row(a.header(), a.path)
    if missing:  # first run with summaries: build them from the existing CSV (one-off, may take a while)
        print(f"building {', '.join(a.path for a in missing)} from {args.csv}...", file=sys.stderr)
        t0 = time.monotonic()
        n = backfill(missing, args.csv, writer)
        print(f"read {n} samples in {time.monotonic() - t0:.0f} s; "
              + ", ".join(f"{len(a.rows)} {a.name} summaries" for a in missing), file=sys.stderr)
    schedule = Schedule(args.schedule or os.path.join(os.path.dirname(os.path.abspath(args.csv)), "schedule.json"))
    commands = CommandLog(f"{base}.commands.csv", hub, writer)
    print(f"loaded {commands.load()} boiler instructions from {commands.path}", file=sys.stderr)
    controller = HeatingController(schedule, hub, commands, host, int(port), args.flow_temp, args.hwc_temp)
    writer.start()
    controller.start()
    serve(hub, auth, controller, args.bind, args.port)
    print(f"web UI on port {args.port}; logging to {args.csv} and {changes} "
          f"every {args.flush_seconds:g} s", file=sys.stderr)

    def on_sigterm(signum, frame):  # systemctl stop/restart: write out what's queued first
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, on_sigterm)
    try:
        poll(hub, writer, host, int(port))
    except KeyboardInterrupt:
        pass
    finally:
        writer.stop()


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Boiler</title>
<script>
// Runs before anything else: no valid token, no page. The server also checks every API call.
const AUTH_KEY = "boilerAuth";
function getAuth() {
  try {
    const a = JSON.parse(localStorage.getItem(AUTH_KEY));
    return a && a.token && Date.now() < a.expiresAt ? a : null;
  } catch (e) { return null; }
}
function toLogin() {
  localStorage.removeItem(AUTH_KEY);
  location.replace("/login?next=" + encodeURIComponent(location.pathname + location.search));
}
if (!getAuth()) toLogin();
</script>
<style>
:root {
  color-scheme: dark;
  --bg: #0d0d0d; --card: #1a1a19; --text: #f0efec; --text2: #c3c2b7; --muted: #898781;
  --track: #2c2c2a; --grid: #2c2c2a; --axis: #383835;
  --on: #ff8a2a; --ok: #3fb950; --warn: #fab219; --bad: #d03b3b;
  --s1: #3987e5; --s2: #d95926; --s3: #199e70; --red: #d03b3b; --yellow: #fab219; --lgrey: #c3c2b7;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text); font: 14px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1400px; margin: 0 auto; padding: 16px; display: grid; gap: 12px; }
.card { background: var(--card); border-radius: 8px; padding: 12px 16px; }
h2 { margin: 0; font-size: 14px; color: var(--muted); font-weight: 600; }
#status { display: flex; flex-wrap: wrap; gap: 10px 24px; align-items: center; }
#state { font-size: 20px; font-weight: 600; min-width: 18ch; }
.lamp { display: inline-flex; align-items: center; gap: 6px; color: var(--muted); }
.lamp i { width: 12px; height: 12px; border-radius: 50%; background: var(--track); }
.lamp.on { color: var(--text); }
.lamp.on i { background: var(--on); box-shadow: 0 0 8px var(--on); }
.kv { color: var(--muted); }
.kv b { color: var(--text); font-weight: 600; }
#gauges { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 8px; }
.gauge { text-align: center; }
.gauge svg { width: 100%; max-width: 150px; display: block; margin: 0 auto; }
.gauge .val { font-size: 17px; font-weight: 600; fill: var(--text); }
.gauge .unit { font-size: 9px; fill: var(--muted); }
.gauge .lbl { color: var(--muted); margin-top: -6px; }
#info { display: flex; flex-wrap: wrap; gap: 6px 20px; }
.stale { color: var(--bad) !important; }

.bar { display: flex; align-items: center; gap: 12px; margin-bottom: 8px; }
.bar select { background: var(--bg); color: var(--text); border: 1px solid var(--axis); border-radius: 6px; padding: 3px 6px; font: inherit; }
#charts { display: grid; grid-template-columns: repeat(auto-fill, minmax(520px, 1fr)); gap: 14px 20px; }
.chart.wide { grid-column: 1 / -1; }
.chead { display: flex; flex-wrap: wrap; gap: 4px 14px; align-items: center; color: var(--text2); font-size: 13px; }
.ctitle { color: var(--text); font-weight: 600; }
.legend { display: inline-flex; align-items: center; gap: 5px; }
.legend i { width: 12px; height: 2px; border-radius: 1px; }
.chart svg { width: 100%; display: block; cursor: crosshair; }
.chart .gridl { stroke: var(--grid); stroke-width: 1; }
.chart .base { stroke: var(--axis); stroke-width: 1; }
.chart .ylab, .chart .xlab { fill: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.chart .vlab { fill: var(--text2); font-size: 11px; font-variant-numeric: tabular-nums; }
.chart .band { stroke: none; opacity: 0.22; }
.chart .zone { opacity: 0.10; }
#who { margin-left: auto; }
#logout { background: var(--bg); color: var(--text2); border: 1px solid var(--axis); border-radius: 6px;
          padding: 2px 10px; font: inherit; cursor: pointer; }
#logout:hover { color: var(--text); }
.btn { background: var(--bg); color: var(--text2); border: 1px solid var(--axis); border-radius: 6px;
       padding: 3px 10px; font: inherit; cursor: pointer; }
.btn:hover:not(:disabled) { color: var(--text); }
.btn:disabled { opacity: 0.5; cursor: default; }
.btn.primary { background: var(--s1); border-color: var(--s1); color: #fff; }
.btn.small { padding: 0 7px; font-size: 12px; }
.err { color: var(--bad); }
.cmd-state { padding: 0 6px; border-radius: 4px; }
.cmd-state.on { background: var(--on); color: #111; }
.cmd-state.off { background: var(--track); color: var(--text); }
.srow { display: grid; grid-template-columns: 44px 1fr; gap: 10px; align-items: center; padding: 3px 0; }
.srow .day { color: var(--text2); }
.srow .day.today { color: var(--text); font-weight: 600; }
.strip { position: relative; height: 14px; background: var(--track); border-radius: 3px; }
.strip span { position: absolute; top: 0; bottom: 0; background: var(--on); border-radius: 2px; min-width: 2px; }
.strip i { position: absolute; top: -3px; bottom: -3px; width: 2px; background: var(--text); }
.hours { position: relative; height: 14px; font-size: 11px; color: var(--muted); }
.hours span { position: absolute; transform: translateX(-50%); }
.hours span:first-child { transform: none; }
.hours span:last-child { transform: translateX(-100%); }
.eday { display: grid; grid-template-columns: 44px 1fr; gap: 10px; padding: 6px 0; border-top: 1px solid #252b33; }
.eday .periods { display: flex; flex-wrap: wrap; gap: 6px 14px; align-items: center; }
.period { display: inline-flex; gap: 4px; align-items: center; }
.period input { background: var(--bg); color: var(--text); border: 1px solid var(--axis); border-radius: 4px;
                padding: 1px 4px; font: inherit; color-scheme: dark; }
.chart .line { fill: none; stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
.chart .lane { fill: var(--track); }
.chart .run { fill: var(--on); }
.chart .cross { stroke: var(--text2); stroke-width: 1; stroke-dasharray: 3 3; }
.chart .dot { stroke: var(--card); stroke-width: 2; }
#tip { position: fixed; pointer-events: none; background: #262624; border: 1px solid rgba(255,255,255,0.10);
       border-radius: 6px; padding: 6px 9px; font-size: 12px; line-height: 1.5; color: var(--text); z-index: 10;
       font-variant-numeric: tabular-nums; white-space: nowrap; }
#tip .tt { color: var(--muted); }
#tip i { display: inline-block; width: 10px; height: 2px; margin: 0 6px 3px 0; border-radius: 1px; }

#log { list-style: none; margin: 8px 0 0; padding: 0; max-height: 45vh; overflow-y: auto;
       font: 13px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
#log li { border-bottom: 1px solid #252b33; padding: 1px 0; }
#log .t { color: var(--muted); }
#log .n { color: var(--on); }
.note { color: var(--muted); font-size: 12px; }
</style>
</head>
<body>
<main>
  <section class="card" id="status">
    <div id="state">—</div>
    <span class="lamp" id="l-Flame"><i></i>Flame</span>
    <span class="lamp" id="l-Gasvalve"><i></i>Gas valve (d.30)</span>
    <span class="lamp" id="l-Ignitor"><i></i>Ignitor</span>
    <span class="lamp" id="l-HwcDemand"><i></i>HW demand (d.22)</span>
    <span class="kv">Valve position (d.35) <b data-v="PositionValveSet">—</b></span>
    <span class="kv">Error <b data-v="Currenterror">—</b></span>
    <span class="kv" id="cmd" title="last SetMode instruction the boiler accepted">Last instruction <b class="cmd-state" id="cmd-state">—</b> <b id="cmd-when"></b> <span class="err" id="cmd-fail"></span></span>
    <span class="kv" id="cmd-next-item" title="next SetMode instruction the app will send">Next instruction <b class="cmd-state" id="cmd-next">—</b> <b id="cmd-next-when"></b></span>
  </section>

  <section class="card" id="gauges"></section>

  <section class="card" id="info">
    <span class="kv">Temp limiter cut-outs (d.60) <b data-v="DeactivationsTemplimiter">—</b></span>
    <span class="kv">IFC cut-outs <b data-v="DeactivationsIFC">—</b></span>
    <span class="kv">Start attempts <b data-v="CounterStartattempts1">—</b></span>
    <span class="kv">Ionisation (d.44) <b data-v="IonisationVoltageLevel">—</b></span>
    <span class="kv">Last update <b id="age">—</b></span>
    <span class="kv" id="who"></span>
    <button id="logout" type="button">Log out</button>
  </section>

  <section class="card" id="sched">
    <div class="bar">
      <h2>Heating schedule</h2>
      <label class="kv" title="change it with Edit"><input type="checkbox" id="sched-on" disabled> enabled</label>
      <button id="sched-edit" class="btn" type="button" hidden>Edit</button>
      <button id="override" class="btn" type="button" hidden
        title="send one 'heating on' now, whatever the schedule says; the schedule takes over again after 14 minutes">Heating ON for 14 min</button>
      <span class="kv" id="override-info"></span>
    </div>
    <div id="sched-view"></div>
    <form id="sched-form" hidden>
      <label class="kv"><input type="checkbox" id="sched-enabled">
        Schedule enabled (when disabled, the boiler follows its own controls)</label>
      <div id="sched-days"></div>
      <div class="bar">
        <button class="btn primary" type="submit">Save</button>
        <button class="btn" type="button" id="sched-cancel">Cancel</button>
        <span class="err" id="sched-err" role="alert"></span>
      </div>
    </form>
    <div class="note">Heating runs only while the boiler's own timer is also on; the schedule can switch it off,
      not on. Times are 24-hour HH:MM: 00:00 is the start of the day and 24:00 the end.</div>
  </section>

  <section class="card">
    <div class="bar">
      <h2>History</h2>
      <label class="kv">Window <select id="win"></select></label>
      <span class="note" id="res"></span>
    </div>
    <div id="charts"></div>
  </section>

  <section class="card">
    <h2>Changes</h2>
    <ul id="log"></ul>
  </section>
  <div class="note">S-code descriptions are approximate (ecoTEC plus service documentation).</div>
</main>
<div id="tip" hidden></div>

<script>
const STATES = {
  0: "Heating: no demand", 1: "Heating: fan start", 2: "Heating: pump pre-run",
  3: "Heating: ignition", 4: "Heating: burner on", 5: "Heating: fan & pump overrun",
  6: "Heating: fan overrun", 7: "Heating: pump overrun", 8: "Heating: anti-cycling",
  10: "HW: demand", 11: "HW: fan start", 13: "HW: ignition", 14: "HW: burner on",
  15: "HW: fan & pump overrun", 16: "HW: fan overrun", 17: "HW: pump overrun",
  30: "No heating demand (thermostat)", 31: "No heating demand (summer / controller)",
  32: "Waiting: fan speed out of range", 34: "Frost protection",
  36: "No heating demand (setpoint < 20 °C)", 39: "Floor contact open",
  41: "Water pressure too high", 53: "Waiting: flow/return ΔT too large",
  54: "Waiting: temperature gradient too steep", 96: "Return sensor test",
  97: "Pressure sensor test", 98: "Flow/return sensor test",
};
const stateText = s => s == null ? "—" : `S.${s}  ${STATES[Number(s)] || ""}`;

// ---------------------------------------------------------------- gauges
// key, label, unit, min, max, decimals. A negative min gives a centre-zero gauge
// (zero at 12 o'clock, negative values to the left).
const GAUGES = [
  ["FlowTemp", "Flow (d.40)", "°C", 0, 90, 1],
  ["ReturnTemp", "Return (d.41)", "°C", 0, 90, 1],
  ["HwcTemp", "Hot water", "°C", 0, 70, 1],
  ["StorageTemp", "Storage", "°C", 0, 70, 1],
  ["FanSpeed", "Fan (d.34)", "rpm", 0, 6000, 0],
  ["HwcWaterflow", "HW flow (d.36)", "l/min", 0, 15, 2],
  ["PumpPower", "Pump", "%", 0, 100, 0],
  ["WaterPressure", "Pressure", "bar", 0, 3, 2],
];
const LAMPS = ["Flame", "Gasvalve", "Ignitor", "HwcDemand"];
const A0 = 150, SWEEP = 240, MID = A0 + SWEEP / 2, MAX_LOG = 500;

function num(raw) {
  if (raw == null) return null;
  const f = parseFloat(String(raw).split(";")[0]);
  return isNaN(f) ? null : f;
}

function arc(a0, a1) {
  const r = 40, cx = 50, cy = 52;
  const p = a => [cx + r * Math.cos(a * Math.PI / 180), cy + r * Math.sin(a * Math.PI / 180)];
  const [x0, y0] = p(a0), [x1, y1] = p(a1);
  return `M${x0.toFixed(2)} ${y0.toFixed(2)} A${r} ${r} 0 ${a1 - a0 > 180 ? 1 : 0} 1 ${x1.toFixed(2)} ${y1.toFixed(2)}`;
}

const gauges = {};
for (const [key, label, unit, min, max, dp] of GAUGES) {
  const div = document.createElement("div");
  div.className = "gauge";
  div.innerHTML =
    `<svg viewBox="0 0 100 82">` +
    `<path d="${arc(A0, A0 + SWEEP)}" stroke="var(--track)" stroke-width="8" fill="none" stroke-linecap="round"/>` +
    `<path class="fill" stroke="var(--ok)" stroke-width="8" fill="none" stroke-linecap="round"/>` +
    (min < 0 ? `<line x1="50" y1="6" x2="50" y2="18" stroke="var(--muted)" stroke-width="1.5"/>` : "") +
    `<text class="val" x="50" y="56" text-anchor="middle">—</text>` +
    `<text class="unit" x="50" y="70" text-anchor="middle"></text></svg>` +
    `<div class="lbl"></div>`;
  div.querySelector(".unit").textContent = unit;
  div.querySelector(".lbl").textContent = label;
  document.getElementById("gauges").appendChild(div);
  gauges[key] = { min, max, dp, fill: div.querySelector(".fill"), val: div.querySelector(".val") };
}

function setGauge(key, v) {
  const g = gauges[key];
  if (v == null) {
    g.val.textContent = "—";
    g.fill.setAttribute("d", "");
    return;
  }
  g.val.textContent = v.toFixed(g.dp);
  let a0, a1, frac;
  if (g.min < 0) {
    const f = v >= 0 ? Math.min(1, v / g.max) : -Math.min(1, v / g.min);
    [a0, a1] = f >= 0 ? [MID, MID + SWEEP / 2 * f] : [MID + SWEEP / 2 * f, MID];
    frac = Math.abs(f);
  } else {
    frac = Math.min(1, Math.max(0, (v - g.min) / (g.max - g.min)));
    [a0, a1] = [A0, A0 + SWEEP * frac];
  }
  g.fill.setAttribute("d", frac < 0.002 ? "" : arc(a0, a1));
  let colour = "var(--ok)";
  if (key === "rise") colour = v >= 5 ? "var(--bad)" : v >= 3 ? "var(--warn)" : v < 0 ? "var(--s1)" : "var(--ok)";
  g.fill.setAttribute("stroke", colour);
}

function applyValues(values, rise) {
  document.getElementById("state").textContent = stateText(values.Statenumber);
  for (const name of LAMPS) {
    const v = values[name];
    document.getElementById("l-" + name).classList.toggle("on", v === "on" || v === "yes");
  }
  for (const el of document.querySelectorAll("[data-v]")) {
    const v = values[el.dataset.v];
    el.textContent = v == null ? "—" : v;
  }
  for (const key in gauges) setGauge(key, key === "rise" ? rise : num(values[key]));
}

// ---------------------------------------------------------------- charts
// One chart per measure (no shared y-scales); every chart shares the time axis and crosshair.
const CHARTS = [
  { title: "Temperatures", unit: "°C", dp: 1, minSpan: 4, wide: true,
    series: [["FlowTemp", "Flow (d.40)", "var(--s1)"], ["ReturnTemp", "Return (d.41)", "var(--s2)"], ["HwcTemp", "Hot water", "var(--s3)"]] },
  { title: "Boiler instructions (SetMode)", wide: true, commands: true,
    legend: [["heating allowed (on)", "var(--on)"], ["heating blocked (off)", "var(--s1)"], ["send failed", "var(--bad)"], ["override (tall tick)", "var(--on)"]] },
  { title: "Burner & demand (gas valve d.30, HW demand d.22)", wide: true,
    strips: [["Flame", "Flame"], ["Gasvalve", "Gas valve"], ["HwcDemand", "HW demand"]] },
  { title: "Flow rise", unit: "°C/s", dp: 1, minSpan: 2, zero: true, series: [["rise", "Flow rise", "var(--s1)"]] },
  { title: "Fan", d: 34, unit: "rpm", dp: 0, minSpan: 500, series: [["FanSpeed", "Fan", "var(--s1)"]] },
  { title: "Hot water flow", d: 36, unit: "l/min", dp: 2, minSpan: 1, series: [["HwcWaterflow", "HW flow", "var(--s1)"]] },
  { title: "Pump", unit: "%", dp: 0, minSpan: 20, series: [["PumpPower", "Pump", "var(--s1)"]] },
  { title: "Pressure", unit: "bar", dp: 2, minSpan: 4, range: [0, 4],
    series: [["WaterPressure", "Pressure", null]],
    zones: [[0, 2.5, "var(--ok)", "normal", ""], [2.5, 3, "var(--yellow)", "high", ""],
            [3, 4, "var(--red)", "too high", ""]] },
  // d.44: 0-102, > 80 no flame, < 40 even flame (ecoTEC plus manual, table 8.2); 80 itself counts as unstable
  { title: "Ionisation (d.44)", unit: "", dp: 1, minSpan: 102, range: [0, 102],
    series: [["IonisationVoltageLevel", "Ionisation", null]],
    zones: [[0, 40, "var(--ok)", "even flame", ""], [40, 80 + 1e-9, "var(--red)", "unstable flame", ""],
            [80 + 1e-9, 103, "var(--lgrey)", "no flame", ""]] },
  // d.35: 0 = heating, 40 = centre, 100 = hot water (ecoTEC plus manual, table 8.2)
  { title: "Diverter valve (d.35)", unit: "", dp: 0, minSpan: 100, range: [0, 100],
    series: [["PositionValveSet", "Diverter", null]],
    zones: [[0, 30, "var(--red)", "heating", "CH"], [30, 80, "var(--yellow)", "middle", "mid"],
            [80, 101, "var(--red)", "hot water", "HW"]] },
];
const ML = 80, MR = 58, MT = 8, MB = 20, LINE_H = 130, ROW_H = 18;
const GAP_MS = 15000;  // longer gaps between raw samples break the line
const WINDOWS = [1, 2, 5, 10, 15, 30, 60, 120, 240, 1440, 2880, 10080, 43200];
const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

let keyIndex = {}, samples = [], historyMinutes = 60, skew = 0, tiers = [];
let windowMin = 10, hoverT = null, hoverChart = null, src = null;

// A data source gives the charts rows plus accessors, so raw samples (one value per key)
// and bucket summaries (mean/min/max per key) draw through the same code.
function rawSource() {
  const v = (r, k) => r[keyIndex[k]];
  return { rows: samples, bucketMs: 0, gap: GAP_MS, t: r => r[0], mean: v, lo: v, hi: v };
}

function aggSource(tier) {
  const b = k => 1 + 3 * (keyIndex[k] - 1);
  return {
    rows: tier.rows || [], bucketMs: tier.bucketMs, gap: tier.bucketMs * 2.5, loading: !tier.rows,
    t: r => r[0] + tier.bucketMs / 2,
    mean: (r, k) => r[b(k)], lo: (r, k) => r[b(k) + 1], hi: (r, k) => r[b(k) + 2],
  };
}

function currentSource() {
  if (windowMin <= historyMinutes || !tiers.length) return rawSource();
  const tier = tiers.find(t => t.keepMinutes >= windowMin) || tiers[tiers.length - 1];
  loadTier(tier);
  return aggSource(tier);
}

function loadTier(tier) {
  if (tier.rows || tier.loading || Date.now() < tier.retryAt) return;
  tier.loading = true;
  api(`/api/agg?tier=${encodeURIComponent(tier.name)}`)
    .then(r => { if (!r.ok) throw new Error(r.status); return r.json(); })
    .then(d => {
      tier.rows = d.rows;
      for (const row of tier.buffer) addBucket(tier, row);
      tier.buffer = [];
    })
    .catch(() => { tier.retryAt = Date.now() + 10000; })
    .finally(() => { tier.loading = false; scheduleDraw(); });
}

function addBucket(tier, row) {
  if (!tier.rows) {  // not fetched yet: keep it in case a fetch is in flight
    if (tier.buffer.length < 100) tier.buffer.push(row);
    return;
  }
  const rows = tier.rows;
  if (rows.length && row[0] <= rows[rows.length - 1][0]) return;
  rows.push(row);
  const cutoff = row[0] - tier.keepMinutes * 60000;
  while (rows.length && rows[0][0] < cutoff) rows.shift();
}

function windowLabel(m) {
  if (m < 60) return `${m} min`;
  if (m < 1440) return `${m / 60} h`;
  return { 1440: "1 day", 10080: "1 week", 43200: "1 month" }[m] || `${m / 1440} days`;
}

const chartsEl = document.getElementById("charts");
const charts = CHARTS.map((c, n) => {
  const card = document.createElement("div");
  card.className = "chart" + (c.wide ? " wide" : "");
  const head = document.createElement("div");
  head.className = "chead";
  const title = document.createElement("span");
  title.className = "ctitle";
  title.textContent = c.title + (c.unit ? ` (${c.unit})` : "") + (c.d ? ` (d.${c.d})` : "");
  head.append(title);
  const legend = c.legend || (c.series && c.series.length > 1 ? c.series.map(([, label, col]) => [label, col]) : []);
  {
    for (const [label, col] of legend) {
      const item = document.createElement("span");
      item.className = "legend";
      const sw = document.createElement("i");
      sw.style.background = col;
      item.append(sw, document.createTextNode(label));
      head.append(item);
    }
  }
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  const height = c.strips ? MT + c.strips.length * ROW_H + MB : c.commands ? MT + 2 * ROW_H + MB : LINE_H;
  svg.setAttribute("height", height);
  card.append(head, svg);
  chartsEl.append(card);
  const ch = { c, svg, height, id: n };
  svg.addEventListener("mousemove", e => onHover(e, ch));
  svg.addEventListener("mouseleave", () => { hoverT = null; tip.hidden = true; scheduleDraw(); });
  return ch;
});

function lowerBound(s, t) {  // first row with time >= t
  let lo = 0, hi = s.rows.length;
  while (lo < hi) { const m = (lo + hi) >> 1; if (s.t(s.rows[m]) < t) lo = m + 1; else hi = m; }
  return lo;
}

function nearest(s, t) {
  const rows = s.rows, i = lowerBound(s, t);
  if (i >= rows.length) return rows[rows.length - 1];
  if (i > 0 && t - s.t(rows[i - 1]) < s.t(rows[i]) - t) return rows[i - 1];
  return rows[i];
}

function niceStep(x) {
  const p = Math.pow(10, Math.floor(Math.log10(x))), f = x / p;
  return (f < 1.5 ? 1 : f < 3 ? 2 : f < 7 ? 5 : 10) * p;
}

const pad2 = n => String(n).padStart(2, "0");
function clock(t, secs) {
  const d = new Date(t);
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}` + (secs ? `:${pad2(d.getSeconds())}` : "");
}

function tickLabel(t, step, span) {
  const d = new Date(t);
  if (step >= 86400e3) return `${DAYS[d.getDay()]} ${d.getDate()}`;
  if (span > 86400e3) return `${DAYS[d.getDay()]} ${clock(t)}`;
  return clock(t, step < 60e3);
}

function timeAxis(x, t0, t1, W, H, baseline = true) {  // lane charts skip the baseline: it looks like another lane
  const steps = [5e3, 10e3, 15e3, 30e3, 60e3, 120e3, 300e3, 600e3, 900e3, 1800e3, 3600e3,
                 7200e3, 10800e3, 21600e3, 43200e3, 86400e3, 172800e3, 604800e3];
  const step = steps.find(s => s >= (t1 - t0) / 6) || 604800e3;
  const off = -new Date(t1).getTimezoneOffset() * 60000;  // put hour/day ticks on local time
  let out = baseline ? `<line class="base" x1="${ML}" x2="${W - MR}" y1="${H - MB}" y2="${H - MB}"/>` : "";
  for (let t = Math.ceil((t0 + off) / step) * step - off; t <= t1; t += step) {
    const px = x(t).toFixed(1);
    out += `<line class="gridl" x1="${px}" x2="${px}" y1="${MT}" y2="${H - MB}"/>` +
           `<text class="xlab" x="${px}" y="${H - 5}" text-anchor="middle">${tickLabel(t, step, t1 - t0)}</text>`;
  }
  return out;
}

function frame(ch, t0, t1) {
  const W = ch.svg.clientWidth || 600, H = ch.height;
  const x = t => ML + (t - t0) / (t1 - t0) * (W - ML - MR);
  const clip = `<clipPath id="clip${ch.id}"><rect x="${ML}" y="0" width="${Math.max(0, W - ML - MR)}" height="${H}"/></clipPath>`;
  return { W, H, x, clip };
}

function crosshair(x, H) {
  if (hoverT == null) return "";
  const px = x(hoverT).toFixed(1);
  return `<line class="cross" x1="${px}" x2="${px}" y1="${MT}" y2="${H - MB}"/>`;
}

// zones: [from (inclusive), to (exclusive), colour, name, short name]; values past the last zone use the last zone
function zoneOf(c, v) {
  return c.zones.find(([a, b]) => v >= a && v < b) || c.zones[c.zones.length - 1];
}

function drawLines(ch, s, t0, t1) {
  const { c } = ch, { W, H, x, clip } = frame(ch, t0, t1);
  let lo = Infinity, hi = -Infinity;
  for (const r of s.rows) {
    if (s.t(r) < t0) continue;
    for (const [k] of c.series) {
      const a = s.lo(r, k), b = s.hi(r, k);
      if (a != null) lo = Math.min(lo, a);
      if (b != null) hi = Math.max(hi, b);
    }
  }
  if (lo === Infinity) { lo = 0; hi = c.minSpan; }
  if (c.range) [lo, hi] = c.range;
  if (c.zero) { lo = Math.min(lo, 0); hi = Math.max(hi, 0); }
  if (hi - lo < c.minSpan) { const m = (hi + lo) / 2; lo = m - c.minSpan / 2; hi = m + c.minSpan / 2; }
  const padY = (hi - lo) * 0.08;
  lo -= padY; hi += padY;
  const y = v => MT + (hi - v) / (hi - lo) * (H - MT - MB);

  let out = clip + timeAxis(x, t0, t1, W, H);
  const step = niceStep((hi - lo) / 3), tdp = Math.max(0, Math.min(3, -Math.floor(Math.log10(step))));
  for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) {
    const py = y(v).toFixed(1);
    out += `<line class="gridl" x1="${ML}" x2="${W - MR}" y1="${py}" y2="${py}"/>` +
           `<text class="ylab" x="${ML - 6}" y="${(+py + 4).toFixed(1)}" text-anchor="end">${v.toFixed(tdp)}</text>`;
  }
  if (c.zero) out += `<line class="base" x1="${ML}" x2="${W - MR}" y1="${y(0).toFixed(1)}" y2="${y(0).toFixed(1)}"/>`;

  const labels = [];
  if (c.zones) {  // step line coloured by the zone each value falls in, over faint zone bands
    const zone = v => zoneOf(c, v);
    for (const [a, b, col] of c.zones) {
      const ya = y(Math.max(a, lo)), yb = y(Math.min(b, hi));
      out += `<rect class="zone" x="${ML}" y="${yb.toFixed(1)}" width="${Math.max(0, W - ML - MR)}" height="${(ya - yb).toFixed(1)}" fill="${col}"/>`;
    }
    for (const [k] of c.series) {
      const segs = {};  // colour -> path
      let rise = "", prev = null, lastV = null;
      for (const r of s.rows) {
        const v = s.mean(r, k), t = s.t(r);
        if (v == null) { prev = null; continue; }
        if (prev && t - prev[0] < s.gap) {
          const col = zone(prev[1])[2], x0 = x(prev[0]).toFixed(1), x1 = x(t).toFixed(1);
          segs[col] = (segs[col] || "") + `M${x0} ${y(prev[1]).toFixed(1)}H${x1}`;
          if (v !== prev[1]) rise += `M${x1} ${y(prev[1]).toFixed(1)}V${y(v).toFixed(1)}`;
        }
        prev = [t, v]; lastV = v;
      }
      if (prev) {  // hold the last value to the right-hand edge
        const col = zone(prev[1])[2];
        segs[col] = (segs[col] || "") + `M${x(prev[0]).toFixed(1)} ${y(prev[1]).toFixed(1)}H${x(Math.min(t1, prev[0] + s.gap)).toFixed(1)}`;
      }
      out += `<path class="line" clip-path="url(#clip${ch.id})" stroke="var(--muted)" stroke-width="1" d="${rise}"/>`;
      for (const col in segs) out += `<path class="line" clip-path="url(#clip${ch.id})" stroke="${col}" d="${segs[col]}"/>`;
      if (lastV != null) labels.push({ y: y(lastV), text: `${lastV.toFixed(c.dp)} ${zone(lastV)[4]}`.trim(), col: zone(lastV)[2] });
    }
  }
  if (!c.zones) c.series.forEach(([k, , col]) => {
    // mean line; for bucket summaries also a min-max band behind it
    let d = "", band = "", pen = false, lastT = -Infinity, lastV = null, top = [], bot = [];
    const closeBand = () => {
      if (top.length > 1) band += "M" + top.join("L") + "L" + bot.reverse().join("L") + "Z";
      top = []; bot = [];
    };
    for (const r of s.rows) {
      const v = s.mean(r, k), t = s.t(r);
      if (v == null) { pen = false; closeBand(); continue; }
      const joined = pen && t - lastT < s.gap;
      if (!joined) closeBand();
      const px = x(t).toFixed(1);
      d += `${joined ? "L" : "M"}${px} ${y(v).toFixed(1)}`;
      if (s.bucketMs) {
        top.push(`${px} ${y(s.hi(r, k)).toFixed(1)}`);
        bot.push(`${px} ${y(s.lo(r, k)).toFixed(1)}`);
      }
      pen = true; lastT = t; lastV = v;
    }
    closeBand();
    if (band) out += `<path class="band" clip-path="url(#clip${ch.id})" fill="${col}" d="${band}"/>`;
    out += `<path class="line" clip-path="url(#clip${ch.id})" stroke="${col}" d="${d}"/>`;
    if (lastV != null) labels.push({ y: y(lastV), text: lastV.toFixed(c.dp), col });
  });

  // current values in the right margin, nudged apart so they never overlap
  labels.sort((a, b) => a.y - b.y);
  for (let i = 1; i < labels.length; i++) labels[i].y = Math.max(labels[i].y, labels[i - 1].y + 13);
  for (const l of labels) {
    const py = Math.min(H - MB, Math.max(MT + 4, l.y));
    out += `<line x1="${W - MR + 4}" x2="${W - MR + 12}" y1="${py.toFixed(1)}" y2="${py.toFixed(1)}" stroke="${l.col}" stroke-width="2"/>` +
           `<text class="vlab" x="${W - MR + 16}" y="${(py + 4).toFixed(1)}">${l.text}</text>`;
  }

  out += crosshair(x, H);
  if (hoverT != null && src && src.rows.length) {
    const r = nearest(src, hoverT);
    for (const [k, , col] of c.series) {
      const v = s.mean(r, k);
      if (v != null) out += `<circle class="dot" r="4" fill="${c.zones ? zoneOf(c, v)[2] : col}" cx="${x(s.t(r)).toFixed(1)}" cy="${y(v).toFixed(1)}"/>`;
    }
  }
  return out;
}

function drawStrips(ch, s, t0, t1) {
  const { c } = ch, { W, H, x, clip } = frame(ch, t0, t1);
  let out = clip + timeAxis(x, t0, t1, W, H, false);
  c.strips.forEach(([k, label], n) => {
    const top = MT + n * ROW_H;
    out += `<text class="ylab" x="${ML - 6}" y="${top + 13}" text-anchor="end">${label}</text>` +
           `<rect class="lane" x="${ML}" y="${top + 3}" width="${Math.max(0, W - ML - MR)}" height="12" rx="2"/>`;
    const bar = (a, b) => {
      const x0 = x(a), w = Math.max(2, x(b) - x0);
      out += `<rect class="run" clip-path="url(#clip${ch.id})" x="${x0.toFixed(1)}" y="${top + 3}" width="${w.toFixed(1)}" height="12" rx="2"/>`;
    };
    if (s.bucketMs) {  // a bucket counts as "on" if it was on at any moment; adjacent buckets merge
      let a = null, b = null;
      for (const r of s.rows) {
        if (s.hi(r, k) !== 1) continue;
        if (a !== null && r[0] <= b) { b = r[0] + s.bucketMs; continue; }
        if (a !== null) bar(a, b);
        a = r[0]; b = r[0] + s.bucketMs;
      }
      if (a !== null) bar(a, b);
    } else {
      let start = null, prevT = null;
      const flush = end => { if (start !== null) { bar(start, end); start = null; } };
      for (const r of s.rows) {
        if (prevT !== null && r[0] - prevT > s.gap) flush(prevT);
        if (s.mean(r, k) === 1) { if (start === null) start = r[0]; } else flush(r[0]);
        prevT = r[0];
      }
      flush(prevT);
    }
  });
  return out + crosshair(x, H);
}

// ---------------------------------------------------------------- boiler instructions chart
// Top lane: what the boiler was told, held from each accepted instruction until the next one,
// or until the boiler's own timeout (holdSeconds) if nothing followed. Bottom lane: every send,
// one tick each (red if ebusd refused it or could not be reached); omitted beyond 2 days.
let commands = [], holdMs = 900000;

function loadCommands() {
  api("/api/commands").then(r => r.ok ? r.json() : null).then(d => {
    if (!d) return;
    commands = d.rows;
    holdMs = d.holdSeconds * 1000;
    scheduleDraw();
  }).catch(() => {});
}

function addCommand(row) {
  if (commands.length && row[0] <= commands[commands.length - 1][0]) return;
  commands.push(row);
  const cutoff = row[0] - 31 * 86400e3;
  while (commands.length && commands[0][0] < cutoff) commands.shift();
  scheduleDraw();
}

// accepted instructions as [from, to, state] runs
function commandRuns(t1) {
  const ok = commands.filter(c => c[2] === 1), runs = [];
  ok.forEach((c, i) => {
    const end = Math.min(i + 1 < ok.length ? ok[i + 1][0] : Infinity, c[0] + holdMs, t1);
    const last = runs[runs.length - 1];
    if (last && last[2] === c[1] && last[1] >= c[0]) last[1] = end;
    else runs.push([c[0], end, c[1]]);
  });
  return runs;
}

function drawCommands(ch, t0, t1) {
  const { W, H, x, clip } = frame(ch, t0, t1);
  const colour = st => st === "on" ? "var(--on)" : "var(--s1)";
  let out = clip + timeAxis(x, t0, t1, W, H, false);
  const lane = (n, label) => {
    const top = MT + n * ROW_H;
    out += `<text class="ylab" x="${ML - 6}" y="${top + 13}" text-anchor="end">${label}</text>` +
           `<rect class="lane" x="${ML}" y="${top + 3}" width="${Math.max(0, W - ML - MR)}" height="12" rx="2"/>`;
    return top;
  };
  let top = lane(0, "Boiler told");
  for (const [a, b, st] of commandRuns(t1)) {
    if (b < t0) continue;
    const x0 = x(Math.max(a, t0)), w = Math.max(2, x(b) - x0);
    out += `<rect clip-path="url(#clip${ch.id})" fill="${colour(st)}" x="${x0.toFixed(1)}" y="${top + 3}" ` +
           `width="${w.toFixed(1)}" height="12" rx="2"/>`;
  }
  top = lane(1, "Sends");
  if (t1 - t0 <= 2 * 86400e3) {
    for (const [ms, st, ok, source] of commands) {
      if (ms < t0) continue;
      const tall = !ok || (source || "").startsWith("override:");  // failures and overrides stand out
      out += `<rect clip-path="url(#clip${ch.id})" fill="${ok ? colour(st) : "var(--bad)"}" ` +
             `x="${(x(ms) - (tall ? 1.5 : 1)).toFixed(1)}" y="${top + (tall ? 0 : 3)}" width="${tall ? 3 : 2}" height="${tall ? 18 : 12}"/>`;
    }
  } else {
    out += `<text class="ylab" x="${ML + 6}" y="${top + 13}">individual sends shown for windows up to 2 days</text>`;
  }
  return out + crosshair(x, H);
}

function sourceText(source) {
  return source && source.startsWith("override:") ? ` (override by ${source.slice(9)})` : "";
}

function fillCommandTip() {
  const t = hoverT, rows = [];
  const add = (text, col, cls) => {
    const d = document.createElement("div");
    if (cls) d.className = cls;
    if (col) { const sw = document.createElement("i"); sw.style.background = col; d.append(sw); }
    d.append(document.createTextNode(text));
    rows.push(d);
  };
  add(clock(t, true), null, "tt");
  const run = commandRuns(Date.now() + skew).find(([a, b]) => a <= t && t < b);
  add(run ? `boiler told: heating ${run[2] === "on" ? "allowed (on)" : "blocked (off)"}`
          : "boiler told: nothing (own controls)", run ? (run[2] === "on" ? "var(--on)" : "var(--s1)") : null);
  let prev = null;
  for (const c of commands) { if (c[0] > t) break; prev = c; }
  if (prev) add(`last send ${clock(prev[0], true)}: ${prev[1]}${sourceText(prev[3])}${prev[2] ? "" : " (failed)"}`,
                prev[2] ? null : "var(--bad)");
  tip.replaceChildren(...rows);
}

const view = () => { const t1 = Date.now() + skew; return [t1 - windowMin * 60000, t1]; };

function draw() {
  const [t0, t1] = view();
  src = currentSource();
  const s = { ...src, rows: src.rows.slice(Math.max(0, lowerBound(src, t0) - 1)) };
  for (const ch of charts) {
    ch.svg.innerHTML = ch.c.commands ? drawCommands(ch, t0, t1)
      : ch.c.strips ? drawStrips(ch, s, t0, t1) : drawLines(ch, s, t0, t1);
  }
  const tier = src.bucketMs ? tiers.find(t => t.bucketMs === src.bucketMs) : null;
  document.getElementById("res").textContent = src.loading ? "loading…"
    : tier ? `${tier.name} averages · shaded band = min–max` : "every reading";
  if (hoverT != null) fillTip();
}

let drawPending = false;
function scheduleDraw() {
  if (drawPending) return;
  drawPending = true;
  requestAnimationFrame(() => { drawPending = false; draw(); });
}

// ---------------------------------------------------------------- hover tooltip
const tip = document.getElementById("tip");

function onHover(e, ch) {
  const r = ch.svg.getBoundingClientRect(), px = e.clientX - r.left;
  if (px < ML || px > r.width - MR || (!ch.c.commands && (!src || !src.rows.length))) {
    hoverT = null; tip.hidden = true; scheduleDraw(); return;
  }
  const [t0, t1] = view();
  hoverT = t0 + (px - ML) / (r.width - ML - MR) * (t1 - t0);
  hoverChart = ch;
  tip.hidden = false;
  fillTip();
  const tw = tip.offsetWidth, th = tip.offsetHeight;
  tip.style.left = (e.clientX + 14 + tw > innerWidth ? e.clientX - 14 - tw : e.clientX + 14) + "px";
  tip.style.top = (e.clientY + 14 + th > innerHeight ? e.clientY - 14 - th : e.clientY + 14) + "px";
  scheduleDraw();
}

function fillTip() {
  if (hoverChart.c.commands) { fillCommandTip(); return; }
  if (!src || !src.rows.length) return;
  const s = src, r = nearest(s, hoverT), c = hoverChart.c, agg = s.bucketMs > 0;
  const fmt = v => v == null ? "—" : v.toFixed(c.dp);
  const rows = [];
  const time = document.createElement("div");
  time.className = "tt";
  const when = agg
    ? `${DAYS[new Date(r[0]).getDay()]} ${clock(r[0])}–${clock(r[0] + s.bucketMs)}`
    : clock(r[0], true);
  time.textContent = `${when}  ·  ${stateText(s.mean(r, "Statenumber"))}${agg ? " (last)" : ""}`;
  rows.push(time);
  if (c.series) {
    for (const [k, label, col] of c.series) {
      const row = document.createElement("div"), sw = document.createElement("i"), v = s.mean(r, k);
      const z = c.zones && v != null ? zoneOf(c, v) : null;
      sw.style.background = z ? z[2] : col;
      const text = (agg
        ? `${label}: ${fmt(v)} ${c.unit} (${fmt(s.lo(r, k))}–${fmt(s.hi(r, k))})`
        : `${label}: ${fmt(v)} ${c.unit}`) + (z ? ` · ${z[3]}` : "");
      row.append(sw, document.createTextNode(text));
      rows.push(row);
    }
  } else {
    for (const [k, label] of c.strips) {
      const row = document.createElement("div"), v = s.mean(r, k);
      row.textContent = agg
        ? `${label}: ${v == null ? "—" : `on ${Math.round(v * 100)}% of the time`}`
        : `${label}: ${v === 1 ? "on" : "off"}`;
      rows.push(row);
    }
  }
  tip.replaceChildren(...rows);
}

// ---------------------------------------------------------------- window selector
const winSel = document.getElementById("win");
function buildWindows() {
  const wanted = Number(new URLSearchParams(location.search).get("minutes")) ||
                 Number(localStorage.getItem("boilerWindow")) || windowMin;
  const opts = WINDOWS.filter(m => m <= historyMinutes || tiers.some(t => t.keepMinutes >= m));
  if (!opts.includes(historyMinutes)) opts.push(historyMinutes);
  opts.sort((a, b) => a - b);
  windowMin = opts.includes(wanted) ? wanted : Math.min(10, historyMinutes);
  winSel.replaceChildren(...opts.map(m => {
    const o = document.createElement("option");
    o.value = m;
    o.textContent = windowLabel(m);
    o.selected = m === windowMin;
    return o;
  }));
}
winSel.addEventListener("change", () => {
  windowMin = Number(winSel.value);
  localStorage.setItem("boilerWindow", windowMin);
  scheduleDraw();
});

// ---------------------------------------------------------------- heating schedule
// Everyone sees it; admins get an editor. The server checks the role again on save.
const DAY_LABELS = { mon: "Mon", tue: "Tue", wed: "Wed", thu: "Thu", fri: "Fri", sat: "Sat", sun: "Sun" };
const JS_DAY = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"];
let sched = null, editing = false, control = null;

const el = (tag, cls, text) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
};
const minutes = t => +t.slice(0, 2) * 60 + +t.slice(3);

function loadSchedule() {
  if (editing) return;
  api("/api/schedule").then(r => r.ok ? r.json() : null).then(d => {
    if (!d || editing) return;
    sched = d;
    if (d.status) control = d.status;
    renderSchedule();
  }).catch(() => {});
}

function renderSchedule() {
  const view = document.getElementById("sched-view");
  document.getElementById("sched-edit").hidden = editing || !sched || (getAuth() || {}).role !== "admin";
  showEnabled();
  showControl(control);
  if (!sched) return;
  const now = new Date(), today = JS_DAY[now.getDay()], nowMin = now.getHours() * 60 + now.getMinutes();
  const rows = [];
  const hours = el("div", "hours");
  for (const h of [0, 3, 6, 9, 12, 15, 18, 21, 24]) {
    const s = el("span", null, String(h).padStart(2, "0"));
    s.style.left = (h / 24 * 100) + "%";
    hours.append(s);
  }
  const head = el("div", "srow");
  head.append(el("span"), hours);
  rows.push(head);
  for (const day of sched.dayOrder) {
    const row = el("div", "srow");
    const strip = el("div", "strip");
    for (const [on, off] of sched.days[day]) {
      const s = el("span");
      s.style.left = (minutes(on) / 1440 * 100) + "%";
      s.style.width = ((minutes(off) - minutes(on)) / 1440 * 100) + "%";
      s.title = `${on}–${off}`;
      strip.append(s);
    }
    if (day === today) {
      const mark = el("i");
      mark.style.left = (nowMin / 1440 * 100) + "%";
      mark.title = "now";
      strip.append(mark);
    }
    row.append(el("span", "day" + (day === today ? " today" : ""), DAY_LABELS[day]), strip);
    rows.push(row);
  }
  view.replaceChildren(...rows);
  view.style.opacity = sched.enabled ? "" : "0.45";
}

// "Last instruction" in the status bar: what the boiler last accepted, how long ago, and any later failure
function ago(iso) {
  const secs = Math.max(0, (Date.now() + skew - new Date(iso).getTime()) / 1000);
  return secs < 60 ? `${Math.round(secs)} s ago` : secs < 3600 ? `${Math.floor(secs / 60)} min ago`
    : `${Math.floor(secs / 3600)} h ${Math.floor(secs % 3600 / 60)} min ago`;
}

function showInstruction() {
  const state = document.getElementById("cmd-state"), when = document.getElementById("cmd-when"),
        fail = document.getElementById("cmd-fail"), ok = control && control.lastOk, last = control && control.lastSend;
  state.className = "cmd-state" + (ok ? " " + ok.state : "");
  state.textContent = ok ? `heating ${ok.state.toUpperCase()}`
    : !control ? "unknown (no status from the app yet)"
    : !control.enabled ? "none: the heating schedule is disabled" : "none sent yet";
  when.textContent = ok ? `at ${ok.time.slice(11, 19)} (${ago(ok.time)})${sourceText(ok.source)}` : "";
  fail.textContent = last && !last.ok ? `· ${last.state} failed at ${last.time.slice(11, 19)}: ${last.result}` : "";

  const next = document.getElementById("cmd-next"), nextWhen = document.getElementById("cmd-next-when");
  const due = control && control.nextSend;
  next.className = "cmd-state" + (due ? " " + control.nextState : "");
  next.textContent = due ? `heating ${control.nextState.toUpperCase()}`
    : !control ? "unknown" : !control.enabled ? "none: the heating schedule is disabled" : "—";
  nextWhen.textContent = due ? `at ${control.nextSend.slice(11, 19)} (${until(control.nextSend)})` : "";
}

function until(iso) {
  const secs = Math.max(0, (new Date(iso).getTime() - Date.now() - skew) / 1000);
  return secs < 60 ? `in ${Math.round(secs)} s` : `in ${Math.floor(secs / 60)} min ${Math.round(secs % 60)} s`;
}

function showControl(c) {
  control = c;
  showEnabled();
  showInstruction();
  showOverride();
}

// Override button: users and admins (the server checks the role too)
function showOverride() {
  const role = (getAuth() || {}).role, btn = document.getElementById("override");
  const info = document.getElementById("override-info"), o = control && control.override;
  btn.hidden = role !== "user" && role !== "admin";
  btn.textContent = o ? "Restart override" : "Heating ON for 14 min";
  info.textContent = o ? `override by ${o.by}: heating ON until ${o.until.slice(11, 19)} (${until(o.until)})`
    + (o.sent ? "" : " · not sent yet, retrying") : "";
  info.classList.toggle("err", !!(o && !o.sent));
}

document.getElementById("override").addEventListener("click", () => {
  const btn = document.getElementById("override"), info = document.getElementById("override-info");
  btn.disabled = true;
  info.textContent = "sending…";
  api("/api/override", { method: "POST" }).then(async r => {
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || `override failed (${r.status})`);
    if (d.status) showControl(d.status);
  }).catch(ex => { info.textContent = ex.message; info.classList.add("err"); })
    .finally(() => { btn.disabled = false; });
});

function showEnabled() {
  const enabled = control ? control.enabled : sched ? sched.enabled : null;
  const box = document.getElementById("sched-on");
  box.checked = !!enabled;
  box.indeterminate = enabled == null;
}

function periodRow(on = "", off = "") {
  const p = el("span", "period");
  const a = el("input"), b = el("input"), x = el("button", "btn small", "✕");
  // text, not type=time: time inputs can't hold 24:00, the end of the day
  for (const [input, value, pattern] of [[a, on, "([01]\\d|2[0-3]):[0-5]\\d"],
                                         [b, off, "([01]\\d|2[0-3]):[0-5]\\d|24:00"]]) {
    input.type = "text";
    input.required = true;
    input.value = value;
    input.pattern = pattern;
    input.placeholder = "HH:MM";
    input.maxLength = 5;
    input.size = 5;
    input.inputMode = "numeric";
  }
  a.title = "on time, 00:00–23:59";
  b.title = "off time, 00:01–24:00 (24:00 = end of the day)";
  a.setAttribute("aria-label", "on");
  b.setAttribute("aria-label", "off");
  x.type = "button";
  x.title = "remove";
  x.addEventListener("click", () => { const box = p.parentElement; p.remove(); updateAdd(box); });
  p.append(a, el("span", "note", "–"), b, x);
  return p;
}

function updateAdd(box) {
  box.querySelector(".add").disabled = box.querySelectorAll(".period").length >= sched.maxPeriods;
}

function readDay(box) {
  return [...box.querySelectorAll(".period")].map(p => {
    const [a, b] = p.querySelectorAll("input");
    return [a.value.trim(), b.value.trim()];
  });
}

function openEditor() {
  editing = true;
  document.getElementById("sched-enabled").checked = sched.enabled;
  const days = sched.dayOrder.map(day => {
    const row = el("div", "eday"), box = el("div", "periods");
    box.dataset.day = day;
    for (const [on, off] of sched.days[day]) box.append(periodRow(on, off));
    const add = el("button", "btn small add", "+ add"), copy = el("button", "btn small", "copy to all days");
    add.type = copy.type = "button";
    add.addEventListener("click", () => { add.before(periodRow()); updateAdd(box); });
    copy.addEventListener("click", () => {
      const periods = readDay(box);
      for (const other of document.querySelectorAll("#sched-days .periods")) {
        if (other === box) continue;
        other.querySelectorAll(".period").forEach(p => p.remove());
        const otherAdd = other.querySelector(".add");
        for (const [on, off] of periods) otherAdd.before(periodRow(on, off));
        updateAdd(other);
      }
    });
    box.append(add, copy);
    row.append(el("span", "day", DAY_LABELS[day]), box);
    updateAdd(box);
    return row;
  });
  document.getElementById("sched-days").replaceChildren(...days);
  document.getElementById("sched-err").textContent = "";
  document.getElementById("sched-view").hidden = true;
  document.getElementById("sched-form").hidden = false;
  renderSchedule();
}

function closeEditor() {
  editing = false;
  document.getElementById("sched-form").hidden = true;
  document.getElementById("sched-view").hidden = false;
  renderSchedule();
}

document.getElementById("sched-edit").addEventListener("click", openEditor);
document.getElementById("sched-cancel").addEventListener("click", closeEditor);
document.getElementById("sched-form").addEventListener("submit", e => {
  e.preventDefault();
  const days = {};
  for (const box of document.querySelectorAll("#sched-days .periods")) days[box.dataset.day] = readDay(box);
  const err = document.getElementById("sched-err");
  err.textContent = "saving…";
  api("/api/schedule", {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled: document.getElementById("sched-enabled").checked, days }),
  }).then(async r => {
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || `save failed (${r.status})`);
    sched = d;
    if (d.status) control = d.status;
    closeEditor();
  }).catch(ex => { err.textContent = ex.message; });
});

loadSchedule();
setInterval(loadSchedule, 60000);  // picks up edits made elsewhere
setInterval(() => { if (!editing) renderSchedule(); }, 60000);  // move the "now" mark

// ---------------------------------------------------------------- change list
const log = document.getElementById("log");
function addChange(c) {
  const li = document.createElement("li");
  const t = document.createElement("span");
  t.className = "t";
  t.textContent = c.time.slice(11) + ": ";
  t.title = c.time;
  const n = document.createElement("span");
  n.className = "n";
  n.textContent = c.name;
  li.append(t, n, document.createTextNode(`: ${c.old} -> ${c.new}`));
  log.prepend(li);
  while (log.children.length > MAX_LOG) log.lastChild.remove();
}

// ---------------------------------------------------------------- auth
// Every API call carries the token; a 401 means it expired or was rejected, so log in again.
// The token is refreshed once a quarter of its lifetime is left (checked every 30 s and when
// the tab becomes visible). If the PC slept past expiry, the next check sends you to /login.
function api(url, opts = {}) {
  const a = getAuth();
  if (!a) { toLogin(); return new Promise(() => {}); }
  return fetch(url, { ...opts, headers: { ...(opts.headers || {}), Authorization: "Bearer " + a.token } })
    .then(r => { if (r.status === 401) { toLogin(); return new Promise(() => {}); } return r; });
}

function saveAuth(info) {
  localStorage.setItem(AUTH_KEY, JSON.stringify({
    token: info.token, user: info.user, role: info.role,
    expiresAt: Date.now() + info.expiresIn * 1000, lifetime: info.expiresIn * 1000,
  }));
}

let refreshing = false;
function checkToken() {
  const a = getAuth();
  if (!a) { toLogin(); return; }
  document.getElementById("who").textContent = `${a.user} (${a.role})`;
  if (refreshing || a.expiresAt - Date.now() > Math.min(a.lifetime / 4, 3600e3)) return;
  refreshing = true;
  api("/api/refresh", { method: "POST" })
    .then(r => r.ok ? r.json().then(saveAuth) : null)
    .catch(() => {})  // network trouble: try again next time
    .finally(() => { refreshing = false; });
}
checkToken();
setInterval(checkToken, 30000);
document.getElementById("logout").addEventListener("click", toLogin);

// ---------------------------------------------------------------- live updates
// Server-sent events read through fetch(), because EventSource cannot send the token.
// After a PC suspend/resume or a network drop, a dead connection can look "open" for minutes.
// So: reconnect whenever nothing has arrived for STALL_MS (the server sends at least a ping
// every 5 s), when the stream ends (the server also ends it when the token expires), and
// straight away when the page becomes visible again or the network comes back.
const STALL_MS = 12000;
let lastRx = 0, lastMsg = 0, connected = false, stream = null, streamOpen = false, retryMs = 1000, retryAt = 0;

function connect() {
  if (stream) stream.abort();
  const ctl = new AbortController();
  stream = ctl;
  streamOpen = false;
  lastMsg = Date.now();
  api("/api/events", { signal: ctl.signal, cache: "no-store" }).then(async r => {
    if (!r.ok || !r.body) throw new Error(r.status);
    streamOpen = true;
    retryMs = 1000;
    const reader = r.body.pipeThrough(new TextDecoderStream()).getReader();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += value;
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const event = buf.slice(0, i);
        buf = buf.slice(i + 2);
        const data = event.split("\n").filter(l => l.startsWith("data: ")).map(l => l.slice(6)).join("\n");
        if (data) onMessage({ data });
      }
    }
  }).catch(() => {}).finally(() => {
    if (stream !== ctl) return;  // superseded by a newer connection
    streamOpen = false;
    scheduleReconnect();
  });
}

function scheduleReconnect() {
  if (retryAt) return;
  retryAt = Date.now() + retryMs;
  retryMs = Math.min(retryMs * 2, 30000);
}

function checkConnection(force) {
  const stalled = Date.now() - lastMsg > STALL_MS;
  if (force || stalled) { retryAt = 0; connect(); return; }
  if (retryAt && Date.now() >= retryAt) { retryAt = 0; connect(); }
}

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible") return;
  checkToken();
  if (Date.now() - lastMsg > 3000) checkConnection(true);
});
addEventListener("online", () => checkConnection(true));
addEventListener("pageshow", e => { if (e.persisted) checkConnection(true); });  // restored from back/forward cache

function onMessage(ev) {
  lastMsg = Date.now();
  const m = JSON.parse(ev.data);
  if (m.type === "ping") return;
  if (m.type === "change") { addChange(m); return; }
  if (m.type === "control") { showControl(m.control); return; }
  if (m.type === "command") { addCommand(m.row); return; }
  if (m.type === "bucket") {
    const tier = tiers.find(t => t.name === m.tier);
    if (tier) { addBucket(tier, m.row); scheduleDraw(); }
    return;
  }
  skew = m.ts - Date.now();
  if (m.type === "snapshot") {
    log.replaceChildren();
    for (const c of m.changes) addChange(c);
    keyIndex = {};
    m.sampleKeys.forEach((k, i) => { keyIndex[k] = i + 1; });
    samples = m.history;
    historyMinutes = m.historyMinutes;
    tiers = (m.tiers || []).map(t => ({ ...t, rows: null, loading: false, buffer: [], retryAt: 0 }));
    showControl(m.control);
    loadCommands();
    buildWindows();
  } else if (m.sample) {
    samples.push(m.sample);
    const cutoff = m.ts - historyMinutes * 60000;
    while (samples.length && samples[0][0] < cutoff) samples.shift();
  }
  connected = m.connected;
  lastRx = m.time ? Date.now() : 0;
  applyValues(m.values, m.rise);
  showAge();
  scheduleDraw();
}
connect();

function showAge() {
  const age = document.getElementById("age");
  const live = streamOpen && Date.now() - lastMsg <= STALL_MS;
  const secs = lastRx ? (Date.now() - lastRx) / 1000 : null;
  age.textContent = !live ? "web connection lost, reconnecting…"
    : !connected ? "ebusd disconnected"
    : secs == null ? "waiting…" : `${secs.toFixed(1)} s ago`;
  age.classList.toggle("stale", !live || !connected || secs == null || secs > 5);
}

setInterval(() => {
  checkConnection(false);
  showAge();
  showInstruction();
  showOverride();
  scheduleDraw();  // keep the charts scrolling between updates
}, 1000);
addEventListener("resize", scheduleDraw);
</script>
</body>
</html>
"""


LOGIN_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Boiler: log in</title>
<style>
:root { color-scheme: dark; }
body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: #0d0d0d; color: #f0efec;
       font: 14px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif; }
form { background: #1a1a19; border-radius: 8px; padding: 24px 28px; display: grid; gap: 12px; width: 280px; }
h1 { margin: 0 0 4px; font-size: 18px; }
label { display: grid; gap: 4px; color: #c3c2b7; }
input { background: #0d0d0d; color: #f0efec; border: 1px solid #383835; border-radius: 6px; padding: 7px 9px; font: inherit; }
button { background: #3987e5; color: #fff; border: 0; border-radius: 6px; padding: 8px; font: inherit; font-weight: 600; cursor: pointer; }
button:disabled { opacity: 0.6; cursor: default; }
#err { color: #f85149; min-height: 1.4em; margin: 0; }
</style>
</head>
<body>
<form id="f">
  <h1>Boiler</h1>
  <label>Username <input id="u" name="username" autocomplete="username" required autofocus></label>
  <label>Password <input id="p" name="password" type="password" autocomplete="current-password" required></label>
  <button id="b">Log in</button>
  <p id="err" role="alert"></p>
</form>
<script>
const next = new URLSearchParams(location.search).get("next") || "/";
const safeNext = next.startsWith("/") && !next.startsWith("//") ? next : "/";
document.getElementById("f").addEventListener("submit", async e => {
  e.preventDefault();
  const b = document.getElementById("b"), err = document.getElementById("err");
  b.disabled = true; err.textContent = "";
  try {
    const r = await fetch("/api/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: document.getElementById("u").value,
                             password: document.getElementById("p").value }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || `login failed (${r.status})`);
    localStorage.setItem("boilerAuth", JSON.stringify({
      token: d.token, user: d.user, role: d.role,
      expiresAt: Date.now() + d.expiresIn * 1000, lifetime: d.expiresIn * 1000,
    }));
    location.replace(safeNext);
  } catch (ex) {
    err.textContent = ex.message === "Failed to fetch" ? "cannot reach the boiler Pi" : ex.message;
    b.disabled = false;
  }
});
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
