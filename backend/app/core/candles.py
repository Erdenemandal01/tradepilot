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

import numpy as np
import pandas as pd

TIMEFRAME_SECONDS = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "M30": 1800,
    "H1": 3600,
    "H4": 14400,
    "D1": 86400,
}

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
    try:
        return TIMEFRAME_SECONDS[timeframe]
    except KeyError:
        known = ", ".join(TIMEFRAME_SECONDS)
        raise ValueError(f"unknown timeframe {timeframe!r}; known: {known}") from None


def validate(candles: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Check a candle frame and return a normalised copy in the standard shape.

    Raises ValueError when a rule is broken. Adds ``volume`` (NaN) and
    ``closed`` (True) when they are absent. Never modifies the input.
    """
    step = timeframe_seconds(timeframe)
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
    if (frame["time"] % step != 0).any():
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

    if not pd.api.types.is_bool_dtype(frame["closed"]):
        raise ValueError("the closed column must be boolean")
    if (~frame["closed"].iloc[:-1]).any():
        raise ValueError("only the last candle may still be forming")
    return frame.astype(_DTYPES)


def find_gaps(candles: pd.DataFrame, timeframe: str) -> list[Gap]:
    """List every place where candles are missing between two present ones."""
    step = timeframe_seconds(timeframe)
    times = validate(candles, timeframe)["time"].to_numpy()
    diffs = np.diff(times)
    return [
        Gap(after=int(times[i]), before=int(times[i + 1]), missing=int(diffs[i] // step - 1))
        for i in np.nonzero(diffs > step)[0]
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
