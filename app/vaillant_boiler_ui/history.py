"""Loading chart history, summaries and changes from the database."""

from .constants import CHANGE_HISTORY, GAP_MS, SUMMARY_COLUMNS, VALUES
from .db import columns, quote
from .samples import make_sample, number
from .util import epoch_ms


def flow_rise(prev_flow, ms, values):
    """FlowTemp rise in degrees per second since the previous sample, or None,
    and the (epoch ms, FlowTemp) to pass in with the next sample."""
    rise, flow = None, number(values.get("FlowTemp"))
    if flow is not None:
        if prev_flow and 200 < ms - prev_flow[0] < GAP_MS:
            rise = (flow - prev_flow[1]) / ((ms - prev_flow[0]) / 1000)
        prev_flow = (ms, flow)
    return rise, prev_flow


def chart_samples(rows):
    """Chart samples from (epoch ms, {value name: raw value}) pairs in time
    order, with the FlowTemp rise worked out between neighbouring ones."""
    prev_flow = None  # (epoch ms, FlowTemp)
    for ms, values in rows:
        rise, prev_flow = flow_rise(prev_flow, ms, values)
        yield make_sample(ms, values, rise)


def load_history(conn, minutes):
    """Chart samples of the last minutes, oldest first."""
    names = [n for n in VALUES if n in columns(conn, "samples")]
    cursor = conn.execute(
        f"SELECT ms, {', '.join(quote(n) for n in names)} FROM samples "
        "WHERE ms >= ? ORDER BY ms",
        (epoch_ms() - minutes * 60000,),
    )
    return list(chart_samples((r[0], dict(zip(names, r[1:]))) for r in cursor))


def load_summaries(conn, agg):
    """An aggregator's rows for the time it keeps, oldest first."""
    cursor = conn.execute(
        f"SELECT ms, {', '.join(quote(c) for c in SUMMARY_COLUMNS)} "
        "FROM summaries WHERE tier = ? AND ms >= ? ORDER BY ms",
        (agg.name, epoch_ms() - agg.keep_minutes * 60000),
    )
    return [list(r) for r in cursor]


def load_changes(conn):
    """The last CHANGE_HISTORY changes, oldest first."""
    cursor = conn.execute(
        "SELECT time, name, old, new FROM changes "
        "ORDER BY ms DESC, id DESC LIMIT ?",
        (CHANGE_HISTORY,),
    )
    rows = [
        {"time": t, "name": name, "old": old, "new": new}
        for t, name, old, new in cursor
    ]
    rows.reverse()
    return rows
