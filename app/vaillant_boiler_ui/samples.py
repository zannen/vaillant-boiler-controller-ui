"""Parsing ebusd values into numbers, change-list text and chart samples."""

import math

from .constants import SAMPLE_KEYS, SWITCHES, THRESHOLDS


def number(raw):
    """First field of an ebusd value as a float, e.g. "51.06;ok" -> 51.06."""
    try:
        f = float(raw.split(";")[0])
    except (ValueError, AttributeError):
        return None
    return f if math.isfinite(f) else None


def display(name, raw):
    return raw.split(";")[0] if name in THRESHOLDS else raw


def significant(name, old, new):
    if old == new:
        return False
    step = THRESHOLDS.get(name)
    a, b = number(old), number(new)
    if step is None or a is None or b is None:
        return True
    return abs(b - a) >= step - 1e-9


def make_sample(ts, values, rise):
    """Chart row [epoch ms, *SAMPLE_KEYS values] from raw ebusd values."""
    row = [ts]
    for k in SAMPLE_KEYS:
        if k == "rise":
            row.append(None if rise is None else round(rise, 3))
        elif k in SWITCHES:
            raw = values.get(k)
            row.append(None if raw is None else int(raw in ("on", "yes")))
        else:
            row.append(number(values.get(k)))
    return row
