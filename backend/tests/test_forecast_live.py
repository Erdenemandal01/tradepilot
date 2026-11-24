"""The forecast around its pure core: the model file, the live feed, the route and the training script.

No test touches the network or the real data/ folder. The model used here
is fitted on a made-up series (test_forecast) and its record is made up for
the test: nothing in it says anything about real prices.
"""

from __future__ import annotations

import json
from typing import Any

import pandas as pd
import pytest

from backend.app import forecast as fcast
from backend.app.config import Settings
from backend.app.core import forecast as fc
from backend.app.datasource.base import DataSource, DataSourceError, Quote
from backend.app.forecast import Forecaster
from backend.scripts import train_forecast as tf
from backend.tests.test_forecast import series
from backend.tests.test_live import TICK_KEYS, make_world, newest_signal, no_exported_settings, strict_json  # noqa: F401
from backend.tests.test_web import site  # noqa: F401

M15 = 900
# Changed on purpose (second review, P5): samples was 2000 and train_rows 3000. The model of the
# ``stored`` fixture is fitted on 1797 rows, and a record must now fit its model: no more scored
# forecasts than the model has rows, and fits between half its size and its size.
SKILL = {
    "samples": 1200, "from": 1_790_000_000, "to": 1_792_000_000, "hit_rate": 0.5, "calls": 400, "mae": 2.5,
    "baseline_mae": 2.4, "coverage": 0.8, "better_than_baseline": False,
    "scope": "open_market", "train_rows": 1000, "calls_up": 300, "calls_down": 100, "hit_rate_baseline": 0.52,
    "hit_rate_baseline_call": "up",
}  # fmt: skip
NO_CALLS = {"calls": 0, "calls_up": 0, "calls_down": 0, "hit_rate": 0.0, "hit_rate_baseline": 0.0}
SOURCE = {"name": "fake", "symbol": "XAUTEST"}  # what the fake feeds of test_live and test_web call themselves
TRAINED_AT = 1_793_000_000
BLOCK_KEYS = {
    "available", "reason", "timeframe", "horizon_candles", "made_at", "base_price", "points", "up_probability",
    "direction", "skill", "trained_at",
}  # fmt: skip
EMPTY = {
    "available": False, "timeframe": "M15", "horizon_candles": 4, "made_at": None, "base_price": None,
    "points": [], "up_probability": None, "direction": None,
}  # fmt: skip


@pytest.fixture(scope="module")
def stored() -> dict[str, Any]:
    return {
        "format": fcast.FILE_FORMAT,
        "model": fc.fit(series(3000, carry=0.5, seed=21)),
        "skill": dict(SKILL),
        "trained_at": TRAINED_AT,
        "source": dict(SOURCE),
    }


def with_forecast(world: Any, stored: dict[str, Any]) -> Forecaster:
    forecaster = Forecaster(fcast.check_file(stored))
    world.feed._forecaster = forecaster
    return forecaster


def last_close(feed: Any) -> int:
    frame = feed.frame()
    return int(frame.loc[frame["closed"], "time"].iloc[-1]) + M15


# --- the model file ---------------------------------------------------------------------


def test_without_a_model_file_there_is_no_forecast(tmp_path, make_world):
    assert fcast.read_model(tmp_path / "absent.json") is None
    forecaster = Forecaster.from_file(tmp_path / "absent.json")
    assert not forecaster.has_model and forecaster.update(series(1500)) is False
    assert forecaster.block("ok", 123) == {**EMPTY, "reason": "no_model", "skill": None, "trained_at": None}
    world = make_world()  # a feed that was given no forecaster at all says the same
    world.poll()
    assert world.feed.prediction() == forecaster.block("ok", 123)


def test_a_good_model_file_is_read_with_its_record(tmp_path, stored):
    path = tmp_path / "model.json"
    path.write_text(json.dumps({**stored, "candles": {"count": 3000}, "anything_else": 1}), encoding="utf-8")
    read = fcast.read_model(path)
    assert read == {"model": stored["model"], "skill": SKILL, "trained_at": TRAINED_AT, "source": SOURCE}
    assert Forecaster.from_file(path).has_model


@pytest.mark.parametrize(
    "spoil",
    [
        lambda s: "this is not json",
        lambda s: json.dumps([1, 2, 3]),
        lambda s: json.dumps({**s, "format": 99}),
        lambda s: json.dumps({**s, "model": {**s["model"], "version": 99}}),
        lambda s: json.dumps({**s, "model": {**s["model"], "features": s["model"]["features"][1:]}}),
        lambda s: json.dumps({key: value for key, value in s.items() if key != "skill"}),  # no measured record
        lambda s: json.dumps({**s, "skill": {**s["skill"], "better_than_baseline": "yes"}}),
        lambda s: json.dumps({**s, "skill": {key: v for key, v in s["skill"].items() if key != "baseline_mae"}}),
        lambda s: json.dumps({**s, "trained_at": None}),
        lambda s: json.dumps({**s, "trained_at": 10**15}),  # milliseconds, or nonsense: not a time the page can show
        lambda s: json.dumps({**s, "trained_at": 0}),
        lambda s: b"\xff\xfe\x00\x01 not text at all",  # not UTF-8
        lambda s: json.dumps(s).encode("utf-16"),  # a wrong encoding
        lambda s: json.dumps(s)[: len(json.dumps(s)) // 2],  # a half-copied file
        lambda s: "[" * 100_000,  # nested deeper than the reader goes
        lambda s: json.dumps({key: value for key, value in s.items() if key != "source"}),
        lambda s: json.dumps({**s, "source": {"name": "fake"}}),
        # A record that cannot be true, or that contradicts itself.
        lambda s: json.dumps({**s, "skill": {**s["skill"], "hit_rate": 7.0}}),
        lambda s: json.dumps({**s, "skill": {**s["skill"], "coverage": -3.0}}),
        lambda s: json.dumps({**s, "skill": {**s["skill"], "samples": -1}}),
        lambda s: json.dumps({**s, "skill": {**s["skill"], "samples": 0, "calls": 0, "calls_up": 0, "calls_down": 0}}),
        lambda s: json.dumps({**s, "skill": {**s["skill"], "calls": 10**9}}),
        lambda s: json.dumps({**s, "skill": {**s["skill"], "calls_up": 399}}),  # 399 + 100 is not 400
        lambda s: json.dumps({**s, "skill": {**s["skill"], "mae": -3.0}}),
        lambda s: json.dumps({**s, "skill": {**s["skill"], "baseline_mae": -0.1}}),
        lambda s: json.dumps({**s, "skill": {**s["skill"], "from": s["skill"]["to"] + 1}}),
        lambda s: json.dumps({**s, "skill": {**s["skill"], "from": 0}}),
        lambda s: json.dumps({**s, "skill": {**s["skill"], "to": 10**15}}),
        lambda s: json.dumps({**s, "skill": {**s["skill"], "hit_rate_baseline": 1.5}}),
        lambda s: json.dumps({**s, "skill": {**s["skill"], "hit_rate_baseline_call": "sideways"}}),
        lambda s: json.dumps({**s, "skill": {**s["skill"], "scope": "all_hours"}}),
        lambda s: json.dumps({**s, "skill": {**s["skill"], "train_rows": 0}}),
        lambda s: json.dumps({**s, "skill": {key: v for key, v in s["skill"].items() if key != "scope"}}),
        # "Better than the baseline" claimed against its own numbers: error not lower, too few calls, a coin's hit rate.
        lambda s: json.dumps({**s, "skill": {**s["skill"], "better_than_baseline": True, "hit_rate": 0.91}}),
        lambda s: json.dumps(
            {**s, "skill": {**s["skill"], "better_than_baseline": True, "mae": 2.0, "hit_rate": 0.9, "calls": 200,
                            "calls_up": 100}}
        ),  # fmt: skip
        lambda s: json.dumps({**s, "skill": {**s["skill"], "better_than_baseline": True, "mae": 2.0, "hit_rate": 0.5}}),
        # The rule's third condition worked out from the record itself: a hit rate above a coin's, but its
        # lower 95% bound is not (0.51 on 400 calls, and 0.55 on 300 calls, whose bound is about 0.44).
        lambda s: json.dumps({**s, "skill": {**s["skill"], "better_than_baseline": True, "mae": 2.0, "hit_rate": 0.51}}),
        lambda s: json.dumps(
            {**s, "skill": {**s["skill"], "better_than_baseline": True, "mae": 2.0, "hit_rate": 0.55, "calls": 300,
                            "calls_up": 200}}
        ),  # fmt: skip
        # A hit rate without a single call.
        lambda s: json.dumps({**s, "skill": {**s["skill"], **NO_CALLS, "hit_rate": 0.5}}),
        lambda s: json.dumps({**s, "skill": {**s["skill"], **NO_CALLS, "hit_rate_baseline": 0.52}}),
        # A record that cannot be this model's: more scored forecasts than the model has rows, fits
        # under half its size (not comparable), fits larger than the model itself, or no row count.
        lambda s: json.dumps({**s, "skill": {**s["skill"], "samples": s["model"]["rows"] + 1}}),
        lambda s: json.dumps({**s, "skill": {**s["skill"], "train_rows": s["model"]["rows"] // 2 - 1}}),
        lambda s: json.dumps({**s, "skill": {**s["skill"], "train_rows": s["model"]["rows"] + 1}}),
        lambda s: json.dumps({**s, "model": {key: v for key, v in s["model"].items() if key != "rows"}}),
        lambda s: json.dumps({**s, "model": {**s["model"], "rows": True}}),
        # A band whose low quantile lies above its high one.
        lambda s: json.dumps({**s, "model": {**s["model"], "band_low": [1.0] * 4, "band_high": [-1.0] * 4}}),
    ],
)
def test_a_model_file_that_cannot_be_trusted_is_not_used(tmp_path, stored, spoil, caplog):
    path = tmp_path / "model.json"
    spoiled = spoil(stored)
    path.write_bytes(spoiled if isinstance(spoiled, bytes) else spoiled.encode("utf-8"))
    caplog.set_level("WARNING", logger="tradepilot.forecast")
    assert fcast.read_model(path) is None  # never an exception
    assert "is not used" in caplog.text or "cannot read" in caplog.text  # and it is said in the log
    assert Forecaster.from_file(path).block("ok", 1)["reason"] == "no_model"


def test_a_record_that_claims_an_edge_and_shows_it_is_accepted(stored):
    proud = {**SKILL, "better_than_baseline": True, "mae": 2.0, "hit_rate": 0.6}
    assert fcast.check_file({**stored, "skill": proud})["skill"] == proud


def test_a_record_without_a_single_direction_call_is_accepted_as_it_is(stored):
    # The honest result of a model shrunk to "no move": no call, so no hit rate. It is a record all the same.
    silent = {**SKILL, **NO_CALLS}
    assert fcast.check_file({**stored, "skill": silent})["skill"] == silent
    rows = stored["model"]["rows"]
    edge = {**SKILL, "samples": rows, "train_rows": rows}  # the limits themselves are allowed
    assert fcast.check_file({**stored, "skill": edge})["skill"] == edge
    half = {**SKILL, "train_rows": -(-rows // 2)}  # half the model's rows, rounded up
    assert fcast.check_file({**stored, "skill": half})["skill"] == half


def test_a_broken_model_file_does_not_stop_the_app(tmp_path, site):  # noqa: F811
    path = tmp_path / "forecast_model.json"
    path.write_bytes(b"\xff\xfe\x00\x01")
    s = site(forecast_model_path=str(path))  # the app is built: no exception
    s.load()
    state = s.member().get("/api/state").json()
    assert state["feed"] == "ok" and state["prediction"]["reason"] == "no_model"


def test_a_model_fitted_on_another_source_or_instrument_is_not_used(tmp_path, stored, site, caplog):  # noqa: F811
    held = fcast.check_file(stored)
    assert Forecaster(held, "M15", ("fake", "XAUTEST")).has_model
    assert Forecaster(held, "M15").has_model  # nothing to compare with: a tool, not the app
    caplog.set_level("WARNING", logger="tradepilot.forecast")
    assert not Forecaster(held, "M15", ("fake", "XAGTEST")).has_model  # another instrument
    assert not Forecaster(held, "M15", ("twelvedata", "XAUTEST")).has_model  # another source
    assert "fitted on fake XAUTEST" in caplog.text

    # Through the app: the feed's own source decides.
    path = tmp_path / "forecast_model.json"
    path.write_text(json.dumps({**stored, "source": {"name": "binance", "symbol": "XAUUSDT"}}), encoding="utf-8")
    s = site(forecast_model_path=str(path))
    s.load()
    block = s.member().get("/api/state").json()["prediction"]
    assert block["available"] is False and block["reason"] == "no_model" and block["skill"] is None


def test_a_model_for_m15_is_not_used_on_another_signal_timeframe(stored):
    assert not Forecaster(fcast.check_file(stored), "H1").has_model


# --- the live feed ------------------------------------------------------------------------


def test_one_forecast_is_made_per_closed_candle_from_the_feeds_own_candles(make_world, stored):
    world = make_world()
    with_forecast(world, stored)
    before = world.feed.prediction()
    assert before == {**EMPTY, "reason": "not_enough_candles", "skill": SKILL, "trained_at": TRAINED_AT}

    world.poll()
    block = strict_json(world.feed.prediction())
    assert set(block) == BLOCK_KEYS
    assert block["available"] is True and block["reason"] is None
    assert (block["timeframe"], block["horizon_candles"]) == ("M15", 4)
    assert block["skill"] == SKILL and block["trained_at"] == TRAINED_AT
    made_at = last_close(world.feed)
    assert block["made_at"] == made_at
    closed = world.feed.frame()
    closed = closed[closed["closed"]]
    assert block["base_price"] == float(closed["close"].iloc[-1])
    # The points sit on the open times of the next four candles: the first is the one now forming.
    assert [p["time"] for p in block["points"]] == [made_at + i * M15 for i in range(4)]
    assert block["points"][0]["time"] == int(world.feed.frame()["time"].iloc[-1])
    assert all(p["low"] <= p["close"] <= p["high"] for p in block["points"])
    # Changed on purpose (second review, P4): the word needs the line's agreement, see fc.call.
    assert block["direction"] == fc.call(block["up_probability"], block["points"][-1]["close"] - block["base_price"])
    # It is exactly what the pure model gives for the feed's candles: nothing is added on the way.
    pure = fc.predict(stored["model"], world.feed.frame())
    # Changed on purpose (second review, P5): predict also says its ATR, which the range check
    # uses. It is not part of the block of docs/WEB.md and is not sent.
    assert pure.pop("atr") > 0 and "atr" not in block
    assert {key: block[key] for key in pure} == pure

    world.close_candles()
    world.poll()
    after = world.feed.prediction()
    assert after["made_at"] == made_at + M15 and after["points"] != block["points"]


def test_the_forming_candle_never_moves_the_forecast(make_world, stored):
    world = make_world()
    with_forecast(world, stored)
    world.poll()
    block = world.feed.prediction()
    world.source.price = float(world.source.candles["open"].iloc[world.source.cursor - 1]) + 55.0
    world.pass_seconds(4)
    world.poll()
    assert world.feed.prediction() == block


def test_the_version_goes_up_when_a_new_forecast_is_made_and_not_in_between(make_world, stored):
    plain, world = make_world(), make_world()
    with_forecast(world, stored)
    plain.poll()
    world.poll()
    lead = world.feed.tick()["v"] - plain.feed.tick()["v"]
    assert lead == 1  # the first forecast
    world.pass_seconds(2)
    plain.pass_seconds(2)
    world.poll()
    plain.poll()
    assert world.feed.tick()["v"] - plain.feed.tick()["v"] == lead  # no new candle: no new forecast
    for extra in (1, 2, 3):
        version = world.feed.tick()["v"]
        for each in (plain, world):
            each.close_candles()
            each.poll()
        assert world.feed.tick()["v"] > version
        assert world.feed.tick()["v"] - plain.feed.tick()["v"] == lead + extra
    assert "prediction" not in world.feed.tick() and set(world.feed.tick()) == TICK_KEYS  # the tick is unchanged


def test_the_forecast_changes_nothing_about_the_signals(make_world, stored):
    plain, world = make_world(), make_world()
    with_forecast(world, stored)
    for each in (plain, world):
        each.poll()
        each.until_signal()
    one, two = plain.feed.snapshot(), world.feed.snapshot()
    assert world.feed.prediction()["available"] is True
    for key in ("card", "log", "reasons", "today", "alerts", "feed", "price"):
        assert one[key] == two[key], key
    assert plain.notifier.sent == world.notifier.sent and world.notifier.sent


def test_while_the_feed_is_down_no_forecast_is_shown(make_world, stored):
    world = make_world()
    with_forecast(world, stored)
    world.poll()
    assert world.feed.prediction()["available"] is True
    world.source.error = DataSourceError("the source is away")
    world.pass_seconds(61)
    world.poll()
    assert world.feed.snapshot()["feed"] == "down"
    assert world.feed.prediction() == {**EMPTY, "reason": "feed_down", "skill": SKILL, "trained_at": TRAINED_AT}
    world.source.error = None
    world.close_candles()
    world.poll()
    world.pass_seconds(2)
    world.poll()
    back = world.feed.prediction()
    assert world.feed.snapshot()["feed"] == "ok" and back["available"] is True
    assert back["made_at"] == last_close(world.feed)  # made on the newest closed candle, not an old one


def test_with_too_few_candles_the_block_says_so(make_world, stored):
    world = make_world(history_candles=600)  # the H1 score alone needs 300 closed H1 candles
    with_forecast(world, stored)
    world.poll()
    assert world.feed.snapshot()["feed"] == "ok"
    assert world.feed.prediction() == {**EMPTY, "reason": "not_enough_candles", "skill": SKILL, "trained_at": TRAINED_AT}


def test_an_error_inside_the_forecast_costs_the_forecast_only(make_world, stored, monkeypatch, caplog):
    plain, world = make_world(), make_world()
    with_forecast(world, stored)

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("planted failure")

    monkeypatch.setattr(fc, "predict", broken)
    for each in (plain, world):
        assert each.poll() == 2.0  # a normal cycle, a normal wait
        each.until_signal()
    assert "planted failure" in caplog.text
    one, two = plain.feed.snapshot(), world.feed.snapshot()
    for key in ("card", "log", "reasons", "alerts", "feed"):
        assert one[key] == two[key], key
    assert plain.notifier.sent == world.notifier.sent and world.notifier.sent
    # Not available, and none of the reason codes is claimed for it.
    assert world.feed.prediction() == {**EMPTY, "reason": None, "skill": SKILL, "trained_at": TRAINED_AT}

    monkeypatch.undo()
    world.close_candles()
    world.poll()
    assert world.feed.prediction()["available"] is True  # the next candle gets its forecast again


def test_even_a_forecaster_that_raises_cannot_stop_a_cycle_or_a_page(make_world):
    class Raises:
        def update(self, frame: Any) -> bool:
            raise RuntimeError("update is broken")

        def block(self, status: str, last: Any) -> dict:
            raise RuntimeError("block is broken")

    world = make_world()
    world.feed._forecaster = Raises()
    assert world.poll() == 2.0
    before = newest_signal(world.feed.snapshot())
    world.until_signal()
    assert newest_signal(world.feed.snapshot()) != before
    assert world.feed.prediction() == {**EMPTY, "reason": None, "skill": None, "trained_at": None}


def test_a_forecast_with_a_number_that_is_not_finite_is_dropped(stored, monkeypatch):
    candles = series(1500, seed=4)
    forecaster = Forecaster(fcast.check_file(stored))
    real = fc.predict

    def poisoned(model: Any, frame: Any) -> Any:
        forecast = real(model, frame)
        forecast["points"][2]["high"] = float("inf")
        return forecast

    monkeypatch.setattr(fc, "predict", poisoned)
    assert forecaster.update(candles) is True
    made_at = int(candles["time"].iloc[-1]) + M15
    assert forecaster.block("ok", made_at)["available"] is False
    assert forecaster.update(candles) is False  # tried once per candle, not on every cycle


@pytest.mark.parametrize(
    "spoil",
    [
        lambda f: f["points"][3].update(close=f["base_price"] + 25 * f["atr"], high=f["base_price"] + 26 * f["atr"]),
        lambda f: f["points"][0].update(low=f["base_price"] - 21 * f["atr"]),  # a band far from the price
        lambda f: f["points"][1].update(low=f["points"][1]["close"] + 0.01),  # the line outside its own band
        lambda f: f["points"][2].update(time=f["points"][2]["time"] + 900),  # not on the next candles
        lambda f: f.update(points=f["points"][:3]),
        lambda f: f.update(up_probability=1.7),
        lambda f: f.update(up_probability=0.99, direction="down"),  # the word against its own probability
        lambda f: f.update(direction="up" if f["points"][3]["close"] < f["base_price"] else "down"),  # against the line
        lambda f: f.update(base_price=-1.0),
        lambda f: f.pop("atr"),
    ],
)
def test_a_forecast_that_is_out_of_range_or_contradicts_itself_is_dropped(stored, monkeypatch, spoil):
    candles = series(1500, seed=4)
    made_at = int(candles["time"].iloc[-1]) + M15
    honest = Forecaster(fcast.check_file(stored))
    assert honest.update(candles) is True and honest.block("ok", made_at)["available"] is True
    real = fc.predict

    def spoiled(model: Any, frame: Any) -> Any:
        forecast = real(model, frame)
        spoil(forecast)
        return forecast

    monkeypatch.setattr(fc, "predict", spoiled)
    forecaster = Forecaster(fcast.check_file(stored))
    assert forecaster.update(candles) is True
    block = forecaster.block("ok", made_at)
    assert block["available"] is False and block["points"] == [] and block["reason"] is None


def test_a_model_with_absurd_weights_draws_nothing(stored):
    candles = series(1500, seed=4)
    wild = {**stored, "model": {**stored["model"], "coef": [[1e6] + row[1:] for row in stored["model"]["coef"]]}}
    forecaster = Forecaster(fcast.check_file(wild))  # the numbers are finite, so the file itself passes
    assert forecaster.update(candles) is True
    assert forecaster.block("ok", int(candles["time"].iloc[-1]) + M15)["available"] is False


def test_a_forecast_made_on_an_older_candle_is_not_shown(stored):
    candles = series(1500, seed=4)
    forecaster = Forecaster(fcast.check_file(stored))
    assert forecaster.update(candles) is True
    made_at = int(candles["time"].iloc[-1]) + M15
    assert forecaster.block("ok", made_at)["available"] is True
    assert forecaster.block("late", made_at)["available"] is True
    assert forecaster.block("ok", made_at + M15)["available"] is False
    assert forecaster.block("down", made_at)["reason"] == "feed_down"


# --- the route ---------------------------------------------------------------------------


def test_state_carries_the_forecast_with_its_record_when_a_model_file_exists(site, tmp_path, stored):  # noqa: F811
    path = tmp_path / "forecast_model.json"
    path.write_text(json.dumps(stored), encoding="utf-8")
    s = site(forecast_model_path=str(path))
    member = s.member()
    waiting = member.get("/api/state").json()["prediction"]
    assert waiting == {**EMPTY, "reason": "not_enough_candles", "skill": SKILL, "trained_at": TRAINED_AT}

    s.load()
    state = member.get("/api/state").json()
    block = state["prediction"]
    assert set(block) == BLOCK_KEYS and block["available"] is True and block["reason"] is None
    assert block == strict_json(s.feed.prediction())
    assert len(block["points"]) == 4 and block["skill"] == SKILL and block["skill"]["better_than_baseline"] is False
    assert block["direction"] in ("up", "down", "flat") and 0.0 < block["up_probability"] < 1.0
    assert block["made_at"] == last_close(s.feed) and state["feed"] == "ok"
    assert s.visitor().get("/api/state").status_code == 401  # behind the login like everything else


# --- the training script --------------------------------------------------------------------


class HistorySource(DataSource):
    name = "fake"
    symbol = "XAUTEST"

    def __init__(self, candles: pd.DataFrame | None = None, error: Exception | None = None) -> None:
        self.candles, self.error, self.asked = candles, error, 0

    def fetch_candles(self, timeframe: str, limit: int = 500) -> pd.DataFrame:
        raise AssertionError("the training script reads history, not the live tail")

    def fetch_quote(self) -> Quote:
        raise AssertionError("the training script needs no quote")

    def fetch_history(self, timeframe: str, start: int | None = None, end: int | None = None) -> pd.DataFrame:
        assert (timeframe, start, end) == ("M15", None, None)
        self.asked += 1
        if self.error is not None:
            raise self.error
        frame = self.candles.copy()
        frame.loc[frame.index[-1], "closed"] = False  # as a source gives it: the newest candle is forming
        return frame


@pytest.fixture
def quick_checks(monkeypatch):
    monkeypatch.setattr(tf, "WINDOW_CHECKS", 3)


@pytest.mark.parametrize("carry, better", [(0.5, True), (0.0, False)])
def test_the_script_measures_fits_and_writes_a_model_the_app_accepts(tmp_path, quick_checks, carry, better):
    candles = series(7200, carry=carry, seed=31)
    out, lines = tmp_path / "sub" / "model.json", []
    code = tf.train(HistorySource(candles), out, window=1500, clock=lambda: TRAINED_AT + 0.7, out=lines.append)
    text = "\n".join(lines)
    assert code == 0 and out.exists() and not out.with_name("model.json.tmp").exists()

    written = json.loads(out.read_text(encoding="utf-8"))
    read = fcast.read_model(out)
    assert read is not None and read["trained_at"] == TRAINED_AT
    assert read["skill"] == fc.evaluate(candles.iloc[:-1]) == written["skill"]
    assert written["skill"]["better_than_baseline"] is better
    # The forming candle is not fitted on: the model is the one of the closed candles alone.
    assert written["model"] == fc.fit(candles.iloc[:-1])
    assert written["model"]["features"] == list(fc.FEATURE_NAMES)
    assert written["candles"] == {
        "from": int(candles["time"].iloc[0]), "to": int(candles["time"].iloc[-2]), "count": 7199, "holes": 0,
        "missing": 0,
    }  # fmt: skip
    assert written["source"] == {"name": "fake", "symbol": "XAUTEST"}
    assert written["evaluation"]["gap"] >= fc.HORIZON and written["evaluation"]["live_window"]["window"] == 1500

    assert "7199 closed candles" in text and "Walk-forward" in text
    if better:
        assert "measurably better" in text and "NOT better" not in text
    else:  # said in plain words, not hidden in a number
        assert "VERDICT: the model is NOT better than 'the price stays where it is'." in text
        assert "measurably better" not in text and "Do not act on it." in text
    # How often the written model itself says "up" or "down" is counted on that model and said.
    own = fc.own_calls(written["model"], candles.iloc[:-1])
    assert own["rows"] == written["model"]["rows"]
    assert "\n".join(tf.own_calls_lines(own, written["skill"])) in text


def record(**skill: Any) -> fc.Evaluation:
    return fc.Evaluation(
        skill={**SKILL, **skill}, blocks=5, scored_blocks=3, closed_market=None, all_forecasts=None,
        mae_by_step=(1.0, 1.5, 2.0, 2.5), baseline_mae_by_step=(1.0, 1.5, 2.0, 2.4), up_share=0.5,
        hit_floor=0.0, mae_gain_floor=-0.1,
    )  # fmt: skip


def test_the_script_says_in_words_when_not_one_direction_call_was_made():
    lines: list[str] = []
    tf.describe(record(**NO_CALLS), lines.append)
    text = "\n".join(lines)
    assert "direction calls   NONE" in text and 'Not one of the 1200 scored forecasts said "up" or "down"' in text
    assert "there is no hit rate" in text
    assert "it made no direction call at all (at least 300 are needed)" in text
    assert "only 0 direction calls" not in text and "right at the 4th close" not in text  # not left to a bare zero
    assert "VERDICT: the model is NOT better than 'the price stays where it is'." in text

    lines = []
    tf.describe(record(), lines.append)  # 400 calls, half of them right: the numbers are said as before
    text = "\n".join(lines)
    assert "direction calls   400 of 1200 forecasts: 300 up, 100 down" in text and "NONE" not in text
    assert "right at the 4th close: 50.0%" in text and "'always up' on the same calls" in text


def test_the_script_says_how_often_the_final_model_itself_calls_a_direction():
    speaks = {"rows": 1000, "up": 10, "down": 5, "open_market_rows": 700, "open_market_calls": 9}
    silent = {"rows": 1000, "up": 0, "down": 0, "open_market_rows": 700, "open_market_calls": 0}
    untested = "\n".join(tf.own_calls_lines(speaks, {**SKILL, **NO_CALLS}))
    assert 'says "up" or "down" on 15 of the 1000 candles it was fitted on (10 up, 5 down; 9 of them' in untested
    # The scored fits never called, this model does: its word has nothing behind it, and that is said.
    assert "no measured record at all" in untested and "never did" in untested
    tested = "\n".join(tf.own_calls_lines(speaks, SKILL))
    assert "15 of the 1000 candles" in tested and "no measured record" not in tested
    quiet = "\n".join(tf.own_calls_lines(silent, {**SKILL, **NO_CALLS}))
    assert "makes no direction call at all" in quiet and "no measured record" not in quiet


@pytest.mark.parametrize(
    "source, said",
    [
        (HistorySource(error=DataSourceError("HTTP 429", status=429, retry_after=90.0)), "refused the download (HTTP 429)"),
        (HistorySource(error=DataSourceError("HTTP 418", status=418)), "refused the download (HTTP 418)"),
        (HistorySource(error=DataSourceError("cannot reach the source")), "gave no history"),
        (HistorySource(error=NotImplementedError("the fake source cannot read older candles in pages")), "cannot read older"),
        (HistorySource(series(3000, seed=2)), "too little history"),
        (HistorySource(series(1, seed=2)), "no closed candles"),
    ],
)
def test_the_script_writes_nothing_when_it_cannot_do_the_whole_job(tmp_path, quick_checks, source, said):
    out, lines = tmp_path / "model.json", []
    out.write_text("an older model, left alone", encoding="utf-8")
    assert tf.train(source, out, window=1500, out=lines.append) == 1
    text = "\n".join(lines)
    assert said in text and "No model was written." in text and source.asked == 1  # asked once, never again
    assert out.read_text(encoding="utf-8") == "an older model, left alone"


def test_the_script_takes_its_source_and_path_from_the_settings(tmp_path, monkeypatch, quick_checks, capsys):
    monkeypatch.setenv("TRADEPILOT_FORECAST_MODEL_PATH", str(tmp_path / "from_settings.json"))
    monkeypatch.setattr(tf, "get_settings", lambda: Settings(_env_file=None))  # never the PC's own .env
    source = HistorySource(error=DataSourceError("HTTP 429", status=429))
    assert tf.main([], source=source) == 1
    assert "refused the download" in capsys.readouterr().out and not (tmp_path / "from_settings.json").exists()
