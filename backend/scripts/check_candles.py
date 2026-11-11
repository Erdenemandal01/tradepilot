"""Check a collected candle file for gaps (Phase 3 acceptance: 24 hours, no gap).

    py -m backend.scripts.check_candles data/collect_binance_M15.csv

Exit code 0 when the file spans at least --hours with no missing candle.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

from backend.app.core import candles as cd
from backend.scripts.collect_candles import utc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("file", type=Path)
    parser.add_argument("--timeframe", default="M15", choices=list(cd.TIMEFRAME_SECONDS))
    parser.add_argument("--hours", type=float, default=24.0)
    args = parser.parse_args(argv)

    step = cd.timeframe_seconds(args.timeframe)
    frame = cd.validate(pd.read_csv(args.file).drop(columns=["utc"], errors="ignore"), args.timeframe)
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
    passed = not gaps and span_hours >= args.hours
    print(f"result:    {'PASS' if passed else 'FAIL'} ({args.hours:g} h without a gap)")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
