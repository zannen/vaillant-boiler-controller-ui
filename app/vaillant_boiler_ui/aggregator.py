import collections

from .constants import SAMPLE_KEYS


class Aggregator:
    """Summarises chart samples into fixed time buckets: [start ms, mean, min,
    max of each SAMPLE_KEYS value]."""

    def __init__(self, name, bucket_ms, keep_minutes):
        self.name, self.bucket_ms, self.keep_minutes = (
            name,
            bucket_ms,
            keep_minutes,
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

    def reload(self, rows):
        """Replace the finished rows with these (sorted, e.g. from the
        database), keeping any held rows that are newer."""
        last = rows[-1][0] if rows else None
        newer = [r for r in self.rows if last is None or r[0] > last]
        self.rows.clear()
        for row in rows + newer:
            if not self.rows or row[0] > self.rows[-1][0]:
                self.append(row)
