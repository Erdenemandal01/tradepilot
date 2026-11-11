"""Collect closed candles from a price source into a CSV file (Phase 3 check).

Polls the source, appends each newly closed candle once, and logs every
failure. A fresh file starts at the latest closed candle. Short outages
after that are filled from the source's own recent history
(real candles, never invented ones); anything longer stays a gap and shows
up in ``check_candles``.

    py -m backend.scripts.collect_candles --hours 24
    py -m backend.scripts.check_candles data/collect_binance_M15.csv

Safe to stop (Ctrl+C) and restart: it resumes after the last saved candle.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from backend.app.config import get_settings
from backend.app.core import candles as cd
from backend.app.datasource import SOURCES, DataSourceError, make_source

FIELDS = ["time", "utc", "open", "high", "low", "close", "volume"]
DEFAULT_POLL = {"binance": 30.0, "twelvedata": 120.0, "csv": 5.0}  # Twelve Data free: 800 calls a day
FETCH_LIMIT = 40  # recent candles per call: covers a 10-hour outage on M15


def utc(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def last_saved_time(path: Path) -> int | None:
    if not path.exists():
        return None
    last = None
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            last = int(row["time"])
    return last


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", choices=SOURCES, default=settings.price_source)
    parser.add_argument("--timeframe", default="M15", choices=list(cd.TIMEFRAME_SECONDS))
    parser.add_argument("--hours", type=float, default=24.0, help="stop after this many hours")
    parser.add_argument("--poll", type=float, help="seconds between calls (default depends on the source)")
    parser.add_argument("--out", type=Path, help="CSV file (default data/collect_<source>_<timeframe>.csv)")
    args = parser.parse_args(argv)

    source = make_source(settings, args.source)
    step = cd.timeframe_seconds(args.timeframe)
    out = args.out or Path("data") / f"collect_{source.name}_{args.timeframe}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    log_path = out.with_suffix(".log")
    poll = args.poll or DEFAULT_POLL[source.name]
    deadline = time.time() + args.hours * 3600

    def log(message: str) -> None:
        line = f"{utc(time.time())}  {message}"
        print(line, flush=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    last_time = last_saved_time(out)
    saved = failures = 0
    log(
        f"start: source={source.name} symbol={source.symbol} timeframe={args.timeframe} "
        f"poll={poll:g}s hours={args.hours:g} out={out}"
        + (f" resume_after={utc(last_time)}" if last_time else "")
    )
    try:
        while time.time() < deadline:
            try:
                frame = source.fetch_candles(args.timeframe, limit=FETCH_LIMIT)
            except DataSourceError as exc:
                failures += 1
                log(f"FETCH FAILED ({failures} so far): {exc}")
                time.sleep(poll)
                continue

            closed = frame[frame["closed"]]
            # A fresh start keeps only the latest closed candle, so the 24-hour
            # check measures live collection rather than downloaded history.
            new = closed.tail(1) if last_time is None else closed[closed["time"] > last_time]
            if len(new):
                first = int(new["time"].iloc[0])
                if last_time is not None and first - last_time > step:
                    log(f"GAP: {(first - last_time) // step - 1} candle(s) missing after {utc(last_time)}")
                write_header = not out.exists()
                with out.open("a", newline="", encoding="utf-8") as handle:
                    writer = csv.DictWriter(handle, fieldnames=FIELDS)
                    if write_header:
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
                saved += len(new)
                last_time = int(new["time"].iloc[-1])
                log(f"saved {len(new)} candle(s), last {utc(last_time)} close {float(new['close'].iloc[-1]):.2f} (total {saved})")
            time.sleep(poll)
    except KeyboardInterrupt:
        log("stopped by user")
    log(f"done: saved={saved} fetch_failures={failures} file={out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
