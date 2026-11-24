import csv
import io
import queue
import threading

from .util import log


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
