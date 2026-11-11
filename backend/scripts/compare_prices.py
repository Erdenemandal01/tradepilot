"""Compare the price feed with the broker's MT5 price (plan section 6).

Record readings:

    py -m backend.scripts.compare_prices

For each reading, look at XAUUSD in the MOT Capital terminal and press Enter
at the moment you read it: the feed's price is fetched right then. Then type
the bid and ask you saw, for example ``4062.08 4062.21``. Twenty readings,
spread over different hours, give a fair picture.

Report:

    py -m backend.scripts.compare_prices --report

The report compares the differences with the entry zone's half width,
0.25 x ATR(14) on the latest closed M15 candles of the feed.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

import pandas as pd

from backend.app.config import get_settings
from backend.app.core import indicators, pricecheck
from backend.app.core.signal import SignalParams
from backend.app.datasource import SOURCES, DataSource, DataSourceError, make_source
from backend.scripts.collect_candles import utc

FIELDS = [
    "time",
    "utc",
    "broker_bid",
    "broker_ask",
    "broker_mid",
    "broker_spread",
    "source",
    "source_symbol",
    "source_bid",
    "source_ask",
    "source_price",
    "difference",
]
TARGET_READINGS = 20


def parse_broker(text: str) -> tuple[float, float]:
    parts = text.replace(",", " ").split()
    if len(parts) != 2:
        raise ValueError("type two numbers: bid and ask, for example 4062.08 4062.21")
    bid, ask = (float(p) for p in parts)
    if not 0 < bid <= ask:
        raise ValueError("bid must be positive and not above ask")
    return bid, ask


def count_rows(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open(newline="", encoding="utf-8") as handle:
        return sum(1 for _ in csv.DictReader(handle))


def record(source: DataSource, out: Path) -> int:
    done = count_rows(out)
    print(f"Saving to {out} ({done} reading(s) so far). Type q to stop.\n")
    while True:
        prompt = f"[{done + 1}/{TARGET_READINGS}] Look at XAUUSD in MT5 and press Enter as you read the bid/ask: "
        if input(prompt).strip().lower() == "q":
            return 0
        try:
            quote = source.fetch_quote()
        except DataSourceError as exc:
            print(f"  feed failed, reading not saved: {exc}")
            continue
        while True:
            text = input("  MT5 bid ask: ").strip()
            if text.lower() == "q":
                return 0
            try:
                bid, ask = parse_broker(text)
                break
            except ValueError as exc:
                print(f"  {exc}")
        broker_mid = pricecheck.mid(bid, ask)
        difference = quote.price - broker_mid
        row = {
            "time": f"{quote.time:.3f}",
            "utc": utc(quote.time),
            "broker_bid": bid,
            "broker_ask": ask,
            "broker_mid": round(broker_mid, 5),
            "broker_spread": round(ask - bid, 5),
            "source": quote.source,
            "source_symbol": quote.symbol,
            "source_bid": "" if quote.bid is None else quote.bid,
            "source_ask": "" if quote.ask is None else quote.ask,
            "source_price": quote.price,
            "difference": round(difference, 5),
        }
        write_header = not out.exists()
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            if write_header:
                writer.writeheader()
            writer.writerow(row)
        done += 1
        print(
            f"  saved: {quote.source} {quote.price:.2f} vs MT5 mid {broker_mid:.3f} -> "
            f"difference {difference:+.2f}, MT5 spread {ask - bid:.2f}"
        )
        if done == TARGET_READINGS:
            print(f"\n{TARGET_READINGS} readings recorded. Run with --report for the summary.\n")


def zone_half_width(source: DataSource) -> float | None:
    params = SignalParams()
    try:
        frame = source.fetch_candles("M15", limit=300)
    except DataSourceError as exc:
        print(f"(cannot fetch M15 candles for the ATR: {exc})")
        return None
    closed = frame[frame["closed"]]
    atr = indicators.atr(closed["high"], closed["low"], closed["close"], params.atr_period)
    if atr.dropna().empty:
        return None
    return params.zone_atr_mult * float(atr.iloc[-1])


def report(source: DataSource, path: Path) -> int:
    if not path.exists():
        print(f"no readings yet: {path}")
        return 1
    rows = pd.read_csv(path)
    rows = rows[rows["source"] == source.name]
    if rows.empty:
        print(f"no readings for source {source.name} in {path}")
        return 1
    zone = zone_half_width(source)
    result = pricecheck.summarize(rows["difference"].tolist(), zone)

    print(f"readings:                 {result.count} (target {TARGET_READINGS}), source {source.name}")
    print(f"period:                   {rows['utc'].iloc[0]} .. {rows['utc'].iloc[-1]}")
    print(f"average difference:       {result.mean:+.3f}  (feed minus MT5; a steady offset)")
    print(f"average |difference|:     {result.mean_abs:.3f}")
    print(f"largest |difference|:     {result.max_abs:.3f}")
    print(f"spread of differences:    {result.std:.3f} (standard deviation)")
    print(f"largest after offset:     {result.max_abs_after_offset:.3f}")
    print(f"MT5 spread, avg / max:    {rows['broker_spread'].mean():.3f} / {rows['broker_spread'].max():.3f}")
    if result.zone_half_width is None:
        print("entry zone half width:    unknown (no ATR)")
        return 0
    print(f"entry zone half width:    {result.zone_half_width:.3f}  (0.25 x ATR14 on M15, now)")
    print(f"raw within zone:          {'yes' if result.raw_within_zone else 'NO'}")
    print(f"within zone after offset: {'yes' if result.offset_within_zone else 'NO'}")
    if result.count < TARGET_READINGS:
        print(f"verdict:                  not enough readings yet ({result.count}/{TARGET_READINGS})")
    elif result.raw_within_zone:
        print("verdict:                  feed is close enough to MT5")
    elif result.offset_within_zone:
        print("verdict:                  usable with a fixed offset correction; discuss before Phase 6")
    else:
        print("verdict:                  too far from MT5: try another source")
    return 0


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", choices=[s for s in SOURCES if s != "csv"], default=settings.price_source)
    parser.add_argument("--file", type=Path, default=Path("data") / "price_compare.csv")
    parser.add_argument("--report", action="store_true", help="print the summary instead of recording")
    args = parser.parse_args(argv)
    source = make_source(settings, args.source)
    if args.report:
        return report(source, args.file)
    try:
        return record(source, args.file)
    except (KeyboardInterrupt, EOFError):
        print()
        return 0


if __name__ == "__main__":
    sys.exit(main())
