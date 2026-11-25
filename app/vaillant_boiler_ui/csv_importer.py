import collections
import json
import os
import threading
import time

from .aggregator import Aggregator
from .constants import SUMMARY_COLUMNS, TIERS
from .csv_import import import_key, parse_ms, records, sample_columns
from .db import connect, ensure_columns, get_meta, set_meta, transaction
from .history import flow_rise, load_history, load_summaries
from .samples import make_sample
from .util import epoch_ms, log


class CsvImporter(threading.Thread):
    """Imports the logger CSV of older versions into the samples table in the
    background, while the program runs. Works in batches of CHUNK rows, each
    one transaction that also records how far it has got, so a restart
    carries on where it stopped. Builds the summaries of tiers that had no
    summary file. When done, reloads the hub's chart history and summaries
    from the database."""

    CHUNK = 5000
    PAUSE = 0.05  # seconds between batches, to leave the Pi time to poll

    def __init__(self, db_path, csv_path, hub):
        super().__init__(daemon=True)
        self.db_path, self.csv_path, self.hub = db_path, csv_path, hub
        self.backfill = []  # aggregators for tiers that had no summary file
        self.cutoff = 0  # older samples are too old for those summaries
        self.prev_flow = None

    def run(self):
        try:
            self.work()
        except Exception as e:  # pylint: disable=broad-exception-caught
            log(f"import of {self.csv_path} stopped: {e!r}; restart to retry")

    def work(self):
        conn = connect(self.db_path)
        key = import_key(self.csv_path)
        state = json.loads(get_meta(conn, key))
        size = os.path.getsize(self.csv_path)
        log(
            f"importing {self.csv_path} in the background (once only), "
            f"from {state['offset'] * 100 // max(size, 1)}%"
        )
        self.backfill = [
            Aggregator(name, ms, keep)
            for name, ms, keep in TIERS
            if name in state["backfill"]
        ]
        self.cutoff = epoch_ms() - 60000 * max(
            (a.keep_minutes for a in self.backfill), default=0
        )
        count, logged = 0, time.monotonic()
        batch = collections.defaultdict(list)  # column names -> rows
        summaries = []
        with open(self.csv_path, "rb") as f:
            f.seek(state["offset"])
            for header, row, offset in records(
                f, state["offset"], state["header"]
            ):
                ms = parse_ms(row[0])
                if ms is None:
                    continue
                cols = sample_columns(header)
                batch[tuple(n for _, n in cols)].append(
                    [ms] + [row[i] or None for i, _ in cols]
                )
                summaries += self.summarise(ms, dict(zip(header, row)))
                count += 1
                if count % self.CHUNK == 0:
                    state.update(offset=offset, header=header)
                    self.save(conn, key, state, batch, summaries)
                    batch.clear()
                    summaries.clear()
                    time.sleep(self.PAUSE)
                    if time.monotonic() - logged > 60:
                        logged = time.monotonic()
                        log(
                            f"importing {self.csv_path}: "
                            f"{offset * 100 // max(size, 1)}%"
                        )
        state.update(offset=size, done=True)
        self.save(conn, key, state, batch, summaries)
        log(
            f"imported {count} rows from {self.csv_path}; "
            "the old CSV files can now be deleted"
        )
        self.hub.reload(
            load_history(conn, self.hub.history_minutes),
            {a.name: load_summaries(conn, a) for a in self.hub.aggs},
        )
        conn.close()

    def summarise(self, ms, values):
        """Feed a sample to the backfill aggregators; returns summaries rows
        (tier, *bucket row) for the buckets it closes."""
        if not self.backfill or ms < self.cutoff:
            return []
        rise, self.prev_flow = flow_rise(self.prev_flow, ms, values)
        sample = make_sample(ms, values, rise)
        done = [(a, a.add(sample)) for a in self.backfill]
        return [(a.name, *row) for a, row in done if row]

    @staticmethod
    def save(conn, key, state, batch, summaries):
        with transaction(conn):
            for names, rows in batch.items():
                ensure_columns(conn, "samples", names, "TEXT")
                cols = ", ".join(f'"{n}"' for n in ("ms", *names))
                marks = ", ".join("?" * (len(names) + 1))
                conn.executemany(
                    f"INSERT INTO samples ({cols}) VALUES ({marks})", rows
                )
            if summaries:
                cols = ", ".join(
                    f'"{c}"' for c in ("tier", "ms", *SUMMARY_COLUMNS)
                )
                marks = ", ".join("?" * (len(SUMMARY_COLUMNS) + 2))
                conn.executemany(
                    f"INSERT OR IGNORE INTO summaries ({cols}) "
                    f"VALUES ({marks})",
                    summaries,
                )
            set_meta(conn, key, json.dumps(state))
