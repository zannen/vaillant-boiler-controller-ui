"""Database schema changes, applied in order by db.migrate().

Append only: never edit a migration once it is on main; fix mistakes with a
new one. Each is a list of SQL statements, or a function taking the
connection, and runs inside the migration's transaction.

The boiler value columns of samples (one TEXT column per VALUES name) and the
summary columns of summaries (REAL <key>_mean/_min/_max per SAMPLE_KEYS name)
are not listed here: db.ensure_columns() adds missing ones at every start."""

MIGRATIONS: list = [
    # 1: the first schema
    [
        # one row per pass of the poller: raw ebusd values, e.g. "51.06;ok"
        "CREATE TABLE samples (id INTEGER PRIMARY KEY, ms INTEGER NOT NULL)",
        "CREATE INDEX samples_ms ON samples (ms)",
        # per-bucket mean/min/max of the chart values; tier is e.g. "1min"
        "CREATE TABLE summaries (tier TEXT NOT NULL, ms INTEGER NOT NULL, "
        "PRIMARY KEY (tier, ms))",
        "CREATE TABLE changes (id INTEGER PRIMARY KEY, ms INTEGER NOT NULL, "
        "time TEXT NOT NULL, name TEXT NOT NULL, old TEXT NOT NULL, "
        "new TEXT NOT NULL)",
        "CREATE INDEX changes_ms ON changes (ms)",
        # every SetMode sent to the boiler
        "CREATE TABLE commands (id INTEGER PRIMARY KEY, "
        "ms INTEGER NOT NULL, state TEXT NOT NULL, ok INTEGER NOT NULL, "
        "result TEXT NOT NULL, source TEXT NOT NULL, flow_temp REAL, "
        "hwc_temp REAL)",
        "CREATE INDEX commands_ms ON commands (ms)",
        # the heating schedule: one settings row, and its on/off periods
        "CREATE TABLE schedule (id INTEGER PRIMARY KEY CHECK (id = 1), "
        "enabled INTEGER NOT NULL, flow_temp REAL NOT NULL, "
        "hwc_temp REAL NOT NULL, updated_by TEXT, updated_at TEXT)",
        "CREATE TABLE schedule_periods (day TEXT NOT NULL, "
        "start TEXT NOT NULL, stop TEXT NOT NULL, PRIMARY KEY (day, start))",
        # program state, e.g. how far the import of old CSV files has got
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    ],
]
