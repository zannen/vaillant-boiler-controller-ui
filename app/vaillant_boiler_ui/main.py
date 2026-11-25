"""Poll ebusd for Vaillant BAI values, append them to a CSV file and serve a
live web UI.

Usage: vaillant-boiler-ui.py [CSV_PATH] --users HTPASSWD --roles ROLES
           [--port 8000] [--changes PATH] [--ebusd HOST:PORT]
           [--history-minutes 60] [--flush-seconds 30] [--secret-file PATH]
           [--token-hours 12] [--session-days 30]
Then open http://<pi-address>:8000/ in a browser on the local network (add
?minutes=N to choose the chart window, e.g.
http://<pi-address>:8000/?minutes=5).

Users: an htpasswd file with bcrypt hashes (htpasswd -B), plus a roles file in
Apache group-file format, one line per role (a user in several roles gets the
highest; a user in none cannot log in):
    admin: alice
    user: bob carol
    viewer: guest
Needs the bcrypt module (Raspberry Pi OS: sudo apt install python3-bcrypt).
"""

import argparse
import os
import signal
import sys
import time

from .aggregator import Aggregator
from .auth import Auth
from .command_log import CommandLog
from .constants import TIERS, VALUES
from .csv_files import logger_row_ok, repair_csv, summary_row_ok, needs_header
from .file_writer import FileWriter
from .heating_controller import HeatingController
from .history import backfill, load_agg, load_changes, load_history
from .hub import Hub
from .poller import poll
from .schedule import Schedule
from .schedule_rules import check_temp
from .server import serve
from .static_files import STATIC_DIR, load_static


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "csv",
        nargs="?",
        default=time.strftime("boiler-%Y%m%d-%H%M%S.csv"),
        help="CSV file to append to",
    )
    ap.add_argument(
        "--changes",
        help="change log file (default: CSV path with .changes.log)",
    )
    ap.add_argument(
        "--bind",
        default="0.0.0.0",
        help="web UI address (default: all interfaces)",
    )
    ap.add_argument(
        "--port", type=int, default=8000, help="web UI port (default: 8000)"
    )
    ap.add_argument(
        "--ebusd",
        default="127.0.0.1:8888",
        help="ebusd command port (default: 127.0.0.1:8888)",
    )
    ap.add_argument(
        "--history-minutes",
        type=int,
        default=60,
        help="chart history kept in memory, i.e. the longest chart window "
        "(default: 60)",
    )
    ap.add_argument(
        "--flush-seconds",
        type=float,
        default=30,
        help="how often to write the CSV and change log to disk (default: 30)",
    )
    ap.add_argument(
        "--users",
        required=True,
        help="htpasswd file with bcrypt passwords (htpasswd -B)",
    )
    ap.add_argument(
        "--roles",
        required=True,
        help="roles file: lines of 'admin|user|viewer: name name ...'",
    )
    ap.add_argument(
        "--secret-file",
        default="boiler-ui.secret",
        help="token signing key, created if missing "
        "(default: ./boiler-ui.secret)",
    )
    ap.add_argument(
        "--token-hours",
        type=float,
        default=12,
        help="token lifetime (default: 12)",
    )
    ap.add_argument(
        "--session-days",
        type=float,
        default=30,
        help="longest a login can be kept alive by refreshing (default: 30)",
    )
    ap.add_argument(
        "--schedule",
        help="heating schedule file (default: schedule.json next to the CSV)",
    )
    ap.add_argument(
        "--flow-temp",
        type=float,
        default=75,
        help="heating flow temperature sent with each SetMode until an admin "
        "sets one on the web page, °C (default: 75)",
    )
    ap.add_argument(
        "--hwc-temp",
        type=float,
        default=52.5,
        help="hot water temperature sent with each SetMode until an admin "
        "sets one on the web page, °C (default: 52.5)",
    )
    ap.add_argument(
        "--static-dir",
        default=STATIC_DIR,
        help="directory with the web UI's HTML, JS and CSS files "
        "(default: static next to this program)",
    )
    args = ap.parse_args()
    for option, field, value in (
        ("--flow-temp", "flowTemp", args.flow_temp),
        ("--hwc-temp", "hwcTemp", args.hwc_temp),
    ):
        err = check_temp(field, value)
        if err:
            ap.error(f"{option}: {err}")
    static = load_static(args.static_dir)
    changes = args.changes or os.path.splitext(args.csv)[0] + ".changes.log"
    host, _, port = args.ebusd.rpartition(":")
    auth = Auth(
        args.users,
        args.roles,
        args.secret_file,
        int(args.token_hours * 3600),
        int(args.session_days * 86400),
    )

    writer = FileWriter(args.csv, changes, args.flush_seconds)
    base = os.path.splitext(args.csv)[0]
    aggs = [
        Aggregator(name, ms, keep, f"{base}.{name}.csv")
        for name, ms, keep in TIERS
    ]
    for path, header, row_ok in (
        [(args.csv, ["time"] + list(VALUES), logger_row_ok)]
        + [(a.path, a.header(), summary_row_ok) for a in aggs]
        + [(f"{base}.commands.csv", CommandLog.HEADER, summary_row_ok)]
    ):
        t0 = time.monotonic()
        result = repair_csv(path, header, row_ok)
        if result:
            print(f"{result} ({time.monotonic() - t0:.0f} s)", file=sys.stderr)
    hub = Hub(writer, args.history_minutes, aggs)
    print(
        f"loaded {load_history(hub, args.csv)} chart samples from {args.csv} "
        f"and {load_changes(hub, changes)} changes from {changes}",
        file=sys.stderr,
    )
    missing = []
    for a in aggs:
        n = load_agg(a)
        if n is None:
            missing.append(a)
        else:
            print(
                f"loaded {n} {a.name} summaries from {a.path}", file=sys.stderr
            )
        if needs_header(a.path):
            writer.csv_row(a.header(), a.path)
    # first run with summaries: build them from the existing CSV (one-off, may
    # take a while)
    if missing:
        print(
            f"building {', '.join(a.path for a in missing)} "
            f"from {args.csv}...",
            file=sys.stderr,
        )
        t0 = time.monotonic()
        n = backfill(missing, args.csv, writer)
        print(
            f"read {n} samples in {time.monotonic() - t0:.0f} s; "
            + ", ".join(f"{len(a.rows)} {a.name} summaries" for a in missing),
            file=sys.stderr,
        )
    schedule = Schedule(
        args.schedule
        or os.path.join(
            os.path.dirname(os.path.abspath(args.csv)), "schedule.json"
        ),
        {"flowTemp": args.flow_temp, "hwcTemp": args.hwc_temp},
    )
    commands = CommandLog(f"{base}.commands.csv", hub, writer)
    print(
        f"loaded {commands.load()} boiler instructions from {commands.path}",
        file=sys.stderr,
    )
    controller = HeatingController(schedule, hub, commands, host, int(port))
    writer.start()
    controller.start()
    serve(hub, auth, controller, static, args.bind, args.port)
    print(
        f"web UI on port {args.port}; logging to {args.csv} and {changes} "
        f"every {args.flush_seconds:g} s",
        file=sys.stderr,
    )

    def on_sigterm(
        signum, frame
    ):  # systemctl stop/restart: write out what's queued first
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, on_sigterm)
    try:
        poll(hub, writer, host, int(port))
    except KeyboardInterrupt:
        pass
    finally:
        writer.stop()
