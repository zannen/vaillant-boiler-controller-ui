import collections
import threading

from .util import epoch_ms


class CommandLog:
    """Every SetMode instruction sent to the boiler: saved in the commands
    table and the last KEEP_DAYS kept in memory for the chart, as [epoch ms,
    "on"|"off", 1 if accepted else 0, source, flow temperature, hot water
    temperature]. source is "schedule", or "override:<user>" for the override
    button. The temperatures are None in rows logged before they were
    recorded."""

    COLUMNS = (
        "ms",
        "state",
        "ok",
        "result",
        "source",
        "flow_temp",
        "hwc_temp",
    )
    KEEP_DAYS = 31

    def __init__(self, hub, writer):
        self.hub, self.writer = hub, writer
        self.lock = threading.Lock()
        self.rows = collections.deque(maxlen=60000)

    def load(self, conn):
        """Fill the in-memory rows from the database; returns how many."""
        cursor = conn.execute(
            "SELECT ms, state, ok, source, flow_temp, hwc_temp FROM commands "
            "WHERE ms >= ? ORDER BY ms",
            (epoch_ms() - self.KEEP_DAYS * 86400000,),
        )
        rows = [list(r) for r in cursor]
        with self.lock:
            self.rows.extend(rows)
        return len(rows)

    def add(self, state, ok, result, source, temps):
        """Log one send; temps is (flow, hot water) in °C."""
        ms = epoch_ms()
        self.writer.insert(
            "commands",
            self.COLUMNS,
            (ms, state, int(ok), result, source, *temps),
        )
        row = [ms, state, int(ok), source, *temps]
        with self.lock:
            self.rows.append(row)
            cutoff = ms - self.KEEP_DAYS * 86400000
            while self.rows and self.rows[0][0] < cutoff:
                self.rows.popleft()
        self.hub.broadcast({"type": "command", "row": row})

    def get(self):
        with self.lock:
            return list(self.rows)
