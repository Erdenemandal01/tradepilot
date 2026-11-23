"""The forecast shown beside the signals: load the fitted model, forecast once per closed candle.

The model is fitted and measured by ``python -m backend.scripts.train_forecast``
and stored as plain numbers in ``data/forecast_model.json``. This module reads
that file once at start-up. On every newly closed candle the live feed hands
over its candles and one forecast is made from them (``core/forecast.py``).

Rules kept here:
- No model file, no forecast. A file without its measured record (the
  ``skill`` block) is refused too: a forecast is never shown without it.
- A file that is broken in any way, whose record contradicts itself, or that
  was fitted on another source or instrument than the feed's, is "no model".
  It never stops the app.
- Closed candles only, and only the candles the feed holds: real prices.
- While the feed is down nothing is shown, however fresh the last forecast.
- The forecast never touches a signal. An error here costs the forecast
  only: it is logged and the page is told "not available".
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any

import pandas as pd

from backend.app.core import forecast as fc
from backend.app.core.candles import MAX_TIME

log = logging.getLogger("tradepilot.forecast")

FILE_FORMAT = 1
NO_MODEL = "no_model"
NOT_ENOUGH_CANDLES = "not_enough_candles"
FEED_DOWN = "feed_down"

# The skill block of docs/WEB.md: every key must be there, with this type.
SKILL_TYPES: dict[str, type] = {
    "samples": int,
    "from": int,
    "to": int,
    "hit_rate": float,
    "calls": int,
    "mae": float,
    "baseline_mae": float,
    "coverage": float,
    "better_than_baseline": bool,
    # Added with the second review. They say what the record covers and put the hit rate in its place.
    "scope": str,  # "open_market": forecasts made while the broker's market is probably open
    "train_rows": int,  # the smallest fit behind a scored forecast
    "calls_up": int,
    "calls_down": int,
    "hit_rate_baseline": float,  # the best constant call ("always up" / "always down") on the same calls
    "hit_rate_baseline_call": str,  # which of the two that was: "up" or "down"
}
# A point further from the base price than this many ATR is not a forecast but a broken model.
MAX_ATR_AWAY = 20.0


def unavailable(reason: str | None, skill: dict[str, Any] | None = None, trained_at: int | None = None) -> dict[str, Any]:
    """The prediction block when there is nothing to draw."""
    return {
        "available": False,
        "reason": reason,
        "timeframe": fc.TIMEFRAME,
        "horizon_candles": fc.HORIZON,
        "made_at": None,
        "base_price": None,
        "points": [],
        "up_probability": None,
        "direction": None,
        "skill": skill,
        "trained_at": trained_at,
    }


def check_skill(skill: Any) -> dict[str, Any]:
    """The measured record as the API shows it. Raises ValueError when it is missing or malformed."""
    if not isinstance(skill, dict):
        raise ValueError("the model file has no measured record (skill)")
    out: dict[str, Any] = {}
    for name, kind in SKILL_TYPES.items():
        value = skill.get(name)
        if kind is bool:
            good = isinstance(value, bool)
        elif kind is str:
            good = isinstance(value, str)
        elif kind is int:
            good = isinstance(value, int) and not isinstance(value, bool)
        else:
            good = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        if not good:
            raise ValueError(f"the model file's skill.{name} is missing or not a {kind.__name__}")
        out[name] = kind(value)
    _check_ranges(out)
    return out


def _check_ranges(skill: dict[str, Any]) -> None:
    """Refuse a record whose numbers cannot be true, or that contradicts itself."""

    def refuse(what: str) -> None:
        raise ValueError(f"the model file's measured record cannot be right: {what}")

    if skill["samples"] <= 0:
        refuse("no forecasts were scored")
    if not 0 <= skill["calls"] <= skill["samples"]:
        refuse("more calls than forecasts, or fewer than none")
    if skill["calls_up"] < 0 or skill["calls_down"] < 0 or skill["calls_up"] + skill["calls_down"] != skill["calls"]:
        refuse("the calls per side do not add up to the calls")
    if not 0 < skill["from"] <= skill["to"] < MAX_TIME:
        refuse("the evaluated period is not a period")
    for name in ("hit_rate", "coverage", "hit_rate_baseline"):
        if not 0.0 <= skill[name] <= 1.0:
            refuse(f"{name} is not a share between 0 and 1")
    for name in ("mae", "baseline_mae"):
        if skill[name] < 0:
            refuse(f"{name} is negative")
    if skill["train_rows"] <= 0:
        refuse("train_rows is not positive")
    if skill["scope"] != fc.SCOPE:
        refuse(f"its scope is {skill['scope']!r}, this code shows {fc.SCOPE!r}")
    if skill["hit_rate_baseline_call"] not in ("up", "down"):
        refuse("hit_rate_baseline_call is neither up nor down")
    if skill["calls"] == 0 and (skill["hit_rate"] != 0 or skill["hit_rate_baseline"] != 0):
        refuse("a hit rate is given although no call was made")
    if skill["better_than_baseline"]:
        # What core/forecast.judge needs at the least; a record that claims more than it shows is refused.
        if skill["calls"] < fc.MIN_CALLS or not skill["mae"] < skill["baseline_mae"] or not skill["hit_rate"] > 0.5:
            refuse("better_than_baseline is true but its own numbers say otherwise")


def _check_record_fits(skill: dict[str, Any], rows: Any) -> None:
    """Refuse a record that cannot be the record of a model fitted on ``rows`` rows.

    The shown record must be the record of the shown model: its scored
    forecasts are among the rows the model was fitted on, and the fits behind
    them saw between ``COMPARABLE_TRAIN`` of those rows and all of them.
    """
    if isinstance(rows, bool) or not isinstance(rows, int) or rows <= 0:
        raise ValueError("the model file does not say how many rows the model was fitted on")
    if skill["samples"] > rows:
        raise ValueError("the measured record is not this model's: more forecasts were scored than the model has rows")
    if not fc.COMPARABLE_TRAIN * rows <= skill["train_rows"] <= rows:
        raise ValueError(
            f"the measured record is not this model's: its fits saw {skill['train_rows']} rows or more, "
            f"the model was fitted on {rows}"
        )


def check_file(stored: Any) -> dict[str, Any]:
    """Validate what the training script wrote. Raises ValueError (ModelMismatch is one) when unusable."""
    if not isinstance(stored, dict) or stored.get("format") != FILE_FORMAT:
        raise ValueError(f"not a forecast model file of format {FILE_FORMAT}")
    fc.check(stored.get("model"))
    trained_at = stored.get("trained_at")
    if isinstance(trained_at, bool) or not isinstance(trained_at, int) or not 0 < trained_at < MAX_TIME:
        raise ValueError("the model file has no time of training, or one that is not a real time")
    source = stored.get("source")
    if not isinstance(source, dict) or not all(isinstance(source.get(key), str) and source[key] for key in ("name", "symbol")):
        raise ValueError("the model file does not say which source and instrument it was fitted on")
    skill = check_skill(stored.get("skill"))
    _check_record_fits(skill, stored["model"].get("rows"))
    return {
        "model": stored["model"],
        "skill": skill,
        "trained_at": trained_at,
        "source": {"name": source["name"], "symbol": source["symbol"]},
    }


def read_model(path: str | Path) -> dict[str, Any] | None:
    """The stored model with its record, or None when there is no usable file."""
    target = Path(path)
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        log.info("forecast: no model file at %s; run python -m backend.scripts.train_forecast", target)
        return None
    except (OSError, ValueError) as error:  # ValueError: the bytes are not UTF-8 text
        log.warning("forecast: cannot read %s: %s", target, error)
        return None
    try:
        return check_file(json.loads(text))
    except ValueError as error:  # bad JSON, another version, other features, no record
        log.warning("forecast: %s is not used: %s", target, error)
        return None
    except Exception as error:  # whatever else a damaged file can do: the forecast is an extra
        log.warning("forecast: %s is not used: %s: %s", target, type(error).__name__, error)
        return None


def _finite(forecast: dict[str, Any]) -> bool:
    numbers = [forecast["base_price"], forecast["up_probability"]]
    for point in forecast["points"]:
        numbers += [point["close"], point["low"], point["high"]]
    return all(math.isfinite(value) for value in numbers)


def check_forecast(forecast: dict[str, Any]) -> None:
    """Raise ValueError unless the forecast is finite, in range and agrees with itself."""
    if not _finite(forecast):
        raise ValueError("the forecast holds a number that is not finite")
    base, atr, made_at = forecast["base_price"], forecast.get("atr"), forecast["made_at"]
    points = forecast["points"]
    if not base > 0 or len(points) != fc.HORIZON:
        raise ValueError("the forecast has no base price or not one point per candle")
    if not 0.0 <= forecast["up_probability"] <= 1.0:
        raise ValueError("the forecast's probability is not between 0 and 1")
    if atr is None or not math.isfinite(atr) or not atr > 0:
        raise ValueError("the forecast does not say its ATR")
    for step, point in enumerate(points):
        if point["time"] != made_at + step * fc.STEP:
            raise ValueError("the forecast's points are not on the next candles")
        if not point["low"] <= point["close"] <= point["high"]:
            raise ValueError("a point of the forecast lies outside its own band")
        if max(abs(point[name] - base) for name in ("close", "low", "high")) > MAX_ATR_AWAY * atr:
            raise ValueError(f"a point of the forecast lies more than {MAX_ATR_AWAY:g} ATR from the price")
    moved = points[-1]["close"] - base
    if forecast["direction"] != fc.call(forecast["up_probability"], moved, float(forecast.get("flat_margin", fc.FLAT_MARGIN))):
        raise ValueError("the forecast's direction contradicts its own line or probability")


class Forecaster:
    """Holds the model and the newest forecast. The live feed calls it under its own lock."""

    def __init__(
        self,
        stored: dict[str, Any] | None,
        timeframe: str = fc.TIMEFRAME,
        source: tuple[str, str] | None = None,
    ) -> None:
        """``source`` is the feed's (source name, instrument); None: there is no feed to compare with."""
        if stored is not None and timeframe != fc.TIMEFRAME:
            log.warning("forecast: the model is for %s candles, the feed runs on %s", fc.TIMEFRAME, timeframe)
            stored = None
        if stored is not None and source is not None:
            fitted = (stored["source"]["name"], stored["source"]["symbol"])
            if fitted != tuple(source):
                # Another market: neither the weights nor the measured record say anything about it.
                log.warning(
                    "forecast: the model was fitted on %s %s, the feed is %s %s; it is not used. Train again.",
                    *fitted, *source,
                )  # fmt: skip
                stored = None
        self._model: dict[str, Any] | None = None if stored is None else stored["model"]
        self._skill: dict[str, Any] | None = None if stored is None else stored["skill"]
        self._trained_at: int | None = None if stored is None else stored["trained_at"]
        self._forecast: dict[str, Any] | None = None
        self._tried: int | None = None  # close time of the candle the last attempt was made on
        self._failed = False

    @classmethod
    def from_file(
        cls, path: str | Path, timeframe: str = fc.TIMEFRAME, source: tuple[str, str] | None = None
    ) -> "Forecaster":
        return cls(read_model(path), timeframe, source)

    @property
    def has_model(self) -> bool:
        return self._model is not None

    def update(self, candles: pd.DataFrame | None) -> bool:
        """Make the forecast for the newest closed candle, once. True when a new attempt was made.

        Never raises: whatever goes wrong is logged and leaves no forecast.
        """
        if self._model is None or candles is None or candles.empty:
            return False
        try:
            closed = candles.loc[candles["closed"], "time"]
            if closed.empty:
                return False
            made_at = int(closed.iloc[-1]) + fc.STEP
            if made_at == self._tried:
                return False
            self._tried = made_at
            self._forecast, self._failed = None, False
            forecast = fc.predict(self._model, candles)
            if forecast is not None:
                check_forecast({**forecast, "flat_margin": self._model["flat_margin"]})
            self._forecast = forecast
        except Exception:
            self._forecast, self._failed = None, True
            log.exception("forecast: no forecast for this candle")
        return True

    def block(self, feed_status: str, last_close: int | None) -> dict[str, Any]:
        """The prediction block of /api/state.

        ``last_close`` is the close time of the feed's newest closed candle: a
        forecast made on an older candle is not shown.
        """
        if self._model is None:
            return unavailable(NO_MODEL)
        if feed_status == "down":
            return unavailable(FEED_DOWN, self._skill, self._trained_at)
        forecast = self._forecast
        if forecast is None or forecast["made_at"] != last_close:
            # No reason code fits an error inside this module: the reason stays empty then.
            return unavailable(None if self._failed else NOT_ENOUGH_CANDLES, self._skill, self._trained_at)
        return {
            "available": True,
            "reason": None,
            "timeframe": fc.TIMEFRAME,
            "horizon_candles": fc.HORIZON,
            "made_at": forecast["made_at"],
            "base_price": forecast["base_price"],
            "points": [dict(point) for point in forecast["points"]],
            "up_probability": forecast["up_probability"],
            "direction": forecast["direction"],
            "skill": dict(self._skill) if self._skill is not None else None,
            "trained_at": self._trained_at,
        }
