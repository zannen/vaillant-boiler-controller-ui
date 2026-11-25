import collections
import queue
import sqlite3
import threading

from .db import connect, quote, transaction
from .util import log


class DbWriter(threading.Thread):
    """Inserts rows into the database in batches from its own thread, one
    transaction per batch, so a stalled SD card holds up only this thread,
    never the polling or the web UI."""

    # rows held while writes keep failing; the oldest are dropped beyond this
    MAX_PENDING = 200_000

    def __init__(self, path, interval):
        super().__init__(daemon=True)
        self.path, self.interval = path, interval
        self.q = queue.Queue()
        self.pending = collections.deque(maxlen=self.MAX_PENDING)
        self.stopping = threading.Event()
        self.conn = None

    def insert(self, table, cols, row):
        """Queue a row for the table; cols names its values. Rows that would
        duplicate a primary key are skipped."""
        self.q.put((table, tuple(cols), tuple(row)))

    def run(self):
        self.conn = connect(self.path)
        while not self.stopping.wait(self.interval):
            self.flush()
        self.flush()

    def stop(self):
        """Write out everything queued so far (waits up to 30 s for a slow
        card)."""
        self.stopping.set()
        self.join(timeout=30)

    def flush(self):
        while True:
            try:
                self.pending.append(self.q.get_nowait())
            except queue.Empty:
                break
        if not self.pending:
            return
        groups = collections.defaultdict(list)  # (table, cols) -> rows
        for table, cols, row in self.pending:
            groups[table, cols].append(row)
        try:
            with transaction(self.conn):
                for (table, cols), rows in groups.items():
                    self.conn.executemany(
                        f"INSERT OR IGNORE INTO {quote(table)} "
                        f"({', '.join(quote(c) for c in cols)}) "
                        f"VALUES ({', '.join('?' * len(cols))})",
                        rows,
                    )
            self.pending.clear()
        except sqlite3.Error as e:  # keep the rows and try again next time
            log(f"cannot write to {self.path}: {e}")
