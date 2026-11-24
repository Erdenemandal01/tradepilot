"""Candle frames: schema, validation, gap detection and timeframe aggregation.

A candle frame is a pandas DataFrame, oldest first, with these columns:

- ``time``: candle open time, UTC Unix seconds, aligned to the timeframe
- ``open``, ``high``, ``low``, ``close``: prices
- ``volume``: NaN when the source reports none
- ``closed``: False only for the last candle, while it is still forming

Every price source returns this shape, so the signal engine and the backtest
never see a source's own format.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd

TIMEFRAME_SECONDS = {
    "M1": 60,
    "M2": 120,
    "M3": 180,
    "M5": 300,
    "M10": 600,
    "M15": 900,
    "M30": 1800,
    "M45": 2700,
    "H1": 3600,
    "H2": 7200,
    "H3": 10800,
    "H4": 14400,
    "D1": 86400,
    "W1": 604800,
}
# Candles that follow the calendar and so have no fixed length: 1, 3, 6 and 12
# months, opening on the first day of a month, a quarter, a half-year, a year.
MONTH_TIMEFRAMES = {"MN1": 1, "MN3": 3, "MN6": 6, "MN12": 12}
# Every candle length a chart can ask for, shortest first.
CHART_TIMEFRAMES = (*TIMEFRAME_SECONDS, *MONTH_TIMEFRAMES)
WEEK_OFFSET = 4 * 86400  # weeks open on Monday 00:00 UTC; second 0 was a Thursday

MAX_TIME = 4_102_444_800  # 2100-01-01: anything later is not seconds (milliseconds, nanoseconds)

CANDLE_COLUMNS = ("time", "open", "high", "low", "close", "volume", "closed")
PRICE_COLUMNS = ["open", "high", "low", "close"]
_DTYPES = {
    "time": "int64",
    "open": float,
    "high": float,
    "low": float,
    "close": float,
    "volume": float,
    "closed": bool,
}


@dataclass(frozen=True)
class Gap:
    """Candles missing between two present ones."""

    after: int  # open time of the last candle before the gap
    before: int  # open time of the first candle after the gap
    missing: int  # number of candles missing


@dataclass(frozen=True)
class Mismatch:
    """One value of a saved candle that differs from the reference."""

    time: int  # open time of the candle
    column: str
    saved: float
    reference: float


def timeframe_seconds(timeframe: str) -> int:
    if timeframe in MONTH_TIMEFRAMES:
        raise ValueError(f"timeframe {timeframe!r} has no fixed length: it follows the calendar")
    try:
        return TIMEFRAME_SECONDS[timeframe]
    except KeyError:
        known = ", ".join(CHART_TIMEFRAMES)
        raise ValueError(f"unknown timeframe {timeframe!r}; known: {known}") from None


def _month_index(times: pd.Series) -> pd.Series:
    """Months counted from year 0, so that adding and dividing them is plain arithmetic."""
    stamps = pd.to_datetime(times.astype("int64"), unit="s")
    return stamps.dt.year * 12 + stamps.dt.month - 1


def _month_open(index: pd.Series) -> pd.Series:
    """UTC Unix seconds of the first day of each counted month."""
    parts = pd.DataFrame({"year": index // 12, "month": index % 12 + 1, "day": 1})
    stamps = pd.to_datetime(parts)
    return ((stamps - pd.Timestamp(0)) // pd.Timedelta(seconds=1)).astype("int64")


def opens(times: pd.Series, timeframe: str) -> pd.Series:
    """Open time of the ``timeframe`` candle each time falls in.

    All boundaries are UTC. Fixed lengths count from midnight, weeks from
    Monday, and the month timeframes from the first day of a calendar month,
    quarter, half-year or year.
    """
    times = pd.Series(times).astype("int64")
    if timeframe in MONTH_TIMEFRAMES:
        size = MONTH_TIMEFRAMES[timeframe]
        return _month_open(_month_index(times) // size * size)
    step = timeframe_seconds(timeframe)
    offset = WEEK_OFFSET if timeframe == "W1" else 0
    return (times - offset) // step * step + offset


def following(times: pd.Series, timeframe: str) -> pd.Series:
    """Open time of the candle after the one that opens at each time."""
    times = pd.Series(times).astype("int64")
    if timeframe in MONTH_TIMEFRAMES:
        return _month_open(_month_index(times) + MONTH_TIMEFRAMES[timeframe])
    return times + timeframe_seconds(timeframe)


def validate(candles: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Check a candle frame and return a normalised copy in the standard shape.

    Raises ValueError when a rule is broken. Adds ``volume`` (NaN) and
    ``closed`` (True) when they are absent. Never modifies the input.
    """
    if timeframe not in MONTH_TIMEFRAMES:
        timeframe_seconds(timeframe)  # an unknown timeframe is refused before anything else
    missing = [c for c in ("time", *PRICE_COLUMNS) if c not in candles.columns]
    if missing:
        raise ValueError(f"candles are missing columns: {', '.join(missing)}")

    frame = candles.copy()
    if "volume" not in frame.columns:
        frame["volume"] = np.nan
    if "closed" not in frame.columns:
        frame["closed"] = True
    frame = frame.loc[:, list(CANDLE_COLUMNS)].reset_index(drop=True)
    if frame.empty:
        return frame.astype(_DTYPES)

    if pd.api.types.is_datetime64_any_dtype(frame["time"]):
        raise ValueError("candle times must be UTC Unix seconds, not a date-time column")
    times = pd.to_numeric(frame["time"]).astype(float)
    if not np.isfinite(times).all() or (times % 1 != 0).any():
        raise ValueError("candle times must be whole UTC Unix seconds")
    if (times <= 0).any() or (times >= MAX_TIME).any():
        raise ValueError("candle times are not plausible UTC Unix seconds (milliseconds?)")
    frame["time"] = times.astype("int64")
    if (opens(frame["time"], timeframe) != frame["time"]).any():
        raise ValueError(f"candle times must be aligned to {timeframe} boundaries")
    if not frame["time"].is_monotonic_increasing or frame["time"].duplicated().any():
        raise ValueError("candle times must be strictly increasing (oldest first)")

    prices = frame[PRICE_COLUMNS].astype(float)
    values = prices.to_numpy()
    if not np.isfinite(values).all() or (values <= 0).any():
        raise ValueError("prices must be positive and finite")
    body_high = prices[["open", "close"]].max(axis=1)
    body_low = prices[["open", "close"]].min(axis=1)
    if (prices["high"] < body_high).any() or (prices["low"] > body_low).any():
        raise ValueError("high and low must contain open and close")
    frame[PRICE_COLUMNS] = prices
    frame["volume"] = pd.to_numeric(frame["volume"]).astype(float)
    volumes = frame["volume"].to_numpy()
    known = ~np.isnan(volumes)
    if np.isinf(volumes).any() or (volumes[known] < 0).any():
        raise ValueError("volume must be finite and not negative (NaN when the source gives none)")

    if not pd.api.types.is_bool_dtype(frame["closed"]):
        raise ValueError("the closed column must be boolean")
    if (~frame["closed"].iloc[:-1]).any():
        raise ValueError("only the last candle may still be forming")
    return frame.astype(_DTYPES)


def steps_between(earlier: pd.Series | np.ndarray, later: pd.Series | np.ndarray, timeframe: str) -> np.ndarray:
    """How many candles of ``timeframe`` lie from each open time in ``earlier`` to the one in ``later``.

    Both are aligned open times. Fixed lengths count seconds; the month
    timeframes count calendar months, so a month tail works like any other.
    """
    earlier = pd.Series(np.asarray(earlier, dtype="int64"))
    later = pd.Series(np.asarray(later, dtype="int64"))
    if timeframe in MONTH_TIMEFRAMES:
        return ((_month_index(later) - _month_index(earlier)) // MONTH_TIMEFRAMES[timeframe]).to_numpy()
    return ((later - earlier) // timeframe_seconds(timeframe)).to_numpy()


def find_gaps(candles: pd.DataFrame, timeframe: str) -> list[Gap]:
    """List every place where candles are missing between two present ones."""
    times = validate(candles, timeframe)["time"].to_numpy()
    if len(times) < 2:
        return []
    steps = steps_between(times[:-1], times[1:], timeframe)
    return [
        Gap(after=int(times[i]), before=int(times[i + 1]), missing=int(steps[i] - 1)) for i in np.nonzero(steps > 1)[0]
    ]


def find_mismatches(
    candles: pd.DataFrame, reference: pd.DataFrame, timeframe: str, tolerance: float = 1e-9
) -> list[Mismatch]:
    """Compare saved candles with a reference frame from the same source.

    Only candles present in both frames and closed in the reference are
    compared. Volume is compared when both sides have one. A candle that was
    saved before it had settled shows up here as a difference.
    """
    saved = validate(candles, timeframe).set_index("time")
    ref = validate(reference, timeframe)
    ref = ref[ref["closed"]].set_index("time")
    found: list[Mismatch] = []
    for time in saved.index.intersection(ref.index):
        for column in (*PRICE_COLUMNS, "volume"):
            a = float(saved.at[time, column])
            b = float(ref.at[time, column])
            if np.isnan(a) or np.isnan(b):
                continue
            if abs(a - b) > tolerance:
                found.append(Mismatch(time=int(time), column=column, saved=a, reference=b))
    return found


def aggregate(candles: pd.DataFrame, source_timeframe: str, target_timeframe: str) -> pd.DataFrame:
    """Build higher-timeframe candles, for example H1 from M15.

    Buckets are aligned to UTC multiples of the target timeframe. A bucket is
    closed only when every source candle in it is present and closed. An
    incomplete bucket is dropped (no candle is built from partial data),
    except the last one, which is kept as still forming.
    """
    source = timeframe_seconds(source_timeframe)
    target = timeframe_seconds(target_timeframe)
    if target <= source or target % source:
        raise ValueError(f"cannot build {target_timeframe} candles from {source_timeframe}")
    frame = validate(candles, source_timeframe)
    if frame.empty:
        return frame

    ratio = target // source
    bucket = (frame["time"] // target * target).rename("bucket")
    grouped = frame.groupby(bucket, sort=True)
    out = pd.DataFrame(
        {
            "open": grouped["open"].first(),
            "high": grouped["high"].max(),
            "low": grouped["low"].min(),
            "close": grouped["close"].last(),
            "volume": grouped["volume"].sum(min_count=1),
            "count": grouped["time"].size(),
            "all_closed": grouped["closed"].all(),
        }
    )
    complete = (out["count"] == ratio) & out["all_closed"]
    is_last = out.index == out.index[-1]
    out = out[complete | is_last].copy()
    out["closed"] = complete[complete | is_last]
    out["time"] = out.index.astype("int64")
    return validate(out.reset_index(drop=True), target_timeframe)


def count_breaks(candles: pd.DataFrame, timeframe: str) -> int:
    """How many times a candle is not followed by the next one of its timeframe."""
    times = candles["time"].reset_index(drop=True)
    if len(times) < 2:
        return 0
    expected = following(times.iloc[:-1], timeframe).to_numpy()
    return int((times.iloc[1:].to_numpy() != expected).sum())


def can_resample(source_timeframe: str, target_timeframe: str) -> bool:
    """Whether whole ``source`` candles fill every ``target`` candle exactly."""
    known = (*TIMEFRAME_SECONDS, *MONTH_TIMEFRAMES)
    if source_timeframe not in known or target_timeframe not in known:
        return False
    source_months = MONTH_TIMEFRAMES.get(source_timeframe)
    target_months = MONTH_TIMEFRAMES.get(target_timeframe)
    if source_months is not None:
        return target_months is not None and target_months > source_months and target_months % source_months == 0
    source = TIMEFRAME_SECONDS[source_timeframe]
    if source_timeframe == "W1" or 86400 % source:
        return False  # weeks fit neither months nor anything else here
    if target_months is not None:
        return True
    target = TIMEFRAME_SECONDS[target_timeframe]
    return target > source and target % source == 0


def resample(
    candles: pd.DataFrame, source_timeframe: str, target_timeframe: str, *, history_starts: bool = False
) -> tuple[pd.DataFrame, bool]:
    """Longer candles for a chart, for example M45 from M15 or MN12 from MN1.

    Each candle takes the first open, the highest high, the lowest low, the
    last close and the summed volume of the candles inside its boundaries
    (see ``opens``). A candle is never built over a hole: a bucket with a
    part missing is left out, and the hole then shows as a break
    (``count_breaks``). Two incomplete buckets are kept because they are
    real: the newest one, as still forming, and the oldest one when
    ``history_starts`` says the source has nothing older than the first
    candle given. The second return value tells whether the first candle is
    such a partial one.
    """
    if not can_resample(source_timeframe, target_timeframe):
        raise ValueError(f"cannot build {target_timeframe} candles from {source_timeframe}")
    source_months = MONTH_TIMEFRAMES.get(source_timeframe)
    target_months = MONTH_TIMEFRAMES.get(target_timeframe)
    source = 0 if source_months is not None else timeframe_seconds(source_timeframe)

    frame = validate(candles, source_timeframe)
    if frame.empty:
        return validate(frame, target_timeframe), False

    bucket = opens(frame["time"], target_timeframe)
    if source_months is not None:
        position = (_month_index(frame["time"]) - _month_index(bucket)) // source_months
    else:
        position = (frame["time"] - bucket) // source
    grouped = frame.assign(bucket=bucket.to_numpy(), position=position.to_numpy()).groupby("bucket", sort=True)
    out = pd.DataFrame(
        {
            "open": grouped["open"].first(),
            "high": grouped["high"].max(),
            "low": grouped["low"].min(),
            "close": grouped["close"].last(),
            "volume": grouped["volume"].sum(min_count=1),
            "count": grouped["time"].size(),
            "first": grouped["position"].min(),
            "last": grouped["position"].max(),
            "all_closed": grouped["closed"].all(),
        }
    )
    starts = pd.Series(out.index.to_numpy(), index=out.index)
    if source_months is not None:
        parts = target_months // source_months
    else:
        parts = (following(starts, target_timeframe) - starts) // source  # a month has 28 to 31 days

    unbroken = out["count"] == out["last"] - out["first"] + 1
    from_open = out["first"] == 0
    to_close = out["last"] == parts - 1
    full = unbroken & from_open & to_close & out["all_closed"]
    newest = pd.Series(out.index == out.index[-1], index=out.index)
    keep = full | (newest & unbroken & from_open)
    closed = full.copy()
    partial = False
    if history_starts and not keep.iloc[0] and unbroken.iloc[0] and (to_close.iloc[0] or newest.iloc[0]):
        # The oldest bucket lacks its beginning because the history begins there.
        keep.iloc[0] = True
        closed.iloc[0] = not newest.iloc[0]
        partial = True

    out = out[keep]
    built = pd.DataFrame(
        {
            "time": out.index.to_numpy().astype("int64"),
            "open": out["open"].to_numpy(),
            "high": out["high"].to_numpy(),
            "low": out["low"].to_numpy(),
            "close": out["close"].to_numpy(),
            "volume": out["volume"].to_numpy(),
            "closed": closed[keep].to_numpy().astype(bool),
        }
    )
    return validate(built, target_timeframe), partial


def lay_over(stored: pd.DataFrame, fresh: pd.DataFrame, timeframe: str) -> tuple[pd.DataFrame | None, list[Gap]]:
    """Stored candles with a newer ``fresh`` tail laid over them, and the holes that are news.

    Fresh rows replace stored rows from their first time on, so a candle that
    was still forming gets its final values. The frame is None when the tail
    does not connect: candles are missing between the two, the stored forming
    candle would stay unfinished in the middle, or the source went back in
    time. The list holds every hole of the result that does not lie inside a
    hole the stored candles had: there a candle that was stored is gone, or
    one is missing among the new ones. A hole the stored candles already had
    is the source's own history and is not in it, and neither is what is left
    of such a hole after the source filled a part of it.
    """
    if fresh.empty:
        merged = stored
    elif stored.empty:
        merged = fresh
    else:
        first = int(fresh["time"].iloc[0])
        last = int(stored["time"].iloc[-1])
        # The fresh tail must begin no later than the first candle the stored ones lack.
        limit = _next_open(last, timeframe) if bool(stored["closed"].iloc[-1]) else last
        if first > limit or int(fresh["time"].iloc[-1]) < last:
            return None, []
        merged = pd.concat([stored[stored["time"] < first], fresh], ignore_index=True)
    merged = validate(merged, timeframe)
    known = [] if stored.empty else [(gap.after, gap.before) for gap in find_gaps(stored, timeframe)]
    return merged, [gap for gap in find_gaps(merged, timeframe) if not within((gap.after, gap.before), known)]


def within(hole: tuple[int, int], known: Iterable[tuple[int, int]]) -> bool:
    """Whether a hole, as (open time before, open time after), lies inside one of the ``known`` holes.

    A known hole itself does, and so does what is left of it when some of its
    candles turned up: no candle that was there before is missing then.
    """
    return any(after <= hole[0] and hole[1] <= before for after, before in known)


def merge(stored: pd.DataFrame, fresh: pd.DataFrame, timeframe: str, keep: int | None = None) -> pd.DataFrame | None:
    """Stored candles with a newer ``fresh`` tail from the same source laid over them.

    See ``lay_over``. Returns None when the tail does not connect, or when
    the result would have a hole the stored candles did not have: loading
    everything again would bring an old hole back, so only a new one means
    candles were missed. The caller then reloads the whole history instead of
    guessing. ``keep`` limits the result to the newest rows.
    """
    merged, news = lay_over(stored, fresh, timeframe)
    if merged is None or news:
        return None
    return merged.iloc[-keep:].reset_index(drop=True) if keep else merged


def hole_reach(candles: pd.DataFrame, timeframe: str, newest: int) -> int:
    """How many of the newest candles a request must ask for to cover the holes among them.

    Counted from the candle before the oldest hole that begins within the
    ``newest`` candles, up to the last one. 0 when there is no such hole: a
    hole further back is the source's own history and is left alone.
    """
    last = int(candles["time"].iloc[-1]) if len(candles) else 0
    reach = 0
    for gap in find_gaps(candles, timeframe):
        need = int(steps_between([gap.after], [last], timeframe)[0]) + 1  # from the candle before the hole to the last
        if need <= newest:
            reach = max(reach, need)
    return reach


def _next_open(time: int, timeframe: str) -> int:
    return int(following(pd.Series([int(time)]), timeframe).iloc[0])


# What laying one stream candle over stored candles did (``lay_stream``).
STREAM_SAME = "same"  # the newest stored candle got the stream's newer state
STREAM_NEXT = "next"  # the stream's candle is the next one: appended, the one before it is closed now
STREAM_OLD = "old"  # older than what is stored (or would reopen a closed candle): nothing changed
STREAM_HOLE = "hole"  # one or more candles are missing between the stored ones and it: refill over REST


def lay_stream(
    stored: pd.DataFrame, bar: Mapping[str, Any], timeframe: str, *, keep_closed: bool = False
) -> tuple[pd.DataFrame, str]:
    """Stored candles with one candle from a live stream laid over them, and what that did.

    ``bar`` has ``time`` (open, UTC seconds), ``open``, ``high``, ``low``,
    ``close``, ``volume`` (NaN when unknown) and ``closed`` (the exchange's
    own flag). The same open time as the newest stored candle replaces it;
    the next open time is appended and the candle before it is then closed;
    an older time changes nothing. A newer time further on is never built
    over: the frame stays as it is and the outcome says a refill is needed.
    A closed newest candle is not reopened by an update that says it is
    still forming, and with ``keep_closed`` no closed candle is replaced at
    all (the signal timeframe: what the rule ran on stays as it was).

    The stored frame is never changed. The result is validated: sorted, no
    two candles at one time. A bar that is not a valid candle of the
    timeframe raises ValueError.
    """
    time = int(bar["time"])
    row = {
        "time": time,
        "open": float(bar["open"]),
        "high": float(bar["high"]),
        "low": float(bar["low"]),
        "close": float(bar["close"]),
        "volume": float(bar["volume"]) if bar.get("volume") is not None else np.nan,
        "closed": bool(bar["closed"]),
    }
    one = validate(pd.DataFrame([row]), timeframe)  # aligned, positive, high and low around open and close
    if stored.empty:
        return stored, STREAM_HOLE
    last = int(stored["time"].iloc[-1])
    if time < last:
        return stored, STREAM_OLD
    if time == last:
        if bool(stored["closed"].iloc[-1]) and (keep_closed or not row["closed"]):
            return stored, STREAM_OLD
        laid = pd.concat([stored.iloc[:-1], one], ignore_index=True)
        return validate(laid, timeframe), STREAM_SAME
    if time != _next_open(last, timeframe):
        return stored, STREAM_HOLE
    before = stored.copy()
    before.loc[before.index[-1], "closed"] = True
    return validate(pd.concat([before, one], ignore_index=True), timeframe), STREAM_NEXT


def older_changed(old: pd.DataFrame | None, new: pd.DataFrame, newest: int = 3) -> bool:
    """Whether anything but the ``newest`` candles differs between two frames of one timeframe.

    Compared over the times both frames cover, up to the ``newest``-th
    newest candle of ``new``. A candle added at the new end or dropped at the
    old end is no change: a page that follows the newest candles already has
    everything else. A changed value, a candle that came or went further
    back, or a candle that turned final further back than ``newest`` is.
    """
    if old is None or old.empty or new.empty:
        return not ((old is None or old.empty) and new.empty)
    cut = int(new["time"].iloc[-min(newest, len(new))])
    low = max(int(old["time"].iloc[0]), int(new["time"].iloc[0]))
    a = old[(old["time"] >= low) & (old["time"] < cut)]
    b = new[(new["time"] >= low) & (new["time"] < cut)]
    if len(a) != len(b):
        return True
    if not len(a):
        return False
    if not np.array_equal(a["time"].to_numpy(), b["time"].to_numpy()):
        return True
    if not np.array_equal(a["closed"].to_numpy(), b["closed"].to_numpy()):
        return True
    values = (*PRICE_COLUMNS, "volume")
    return not np.array_equal(a[list(values)].to_numpy(float), b[list(values)].to_numpy(float), equal_nan=True)


def lay_rest(stored: pd.DataFrame, tail: pd.DataFrame, timeframe: str, keep: int | None = None) -> pd.DataFrame | None:
    """A short REST tail laid over stored candles that a live stream may have carried further.

    A tail that reaches at least as far as the stored candles is laid as
    ``merge`` lays it (None when it does not connect or opens a new hole:
    the caller then loads the history again). One exception: a tail that
    ends at the stored newest candle and still lists it as forming, while
    the stored one is closed, comes from a copy that lags the exchange's
    own closed flag. The stored closed candle is kept as it is: a closed
    candle is never reopened, and never given values from before its
    close. A tail that ends before the stored candles comes from a copy
    that is behind the stream: its closed candles are final and replace the
    stored ones at the same times; its forming candle is older than what
    the stream gave and is not used, and no stored candle is removed.
    """
    if not tail.empty and not stored.empty and int(tail["time"].iloc[-1]) == int(stored["time"].iloc[-1]):
        if bool(stored["closed"].iloc[-1]) and not bool(tail["closed"].iloc[-1]):
            tail = pd.concat([tail.iloc[:-1], stored.iloc[-1:]], ignore_index=True)
    if tail.empty or stored.empty or int(tail["time"].iloc[-1]) >= int(stored["time"].iloc[-1]):
        return merge(stored, tail, timeframe, keep=keep)
    final = tail[tail["closed"]].set_index("time")
    laid = stored.copy()
    hits = laid["time"].isin(final.index)
    if hits.any():
        times = laid.loc[hits, "time"]
        for column in (*PRICE_COLUMNS, "volume"):
            laid.loc[hits, column] = final.loc[times, column].to_numpy()
        laid.loc[hits, "closed"] = True
    laid = validate(laid, timeframe)
    return laid.iloc[-keep:].reset_index(drop=True) if keep else laid
