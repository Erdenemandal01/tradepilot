"""Indicator tests against values worked out by hand.

Each expected value is derived in the comment above its test, so a reader can
check the arithmetic without running the code.
"""

from __future__ import annotations

from fractions import Fraction

import numpy as np
import pandas as pd
import pytest

from backend.app.core import indicators

NAN = float("nan")


def assert_series_close(actual: pd.Series, expected: list[float], tol: float = 1e-9) -> None:
    assert len(actual) == len(expected)
    np.testing.assert_allclose(actual.to_numpy(dtype=float), expected, rtol=0, atol=tol, equal_nan=True)


# --- EMA ---------------------------------------------------------------------


def test_ema_hand_computed():
    # period 3 -> alpha = 2 / (3 + 1) = 0.5, seeded with the first value:
    # 1 -> 1.5 -> 2.25 -> 3.125 -> 4.0625. The first two outputs are masked.
    result = indicators.ema(pd.Series([1, 2, 3, 4, 5]), 3)
    assert_series_close(result, [NAN, NAN, 2.25, 3.125, 4.0625])


def test_ema_of_constant_series_is_constant():
    result = indicators.ema(pd.Series([7.5] * 30), 10)
    assert_series_close(result.iloc[9:], [7.5] * 21)
    assert result.iloc[:9].isna().all()


def test_ema_period_one_returns_the_series():
    values = [3.0, 1.0, 4.0, 1.0, 5.0]
    assert_series_close(indicators.ema(pd.Series(values), 1), values)


# --- Wilder smoothing --------------------------------------------------------


def test_wilder_smooth_hand_computed():
    # period 3: seed = mean(3, 6, 9) = 6 at index 2,
    # then (6 * 2 + 12) / 3 = 8 and (8 * 2 + 2) / 3 = 6.
    result = indicators.wilder_smooth(pd.Series([3, 6, 9, 12, 2]), 3)
    assert_series_close(result, [NAN, NAN, 6.0, 8.0, 6.0])


def test_wilder_smooth_skips_leading_nan():
    result = indicators.wilder_smooth(pd.Series([NAN, 3, 6, 9, 12]), 3)
    assert_series_close(result, [NAN, NAN, NAN, 6.0, 8.0])


def test_wilder_smooth_too_short_is_all_nan():
    assert indicators.wilder_smooth(pd.Series([1.0, 2.0]), 3).isna().all()


# --- RSI ---------------------------------------------------------------------


def test_rsi_hand_computed():
    # closes 10, 11, 10, 12, 11 -> changes +1, -1, +2, -1; period 2.
    # index 2: avg gain (1 + 0) / 2 = 0.5, avg loss (0 + 1) / 2 = 0.5 -> RS 1 -> RSI 50
    # index 3: avg gain (0.5 + 2) / 2 = 1.25, avg loss (0.5 + 0) / 2 = 0.25 -> RS 5 -> 100 - 100/6
    # index 4: avg gain (1.25 + 0) / 2 = 0.625, avg loss (0.25 + 1) / 2 = 0.625 -> RS 1 -> RSI 50
    result = indicators.rsi(pd.Series([10, 11, 10, 12, 11]), 2)
    assert_series_close(result, [NAN, NAN, 50.0, 100 - 100 / 6, 50.0])


def test_rsi_matches_published_14_period_example():
    # The 14-period worked example found in many RSI tutorials: 33 closes,
    # first RSI at the 15th close. Published values are rounded to 2 decimals.
    closes = [
        44.3389, 44.0902, 44.1497, 43.6124, 44.3278, 44.8264, 45.0955, 45.4245,
        45.8433, 46.0826, 45.8931, 46.0328, 45.6140, 46.2820, 46.2820, 46.0028,
        46.0328, 46.4116, 46.2222, 45.6439, 46.2122, 46.2521, 45.7137, 46.4515,
        45.7835, 45.3548, 44.0288, 44.1783, 44.2181, 44.5672, 43.4205, 42.6628,
        43.1314,
    ]  # fmt: skip
    published = [
        70.53, 66.32, 66.55, 69.41, 66.36, 57.97, 62.93, 63.26, 56.06, 62.38,
        54.71, 50.42, 39.99, 41.46, 41.87, 45.46, 37.30, 33.08, 37.77,
    ]  # fmt: skip
    result = indicators.rsi(pd.Series(closes), 14)
    assert result.iloc[:14].isna().all()
    assert_series_close(result.iloc[14:].round(2), published, tol=1e-9)


def test_rsi_extremes():
    rising = indicators.rsi(pd.Series(np.arange(1.0, 41.0)), 14)
    falling = indicators.rsi(pd.Series(np.arange(40.0, 0.0, -1.0)), 14)
    flat = indicators.rsi(pd.Series([5.0] * 40), 14)
    assert_series_close(rising.iloc[14:], [100.0] * 26)
    assert_series_close(falling.iloc[14:], [0.0] * 26)
    assert_series_close(flat.iloc[14:], [50.0] * 26)


def test_rsi_stays_between_0_and_100():
    rng = np.random.default_rng(7)
    closes = pd.Series(2000 + rng.normal(0, 3, 500).cumsum())
    values = indicators.rsi(closes, 14).dropna()
    assert len(values) == 500 - 14
    assert values.between(0, 100).all()


# --- MACD --------------------------------------------------------------------


def test_macd_hand_computed():
    # closes 1..5 with fast 2, slow 3, signal 2, in exact fractions.
    # EMA2 (alpha 2/3): 1, 5/3, 23/9, 95/27, 365/81
    # EMA3 (alpha 1/2): 1, 3/2, 9/4, 25/8, 65/16
    # MACD = EMA2 - EMA3, valid once EMA3 is (index 2): 11/36, 85/216, 575/1296
    # signal = EMA2 of MACD, seeded at index 2, shown from index 3: 59/162, 811/1944
    result = indicators.macd(pd.Series([1, 2, 3, 4, 5]), fast=2, slow=3, signal=2)
    assert list(result.columns) == ["macd", "signal", "hist"]
    assert_series_close(
        result["macd"],
        [NAN, NAN, float(Fraction(11, 36)), float(Fraction(85, 216)), float(Fraction(575, 1296))],
    )
    assert_series_close(
        result["signal"],
        [NAN, NAN, NAN, float(Fraction(59, 162)), float(Fraction(811, 1944))],
    )
    assert_series_close(
        result["hist"],
        [NAN, NAN, NAN, float(Fraction(19, 648)), float(Fraction(103, 3888))],
    )


def test_macd_default_warmup_lengths():
    result = indicators.macd(pd.Series(np.linspace(2000, 2100, 60)))
    assert result["macd"].first_valid_index() == 25  # slow EMA needs 26 closes
    assert result["signal"].first_valid_index() == 33  # plus 9 MACD values


def test_macd_rejects_fast_not_below_slow():
    with pytest.raises(ValueError):
        indicators.macd(pd.Series([1.0, 2.0, 3.0]), fast=26, slow=12)


# --- True range and ATR ------------------------------------------------------


def test_true_range_and_atr_hand_computed():
    # bars (high, low, close): (10, 8, 9) (11, 9, 10) (12, 10, 11) (11, 7, 8)
    # TR: 10-8 = 2 | max(2, |11-9|, |9-9|) = 2 | max(2, |12-10|, |10-10|) = 2
    #     | max(11-7, |11-11|, |7-11|) = 4
    # ATR period 2: seed (2 + 2) / 2 = 2 at index 1, then (2 + 2) / 2 = 2, (2 + 4) / 2 = 3
    high = pd.Series([10, 11, 12, 11])
    low = pd.Series([8, 9, 10, 7])
    close = pd.Series([9, 10, 11, 8])
    assert_series_close(indicators.true_range(high, low, close), [2.0, 2.0, 2.0, 4.0])
    assert_series_close(indicators.atr(high, low, close, 2), [NAN, 2.0, 2.0, 3.0])


def test_true_range_uses_gap_from_previous_close():
    # The second bar's own range is 1, but it gapped 5 above the previous close.
    high = pd.Series([10.0, 16.0])
    low = pd.Series([9.0, 15.0])
    close = pd.Series([10.0, 15.5])
    assert_series_close(indicators.true_range(high, low, close), [1.0, 6.0])


# --- Shared behaviour --------------------------------------------------------


@pytest.mark.parametrize("period", [0, -1, 2.5, True])
def test_invalid_period_is_rejected(period):
    series = pd.Series([1.0, 2.0, 3.0])
    with pytest.raises(ValueError):
        indicators.ema(series, period)
    with pytest.raises(ValueError):
        indicators.rsi(series, period)
    with pytest.raises(ValueError):
        indicators.atr(series, series, series, period)


def test_inputs_are_not_modified_and_index_is_kept():
    index = pd.Index([100, 200, 300, 400, 500, 600], name="time")
    close = pd.Series([10.0, 11.0, 10.5, 12.0, 11.5, 13.0], index=index)
    high = close + 0.5
    low = close - 0.5
    before = (close.copy(), high.copy(), low.copy())

    outputs = [
        indicators.ema(close, 3),
        indicators.rsi(close, 3),
        indicators.atr(high, low, close, 3),
        indicators.macd(close, 2, 3, 2),
    ]

    for output in outputs:
        assert output.index.equals(index)
    pd.testing.assert_series_equal(close, before[0])
    pd.testing.assert_series_equal(high, before[1])
    pd.testing.assert_series_equal(low, before[2])
