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
        self.f.write(f"read -f -c bai {name}\n")
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


def serve(hub, auth, bind, port):
    started = time.time()

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
            ("GET", "/api/events"): ("_events", "read"),
        }

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

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

        def _body(self):
            try:
                n = int(self.headers.get("Content-Length", 0))
            except ValueError:
                n = 0
            if not 0 < n <= 4096:
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

        def _state(self):
            with hub.lock:
                snap = hub.snapshot()
            self._json(200, snap)

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
    args = ap.parse_args()
    changes = args.changes or os.path.splitext(args.csv)[0] + ".changes.log"
    host, _, port = args.ebusd.rpartition(":")
    auth = Auth(args.users, args.roles, args.secret_file,
                int(args.token_hours * 3600), int(args.session_days * 86400))

    writer = FileWriter(args.csv, changes, args.flush_seconds)
    base = os.path.splitext(args.csv)[0]
    aggs = [Aggregator(name, ms, keep, f"{base}.{name}.csv") for name, ms, keep in TIERS]
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
        writer.csv_row(a.header(), a.path)
    if missing:  # first run with summaries: build them from the existing CSV (one-off, may take a while)
        print(f"building {', '.join(a.path for a in missing)} from {args.csv}...", file=sys.stderr)
        t0 = time.monotonic()
        n = backfill(missing, args.csv, writer)
        print(f"read {n} samples in {time.monotonic() - t0:.0f} s; "
              + ", ".join(f"{len(a.rows)} {a.name} summaries" for a in missing), file=sys.stderr)
    writer.start()
    serve(hub, auth, args.bind, args.port)
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
  ["rise", "Flow rise", "°C/s", -10, 10, 1],
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
  if (c.series && c.series.length > 1) {
    for (const [, label, col] of c.series) {
      const item = document.createElement("span");
      item.className = "legend";
      const sw = document.createElement("i");
      sw.style.background = col;
      item.append(sw, document.createTextNode(label));
      head.append(item);
    }
  }
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  const height = c.strips ? MT + c.strips.length * ROW_H + MB : LINE_H;
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

function timeAxis(x, t0, t1, W, H) {
  const steps = [5e3, 10e3, 15e3, 30e3, 60e3, 120e3, 300e3, 600e3, 900e3, 1800e3, 3600e3,
                 7200e3, 10800e3, 21600e3, 43200e3, 86400e3, 172800e3, 604800e3];
  const step = steps.find(s => s >= (t1 - t0) / 6) || 604800e3;
  const off = -new Date(t1).getTimezoneOffset() * 60000;  // put hour/day ticks on local time
  let out = `<line class="base" x1="${ML}" x2="${W - MR}" y1="${H - MB}" y2="${H - MB}"/>`;
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
  let out = clip + timeAxis(x, t0, t1, W, H);
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

const view = () => { const t1 = Date.now() + skew; return [t1 - windowMin * 60000, t1]; };

function draw() {
  const [t0, t1] = view();
  src = currentSource();
  const s = { ...src, rows: src.rows.slice(Math.max(0, lowerBound(src, t0) - 1)) };
  for (const ch of charts) ch.svg.innerHTML = ch.c.strips ? drawStrips(ch, s, t0, t1) : drawLines(ch, s, t0, t1);
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
  if (px < ML || px > r.width - MR || !src || !src.rows.length) {
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
