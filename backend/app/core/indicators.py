"""Technical indicators as pure functions over pandas Series.

The formulas are fixed here so results are reproducible and testable:

- EMA: recursive, alpha = 2 / (period + 1), seeded with the first value.
  The first ``period - 1`` outputs are NaN.
- RSI and ATR: Wilder's smoothing. The first average is the simple mean of
  the first ``period`` inputs; after that
  ``average = (previous * (period - 1) + value) / period``.
- MACD: EMA(fast) - EMA(slow); the signal line is the EMA of the MACD line.

A trading platform may smooth some of these differently, so a small
difference against a platform's own indicator window is not a bug by itself.
Compare formulas before treating a mismatch as an error.

Every function returns a new object aligned to the input index and never
modifies its inputs.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _require_period(period: int, name: str = "period") -> None:
    if isinstance(period, bool) or not isinstance(period, int) or period < 1:
        raise ValueError(f"{name} must be a positive integer, got {period!r}")


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average, seeded with the first value."""
    _require_period(period)
    return series.astype(float).ewm(span=period, adjust=False, min_periods=period).mean()


def wilder_smooth(series: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing: SMA seed over the first ``period`` values, then recursive.

    Leading NaNs are skipped. The series must have no gaps after its first
    valid value; a gap turns the rest of the output into NaN.
    """
    _require_period(period)
    values = series.to_numpy(dtype=float)
    out = np.full(values.shape, np.nan)
    valid = np.flatnonzero(~np.isnan(values))
    if valid.size >= period:
        seed_end = int(valid[0]) + period
        average = float(values[seed_end - period : seed_end].mean())
        out[seed_end - 1] = average
        for i in range(seed_end, values.size):
            average = (average * (period - 1) + values[i]) / period
            out[i] = average
    return pd.Series(out, index=series.index, name=series.name)


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index with Wilder's smoothing. First value at index ``period``.

    With no losses in the window the result is 100; with no movement at all
    it is 50.
    """
    _require_period(period)
    delta = close.astype(float).diff()
    avg_gain = wilder_smooth(delta.clip(lower=0.0), period).to_numpy()
    avg_loss = wilder_smooth((-delta).clip(lower=0.0), period).to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        value = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    no_loss = avg_loss == 0.0
    value = np.where(no_loss & (avg_gain > 0.0), 100.0, value)
    value = np.where(no_loss & (avg_gain == 0.0), 50.0, value)
    return pd.Series(value, index=close.index, name="rsi")


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    """MACD line, signal line and histogram as columns ``macd``, ``signal``, ``hist``."""
    _require_period(fast, "fast")
    _require_period(slow, "slow")
    _require_period(signal, "signal")
    if fast >= slow:
        raise ValueError(f"fast ({fast}) must be smaller than slow ({slow})")
    line = ema(close, fast) - ema(close, slow)
    signal_line = ema(line, signal)
    return pd.DataFrame(
        {"macd": line, "signal": signal_line, "hist": line - signal_line},
        index=close.index,
    )


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """True range. The first bar has no previous close, so it is ``high - low``."""
    high = high.astype(float)
    low = low.astype(float)
    previous_close = close.astype(float).shift(1)
    ranges = pd.concat(
        [high - low, (high - previous_close).abs(), (low - previous_close).abs()],
        axis=1,
    )
    return ranges.max(axis=1).rename("true_range")


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Average True Range with Wilder's smoothing. First value at index ``period - 1``."""
    _require_period(period)
    return wilder_smooth(true_range(high, low, close), period).rename("atr")
