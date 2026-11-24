"""Heating schedule days, times and validation."""

DAY_KEYS = [
    "mon",
    "tue",
    "wed",
    "thu",
    "fri",
    "sat",
    "sun",
]  # datetime.weekday() order
MAX_PERIODS = 10  # on/off pairs per day


def parse_hhmm(text, allow_2400=False):
    """Minutes since midnight for "HH:MM", or None. "24:00" (end of day) only
    if allowed."""
    if not isinstance(text, str) or len(text) != 5 or text[2] != ":":
        return None
    try:
        h, m = int(text[:2]), int(text[3:])
    except ValueError:
        return None
    if allow_2400 and (h, m) == (24, 0):
        return 1440
    return h * 60 + m if 0 <= h < 24 and 0 <= m < 60 else None


def hhmm(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def validate_schedule(obj):
    """Checked, normalised copy of a schedule from the web page, or (None,
    error message)."""
    if (
        not isinstance(obj, dict)
        or not isinstance(obj.get("enabled"), bool)
        or not isinstance(obj.get("days"), dict)
    ):
        return (
            None,
            'expected {"enabled": true|false, '
            '"days": {"mon": [["06:30", "08:30"], ...], ...}}',
        )
    unknown = set(obj["days"]) - set(DAY_KEYS)
    if unknown:
        return None, f"unknown day(s): {', '.join(sorted(unknown))}"
    days = {}
    for day in DAY_KEYS:
        periods = obj["days"].get(day, [])
        if not isinstance(periods, list) or len(periods) > MAX_PERIODS:
            return None, f"{day}: at most {MAX_PERIODS} on/off periods"
        spans = []
        for p in periods:
            if not (isinstance(p, list) and len(p) == 2):
                return None, f"{day}: each period must be [on, off]"
            on, off = parse_hhmm(p[0]), parse_hhmm(p[1], allow_2400=True)
            if on is None or off is None:
                return (
                    None,
                    f"{day}: times must be HH:MM (off may be 24:00), "
                    f"got {p[0]!r}-{p[1]!r}",
                )
            if on >= off:
                return (
                    None,
                    f"{day}: {p[0]}-{p[1]}: on must be before off "
                    "(use 24:00 for midnight)",
                )
            spans.append((on, off))
        spans.sort()
        for (a0, a1), (b0, b1) in zip(spans, spans[1:]):
            if b0 < a1:
                return (
                    None,
                    f"{day}: {hhmm(a0)}-{hhmm(a1)} "
                    f"overlaps {hhmm(b0)}-{hhmm(b1)}",
                )
        days[day] = [[hhmm(a), hhmm(b)] for a, b in spans]
    return {"enabled": obj["enabled"], "days": days}, None
