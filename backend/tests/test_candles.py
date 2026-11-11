"""Candle frames: validation, gap detection and M15 -> H1 aggregation."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from backend.app.core import candles as cd

T0 = 1_793_998_800  # an H1 (and so M15) boundary, UTC Unix seconds
M15 = 900
H1 = 3600


def m15(n: int, start: int = T0, *, last_open: bool = False, volume: bool = True) -> pd.DataFrame:
    """n consecutive M15 candles; candle i has open 100+i, close 100.5+i."""
    i = np.arange(n, dtype=float)
    frame = pd.DataFrame(
        {
            "time": start + M15 * np.arange(n),
            "open": 100 + i,
            "high": 101 + i,
            "low": 99 + i,
            "close": 100.5 + i,
            "volume": np.full(n, 2.0) if volume else np.full(n, np.nan),
            "closed": True,
        }
    )
    if last_open:
        frame.loc[n - 1, "closed"] = False
    return frame


# --- timeframes -------------------------------------------------------------


def test_timeframe_seconds():
    assert cd.timeframe_seconds("M15") == 900
    assert cd.timeframe_seconds("H1") == 3600
    with pytest.raises(ValueError, match="unknown timeframe"):
        cd.timeframe_seconds("M7")


# --- validate ---------------------------------------------------------------


def test_validate_fills_optional_columns_and_orders_them():
    frame = m15(3).drop(columns=["volume", "closed"])
    out = cd.validate(frame, "M15")
    assert list(out.columns) == list(cd.CANDLE_COLUMNS)
    assert out["closed"].all()
    assert out["volume"].isna().all()
    assert out["time"].dtype == "int64"


def test_validate_does_not_modify_input():
    frame = m15(3).drop(columns=["volume"])
    cd.validate(frame, "M15")
    assert "volume" not in frame.columns


def test_validate_accepts_empty_frame():
    out = cd.validate(m15(0), "M15")
    assert out.empty and list(out.columns) == list(cd.CANDLE_COLUMNS)


@pytest.mark.parametrize(
    "change, message",
    [
        (lambda f: f.drop(columns=["close"]), "missing columns"),
        (lambda f: f.assign(time=f["time"] + 60), "aligned"),
        (lambda f: f.iloc[[1, 0, 2]], "strictly increasing"),
        (lambda f: f.iloc[[0, 1, 1]], "strictly increasing"),
        (lambda f: f.assign(high=f["low"] - 1), "high and low"),
        (lambda f: f.assign(low=f["close"] + 1), "high and low"),
        (lambda f: f.assign(close=[1.0, math.nan, 2.0]), "positive and finite"),
        (lambda f: f.assign(open=[0.0, 1.0, 1.0]), "positive and finite"),
        (lambda f: f.assign(closed=[True, False, True]), "only the last candle"),
        (lambda f: f.assign(closed=["True", "True", "True"]), "boolean"),
    ],
)
def test_validate_rejects_bad_frames(change, message):
    with pytest.raises(ValueError, match=message):
        cd.validate(change(m15(3)), "M15")


def test_validate_allows_the_last_candle_to_be_forming():
    out = cd.validate(m15(3, last_open=True), "M15")
    assert out["closed"].tolist() == [True, True, False]


# --- gaps -------------------------------------------------------------------


def test_find_gaps_none_for_a_continuous_series():
    assert cd.find_gaps(m15(10), "M15") == []


def test_find_gaps_reports_missing_candles():
    frame = m15(10).drop(index=[3, 4, 7]).reset_index(drop=True)
    gaps = cd.find_gaps(frame, "M15")
    assert gaps == [
        cd.Gap(after=T0 + 2 * M15, before=T0 + 5 * M15, missing=2),
        cd.Gap(after=T0 + 6 * M15, before=T0 + 8 * M15, missing=1),
    ]


# --- aggregate --------------------------------------------------------------


def test_aggregate_m15_to_h1_complete_hours():
    out = cd.aggregate(m15(8), "M15", "H1")
    assert out["time"].tolist() == [T0, T0 + H1]
    first = out.iloc[0]
    assert first["open"] == 100.0  # open of the first M15
    assert first["close"] == 103.5  # close of the fourth M15
    assert first["high"] == 104.0
    assert first["low"] == 99.0
    assert first["volume"] == 8.0
    assert out["closed"].tolist() == [True, True]


def test_aggregate_marks_the_forming_hour_as_not_closed():
    # 6 candles: the second hour has only 2 of 4 quarters so far.
    out = cd.aggregate(m15(6), "M15", "H1")
    assert out["closed"].tolist() == [True, False]
    assert out.iloc[1]["close"] == 105.5


def test_aggregate_hour_with_a_forming_quarter_is_not_closed():
    out = cd.aggregate(m15(8, last_open=True), "M15", "H1")
    assert out["closed"].tolist() == [True, False]


def test_aggregate_drops_an_hour_with_a_missing_quarter():
    # A quarter missing inside hour 2: that hour is not built from 3 candles.
    frame = m15(12).drop(index=[5]).reset_index(drop=True)
    out = cd.aggregate(frame, "M15", "H1")
    assert out["time"].tolist() == [T0, T0 + 2 * H1]
    assert out["closed"].all()


def test_aggregate_drops_a_partial_first_hour():
    # History that starts at a quarter past: the first hour is incomplete.
    out = cd.aggregate(m15(7, start=T0 + M15), "M15", "H1")
    assert out["time"].tolist() == [T0 + H1]
    assert out["closed"].all()


def test_aggregate_keeps_volume_missing_when_the_source_has_none():
    out = cd.aggregate(m15(4, volume=False), "M15", "H1")
    assert math.isnan(out.iloc[0]["volume"])


def test_aggregate_output_feeds_the_signal_engine_shape():
    out = cd.aggregate(m15(8), "M15", "H1")
    assert list(out.columns) == list(cd.CANDLE_COLUMNS)


@pytest.mark.parametrize("source, target", [("H1", "M15"), ("M15", "M15"), ("M30", "M45")])
def test_aggregate_rejects_impossible_timeframes(source, target):
    with pytest.raises(ValueError):
        cd.aggregate(m15(8), source, target)
