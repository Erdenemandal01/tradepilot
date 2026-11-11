"""Check a collected candle file (Phase 3 acceptance: 24 hours, no gap).

    .venv/Scripts/python -m backend.scripts.check_candles data/collect_binance_M15.csv

Three checks: no candle is missing, the file spans at least --hours, and every
saved candle equals what the source reports now (a candle saved a moment too
early would differ). Exit code 0 only when all three hold. Failed polls and
catch-ups recorded in the collector's log are reported for information.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

from backend.app.config import get_settings
from backend.app.core import candles as cd
from backend.app.datasource import SOURCES, DataSourceError, make_source
from backend.scripts.collect_candles import utc

VERIFY_LIMIT = 1500  # candles asked from the source for the comparison (Binance's maximum)
SHOWN = 20  # differences printed at most


def outage_summary(log_path: Path) -> str | None:
    """Failed polls and the largest catch-up in the collector's log, if there is a log."""
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    failed = catch_up = 0
    for line in lines:
        if "FAILED" in line or "UNEXPECTED ERROR" in line:
            failed += 1
        if "  saved " in line:
            try:
                catch_up = max(catch_up, int(line.split("  saved ", 1)[1].split()[0]))
            except (ValueError, IndexError):
                pass
    return f"{failed} failed poll(s), largest catch-up {catch_up} candle(s)"


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("file", type=Path)
    parser.add_argument("--timeframe", default="M15", choices=list(cd.TIMEFRAME_SECONDS))
    parser.add_argument("--hours", type=float, default=24.0)
    parser.add_argument("--source", choices=SOURCES, default=settings.price_source, help="source to compare with")
    parser.add_argument("--no-verify", action="store_true", help="skip the comparison with the source")
    args = parser.parse_args(argv)

    step = cd.timeframe_seconds(args.timeframe)
    try:
        raw = pd.read_csv(args.file)
        frame = cd.validate(raw.drop(columns=["utc"], errors="ignore"), args.timeframe)
    except (OSError, ValueError) as exc:
        print(f"cannot check {args.file}: {exc}")
        return 1
    if frame.empty:
        print("no candles in file")
        return 1

    first, last = int(frame["time"].iloc[0]), int(frame["time"].iloc[-1])
    span_hours = (last + step - first) / 3600
    expected = (last - first) // step + 1
    gaps = cd.find_gaps(frame, args.timeframe)

    print(f"file:      {args.file}")
    print(f"candles:   {len(frame)} of {expected} expected")
    print(f"first:     {utc(first)}")
    print(f"last:      {utc(last)}")
    print(f"span:      {span_hours:.2f} h")
    print(f"gaps:      {len(gaps)}")
    for gap in gaps:
        print(f"  {gap.missing} missing between {utc(gap.after)} and {utc(gap.before)}")

    if args.no_verify:
        print("content:   not compared with the source (--no-verify)")
        content_ok = True
    else:
        try:
            reference = make_source(settings, args.source).fetch_candles(args.timeframe, limit=VERIFY_LIMIT)
        except (DataSourceError, ValueError) as exc:
            print(f"content:   NOT VERIFIED, cannot fetch from {args.source}: {exc}")
            content_ok = False
        else:
            mismatches = cd.find_mismatches(frame, reference, args.timeframe)
            closed_times = reference.loc[reference["closed"], "time"]
            compared = int(frame["time"].isin(closed_times).sum())
            differing = sorted({m.time for m in mismatches})
            print(f"content:   {compared} of {len(frame)} candles compared with {args.source}, {len(differing)} differ")
            for m in mismatches[:SHOWN]:
                print(f"  {utc(m.time)} {m.column}: saved {m.saved} source {m.reference}")
            if len(mismatches) > SHOWN:
                print(f"  ... and {len(mismatches) - SHOWN} more value(s)")
            if compared < len(frame):
                print(f"  {len(frame) - compared} candle(s) are outside what the source returned and were not compared")
            content_ok = not mismatches and compared == len(frame)

    outages = outage_summary(args.file.with_suffix(".log"))
    if outages:
        print(f"log:       {outages}")

    passed = not gaps and span_hours >= args.hours and content_ok
    print(f"result:    {'PASS' if passed else 'FAIL'} ({args.hours:g} h without a gap, content equal to the source)")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
