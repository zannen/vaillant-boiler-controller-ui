"""The loop that reads the boiler values from ebusd."""

import time

from .constants import VALUES
from .ebusd import Ebusd
from .samples import number
from .util import epoch_ms, log, stamp


def poll(hub, writer, host, port):
    names = list(VALUES)
    last = {n: "" for n in names}
    last_read = {n: 0.0 for n in names}
    # (monotonic time, FlowTemp) of the previous FlowTemp read
    prev_flow = None
    rise = None  # FlowTemp change in degrees per second
    bus = None

    def read(n):
        nonlocal prev_flow, rise
        raw = bus.read(n)
        now = time.monotonic()
        last[n] = raw
        last_read[n] = now
        hub.record(n, raw, stamp())
        if n == "FlowTemp":
            t = number(raw)
            if t is not None:
                if prev_flow and now - prev_flow[0] > 0.2:
                    rise = (t - prev_flow[1]) / (now - prev_flow[0])
                prev_flow = (now, t)

    while True:
        try:
            bus = bus or Ebusd(host, port)
            flame_before = last["Flame"]
            for n in names:
                if time.monotonic() - last_read[n] >= VALUES[n]:
                    read(n)
            # flame went out: refresh the fault counters straight away
            if flame_before == "on" and last["Flame"] != "on":
                for n in names:
                    if VALUES[n] >= 30:
                        read(n)
            now = stamp()
            writer.insert(
                "samples",
                ["ms", *names],
                [epoch_ms()] + [last[n] or None for n in names],
            )
            hub.publish(now, rise, True)
        except (OSError, ConnectionError) as e:
            log(f"ebusd error: {e}; retrying")
            hub.publish(stamp(), rise, False)
            bus = None
            time.sleep(5)
