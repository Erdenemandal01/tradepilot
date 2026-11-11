"""Compare the price feed with the broker's MT5 price (plan section 6).

Record readings, spread over different hours of several days:

    .venv/Scripts/python -m backend.scripts.compare_prices

For each reading: look at XAUUSD in the MOT Capital terminal and press Enter
at that moment, then type the bid and ask you saw, for example
``4062.08 4062.21``. The feed is read when you press Enter and again once the
numbers are in; if that took too long or the feed moved in between, the
reading is dropped and you start again. At the Enter prompt ``u`` removes the
last saved reading and ``q`` stops.

Report:

    .venv/Scripts/python -m backend.scripts.compare_prices --report

Each reading is judged against the entry zone of its own moment
(0.25 x ATR14 on closed M15 candles of the feed, stored with the reading), so
the report says the same whenever it is run. A verdict needs 20 readings in
at least 6 different clock hours.
"""

from __future__ import annotations

import argparse
import csv
import math
import re
import sys
import time
from pathlib import Path
from typing import Callable

from backend.app.config import get_settings
from backend.app.core import indicators, pricecheck
from backend.app.core.signal import SignalParams
from backend.app.datasource import SOURCES, DataSource, DataSourceError, make_source
from backend.scripts.collect_candles import utc

LIVE_SOURCES = tuple(name for name in SOURCES if name != "csv")
FIELDS = [
    "time",
    "utc",
    "broker_bid",
    "broker_ask",
    "broker_mid",
    "broker_spread",
    "source",
    "source_symbol",
    "source_price",
    "difference",
    "seconds",
    "zone_half_width",
    "flag",
]
TARGET_READINGS = 20
MIN_HOURS = 6  # a verdict needs readings in at least this many different clock hours
MAX_SECONDS = 30.0  # longest time allowed between the two feed readings of one reading
MAX_FEED_MOVE = 0.5  # how far (USD) the feed may move between them
MARKET_CLOSED = "market_closed"
CONFIRMED = "confirmed_unusual"

Reader = Callable[[str], str]
Printer = Callable[[str], None]


def parse_broker(text: str) -> tuple[float, float]:
    """Two prices, bid then ask. A decimal comma is accepted: ``4062,08 4062,21``."""
    tokens = [t for t in re.split(r"[\s;/]+", text.strip()) if t]
    if len(tokens) == 1 and tokens[0].count(",") == 1 and "." in tokens[0]:
        tokens = tokens[0].split(",")  # 4062.08,4062.21
    if len(tokens) != 2:
        raise ValueError("type two numbers, bid then ask, for example 4062.08 4062.21")
    try:
        bid, ask = (float(t.replace(",", ".")) for t in tokens)
    except ValueError:
        raise ValueError("type two numbers, bid then ask, for example 4062.08 4062.21") from None
    if not (math.isfinite(bid) and math.isfinite(ask)) or bid <= 0:
        raise ValueError("prices must be positive numbers")
    if bid > ask:
        raise ValueError("bid must not be above ask")
    return bid, ask


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def write_rows(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def append_row(path: Path, row: dict) -> None:
    rows = read_rows(path)
    rows.append({key: str(value) for key, value in row.items()})
    write_rows(path, rows)  # rewriting keeps the header and the line ends right whatever was there


def undo_last(path: Path, source_name: str) -> dict[str, str] | None:
    rows = read_rows(path)
    for index in range(len(rows) - 1, -1, -1):
        if rows[index].get("source") == source_name:
            removed = rows.pop(index)
            write_rows(path, rows)
            return removed
    return None


def zone_half_width(source: DataSource, say: Printer) -> float | None:
    """0.25 x ATR14 on the feed's closed M15 candles, right now."""
    params = SignalParams()
    try:
        frame = source.fetch_candles("M15", limit=300)
    except DataSourceError as exc:
        say(f"  (no entry zone stored with this reading: {exc})")
        return None
    closed = frame[frame["closed"]]
    atr = indicators.atr(closed["high"], closed["low"], closed["close"], params.atr_period).dropna()
    if atr.empty:
        return None
    return params.zone_atr_mult * float(atr.iloc[-1])


def record(
    source: DataSource,
    out: Path,
    stale_after: float,
    read: Reader = input,
    say: Printer = print,
    clock: Callable[[], float] = time.time,
    timer: Callable[[], float] = time.monotonic,
) -> int:
    done = sum(row.get("source") == source.name for row in read_rows(out))
    say(f"Saving to {out} ({done} {source.name} reading(s) so far).")
    say("Enter = take a reading, u = remove the last one, q = stop.\n")
    while True:
        prompt = f"[{done + 1}/{TARGET_READINGS}] Look at XAUUSD in MT5 and press Enter as you read the bid/ask: "
        answer = read(prompt).strip().lower()
        if answer == "q":
            return 0
        if answer == "u":
            removed = undo_last(out, source.name)
            if removed:
                done -= 1
                say(f"  removed the reading of {removed.get('utc')}")
            else:
                say("  nothing to remove")
            continue
        if answer:
            say("  press Enter first, then type the numbers")
            continue

        try:
            first = source.fetch_quote()
        except DataSourceError as exc:
            say(f"  feed failed, no reading taken: {exc}")
            continue
        started, now = timer(), clock()
        if first.timed_by_source and now - first.time > stale_after:
            say(f"  the feed's price is {now - first.time:.0f} s old, no reading taken")
            continue

        text = read("  MT5 bid ask: ").strip()
        if text.lower() == "q":
            return 0
        try:
            bid, ask = parse_broker(text)
        except ValueError as exc:
            say(f"  {exc}. Reading dropped, start again.")
            continue
        try:
            second = source.fetch_quote()
        except DataSourceError as exc:
            say(f"  feed failed, reading dropped: {exc}")
            continue
        seconds = timer() - started
        if seconds > MAX_SECONDS:
            say(f"  that took {seconds:.0f} s (limit {MAX_SECONDS:g}). Reading dropped, start again.")
            continue
        if abs(second.price - first.price) > MAX_FEED_MOVE:
            say(f"  the feed moved {second.price - first.price:+.2f} meanwhile. Reading dropped, start again.")
            continue

        feed = (first.price + second.price) / 2
        flag = ""
        doubt = pricecheck.implausible(bid, ask, feed)
        if doubt:
            keep = read(f"  {doubt}: MT5 {bid} / {ask}, feed {feed:.2f}. y = keep it, anything else = drop: ")
            if keep.strip().lower() != "y":
                continue
            flag = CONFIRMED
        mine = [row for row in read_rows(out) if row.get("source") == source.name]
        frozen = (
            bool(mine)
            and float(mine[-1]["broker_bid"]) == bid
            and float(mine[-1]["broker_ask"]) == ask
            and now - float(mine[-1]["time"]) > 60
        )
        if frozen or pricecheck.probably_closed(now):
            why = "MT5 shows the same numbers as last time" if frozen else "the gold market is usually closed at this hour"
            keep = read(f"  {why}, so MT5 may be showing a frozen price. y = save anyway, anything else = drop: ")
            if keep.strip().lower() != "y":
                continue
            flag = MARKET_CLOSED

        broker_mid = pricecheck.mid(bid, ask)
        difference = feed - broker_mid
        zone = zone_half_width(source, say)
        moment = first.time if first.timed_by_source else now
        append_row(
            out,
            {
                "time": f"{moment:.3f}",
                "utc": utc(moment),
                "broker_bid": bid,
                "broker_ask": ask,
                "broker_mid": round(broker_mid, 5),
                "broker_spread": round(ask - bid, 5),
                "source": source.name,
                "source_symbol": source.symbol,
                "source_price": round(feed, 5),
                "difference": round(difference, 5),
                "seconds": round(seconds, 1),
                "zone_half_width": "" if zone is None else round(zone, 5),
                "flag": flag,
            },
        )
        done += 1
        say(
            f"  saved: {source.name} {feed:.2f} vs MT5 mid {broker_mid:.3f} -> difference {difference:+.2f}, "
            f"MT5 spread {ask - bid:.2f}" + ("" if zone is None else f", zone +-{zone:.2f}")
        )
        if done == TARGET_READINGS:
            say(f"\n{TARGET_READINGS} readings recorded. Run with --report for the summary.\n")


def report(source_name: str, path: Path, say: Printer = print) -> int:
    rows = [row for row in read_rows(path) if row.get("source") == source_name]
    if not rows:
        say(f"no readings for source {source_name} in {path}")
        return 1
    used = [row for row in rows if row.get("flag") != MARKET_CLOSED]
    left_out = len(rows) - len(used)
    if not used:
        say(f"all {len(rows)} reading(s) were taken while the market was probably closed; none can be used")
        return 1

    times = [float(row["time"]) for row in used]
    spreads = [float(row["broker_spread"]) for row in used]
    zones = [float(row["zone_half_width"]) if row.get("zone_half_width") else None for row in used]
    result = pricecheck.summarize([float(row["difference"]) for row in used], zones)
    hours = pricecheck.distinct_hours(times)

    say(f"readings:                 {result.count} (target {TARGET_READINGS}), source {source_name}")
    if left_out:
        say(f"left out:                 {left_out} taken while the market was probably closed")
    say(f"period:                   {used[0]['utc']} .. {used[-1]['utc']}")
    say(f"different clock hours:    {hours} (at least {MIN_HOURS} needed)")
    say(f"average difference:       {result.mean:+.3f}  (feed minus MT5; a steady offset)")
    say(f"average |difference|:     {result.mean_abs:.3f}")
    say(f"largest |difference|:     {result.max_abs:.3f}")
    say(f"spread of differences:    {result.std:.3f} (standard deviation)")
    say(f"largest after offset:     {result.max_abs_after_offset:.3f}")
    say(f"MT5 spread, avg / max:    {sum(spreads) / len(spreads):.3f} / {max(spreads):.3f}")
    if result.judged:
        say(f"outside their zone:       {result.raw_outside} of {result.judged} (worst {result.worst_raw_ratio:.2f} x zone)")
        say(f"outside after offset:     {result.offset_outside} of {result.judged} (worst {result.worst_offset_ratio:.2f} x zone)")
    if result.judged < result.count:
        say(f"no zone stored:           {result.count - result.judged} reading(s), not judged against a zone")

    if result.count < TARGET_READINGS:
        verdict = f"not enough readings yet ({result.count}/{TARGET_READINGS})"
    elif hours < MIN_HOURS:
        verdict = f"not spread over enough hours yet ({hours} different hour(s), {MIN_HOURS} needed)"
    elif not result.judged:
        verdict = "cannot judge: no reading has an entry zone stored"
    elif result.raw_within_zone:
        verdict = "feed is close enough to MT5"
    elif result.offset_within_zone:
        verdict = f"usable with a fixed offset correction of {result.mean:+.2f}; discuss before Phase 6"
    else:
        verdict = "too far from MT5: try another source"
    say(f"verdict:                  {verdict}")
    return 0


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", choices=LIVE_SOURCES, default=settings.price_source)
    parser.add_argument("--file", type=Path, default=Path("data") / "price_compare.csv")
    parser.add_argument("--report", action="store_true", help="print the summary instead of recording")
    args = parser.parse_args(argv)
    if args.source not in LIVE_SOURCES:  # argparse does not check a default against choices
        parser.error(f"a live source is needed ({', '.join(LIVE_SOURCES)}); TRADEPILOT_PRICE_SOURCE is {args.source!r}")
    if args.report:
        return report(args.source, args.file)
    source = make_source(settings, args.source)
    try:
        return record(source, args.file, float(settings.stale_after_seconds))
    except (KeyboardInterrupt, EOFError):
        print()
        return 0


if __name__ == "__main__":
    sys.exit(main())
