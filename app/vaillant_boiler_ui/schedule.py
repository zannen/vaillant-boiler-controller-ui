import datetime
import json
import os
import sys
import threading

from .schedule_rules import DAY_KEYS, parse_hhmm, validate_schedule
from .util import stamp


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
