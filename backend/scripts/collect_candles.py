"""Collect closed candles from a price source into a CSV file (Phase 3 check).

Polls the source, appends each newly closed candle once, and logs every
failure. A fresh file starts at the latest closed candle. Short outages
after that are filled from the source's own recent history (real candles,
never invented ones); anything longer stays a gap and shows up in
``check_candles``. A candle is saved only once the source lists a newer one,
so a candle that is still settling is never written.

    .venv/Scripts/python -m backend.scripts.collect_candles --hours 24
    .venv/Scripts/python -m backend.scripts.check_candles data/collect_binance_M15.csv

Safe to stop (Ctrl+C) and restart: it resumes after the last saved candle.
Do not run two collectors on the same file.
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import pandas as pd

from backend.app.config import get_settings
from backend.app.core import candles as cd
from backend.app.datasource import SOURCES, DataSourceError, make_source

FIELDS = ["time", "utc", "open", "high", "low", "close", "volume"]
DEFAULT_POLL = {"binance": 30.0, "twelvedata": 120.0, "csv": 5.0}  # Twelve Data free: 800 calls a day
FETCH_LIMIT = 40  # recent candles per call: 39 closed ones, an outage of about 9.5 hours on M15
REFUSED = (418, 429)  # HTTP statuses that mean "slow down" (429) and "banned for a while" (418)
BACKOFF_START, BACKOFF_MAX, RETRY_AFTER_MAX = 60.0, 900.0, 3600.0


def utc(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def wait_after_refusal(error: DataSourceError, poll: float, streak: int) -> float:
    """Seconds to wait after the source refused a request with HTTP 429 or 418.

    Its own Retry-After when it sent one; otherwise one minute, doubling with
    every further refusal in a row, up to 15 minutes. Polling on regardless is
    what turns a rate limit into a ban.
    """
    if error.retry_after is not None and error.retry_after > 0:
        return max(poll, min(float(error.retry_after), RETRY_AFTER_MAX))
    return max(poll, min(BACKOFF_START * 2 ** max(streak - 1, 0), BACKOFF_MAX))


def hold_lock(path: Path):
    """Take a lock that lasts as long as the process; None when another collector holds it."""
    handle = path.open("a+")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def last_saved_time(path: Path) -> int | None:
    if not path.exists():
        return None
    last = None
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            last = int(row["time"])
    return last


def save_new_candles(
    frame: pd.DataFrame,
    out: Path,
    last_time: int | None,
    step: int,
    log: Callable[[str], None],
) -> tuple[int | None, int]:
    """Append the closed candles newer than ``last_time``.

    Returns the new last saved time and how many candles were written. A
    fresh file (``last_time`` is None) starts at the latest closed candle, so
    the 24-hour check measures live collection rather than downloaded
    history. If the file cannot be written (on Windows: it is open in Excel),
    nothing counts as saved and the same candles are tried again next poll.
    """
    closed = frame[frame["closed"]]
    new = closed.tail(1) if last_time is None else closed[closed["time"] > last_time]
    if new.empty:
        return last_time, 0

    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=FIELDS)
    if not out.exists():
        writer.writeheader()
    for row in new.itertuples(index=False):
        writer.writerow(
            {
                "time": int(row.time),
                "utc": utc(row.time),
                "open": repr(float(row.open)),
                "high": repr(float(row.high)),
                "low": repr(float(row.low)),
                "close": repr(float(row.close)),
                "volume": "" if row.volume != row.volume else repr(float(row.volume)),
            }
        )
    try:
        with out.open("a", newline="", encoding="utf-8") as handle:
            handle.write(buffer.getvalue())
    except OSError as exc:
        log(f"WRITE FAILED, will retry on the next poll: {exc} (is {out.name} open in another program?)")
        return last_time, 0

    first = int(new["time"].iloc[0])
    newest = int(new["time"].iloc[-1])
    if last_time is not None and first - last_time > step:
        log(f"GAP: {(first - last_time) // step - 1} candle(s) missing after {utc(last_time)}")
    log(f"saved {len(new)} candle(s), last {utc(newest)} close {float(new['close'].iloc[-1]):.2f}")
    return newest, len(new)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")  # an odd character must never stop the run

    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", choices=SOURCES, default=settings.price_source)
    parser.add_argument("--timeframe", default="M15", choices=list(cd.TIMEFRAME_SECONDS))
    parser.add_argument("--hours", type=float, default=24.0, help="stop after this many hours")
    parser.add_argument("--poll", type=float, help="seconds between calls (default depends on the source)")
    parser.add_argument("--out", type=Path, help="CSV file (default data/collect_<source>_<timeframe>.csv)")
    args = parser.parse_args(argv)
    if args.source not in SOURCES:  # argparse does not check a default against choices
        parser.error(f"unknown price source {args.source!r} in TRADEPILOT_PRICE_SOURCE; choose one of {', '.join(SOURCES)}")

    source = make_source(settings, args.source)
    step = cd.timeframe_seconds(args.timeframe)
    out = args.out or Path("data") / f"collect_{source.name}_{args.timeframe}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    lock = hold_lock(out.with_suffix(".lock"))  # kept until the process ends
    if lock is None:
        print(f"another collector is already writing {out}; stop it first")
        return 2
    log_path = out.with_suffix(".log")
    poll = args.poll or DEFAULT_POLL[source.name]
    deadline = time.time() + args.hours * 3600

    def log(message: str) -> None:
        line = f"{utc(time.time())}  {message}"
        print(line, flush=True)
        try:
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass  # the console still has the line

    last_time = last_saved_time(out)
    saved = failures = 0
    log(
        f"start: source={source.name} symbol={source.symbol} timeframe={args.timeframe} "
        f"poll={poll:g}s hours={args.hours:g} out={out}"
        + (f" resume_after={utc(last_time)}" if last_time else "")
    )
    refusals = 0
    try:
        while time.time() < deadline:
            delay = poll
            try:
                frame = source.fetch_candles(args.timeframe, limit=FETCH_LIMIT)
                last_time, count = save_new_candles(frame, out, last_time, step, log)
                saved += count
                refusals = 0
            except DataSourceError as exc:
                failures += 1
                log(f"FETCH FAILED ({failures} so far): {exc}")
                if exc.status in REFUSED:
                    refusals += 1
                    delay = wait_after_refusal(exc, poll, refusals)
                    log(f"the source asked for a pause: next try in {delay:g} s")
            except Exception as exc:  # a 24-hour run must outlive surprises; the log names them
                failures += 1
                log(f"UNEXPECTED ERROR ({failures} so far): {type(exc).__name__}: {exc}")
            time.sleep(delay)
    except KeyboardInterrupt:
        log("stopped by user")
    log(f"done: saved={saved} failed_polls={failures} file={out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
