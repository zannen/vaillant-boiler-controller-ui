import collections
import datetime

from .constants import SAMPLE_KEYS


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
