import datetime
import threading

from .ebusd import Ebusd
from .util import log, stamp


class HeatingController(threading.Thread):
    """Applies the schedule by telling the boiler, over eBUS, to block or allow
    central heating.

    The block only lasts about 15 minutes after the last SetMode message, so
    the current state is re-sent on every 10-minute boundary of the clock
    (xx:00, xx:10, ...). It is also sent at once on startup and when the
    schedule is saved, and at the exact minute of each schedule on/off time
    (e.g. 07:31), then again on the following boundaries. A failed send is
    retried after RETRY_SECONDS. When the schedule is disabled, heating is
    allowed once, and then the boiler goes back to its own controls. The
    boiler's own timer still applies: the Pi can stop heating, but cannot start
    it while that timer is off.

    Override: one "heating on" is sent at once, whatever the schedule says,
    then nothing for OVERRIDE_SECONDS (the boiler holds it for about 15
    minutes); then the schedule state is sent and the normal 10-minute
    boundaries resume.

    Each SetMode carries the heating flow and hot water temperatures saved
    with the schedule. When they change, the current state (or the override's
    "heating on") is sent again at once with the new temperatures."""

    RESEND_SECONDS = 600  # boundary spacing; must divide an hour
    RETRY_SECONDS = 60
    # re-check at least this often, in case the clock is stepped
    MAX_SLEEP = 60
    # how long the boiler keeps a SetMode block without a repeat
    HOLD_SECONDS = 900
    OVERRIDE_SECONDS = 840

    def __init__(self, schedule, hub, commands, host, port):
        super().__init__(daemon=True)
        self.schedule, self.hub, self.commands, self.host, self.port = (
            schedule,
            hub,
            commands,
            host,
            port,
        )
        # (flow, hot water) temperatures last confirmed by ebusd, or None
        self.sent_temps = None
        self.wake = threading.Event()
        self.sent = None  # "on"/"off" last confirmed by ebusd, or None
        # local time of the next regular send (boundary or schedule change)
        self.due = None
        self.retry_at = None  # local time to retry after a failed send
        # {"time", "state", "ok", "result"} of the last attempt
        self.last = None
        # the same, for the last attempt the boiler accepted
        self.last_ok = None
        # {"until": local time, "by": user, "sent": bool} while overriding
        self.override = None
        self.requests = []  # override requests from the web, handled by step()
        self.lock = threading.Lock()

    def request_override(self, user):
        with self.lock:
            self.requests.append(user)
        self.wake.set()

    @staticmethod
    def setmode(off, flow_temp, hwc_temp):
        return f"auto;{flow_temp:g};{hwc_temp:g};-;{int(off)};0;0;0;0;0"

    def temps_changed(self):
        """True if the saved temperatures differ from those last sent."""
        return self.schedule.temps() != self.sent_temps

    def send(self, state, source="schedule"):
        temps = self.schedule.temps()
        try:
            bus = Ebusd(self.host, self.port)
            try:
                result = bus.write(
                    "SetMode", self.setmode(state == "off", *temps)
                )
            finally:
                bus.close()
            ok = not result.startswith("ERR")
        except (OSError, ConnectionError) as e:
            result, ok = f"ERR: {e}", False
        self.last = {
            "time": stamp(),
            "state": state,
            "ok": ok,
            "result": result,
            "source": source,
            "flowTemp": temps[0],
            "hwcTemp": temps[1],
        }
        self.commands.add(state, ok, result, source, temps)
        if ok:
            self.last_ok = self.last
            if state != self.sent:
                self.hub.record("HeatingSchedule", state, stamp())
            self.hub.record("SetModeFlowTemp", f"{temps[0]:g}", stamp())
            self.hub.record("SetModeHwcTemp", f"{temps[1]:g}", stamp())
            self.sent, self.sent_temps = state, temps
        else:
            log(f"SetMode heating {state} failed: {result}")
        return ok

    def next_slot(self, now):
        """The next 10-minute boundary after now, or the next schedule change
        if that comes first."""
        step = self.RESEND_SECONDS
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        secs = (now - midnight).total_seconds()
        boundary = midnight + datetime.timedelta(
            seconds=(secs // step + 1) * step
        )
        change = self.schedule.next_change(now)
        return min(boundary, change) if change else boundary

    def step(self, now=None):
        now = now or datetime.datetime.now()
        with self.lock:
            requests, self.requests = self.requests, []
        if requests:  # a new press restarts the override
            until = now + datetime.timedelta(seconds=self.OVERRIDE_SECONDS)
            self.override = {"until": until, "by": requests[-1], "sent": False}
            self.retry_at = None
            self.hub.record(
                "HeatingOverride",
                f"on until {until:%H:%M:%S} ({requests[-1]})",
                stamp(),
            )
        if self.override and now >= self.override["until"]:
            self.override = None
            # back to the schedule: send its state now
            self.due = self.retry_at = None
            self.hub.record("HeatingOverride", "ended", stamp())
        wanted = self.schedule.wanted(now)
        target = None if wanted is None else ("on" if wanted else "off")
        if self.override:
            o = self.override
            if (not o["sent"] or self.temps_changed()) and (
                self.retry_at is None or now >= self.retry_at
            ):
                if self.send("on", f"override:{o['by']}"):
                    o["sent"], self.retry_at = True, None
                else:
                    self.retry_at = min(
                        now + datetime.timedelta(seconds=self.RETRY_SECONDS),
                        o["until"],
                    )
            self.due = o["until"]
        elif (
            target is None
        ):  # schedule disabled: if heating is blocked, release it once
            self.due = None
            if self.sent == "off" and (
                self.retry_at is None or now >= self.retry_at
            ):
                if self.send("on"):
                    self.sent, self.retry_at = None, None
                else:
                    self.retry_at = now + datetime.timedelta(
                        seconds=self.RETRY_SECONDS
                    )
            elif self.sent != "off":
                self.retry_at = None
        else:
            failed = self.retry_at is not None
            need = (
                self.due is None
                or now >= self.due
                or (failed and now >= self.retry_at)
                or (not failed and target != self.sent)
                or (not failed and self.temps_changed())
            )  # e.g. the schedule was just saved
            if need:
                ok = self.send(target)
                self.retry_at = (
                    None
                    if ok
                    else now + datetime.timedelta(seconds=self.RETRY_SECONDS)
                )
            self.due = self.next_slot(
                now
            )  # recomputed each time, so schedule edits take effect
        nxt = self.schedule.next_change(now)
        nxt_send = (
            min(t for t in (self.due, self.retry_at) if t)
            if (self.due or self.retry_at)
            else None
        )
        o = self.override
        if (
            o
            and nxt_send == o["until"]
            and wanted is None
            and self.sent != "off"
        ):
            # override ending with the schedule disabled: nothing more is sent
            nxt_send = None
        if nxt_send is None:
            nxt_state = None
        elif nxt_send == self.retry_at:
            nxt_state = "on" if o else target
        else:
            nxt_state = "on" if self.schedule.wanted(nxt_send) else "off"
        self.hub.set_control(
            {
                "enabled": wanted is not None,
                "target": target,
                "nextChange": (
                    nxt.isoformat(timespec="minutes") if nxt else None
                ),
                "nextSend": (
                    nxt_send.isoformat(timespec="seconds")
                    if nxt_send
                    else None
                ),
                "nextState": nxt_state,
                "override": (
                    None
                    if not o
                    else {
                        "until": o["until"].isoformat(timespec="seconds"),
                        "by": o["by"],
                        "sent": o["sent"],
                    }
                ),
                "lastSend": self.last,
                "lastOk": self.last_ok,
                "resendSeconds": self.RESEND_SECONDS,
            }
        )

    def sleep_seconds(self, now):
        times = [t for t in (self.due, self.retry_at) if t]
        if not times:
            return self.MAX_SLEEP
        # wake just after the due time; if woken early, the next step sees it
        # isn't due and waits again
        return max(
            0.01,
            min(self.MAX_SLEEP, (min(times) - now).total_seconds() + 0.02),
        )

    def run(self):
        while True:
            try:
                self.step()
            except Exception as e:  # never let the controller die quietly
                log(f"heating controller error: {e!r}")
            self.wake.wait(self.sleep_seconds(datetime.datetime.now()))
            self.wake.clear()
