"""Signal engine tests: each condition, the thresholds, the levels and the guards."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from backend.app.core import signal as sg

START = 1_794_000_000  # an arbitrary M15 boundary, UTC Unix seconds
M15 = 900


def make_candles(closes, start: int = START, step: int = M15) -> pd.DataFrame:
    """Candles whose open is the previous close and whose range is close/open +- 0.5."""
    closes = np.asarray(closes, dtype=float)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    return pd.DataFrame(
        {
            "time": start + step * np.arange(len(closes)),
            "open": opens,
            "high": np.maximum(opens, closes) + 0.5,
            "low": np.minimum(opens, closes) - 0.5,
            "close": closes,
        }
    )


def accelerating_uptrend(bars: int = 400) -> pd.DataFrame:
    # Every close is higher than the last and the rise speeds up, so the MACD
    # keeps climbing above its signal line. RSI sits at 100 (no losing bar).
    t = np.arange(bars)
    return make_candles(2000 + 0.0005 * t**2)


def accelerating_downtrend(bars: int = 400) -> pd.DataFrame:
    t = np.arange(bars)
    return make_candles(3000 - 0.0005 * t**2)


def uptrend_with_pullbacks(bars: int = 400) -> pd.DataFrame:
    # A speeding-up rise with a small pullback every other bar. The pullbacks
    # keep RSI between 50 and 70, so all five conditions score +1.
    t = np.arange(bars)
    zigzag = 2.0 * np.where(t % 2 == 0, 1.0, -1.0)
    return make_candles(2000 + 2.5e-6 * t**3 + zigzag)


# Neutral inputs: every pair equal, MACD at zero, RSI exactly at the midpoint.
NEUTRAL = dict(
    close=2000.0,
    ema_fast=2000.0,
    ema_mid=2000.0,
    ema_slow=2000.0,
    macd=0.0,
    macd_signal=0.0,
    rsi=50.0,
)


def values(conditions) -> dict[str, int]:
    return {condition.id: condition.value for condition in conditions}


# --- Building blocks ----------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [(2.0, 1.0, 1), (1.0, 2.0, -1), (1.0, 1.0, 0), (2000.0, 2000.0 + 1e-12, 0), (0.0, 1e-12, 0)],
)
def test_compare(a, b, expected):
    assert sg.compare(a, b) == expected


@pytest.mark.parametrize(
    ("rsi", "expected"),
    [
        (50.01, 1), (60.0, 1), (69.99, 1),
        (70.0, 0), (85.0, 0), (100.0, 0),
        (50.0, 0),
        (49.99, -1), (40.0, -1), (30.01, -1),
        (30.0, 0), (15.0, 0), (0.0, 0),
    ],
)  # fmt: skip
def test_rsi_zone(rsi, expected):
    assert sg.rsi_zone(rsi, sg.SignalParams()) == expected


def test_neutral_inputs_score_zero_everywhere():
    conditions = sg.score_conditions(**NEUTRAL)
    assert [condition.id for condition in conditions] == list(sg.CONDITION_IDS)
    assert all(condition.value == 0 for condition in conditions)


@pytest.mark.parametrize(
    ("condition_id", "bullish", "bearish"),
    [
        ("trend_ema200", {"close": 2001.0}, {"close": 1999.0}),
        ("trend_ema20_50", {"ema_fast": 2001.0}, {"ema_fast": 1999.0}),
        # Only the signal line moves; MACD stays at zero, so macd_vs_zero stays neutral.
        ("macd_vs_signal", {"macd_signal": -0.5}, {"macd_signal": 0.5}),
        ("rsi_zone", {"rsi": 60.0}, {"rsi": 40.0}),
    ],
)
def test_each_condition_scores_plus_one_zero_minus_one(condition_id, bullish, bearish):
    up = values(sg.score_conditions(**{**NEUTRAL, **bullish}))
    down = values(sg.score_conditions(**{**NEUTRAL, **bearish}))
    for cid in sg.CONDITION_IDS:
        assert up[cid] == (1 if cid == condition_id else 0)
        assert down[cid] == (-1 if cid == condition_id else 0)


def test_macd_vs_zero_condition():
    # Signal line equal to MACD isolates the zero-line condition.
    up = values(sg.score_conditions(**{**NEUTRAL, "macd": 0.4, "macd_signal": 0.4}))
    down = values(sg.score_conditions(**{**NEUTRAL, "macd": -0.4, "macd_signal": -0.4}))
    assert up == {**dict.fromkeys(sg.CONDITION_IDS, 0), "macd_vs_zero": 1}
    assert down == {**dict.fromkeys(sg.CONDITION_IDS, 0), "macd_vs_zero": -1}


@pytest.mark.parametrize(
    ("score", "expected"),
    [(5, sg.BUY), (4, sg.BUY), (3, sg.BUY), (2, sg.HOLD), (0, sg.HOLD),
     (-2, sg.HOLD), (-3, sg.SELL), (-4, sg.SELL), (-5, sg.SELL)],
)  # fmt: skip
def test_classify_thresholds_at_exactly_three(score, expected):
    assert sg.classify(score) == expected


def test_four_bullish_and_one_bearish_is_exactly_three_and_buys():
    # close > EMA200, EMA20 > EMA50, MACD > signal, MACD > 0, but RSI in the bearish zone.
    conditions = sg.score_conditions(
        close=2010.0, ema_fast=2005.0, ema_mid=2003.0, ema_slow=2000.0,
        macd=0.8, macd_signal=0.5, rsi=45.0,
    )  # fmt: skip
    score = sum(condition.value for condition in conditions)
    assert score == 3
    assert sg.classify(score) == sg.BUY


def test_three_bullish_one_neutral_one_bearish_is_two_and_holds():
    # As above, but MACD equals its signal line (0 points).
    conditions = sg.score_conditions(
        close=2010.0, ema_fast=2005.0, ema_mid=2003.0, ema_slow=2000.0,
        macd=0.8, macd_signal=0.8, rsi=45.0,
    )  # fmt: skip
    score = sum(condition.value for condition in conditions)
    assert score == 2
    assert sg.classify(score) == sg.HOLD


def test_mirrored_inputs_reach_exactly_minus_three_and_sell():
    conditions = sg.score_conditions(
        close=1990.0, ema_fast=1995.0, ema_mid=1997.0, ema_slow=2000.0,
        macd=-0.8, macd_signal=-0.5, rsi=55.0,
    )  # fmt: skip
    score = sum(condition.value for condition in conditions)
    assert score == -3
    assert sg.classify(score) == sg.SELL


# --- Levels -------------------------------------------------------------------


def test_buy_levels():
    # ATR 8: zone 0.25 * 8 = 2, stop 1.5 * 8 = 12, target 2.0 * 8 = 16.
    levels = sg.compute_levels(sg.BUY, entry=4050.0, atr=8.0)
    assert levels == sg.Levels(
        entry=4050.0, zone_low=4048.0, zone_high=4052.0,
        stop_loss=4038.0, take_profit=4066.0, sl_distance=12.0, tp_distance=16.0,
    )  # fmt: skip


def test_sell_levels_mirror_buy_levels():
    levels = sg.compute_levels(sg.SELL, entry=4050.0, atr=8.0)
    assert levels == sg.Levels(
        entry=4050.0, zone_low=4048.0, zone_high=4052.0,
        stop_loss=4062.0, take_profit=4034.0, sl_distance=12.0, tp_distance=16.0,
    )  # fmt: skip


@pytest.mark.parametrize(
    ("side", "entry", "atr"),
    [(sg.HOLD, 4050.0, 8.0), ("buy", 4050.0, 8.0), (sg.BUY, 4050.0, -1.0), (sg.BUY, float("nan"), 8.0)],
)
def test_levels_reject_invalid_input(side, entry, atr):
    with pytest.raises(ValueError):
        sg.compute_levels(side, entry, atr)


# --- evaluate() on candles ----------------------------------------------------


def test_fewer_than_300_closed_candles_gives_no_signal():
    result = sg.evaluate(accelerating_uptrend(299))
    assert result.signal is None
    assert result.reason == sg.INSUFFICIENT_BARS
    assert result.score is None
    assert result.levels is None
    assert result.conditions == ()
    assert result.expires_at is None


def test_exactly_300_closed_candles_is_enough():
    result = sg.evaluate(accelerating_uptrend(300))
    assert result.signal == sg.BUY
    assert result.reason is None


def test_min_bars_below_indicator_warmup_still_gives_no_signal():
    # 100 candles cannot feed a 200-period EMA, whatever min_bars says.
    result = sg.evaluate(accelerating_uptrend(100), sg.SignalParams(min_bars=50))
    assert result.signal is None
    assert result.reason == sg.INSUFFICIENT_BARS


def test_accelerating_uptrend_buys_with_score_four():
    candles = accelerating_uptrend()
    result = sg.evaluate(candles)

    assert result.signal == sg.BUY
    assert result.score == 4
    assert result.strength == pytest.approx(0.8)
    assert values(result.conditions) == {
        "trend_ema200": 1,
        "trend_ema20_50": 1,
        "macd_vs_signal": 1,
        "macd_vs_zero": 1,
        "rsi_zone": 0,  # RSI is 100: overbought earns no point
    }
    assert result.indicators["rsi"] == pytest.approx(100.0)
    assert result.rules_version == sg.RULES_VERSION

    entry = candles["close"].iloc[-1]
    atr = result.indicators["atr"]
    assert result.levels.entry == pytest.approx(entry)
    assert result.levels.stop_loss == pytest.approx(entry - 1.5 * atr)
    assert result.levels.take_profit == pytest.approx(entry + 2.0 * atr)
    assert result.levels.zone_low == pytest.approx(entry - 0.25 * atr)
    assert result.levels.zone_high == pytest.approx(entry + 0.25 * atr)


def test_accelerating_downtrend_sells_with_score_minus_four():
    candles = accelerating_downtrend()
    result = sg.evaluate(candles)

    assert result.signal == sg.SELL
    assert result.score == -4
    assert result.indicators["rsi"] == pytest.approx(0.0)
    entry = candles["close"].iloc[-1]
    atr = result.indicators["atr"]
    assert result.levels.stop_loss == pytest.approx(entry + 1.5 * atr)
    assert result.levels.take_profit == pytest.approx(entry - 2.0 * atr)


def test_uptrend_with_pullbacks_scores_all_five():
    result = sg.evaluate(uptrend_with_pullbacks())
    assert result.signal == sg.BUY
    assert result.score == 5
    assert result.strength == pytest.approx(1.0)
    assert 50 < result.indicators["rsi"] < 70


def test_flat_market_holds_without_levels():
    result = sg.evaluate(make_candles(np.full(400, 2500.0)))
    assert result.signal == sg.HOLD
    assert result.score == 0
    assert result.strength == 0
    assert result.levels is None
    assert result.expires_at is None
    assert result.reason is None


def test_timing_fields_follow_the_last_closed_candle():
    candles = accelerating_uptrend()
    result = sg.evaluate(candles)
    last_open = START + M15 * 399
    assert result.candle_time == last_open
    assert result.signal_time == last_open + M15  # the candle's close
    assert result.expires_at == last_open + M15 + 2 * M15  # valid for two more candles


def hourly_uptrend(bars: int = 400) -> pd.DataFrame:
    return make_candles(2000 + 0.0005 * np.arange(bars) ** 2, step=3600)


def test_timeframe_and_validity_are_configurable():
    params = sg.SignalParams(timeframe_seconds=3600, valid_bars=3)
    result = sg.evaluate(hourly_uptrend(), params)
    assert result.signal_time == result.candle_time + 3600
    assert result.expires_at == result.signal_time + 3 * 3600


def test_params_for_sets_the_candle_length_and_keeps_the_rest():
    base = sg.SignalParams(valid_bars=3, buy_threshold=4)
    hourly = sg.params_for("H1", base)
    assert (hourly.timeframe_seconds, hourly.valid_bars, hourly.buy_threshold) == (3600, 3, 4)
    assert sg.params_for("M15") == sg.SignalParams()
    with pytest.raises(ValueError):
        sg.params_for("M7")


def test_candles_of_another_timeframe_need_matching_params():
    # H1 candles scored with the M15 rule set would get M15 signal and expiry
    # times without anyone noticing, so the mismatch is refused.
    with pytest.raises(ValueError, match="params_for"):
        sg.evaluate(hourly_uptrend())
    result = sg.evaluate(hourly_uptrend(), sg.params_for("H1"))
    assert result.signal == sg.BUY and result.signal_time == result.candle_time + 3600
    # M15 candles with a gap in them are still M15 candles.
    gappy = accelerating_uptrend().drop(index=[50, 51, 52]).reset_index(drop=True)
    assert sg.evaluate(gappy).signal_time == gappy["time"].iloc[-1] + M15


def test_forming_candle_is_ignored():
    closed = accelerating_uptrend().assign(closed=True)
    forming = closed.iloc[[-1]].assign(
        time=closed["time"].iloc[-1] + M15, close=1000.0, low=999.5, closed=False
    )
    with_forming = pd.concat([closed, forming], ignore_index=True)

    assert sg.evaluate(with_forming) == sg.evaluate(closed)


def test_forming_candle_does_not_count_towards_min_bars():
    closed = accelerating_uptrend(299).assign(closed=True)
    forming = closed.iloc[[-1]].assign(time=closed["time"].iloc[-1] + M15, closed=False)
    result = sg.evaluate(pd.concat([closed, forming], ignore_index=True))
    assert result.reason == sg.INSUFFICIENT_BARS


def test_custom_threshold_changes_the_outcome():
    # The same score-4 uptrend no longer buys when 5 points are required.
    result = sg.evaluate(accelerating_uptrend(), sg.SignalParams(buy_threshold=5))
    assert result.score == 4
    assert result.signal == sg.HOLD
    assert result.levels is None


def test_candles_without_time_column_still_evaluate():
    result = sg.evaluate(accelerating_uptrend().drop(columns="time"))
    assert result.signal == sg.BUY
    assert result.candle_time is None
    assert result.signal_time is None
    assert result.expires_at is None


def test_unordered_candles_are_rejected():
    candles = accelerating_uptrend().iloc[::-1]
    with pytest.raises(ValueError, match="oldest first"):
        sg.evaluate(candles)


def test_missing_columns_are_rejected():
    with pytest.raises(ValueError, match="high"):
        sg.evaluate(accelerating_uptrend().drop(columns="high"))


def test_evaluate_does_not_modify_its_input():
    candles = accelerating_uptrend().assign(closed=True)
    before = candles.copy(deep=True)
    sg.evaluate(candles)
    pd.testing.assert_frame_equal(candles, before)


def test_result_serializes_to_json():
    payload = json.loads(json.dumps(sg.evaluate(accelerating_uptrend()).to_dict()))
    assert payload["signal"] == "BUY"
    assert payload["score"] == 4
    assert [item["id"] for item in payload["conditions"]] == list(sg.CONDITION_IDS)
    assert set(payload["levels"]) == {
        "entry", "zone_low", "zone_high", "stop_loss", "take_profit", "sl_distance", "tp_distance",
    }  # fmt: skip

    empty = json.loads(json.dumps(sg.evaluate(accelerating_uptrend(10)).to_dict()))
    assert empty["signal"] is None
    assert empty["reason"] == "INSUFFICIENT_BARS"
    assert empty["levels"] is None


# --- Parameters ---------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"buy_threshold": -3, "sell_threshold": 3},
        {"rsi_lower": 60.0},
        {"sl_atr_mult": 0.0},
    ],
)
def test_invalid_params_are_rejected(kwargs):
    with pytest.raises(ValueError):
        sg.SignalParams(**kwargs)
