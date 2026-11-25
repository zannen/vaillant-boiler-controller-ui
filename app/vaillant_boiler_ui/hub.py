import collections
import queue
import threading

from .constants import CHANGE_HISTORY, SAMPLE_KEYS, SUMMARY_COLUMNS
from .samples import display, make_sample, significant
from .util import epoch_ms


class Hub:
    """Latest values, chart history and recent changes, shared between the
    poller and the web clients."""

    CHANGE_COLUMNS = ("ms", "time", "name", "old", "new")
    SUMMARY_COLUMNS = ("tier", "ms", *SUMMARY_COLUMNS)

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
        self.writer.insert(
            "changes",
            self.CHANGE_COLUMNS,
            (epoch_ms(), when, name, entry["old"], entry["new"]),
        )

    def reload(self, history, summaries):
        """Replace the chart history and summaries with these, loaded from
        the database (e.g. once old CSV data has been imported), keeping held
        rows newer than them, and send everyone a fresh snapshot.
        summaries: tier name -> rows."""
        with self.lock:
            last = history[-1][0] if history else None
            newer = [r for r in self.history if last is None or r[0] > last]
            self.history.clear()
            self.history.extend(history + newer)
            for a in self.aggs:
                a.reload(summaries.get(a.name, []))
            self._broadcast(self.snapshot())

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
                        self.writer.insert(
                            "summaries", self.SUMMARY_COLUMNS, (a.name, *row)
                        )
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
