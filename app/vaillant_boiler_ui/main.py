"""Poll ebusd for Vaillant BAI values, save them in an SQLite database and
serve a live web UI.

Usage: vaillant-boiler-ui.py [DB_PATH] --users HTPASSWD --roles ROLES
           [--port 8000] [--ebusd HOST:PORT]
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

Data from older versions (a logger CSV with its .changes.log, .1min.csv,
.15min.csv and .commands.csv files, and schedule.json) is imported once,
the logger CSV in the background while the program runs.
"""

import argparse
import os
import signal
import sqlite3
import sys

from .aggregator import Aggregator
from .auth import Auth
from .command_log import CommandLog
from .constants import TIERS
from .csv_import import import_old_files
from .csv_importer import CsvImporter
from .db import open_database
from .db_writer import DbWriter
from .heating_controller import HeatingController
from .history import load_changes, load_history, load_summaries
from .hub import Hub
from .poller import poll
from .schedule import Schedule
from .schedule_rules import check_temp
from .server import serve
from .static_files import STATIC_DIR, load_static


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "db",
        nargs="?",
        default="boiler.db",
        help="SQLite database for the logged data and the heating schedule, "
        "created if missing (default: boiler.db)",
    )
    ap.add_argument(
        "--import-csv",
        help="logger CSV of an older version to import once, with its "
        ".1min.csv, .15min.csv and .commands.csv files "
        "(default: the database path with .csv)",
    )
    ap.add_argument(
        "--changes",
        help="change log of an older version to import once "
        "(default: the import CSV path with .changes.log)",
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
        help="how often to write to the database (default: 30)",
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
        help="schedule.json of an older version to import once "
        "(default: schedule.json next to the database)",
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
    host, _, port = args.ebusd.rpartition(":")
    auth = Auth(
        args.users,
        args.roles,
        args.secret_file,
        int(args.token_hours * 3600),
        int(args.session_days * 86400),
    )
    temps = {"flowTemp": args.flow_temp, "hwcTemp": args.hwc_temp}
    csv_path = args.import_csv or os.path.splitext(args.db)[0] + ".csv"
    try:
        conn = open_database(args.db)
        importing = import_old_files(
            conn,
            csv_path,
            args.changes or os.path.splitext(csv_path)[0] + ".changes.log",
            args.schedule
            or os.path.join(
                os.path.dirname(os.path.abspath(args.db)), "schedule.json"
            ),
            temps,
        )
    except (sqlite3.Error, RuntimeError) as e:
        sys.exit(f"cannot use database {args.db}: {e}")

    writer = DbWriter(args.db, args.flush_seconds)
    aggs = [Aggregator(name, ms, keep) for name, ms, keep in TIERS]
    hub = Hub(writer, args.history_minutes, aggs)
    hub.history.extend(load_history(conn, args.history_minutes))
    hub.changes.extend(load_changes(conn))
    for a in aggs:
        a.reload(load_summaries(conn, a))
    schedule = Schedule(conn, temps)
    commands = CommandLog(hub, writer)
    print(
        f"loaded from {args.db}: {len(hub.history)} chart samples, "
        f"{len(hub.changes)} changes, "
        + ", ".join(f"{len(a.rows)} {a.name} summaries" for a in aggs)
        + f", {commands.load(conn)} boiler instructions",
        file=sys.stderr,
    )
    controller = HeatingController(schedule, hub, commands, host, int(port))
    writer.start()
    controller.start()
    serve(hub, auth, controller, static, args.bind, args.port)
    if importing:
        CsvImporter(args.db, csv_path, hub).start()
    print(
        f"web UI on port {args.port}; saving to {args.db} "
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
