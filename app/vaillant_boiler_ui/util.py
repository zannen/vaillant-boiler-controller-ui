"""Timestamps and logging."""

import datetime
import sys
import time


def stamp():
    return datetime.datetime.now().isoformat(timespec="milliseconds")


def log(msg):
    """Print a timestamped message to stderr."""
    print(f"{datetime.datetime.now():%H:%M:%S} {msg}", file=sys.stderr)


def epoch_ms():
    return int(time.time() * 1000)
