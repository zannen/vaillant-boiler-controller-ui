import datetime
import json
import sys
import threading

from .db import transaction
from .schedule_rules import (
    DAY_KEYS,
    TEMP_LIMITS,
    parse_hhmm,
    validate_schedule,
)
from .util import stamp


class Schedule:
    """7-day heating schedule, and the heating flow and hot water
    temperatures sent with it, kept in the schedule and schedule_periods
    tables.

    temps holds the temperatures used until a schedule is saved:
    {"flowTemp": 75.0, "hwcTemp": 52.5}."""

    def __init__(self, conn, temps):
        self.conn = conn
        temps = {k: float(v) for k, v in temps.items()}
        self.lock = threading.Lock()
        self.data = {
            "enabled": False,
            "days": {d: [] for d in DAY_KEYS},
            **temps,
            "updatedBy": None,
            "updatedAt": None,
        }
        saved = self.read(conn)
        if saved:
            checked, err = validate_schedule(saved)
            if err:
                sys.exit(f"the heating schedule in the database is bad: {err}")
            self.data = {
                **temps,
                **checked,
                "updatedBy": saved["updatedBy"],
                "updatedAt": saved["updatedAt"],
            }

    @staticmethod
    def read(conn):
        """The saved schedule, unchecked, or None if there is none."""
        row = conn.execute(
            "SELECT enabled, flow_temp, hwc_temp, updated_by, updated_at "
            "FROM schedule"
        ).fetchone()
        if row is None:
            return None
        days = {d: [] for d in DAY_KEYS}
        for day, start, stop in conn.execute(
            "SELECT day, start, stop FROM schedule_periods ORDER BY start"
        ):
            days.setdefault(day, []).append([start, stop])
        return {
            "enabled": bool(row[0]),
            "days": days,
            "flowTemp": row[1],
            "hwcTemp": row[2],
            "updatedBy": row[3],
            "updatedAt": row[4],
        }

    @staticmethod
    def write(conn, data):
        """Save a checked schedule with its temperatures; the caller holds a
        transaction."""
        conn.execute(
            "INSERT OR REPLACE INTO schedule (id, enabled, flow_temp, "
            "hwc_temp, updated_by, updated_at) VALUES (1, ?, ?, ?, ?, ?)",
            (
                int(data["enabled"]),
                data["flowTemp"],
                data["hwcTemp"],
                data.get("updatedBy"),
                data.get("updatedAt"),
            ),
        )
        conn.execute("DELETE FROM schedule_periods")
        conn.executemany(
            "INSERT INTO schedule_periods (day, start, stop) VALUES (?, ?, ?)",
            [
                (day, start, stop)
                for day, periods in data["days"].items()
                for start, stop in periods
            ],
        )

    def get(self):
        with self.lock:
            return json.loads(json.dumps(self.data))

    def set(self, checked, user):
        """Save a checked schedule; temperatures it leaves out are kept."""
        with self.lock:
            temps = {k: self.data[k] for k in TEMP_LIMITS}
        data = {**temps, **checked, "updatedBy": user, "updatedAt": stamp()}
        with self.lock:
            with transaction(self.conn):
                self.write(self.conn, data)
            self.data = data
        return self.get()

    def temps(self):
        """(heating flow, hot water) temperatures to send, °C."""
        with self.lock:
            return self.data["flowTemp"], self.data["hwcTemp"]

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
