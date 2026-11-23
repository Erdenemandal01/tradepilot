"""The forecast model: features without look-ahead, a small auditable fit, an honest record.

Every series here is made up with a seeded generator. Two kinds matter: one
with a planted pattern (each move tends to continue), which a model can
learn, and a pure random walk, on which nothing can be learnt and on which
the evaluation must not claim anything.
"""

from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd
import pytest

from backend.app.core import candles as cd
from backend.app.core import filters as fl
from backend.app.core import forecast as fc
from backend.app.core.pricecheck import probably_closed
from backend.app.core.signal import evaluate

M15 = 900
START = 1_794_000_000 - 1_794_000_000 % 86400  # midnight UTC


def series(count: int, carry: float = 0.0, seed: int = 1, start: int = START) -> pd.DataFrame:
    """Closed M15 candles. ``carry`` is how much of each move continues into the next one."""
    rng = np.random.default_rng(seed)
    noise = rng.normal(0.0, 1.0, count)
    moves = np.empty(count)
    previous = 0.0
    for i in range(count):
        previous = carry * previous + noise[i]
        moves[i] = previous
    close = 4000.0 + np.cumsum(moves)
    opened = np.concatenate([[4000.0], close[:-1]])
    frame = pd.DataFrame(
        {
            "time": start + M15 * np.arange(count),
            "open": opened,
            "high": np.maximum(opened, close) + 0.3 * np.abs(rng.normal(0.0, 1.0, count)),
            "low": np.minimum(opened, close) - 0.3 * np.abs(rng.normal(0.0, 1.0, count)),
            "close": close,
            "volume": 1.0 + rng.random(count),
            "closed": True,
        }
    )
    return cd.validate(frame, "M15")


WALK = series(2000, seed=3)  # long enough for the H1 score: 300 closed H1 candles and some more
PLANTED = series(6000, carry=0.5, seed=11)
RANDOM = series(6000, carry=0.0, seed=12)
SMALL = {"min_train": 2500, "block": 1000}


@pytest.fixture(scope="module")
def planted_model() -> dict:
    return fc.fit(PLANTED)


# --- features ---------------------------------------------------------------------


def test_the_features_cover_every_side_the_project_has():
    names = fc.FEATURE_NAMES
    assert len(names) == len(set(names))
    for wanted in (
        "rule_score", "cond_trend_ema200", "cond_trend_ema20_50", "cond_macd_vs_signal", "cond_macd_vs_zero",
        "cond_rsi_zone", "h1_score", "rsi", "macd_hist", "dist_ema20", "dist_ema50", "dist_ema200", "atr_share",
        "ret_1", "ret_4", "ret_16", "ret_96", "body", "wick_up", "wick_down", "volume", "h4_trend", "d1_trend",
        "hour_sin", "hour_cos", "day_sin", "day_cos", "market_closed",
    ):  # fmt: skip
        assert wanted in names, wanted
    data = fc.dataset(WALK)
    assert data.features.shape == (len(WALK), len(names))
    assert data.usable[-1] and not data.usable[0]  # the first rows wait for the indicators to warm up
    assert np.isfinite(data.features[data.usable]).all()


def test_the_rule_score_and_conditions_are_those_of_the_signal_engine():
    data = fc.dataset(WALK)
    names = list(fc.FEATURE_NAMES)
    for end in (300, 301, 777, 1203, 1500, 1999, 2000):
        result = evaluate(WALK.iloc[:end])
        row = data.features[end - 1]
        assert row[names.index("rule_score")] == result.score
        for condition in result.conditions:
            assert row[names.index(f"cond_{condition.id}")] == condition.value
    assert math.isnan(data.features[298, names.index("rule_score")])  # the rule needs 300 candles


def test_the_h1_score_is_the_one_the_filter_uses():
    column = list(fc.FEATURE_NAMES).index("h1_score")
    data = fc.dataset(WALK)
    for end in (1203, 1204, 1205, 1640, 1999, 2000):
        decision = fl.replay(WALK.iloc[:end], "M15", 1)[0]
        value = data.features[end - 1, column]
        if decision.h1_score is None:
            assert math.isnan(value)
        else:
            assert value == decision.h1_score
    assert math.isnan(data.features[1000, column])  # fewer than 300 closed H1 candles


def test_the_clock_features_are_taken_at_the_candles_close():
    names = list(fc.FEATURE_NAMES)
    data = fc.dataset(WALK)
    for row in (1300, 1337, 1999):
        made_at = int(WALK["time"].iloc[row]) + M15
        assert data.made_at[row] == made_at
        hour = (made_at % 86400) / 86400
        assert data.features[row, names.index("hour_sin")] == pytest.approx(math.sin(2 * math.pi * hour))
        assert data.features[row, names.index("hour_cos")] == pytest.approx(math.cos(2 * math.pi * hour))
        assert data.features[row, names.index("market_closed")] == float(probably_closed(made_at))


def test_a_feature_row_is_the_same_whether_or_not_later_candles_exist():
    whole = fc.dataset(WALK)
    for end in (1250, 1301, 1500, 1777, 1999):
        part = fc.dataset(WALK.iloc[:end])
        np.testing.assert_array_equal(part.features[-1], whole.features[end - 1])
        np.testing.assert_array_equal(part.features, whole.features[:end])
        assert part.atr[-1] == whole.atr[end - 1] and part.base[-1] == whole.base[end - 1]


def test_a_candle_that_is_still_forming_changes_nothing():
    closed = WALK.iloc[:1500]
    forming = WALK.iloc[:1501].copy()
    last = forming.index[-1]
    forming.loc[last, ["open", "high", "low", "close", "volume"]] = [9000.0, 9900.0, 8000.0, 9500.0, 999.0]
    forming.loc[last, "closed"] = False
    one, two = fc.dataset(closed), fc.dataset(forming)
    np.testing.assert_array_equal(one.features, two.features)
    np.testing.assert_array_equal(one.targets, two.targets)
    model = fc.fit(PLANTED.iloc[:3000])
    assert fc.predict(model, closed) == fc.predict(model, forming)


def test_targets_are_the_next_four_closes_in_atr_units_and_unknown_at_the_end():
    data = fc.dataset(WALK)
    close = WALK["close"].to_numpy()
    for row in (1300, 1500):
        for step in range(1, fc.HORIZON + 1):
            expected = (close[row + step] - close[row]) / data.atr[row]
            assert data.targets[row, step - 1] == pytest.approx(expected)
    assert np.isnan(data.targets[-1]).all()  # nothing after the last candle is known
    assert math.isnan(data.targets[-4, 3]) and not math.isnan(data.targets[-5, 3])
    assert not data.scored[-4:].any() and data.scored[-5]


def test_a_hole_in_the_history_is_never_bridged_by_a_target_or_a_return():
    holed = pd.concat([WALK.iloc[:1500], WALK.iloc[1503:]], ignore_index=True)
    data = fc.dataset(holed)
    names = list(fc.FEATURE_NAMES)
    assert np.isnan(data.targets[1499]).all()  # the next candle is missing
    assert math.isnan(data.targets[1497, 3]) and not math.isnan(data.targets[1497, 1])
    assert math.isnan(data.features[1500, names.index("ret_1")])  # the candle before it is missing
    assert not data.usable[1500] and data.usable[1499]


# --- fit and predict -----------------------------------------------------------------


def test_the_model_is_plain_numbers_and_survives_json(planted_model):
    stored = json.loads(json.dumps(planted_model, allow_nan=False))
    assert stored == planted_model
    assert stored["version"] == fc.MODEL_VERSION and stored["features"] == list(fc.FEATURE_NAMES)
    assert stored["timeframe"] == "M15" and stored["horizon"] == 4
    assert fc.predict(stored, PLANTED) == fc.predict(planted_model, PLANTED)


def test_the_same_candles_give_the_same_numbers(planted_model):
    assert fc.fit(PLANTED) == planted_model
    assert fc.predict(planted_model, PLANTED) == fc.predict(planted_model, PLANTED.copy())


def test_a_forecast_is_four_points_on_the_time_axis_with_a_band(planted_model):
    forecast = fc.predict(planted_model, PLANTED)
    last = PLANTED.iloc[-1]
    assert forecast["made_at"] == int(last["time"]) + M15 and forecast["base_price"] == float(last["close"])
    points = forecast["points"]
    assert [p["time"] for p in points] == [forecast["made_at"] + M15 * i for i in range(4)]
    for point in points:
        assert set(point) == {"time", "close", "low", "high"}
        assert point["low"] < point["close"] < point["high"]
    widths = [p["high"] - p["low"] for p in points]
    assert widths == sorted(widths)  # the further ahead, the less is known
    assert 0.0 < forecast["up_probability"] < 1.0
    # Changed on purpose (second review, P4): the word needs the line's agreement, see fc.call.
    moved = points[-1]["close"] - forecast["base_price"]
    assert forecast["direction"] == fc.call(forecast["up_probability"], moved)
    assert forecast["atr"] > 0 and abs(moved) < 20 * forecast["atr"]


def test_the_direction_word_never_contradicts_the_drawn_line(planted_model):
    edge = fc.FLAT_MARGIN
    assert fc.call(0.9, 1.5) == "up" and fc.call(0.1, -1.5) == "down"
    assert fc.call(0.9, -1.5) == "flat" and fc.call(0.1, 1.5) == "flat"  # the two fits disagree: no call
    assert fc.call(0.9, 0.0) == "flat" and fc.call(0.1, 0.0) == "flat"  # a line that ends where it began
    assert fc.call(0.5 + edge / 2, 1.5) == "flat" and fc.call(0.5 - edge / 2, -1.5) == "flat"

    # The same through predict, with the two fits of a real model set against each other.
    honest = fc.predict(planted_model, PLANTED)
    lean = 30.0 if honest["points"][-1]["close"] < honest["base_price"] else -30.0
    against = {**planted_model, "up_coef": [lean] + [0.0] * len(fc.FEATURE_NAMES)}
    forecast = fc.predict(against, PLANTED)
    assert forecast["points"] == honest["points"]  # the line is the same
    assert (forecast["up_probability"] > 0.99) == (lean > 0) and forecast["direction"] == "flat"
    agreeing = {**planted_model, "up_coef": [-lean] + [0.0] * len(fc.FEATURE_NAMES)}
    assert fc.predict(agreeing, PLANTED)["direction"] == ("down" if lean > 0 else "up")
    for end in range(5000, 6000, 50):  # and on ordinary forecasts the word and the line never disagree
        made = fc.predict(planted_model, PLANTED.iloc[:end])
        moved = made["points"][-1]["close"] - made["base_price"]
        assert made["direction"] == "flat" or (made["direction"] == "up") == (moved > 0)


def test_how_often_a_model_itself_says_up_or_down_is_counted_with_the_rule_of_the_shown_word(planted_model):
    # The walk-forward record is that of other fits. Each chooses its own strength, so it cannot say
    # how often THIS model leaves the flat zone. That is counted on the model itself, word by word.
    candles = PLANTED.iloc[:1320]
    data = fc.dataset(candles)
    rows = np.flatnonzero(data.scored)  # the rows a fit on these candles uses
    words = [fc.predict(planted_model, candles.iloc[: row + 1])["direction"] for row in rows]
    closed = [probably_closed(int(data.made_at[row])) for row in rows]
    counted = fc.own_calls(planted_model, candles)
    assert set(counted) == {"rows", "up", "down", "open_market_rows", "open_market_calls"}
    assert counted["rows"] == len(rows) > 90
    assert (counted["up"], counted["down"]) == (words.count("up"), words.count("down"))
    assert min(counted["up"], counted["down"]) > 0 and "flat" in words  # all three words occur here
    assert 0 < counted["open_market_rows"] == closed.count(False) < len(rows)  # the daily break is in it
    assert counted["open_market_calls"] == sum(word != "flat" and not shut for word, shut in zip(words, closed))
    assert json.loads(json.dumps(counted)) == counted  # plain whole numbers

    assert fc.own_calls(planted_model, PLANTED.iloc[:1000]) == {
        "rows": 0, "up": 0, "down": 0, "open_market_rows": 0, "open_market_calls": 0,
    }  # fmt: skip
    with pytest.raises(fc.ModelMismatch):
        fc.own_calls({**planted_model, "version": fc.MODEL_VERSION + 1}, candles)


def test_a_model_whose_band_is_upside_down_is_refused(planted_model):
    fc.check(planted_model)  # a fitted model passes
    low, high = planted_model["band_low"], planted_model["band_high"]
    assert all(a < b for a, b in zip(low, high))
    with pytest.raises(fc.ModelMismatch):  # the 10% quantile above the 90% one: the model contradicts itself
        fc.check({**planted_model, "band_low": high, "band_high": low})
    crossed = {**planted_model, "band_low": [*low[:3], high[3] + 0.01]}  # one step is enough
    with pytest.raises(fc.ModelMismatch):
        fc.predict(crossed, PLANTED)


def test_the_planted_pattern_is_what_the_model_picks_up(planted_model):
    # After an up move this series tends to go on up: the forecasts must follow the last move.
    closes = PLANTED["close"].to_numpy()
    agree = total = 0
    for end in range(5000, 6000, 20):
        forecast = fc.predict(planted_model, PLANTED.iloc[:end])
        moved = closes[end - 1] - closes[end - 2]
        if abs(moved) > 1.0:
            total += 1
            agree += (forecast["points"][-1]["close"] > forecast["base_price"]) == (moved > 0)
    assert total >= 10 and agree / total > 0.9


def test_no_call_is_made_close_to_a_coin_toss():
    edge = fc.FLAT_MARGIN
    assert fc.direction(0.5) == "flat" and fc.direction(0.5 + edge / 2) == "flat" and fc.direction(0.5 - edge / 2) == "flat"
    assert fc.direction(0.5 + edge + 1e-9) == "up" and fc.direction(0.5 - edge - 1e-9) == "down"
    assert 0.0 < edge < 0.2


def test_predict_refuses_a_model_of_another_version_or_with_other_features(planted_model):
    with pytest.raises(fc.ModelMismatch):
        fc.predict({**planted_model, "version": fc.MODEL_VERSION + 1}, PLANTED)
    with pytest.raises(fc.ModelMismatch):
        fc.predict({**planted_model, "features": planted_model["features"][:-1]}, PLANTED)
    swapped = list(planted_model["features"])
    swapped[0], swapped[1] = swapped[1], swapped[0]
    with pytest.raises(fc.ModelMismatch):
        fc.predict({**planted_model, "features": swapped}, PLANTED)
    with pytest.raises(fc.ModelMismatch):
        fc.predict({**planted_model, "coef": [row[:-1] for row in planted_model["coef"]]}, PLANTED)
    with pytest.raises(fc.ModelMismatch):
        fc.predict({"version": fc.MODEL_VERSION}, PLANTED)
    with pytest.raises(fc.ModelMismatch):
        fc.predict(None, PLANTED)


def test_with_too_few_candles_there_is_no_forecast_and_no_fit(planted_model):
    assert fc.predict(planted_model, PLANTED.iloc[:1000]) is None  # the H1 score does not exist yet
    assert fc.predict(planted_model, PLANTED.iloc[:0]) is None
    with pytest.raises(ValueError):
        fc.fit(PLANTED.iloc[:1400])


# --- walk-forward evaluation -----------------------------------------------------------


def test_the_blocks_keep_a_gap_so_no_training_target_reaches_into_what_is_scored():
    positions = np.array([i for i in range(100, 1000) if i % 7])  # candle numbers with some rows missing
    blocks = fc.splits(positions, min_train=200, block=150, gap=fc.HORIZON)
    assert len(blocks) >= 3
    scored: list[int] = []
    for train, test in blocks:
        first = positions[test[0]]
        assert 200 - fc.HORIZON <= len(train) < test[0]  # the first fit sees min_train rows less the gap
        # A training row's last target is the close HORIZON candles on: it lies before the first scored candle.
        assert positions[train].max() + fc.HORIZON < first
        assert positions[train].max() >= first - fc.HORIZON - 2  # and nothing more than needed is thrown away
        assert len(test) <= 150
        scored += list(test)
    assert scored == list(range(scored[0], len(positions)))  # every later row is scored exactly once
    grown = [len(train) for train, _ in blocks]
    assert grown == sorted(grown) and grown[0] < grown[-1]  # an expanding window

    with pytest.raises(ValueError):
        fc.splits(positions, min_train=200, block=150, gap=fc.HORIZON - 1)
    assert fc.splits(positions[:150], min_train=200, block=150, gap=fc.HORIZON) == []


def test_a_planted_pattern_beats_the_baseline():
    skill = fc.evaluate(PLANTED, **SMALL)
    # Changed on purpose (second review, P2 and P3): six keys were added, none renamed or removed.
    assert set(skill) == {
        "samples", "from", "to", "hit_rate", "calls", "mae", "baseline_mae", "coverage", "better_than_baseline",
        "scope", "train_rows", "calls_up", "calls_down", "hit_rate_baseline", "hit_rate_baseline_call",
    }  # fmt: skip
    assert skill["better_than_baseline"] is True
    assert skill["mae"] < skill["baseline_mae"] and skill["hit_rate"] > 0.6
    assert fc.MIN_CALLS <= skill["calls"] <= skill["samples"]
    assert 0.7 < skill["coverage"] < 0.9  # the band is meant to hold 80% of the closes
    data = fc.dataset(PLANTED)
    # Changed on purpose (P2, P3): the record starts at the first block whose fit is of a comparable
    # size, and ends at the last scored forecast made in open-market hours.
    assert skill["from"] > int(data.made_at[3500]) and skill["to"] <= int(data.made_at[-5])
    assert not probably_closed(skill["from"]) and not probably_closed(skill["to"])
    assert json.loads(json.dumps(skill, allow_nan=False)) == skill


@pytest.mark.parametrize("seed", [12, 13])
def test_a_random_walk_is_not_claimed_to_be_predictable(seed):
    candles = RANDOM if seed == 12 else series(6000, seed=seed)
    skill = fc.evaluate(candles, **SMALL)
    assert skill["better_than_baseline"] is False
    # Changed on purpose (P2, P3): fewer forecasts are scored now, the open-market ones of the
    # comparable blocks. 2000 was the count of all blocks and all hours.
    assert skill["samples"] > 1000
    assert 0.7 < skill["coverage"] < 0.9  # the band is still honest when the line is worth nothing


def test_the_record_is_that_of_comparable_fits_in_open_market_hours_and_the_rest_is_kept_beside_it():
    early = {"min_train": 1500, "block": 1000}  # the first fit is small: under half of the final one
    result = fc.walk_forward(PLANTED, **early)
    skill = result.skill
    data = fc.dataset(PLANTED)
    rows = np.flatnonzero(data.scored)
    blocks = fc.splits(rows, **early)
    comparable = [(train, test) for train, test in blocks if len(train) >= fc.COMPARABLE_TRAIN * len(rows)]
    assert 0 < len(comparable) < len(blocks) == result.blocks and result.scored_blocks == len(comparable)
    made = np.concatenate([data.made_at[rows[test]] for _, test in comparable])
    closed = np.array([probably_closed(int(at)) for at in made])
    assert 0 < closed.sum() < len(made)  # the series has both kinds of hours
    assert skill["scope"] == "open_market" and skill["samples"] == int((~closed).sum())
    assert (skill["from"], skill["to"]) == (int(made[~closed][0]), int(made[~closed][-1]))
    assert skill["train_rows"] == min(len(train) for train, _ in comparable)
    assert skill["train_rows"] >= fc.COMPARABLE_TRAIN * fc.fit(PLANTED)["rows"]  # half the final fit or more
    # Beside it, not mixed in: the closed hours of the same blocks, and everything as it was scored before.
    assert result.closed_market["samples"] == int(closed.sum())
    assert result.all_forecasts["samples"] == sum(len(test) for _, test in blocks) > skill["samples"]
    assert result.all_forecasts["from"] < skill["from"]
    # The calls per side add up, and the hit rate stands next to the best constant call on the same calls.
    assert skill["calls_up"] + skill["calls_down"] == skill["calls"] and min(skill["calls_up"], skill["calls_down"]) > 0
    assert 0.5 <= skill["hit_rate_baseline"] <= 1.0 and skill["hit_rate_baseline_call"] in ("up", "down")
    assert skill["hit_rate"] > skill["hit_rate_baseline"]  # the planted pattern beats "always up" too
    assert result.hit_floor == fc.hit_rate_floor(round(skill["hit_rate"] * skill["calls"]), skill["calls"])
    assert json.loads(json.dumps([result.closed_market, result.all_forecasts], allow_nan=False))


def test_the_best_constant_call_is_counted_on_the_calls_that_were_made():
    count = 8
    real = np.zeros((count, fc.HORIZON))
    real[:, -1] = [1, 1, 1, -1, -1, 1, 1, 0]  # the last one closed exactly at the base price
    chance = np.array([0.9, 0.9, 0.1, 0.1, 0.9, 0.5, 0.9, 0.9])
    move = np.zeros((count, fc.HORIZON))
    move[:, -1] = [1, 1, -1, -1, 1, 1, -1, 1]  # row 6: the line says down while the probability says up
    record = fc._record(
        made_at=np.arange(count) * 900, real=real, move=move, chance=chance, margin=np.full(count, fc.FLAT_MARGIN),
        low=np.full(count, -5.0), high=np.full(count, 5.0), atr=np.ones(count),
    )  # fmt: skip
    # Calls: rows 0, 1, 4, 7 up, rows 2, 3 down. Row 5 is a coin toss, row 6 contradicts its own line.
    assert (record["calls"], record["calls_up"], record["calls_down"]) == (6, 4, 2)
    assert record["hit_rate"] == 3 / 6  # rows 0, 1 and 3; the close at the base price counts against the call
    assert record["hit_rate_baseline"] == 3 / 6 and record["hit_rate_baseline_call"] == "up"  # rows 0, 1, 2 went up
    assert record["samples"] == count and record["coverage"] == 1.0 and record["better_than_baseline"] is False
    empty = np.zeros((0, fc.HORIZON))
    none = np.zeros(0)
    assert fc._record(made_at=none, real=empty, move=empty, chance=none, margin=none, low=none, high=none, atr=none) is None


def test_the_ridge_grid_reaches_far_above_one_so_the_choice_is_a_real_choice():
    assert list(fc.RIDGE_GRID) == sorted(fc.RIDGE_GRID) and fc.RIDGE_GRID[-1] >= 100 and fc.RIDGE_GRID[0] <= 0.01
    # On a pure random walk nothing can be learnt: the fit must be free to shrink beyond the old top of 1.0.
    assert fc.fit(RANDOM)["ridge"] in fc.RIDGE_GRID


def test_too_little_history_gives_no_record_at_all():
    assert fc.evaluate(PLANTED.iloc[:2000], **SMALL) is None


def test_the_rule_for_better_than_baseline_is_strict():
    rng = np.random.default_rng(5)
    count = 4000
    baseline = np.abs(rng.normal(0.0, 1.0, count))
    better = baseline * 0.9
    assert fc.judge(model_errors=better, baseline_errors=baseline, hits=2400, calls=4000) is True
    # A lower error alone is not enough: the direction must be right clearly more often than a coin.
    assert fc.judge(model_errors=better, baseline_errors=baseline, hits=2040, calls=4000) is False
    # Neither is a good hit rate with a worse error,
    assert fc.judge(model_errors=baseline * 1.01, baseline_errors=baseline, hits=2400, calls=4000) is False
    # nor one measured on a handful of calls,
    assert fc.judge(model_errors=better, baseline_errors=baseline, hits=fc.MIN_CALLS - 1, calls=fc.MIN_CALLS - 1) is False
    # nor an error that is lower by too little to tell from chance.
    wobble = baseline + rng.normal(-0.001, 0.5, count)
    assert np.abs(wobble).mean() < 10 and fc.judge(
        model_errors=wobble, baseline_errors=baseline, hits=2400, calls=4000
    ) is False
    assert fc.judge(model_errors=better[:0], baseline_errors=baseline[:0], hits=0, calls=0) is False


def test_the_hit_rates_lower_bound_counts_overlapping_calls_as_fewer():
    # 60% right on 400 calls: clear for 400 separate tries, but calls one candle apart share
    # three of their four candles, so they are worth a quarter as many.
    assert fc.hit_rate_floor(240, 400, overlap=1) > 0.55
    assert 0.5 < fc.hit_rate_floor(240, 400, overlap=fc.HORIZON) < 0.52
    assert fc.hit_rate_floor(55, 100, overlap=fc.HORIZON) < 0.5
    assert fc.hit_rate_floor(0, 0) == 0.0
