"""Reading and repairing the CSV files this program writes."""

import csv
import datetime
import os
import shutil

from .constants import GAP_MS
from .samples import make_sample, number


def tail_lines(path, max_bytes):
    """Text lines from the last max_bytes of a file, and the byte offset they
    start at."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        start = max(0, f.tell() - max_bytes)
        f.seek(start)
        data = f.read()
    # a crash can leave runs of NUL bytes in a file; drop them
    lines = data.replace(b"\0", b"").decode("utf-8", "replace").splitlines()
    if start > 0 and lines:
        lines = lines[1:]  # probably a partial line
    return lines, start


def header_before(path, offset):
    """Columns of the last CSV header line ("time,...") before a byte offset,
    or None."""
    block, pos, carry = 65536, offset, b""
    with open(path, "rb") as f:
        while pos > 0:
            step = min(block, pos)
            pos -= step
            f.seek(pos)
            buf = (f.read(step) + carry).replace(
                b"\0", b"\n"
            )  # crash debris may precede a header
            i = buf.rfind(b"\ntime,")
            if i < 0 and pos == 0 and buf.startswith(b"time,"):
                i = -1
            elif i < 0:
                carry = buf[:1024]  # a header may straddle two blocks
                continue
            line = buf[i + 1 :].split(b"\n", 1)[0]
            return line.decode("utf-8", "replace").split(",")
    return None


def csv_samples(lines, header=None):
    """Chart samples from logger CSV lines; header lines (from restarts) switch
    the columns."""
    prev_flow = None  # (epoch ms, FlowTemp)
    for row in csv.reader(lines):
        if not row:
            continue
        if (
            row[0] == "time"
        ):  # the script was restarted here; columns may differ
            header, prev_flow = row, None
            continue
        if header is None or len(row) != len(header):
            continue
        try:
            ts = int(
                datetime.datetime.fromisoformat(row[0]).timestamp() * 1000
            )
        except ValueError:
            continue
        values = dict(zip(header, row))
        rise, flow = None, number(values.get("FlowTemp"))
        if flow is not None:
            if prev_flow and 200 < ts - prev_flow[0] < GAP_MS:
                rise = (flow - prev_flow[1]) / ((ts - prev_flow[0]) / 1000)
            prev_flow = (ts, flow)
        yield make_sample(ts, values, rise)


def csv_is_clean(path, header):
    """True if the file starts with exactly this header, has no other header
    lines and no NUL bytes, and ends with a complete line. Reads the file in
    large blocks, so it's quick."""
    want = ",".join(header).encode()
    with open(path, "rb") as f:
        if f.readline().rstrip(b"\r\n") != want:
            return False
        carry = b""
        while True:
            block = f.read(1 << 20)
            if not block:
                break
            buf = carry + block
            if b"\0" in block or b"\ntime," in buf:
                return False
            carry = buf[-8:]
        f.seek(0, os.SEEK_END)
        f.seek(f.tell() - 1)
        return f.read(1) == b"\n"


def repair_csv(path, header, row_ok):
    """Clean up a CSV file written by this program, rewriting it only if
    needed: one header line (the current columns) at the top, no NUL bytes
    (left by crashes or power cuts), and no incomplete or unreadable rows. Rows
    written under an older header are converted to the current columns by name.
    Returns a summary string, or None if nothing changed."""
    tmp = path + ".repair"
    if os.path.exists(tmp):
        os.remove(
            tmp
        )  # left over from an interrupted repair; the original is intact
    if (
        not os.path.exists(path)
        or os.path.getsize(path) == 0
        or csv_is_clean(path, header)
    ):
        return None
    kept = dropped = headers = converted = nuls = 0
    cols = None  # the header the following rows were written under
    with open(path, "rb") as src, open(tmp, "w", newline="") as dst:
        w = csv.writer(dst)
        w.writerow(header)
        for raw in src:
            nuls += raw.count(b"\0")
            for piece in raw.split(
                b"\0"
            ):  # text either side of NULs is a fragment of a line
                line = piece.decode("utf-8", "replace").strip("\r\n")
                if not line.strip():
                    continue
                try:
                    row = next(csv.reader([line]))
                except (csv.Error, StopIteration):
                    dropped += 1
                    continue
                if row[0] == "time":
                    cols = row
                    headers += 1
                    continue
                if (
                    cols is None
                    or len(row) != len(cols)
                    or not row_ok(dict(zip(cols, row)))
                ):
                    dropped += 1
                    continue
                if cols != header:
                    values = dict(zip(cols, row))
                    row = [values.get(c, "") for c in header]
                    converted += 1
                w.writerow(row)
                kept += 1
        dst.flush()
        os.fsync(
            dst.fileno()
        )  # the card must hold the new file before it replaces the old one
    shutil.copymode(path, tmp)
    os.replace(tmp, path)
    dirfd = os.open(os.path.dirname(os.path.abspath(path)), os.O_RDONLY)
    try:
        os.fsync(dirfd)
    finally:
        os.close(dirfd)
    return (
        f"repaired {path}: kept {kept} rows, "
        f"removed {max(0, headers - 1)} extra header lines, "
        f"{nuls} NUL bytes and {dropped} incomplete or unreadable rows; "
        f"converted {converted} rows from older column layouts"
    )


def valid_time(value):
    try:
        datetime.datetime.fromisoformat(value)
        return True
    except (TypeError, ValueError):
        return False


def logger_row_ok(row):
    return valid_time(row.get("time"))


def summary_row_ok(row):
    if not valid_time(row.get("time")):
        return False
    try:
        int(row.get("ms"))
        return True
    except (TypeError, ValueError):
        return False


def needs_header(path):
    return not os.path.exists(path) or os.path.getsize(path) == 0
