"""A small forecast of the next four M15 closes, and the honest record of how good it is.

What it is: after each closed M15 candle, the expected close of each of the
next four candles (60 minutes), a band around it and the probability that
the fourth close is above the last one. It is shown beside the signals. It
never creates, changes, blocks or grades a signal.

How it is made, so that every number can be checked by hand:

- Features (``FEATURE_NAMES``) come from closed M15 candles only: the rule's
  score and its five conditions, the H1 score the filter uses, RSI, MACD,
  distances from the EMAs, ATR, recent returns, the candle's shape, volume,
  the H4 and D1 trend built from the same candles, the clock, and whether the
  broker's market is probably closed. There is no news data in the project,
  so none is used.
- The move to each of the four closes is measured in ATR units and fitted by
  ridge regression on standardised features. A logistic model on the same
  features gives the probability that the fourth close is higher.
- The band is the 10% and 90% quantile of the errors the model made on
  candles it had not been fitted on.

``walk_forward`` measures the model the way it would have been used: fit on
the past, forecast the next block, move on. Its result says in plain data
whether the model beat "the price stays where it is" (``judge``). A forecast
without that record must not be shown.

The record that is shown is the record of the model that is shown, in the
hours the owner can trade: only forecasts made while the broker's market is
probably open are scored, and only those made by a fit of a size comparable
to the final one (``COMPARABLE_TRAIN``). The rest is measured too and kept
beside it, not mixed in.

Nothing here reads a clock, a file or the network. Same candles, same numbers.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from backend.app.core import indicators
from backend.app.core.candles import aggregate, timeframe_seconds, validate
from backend.app.core.pricecheck import probably_closed
from backend.app.core.signal import CONDITION_IDS, SignalParams, params_for

MODEL_VERSION = 1
TIMEFRAME = "M15"
HORIZON = 4  # candles ahead: 4 x M15 = 60 minutes
STEP = timeframe_seconds(TIMEFRAME)

RETURN_SPANS = (1, 4, 16, 96)
VOLUME_WINDOW = 96  # one day of M15 candles
H4_AVERAGE, H4_RANGE = 20, 14
D1_SPAN = 5

FEATURE_NAMES: tuple[str, ...] = (
    "rule_score",
    *(f"cond_{name}" for name in CONDITION_IDS),
    "h1_score",
    "rsi",
    "macd_hist",
    "dist_ema20",
    "dist_ema50",
    "dist_ema200",
    "atr_share",
    *(f"ret_{span}" for span in RETURN_SPANS),
    "body",
    "wick_up",
    "wick_down",
    "volume",
    "h4_trend",
    "d1_trend",
    "hour_sin",
    "hour_cos",
    "day_sin",
    "day_cos",
    "market_closed",
)

# Fitting.
CLIP = 5.0  # a standardised feature further out than this is cut back: one wild candle must not steer the fit
# Strengths tried; the one with the smallest error on held-back rows is kept. The grid reaches far
# up on purpose: when the features carry almost nothing, the honest choice is a model shrunk
# nearly to "no move", and the grid must be able to find it.
RIDGE_GRID = (0.01, 0.1, 1.0, 10.0, 100.0, 1000.0)
HOLD_BACK = 0.2  # the newest share of the rows a fit holds back to choose the strength and measure the band
MIN_FIT_ROWS = 500
BAND = (0.10, 0.90)

# No call is made when the probability of "up" is this close to a coin toss.
FLAT_MARGIN = 0.03

# Walk-forward evaluation: the first fit sees MIN_TRAIN rows (about six weeks),
# then the model is fitted again every BLOCK rows (about ten days).
MIN_TRAIN = 4000
BLOCK = 1000

# A walk-forward block counts for the shown record only when its fit saw at least this share
# of the rows the final model is fitted on. An early fit on a small sample behaves unlike the
# final model (it makes many more direction calls), so its record is not the shown model's.
# One half: the fit that is scored is never less than half the size of the fit that is shown,
# and the newer half of the history is left to score it on.
COMPARABLE_TRAIN = 0.5
SCOPE = "open_market"  # what the shown record covers: forecasts made while the broker's market is probably open

# The rule for "better than the baseline" (see ``judge``).
MIN_CALLS = 300
Z_TWO_SIDED_95 = 1.96
Z_ONE_SIDED_95 = 1.645


class ModelMismatch(ValueError):
    """The stored model was made by another version of this code, or for other features."""


@dataclass(frozen=True)
class Dataset:
    """One row per closed candle, oldest first."""

    time: np.ndarray  # open time of the candle
    made_at: np.ndarray  # its close time: the moment a forecast from this row is made
    base: np.ndarray  # its close
    atr: np.ndarray
    features: np.ndarray  # rows x FEATURE_NAMES; NaN where a feature cannot be known yet
    targets: np.ndarray  # rows x HORIZON: (close h candles on - close) / ATR; NaN where unknown
    usable: np.ndarray  # every feature is known: a forecast can be made from this row
    scored: np.ndarray  # usable, and all four later closes are known


@dataclass(frozen=True)
class Evaluation:
    """What a walk-forward run measured. ``skill`` is the block the API shows.

    ``skill`` covers the comparable blocks in open-market hours. The same
    numbers for the closed hours of those blocks, and for every forecast of
    every block, are kept beside it for the record.
    """

    skill: dict[str, Any]
    blocks: int  # walk-forward blocks run
    scored_blocks: int  # of those, the blocks whose fit was of a comparable size: the ones in ``skill``
    closed_market: dict[str, Any] | None  # the comparable blocks in the hours the market is probably closed
    all_forecasts: dict[str, Any] | None  # every block, every hour
    mae_by_step: tuple[float, ...]  # price units, for the close 1, 2, 3 and 4 candles on
    baseline_mae_by_step: tuple[float, ...]
    up_share: float  # how often the fourth close really was higher
    hit_floor: float  # lower 95% bound of the hit rate, overlap counted
    mae_gain_floor: float  # lower 95% bound of (baseline error - model error), price units


# --- features ---------------------------------------------------------------------


def _compare(a: np.ndarray, b: np.ndarray | float) -> np.ndarray:
    """``signal.compare`` for whole columns: +1, 0 or -1, NaN where a side is unknown."""
    a = np.asarray(a, dtype=float)
    b = np.broadcast_to(np.asarray(b, dtype=float), a.shape)
    difference = a - b
    tolerance = np.maximum(1e-9 * np.maximum(np.abs(a), np.abs(b)), 1e-9)
    value = np.where(np.abs(difference) <= tolerance, 0.0, np.sign(difference))
    return np.where(np.isfinite(difference), value, np.nan)


def _rule(frame: pd.DataFrame, params: SignalParams) -> dict[str, np.ndarray]:
    """The rule of ``signal.evaluate`` on every row at once, with the indicators it uses.

    ``score`` and the conditions are NaN where ``evaluate`` would give no
    score: before ``min_bars`` candles, or while an indicator is warming up.
    """
    close = frame["close"].astype(float)
    macd = indicators.macd(close, params.macd_fast, params.macd_slow, params.macd_signal)
    out = {
        "close": close.to_numpy(),
        "ema_fast": indicators.ema(close, params.ema_fast).to_numpy(),
        "ema_mid": indicators.ema(close, params.ema_mid).to_numpy(),
        "ema_slow": indicators.ema(close, params.ema_slow).to_numpy(),
        "macd": macd["macd"].to_numpy(),
        "macd_signal": macd["signal"].to_numpy(),
        "macd_hist": macd["hist"].to_numpy(),
        "rsi": indicators.rsi(close, params.rsi_period).to_numpy(),
        "atr": indicators.atr(
            frame["high"].astype(float), frame["low"].astype(float), close, params.atr_period
        ).to_numpy(),
    }
    rsi = out["rsi"]
    zone = np.where(
        (rsi > params.rsi_mid) & (rsi < params.rsi_upper),
        1.0,
        np.where((rsi > params.rsi_lower) & (rsi < params.rsi_mid), -1.0, 0.0),
    )
    conditions = np.column_stack(
        [
            _compare(out["close"], out["ema_slow"]),
            _compare(out["ema_fast"], out["ema_mid"]),
            _compare(out["macd"], out["macd_signal"]),
            _compare(out["macd"], 0.0),
            np.where(np.isfinite(rsi), zone, np.nan),
        ]
    )
    known = (np.arange(len(frame)) >= params.min_bars - 1) & np.isfinite(conditions).all(axis=1)
    known &= np.isfinite(out["atr"])
    conditions[~known] = np.nan
    out["conditions"] = conditions
    out["score"] = conditions.sum(axis=1)
    return out


def _higher(frame: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Closed candles of a longer timeframe, built from the M15 ones (never over a hole)."""
    if frame.empty:
        return frame
    higher = aggregate(frame, TIMEFRAME, timeframe)
    return higher[higher["closed"]].reset_index(drop=True)


def _latest(values: np.ndarray, closes: np.ndarray, at: np.ndarray) -> np.ndarray:
    """For each moment in ``at``: the value of the newest longer candle that had closed by then."""
    out = np.full(len(at), np.nan)
    if len(values):
        known = np.searchsorted(closes, at, side="right")  # how many had closed
        has = known > 0
        out[has] = values[known[has] - 1]
    return out


def _mean_range(frame: pd.DataFrame, window: int) -> pd.Series:
    ranges = indicators.true_range(frame["high"], frame["low"], frame["close"])
    return ranges.rolling(window).mean()


def _divide(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.asarray(a, dtype=float) / np.asarray(b, dtype=float)


def dataset(candles: pd.DataFrame) -> Dataset:
    """Features and targets for every closed candle. A forming candle is dropped first.

    Every feature of a row is computed from that candle and earlier ones
    only, so the row is the same whether or not later candles exist.
    """
    frame = validate(candles, TIMEFRAME)
    frame = frame[frame["closed"]].reset_index(drop=True)
    count = len(frame)
    time = frame["time"].to_numpy(dtype="int64")
    made_at = time + STEP
    params = params_for(TIMEFRAME)
    rule = _rule(frame, params)
    close, atr = rule["close"], rule["atr"]
    columns: dict[str, np.ndarray] = {"rule_score": rule["score"]}
    for index, name in enumerate(CONDITION_IDS):
        columns[f"cond_{name}"] = rule["conditions"][:, index]

    # The H1 score exactly as filters.replay takes it: from the H1 candles closed by this candle's close.
    h1 = _higher(frame, "H1")
    h1_score = _rule(h1, params_for("H1"))["score"] if len(h1) else np.empty(0)
    columns["h1_score"] = _latest(h1_score, h1["time"].to_numpy() + 3600 if len(h1) else np.empty(0), made_at)

    columns["rsi"] = (rule["rsi"] - 50.0) / 50.0
    columns["macd_hist"] = _divide(rule["macd_hist"], atr)
    columns["dist_ema20"] = _divide(close - rule["ema_fast"], atr)
    columns["dist_ema50"] = _divide(close - rule["ema_mid"], atr)
    columns["dist_ema200"] = _divide(close - rule["ema_slow"], atr)
    columns["atr_share"] = _divide(atr, close)

    closes, times = frame["close"], frame["time"]
    for span in RETURN_SPANS:
        moved = (closes - closes.shift(span)).to_numpy()
        unbroken = (times - times.shift(span)).to_numpy() == span * STEP  # no hole inside the span
        columns[f"ret_{span}"] = np.where(unbroken, _divide(moved, atr * math.sqrt(span)), np.nan)

    opened, high, low = (frame[name].to_numpy(dtype=float) for name in ("open", "high", "low"))
    size = high - low
    flat = size <= 0
    safe = np.where(flat, 1.0, size)
    columns["body"] = np.where(flat, 0.0, (close - opened) / safe)
    columns["wick_up"] = np.where(flat, 0.0, (high - np.maximum(opened, close)) / safe)
    columns["wick_down"] = np.where(flat, 0.0, (np.minimum(opened, close) - low) / safe)

    # Volume against the average of the day before this candle, as a logarithm.
    volume = frame["volume"]
    usual = volume.shift(1).rolling(VOLUME_WINDOW).mean().to_numpy()
    ratio = np.where(usual > 0, _divide(volume.to_numpy(), np.where(usual > 0, usual, 1.0)), np.nan)
    columns["volume"] = np.log(np.clip(ratio, 0.05, 20.0))

    # The longer trends use fixed windows, so they do not depend on where the history starts.
    h4 = _higher(frame, "H4")
    if len(h4):
        trend = (h4["close"] - h4["close"].rolling(H4_AVERAGE).mean()) / _mean_range(h4, H4_RANGE)
        columns["h4_trend"] = _latest(_finite(trend), h4["time"].to_numpy() + 4 * 3600, made_at)
    else:
        columns["h4_trend"] = np.full(count, np.nan)
    d1 = _higher(frame, "D1")
    if len(d1):
        trend = (d1["close"] - d1["close"].shift(D1_SPAN)) / (_mean_range(d1, D1_SPAN) * math.sqrt(D1_SPAN))
        columns["d1_trend"] = _latest(_finite(trend), d1["time"].to_numpy() + 86400, made_at)
    else:
        columns["d1_trend"] = np.full(count, np.nan)

    # The clock at the candle's close: the moment the forecast is made.
    hour = (made_at % 86400) / 86400.0
    day = ((made_at // 86400 + 3) % 7 + hour) / 7.0  # Monday is 0
    columns["hour_sin"], columns["hour_cos"] = np.sin(2 * np.pi * hour), np.cos(2 * np.pi * hour)
    columns["day_sin"], columns["day_cos"] = np.sin(2 * np.pi * day), np.cos(2 * np.pi * day)
    columns["market_closed"] = np.array([float(probably_closed(int(at))) for at in made_at], dtype=float)

    features = np.column_stack([np.asarray(columns[name], dtype=float) for name in FEATURE_NAMES])
    features = features.reshape(count, len(FEATURE_NAMES))
    features[~np.isfinite(features)] = np.nan

    targets = np.full((count, HORIZON), np.nan)
    for step in range(1, HORIZON + 1):
        moved = (closes.shift(-step) - closes).to_numpy()
        unbroken = (times.shift(-step) - times).to_numpy() == step * STEP
        targets[:, step - 1] = np.where(unbroken, _divide(moved, atr), np.nan)
    targets[~np.isfinite(targets)] = np.nan

    usable = np.isfinite(features).all(axis=1) & np.isfinite(atr) & (atr > 0)
    scored = usable & np.isfinite(targets).all(axis=1)
    return Dataset(time, made_at, close, atr, features, targets, usable, scored)


def _finite(values: pd.Series) -> np.ndarray:
    out = values.to_numpy(dtype=float).copy()
    out[~np.isfinite(out)] = np.nan
    return out


# --- the model ----------------------------------------------------------------------


def _scale(features: np.ndarray, mean: np.ndarray, std: np.ndarray) -> np.ndarray:
    """Standardised features with a leading column of ones for the constant term."""
    scaled = np.clip((features - mean) / std, -CLIP, CLIP)
    return np.column_stack([np.ones(len(scaled)), scaled])


def _moments(features: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    mean = features.mean(axis=0)
    std = features.std(axis=0)
    return mean, np.where(std > 1e-12, std, 1.0)  # a feature that never moves carries nothing


def _penalty(width: int, strength: float) -> np.ndarray:
    penalty = np.eye(width) * strength
    penalty[0, 0] = 0.0  # the constant term is not shrunk
    return penalty


def _ridge(design: np.ndarray, targets: np.ndarray, strength: float) -> np.ndarray:
    rows = len(design)
    return np.linalg.solve(
        design.T @ design / rows + _penalty(design.shape[1], strength), design.T @ targets / rows
    )


def _logistic(design: np.ndarray, up: np.ndarray, strength: float) -> np.ndarray:
    """Penalised logistic regression by Newton's method. Starts at zero, so it is repeatable."""
    rows, width = design.shape
    penalty = _penalty(width, strength)
    weights = np.zeros(width)
    for _ in range(50):
        chance = 1.0 / (1.0 + np.exp(-np.clip(design @ weights, -30.0, 30.0)))
        slope = design.T @ (chance - up) / rows + penalty @ weights
        curve = (design.T * (chance * (1.0 - chance))) @ design / rows + penalty
        change = np.linalg.solve(curve, slope)
        weights = weights - change
        if np.abs(change).max() < 1e-10:
            break
    return weights


def _fit_rows(features: np.ndarray, targets: np.ndarray, positions: np.ndarray, gap: int = HORIZON) -> dict[str, Any]:
    """Fit on the given rows; ``positions`` are their candle numbers, oldest first.

    The newest fifth of the rows is held back once, with the gap before it:
    a model fitted on the older rows forecasts them. Those errors choose the
    ridge strength and give the band. Then everything is fitted again on all
    rows with that strength.
    """
    rows = len(features)
    if rows < MIN_FIT_ROWS:
        raise ValueError(f"a fit needs at least {MIN_FIT_ROWS} rows, got {rows}")
    cut = int(rows * (1.0 - HOLD_BACK))
    older = positions < positions[cut] - gap
    if older.sum() < MIN_FIT_ROWS // 2:
        raise ValueError("too few rows before the held-back part")
    mean, std = _moments(features[older])
    design_older = _scale(features[older], mean, std)
    design_held = _scale(features[cut:], mean, std)
    best: tuple[float, float, np.ndarray] | None = None
    for strength in RIDGE_GRID:
        errors = targets[cut:] - design_held @ _ridge(design_older, targets[older], strength)
        loss = float((errors**2).mean())
        if best is None or loss < best[0]:
            best = (loss, strength, errors)
    assert best is not None
    _, strength, errors = best
    low, high = np.quantile(errors, BAND[0], axis=0), np.quantile(errors, BAND[1], axis=0)

    mean, std = _moments(features)
    design = _scale(features, mean, std)
    coef = _ridge(design, targets, strength)
    up = (targets[:, HORIZON - 1] > 0).astype(float)
    return {
        "version": MODEL_VERSION,
        "timeframe": TIMEFRAME,
        "horizon": HORIZON,
        "features": list(FEATURE_NAMES),
        "mean": [float(v) for v in mean],
        "std": [float(v) for v in std],
        "clip": CLIP,
        "ridge": float(strength),
        # One row per step ahead: the constant term, then one weight per feature. ATR units.
        "coef": [[float(v) for v in coef[:, step]] for step in range(HORIZON)],
        "up_coef": [float(v) for v in _logistic(design, up, strength)],
        # Quantiles of (real move - forecast move) on the held-back rows, ATR units, per step.
        "band_low": [float(v) for v in low],
        "band_high": [float(v) for v in high],
        "flat_margin": FLAT_MARGIN,
        "rows": int(rows),
        "band_rows": int(rows - cut),
    }


def check(params: Any) -> None:
    """Raise ModelMismatch unless ``params`` is a model this code can use."""
    if not isinstance(params, dict):
        raise ModelMismatch("the model is not a dictionary")
    if params.get("version") != MODEL_VERSION:
        raise ModelMismatch(f"model version {params.get('version')!r}, this code needs {MODEL_VERSION}")
    if params.get("features") != list(FEATURE_NAMES):
        raise ModelMismatch("the model was fitted on other features")
    if params.get("timeframe") != TIMEFRAME or params.get("horizon") != HORIZON:
        raise ModelMismatch("the model is for another timeframe or horizon")
    width = len(FEATURE_NAMES)
    shapes = {
        "mean": (width,),
        "std": (width,),
        "coef": (HORIZON, width + 1),
        "up_coef": (width + 1,),
        "band_low": (HORIZON,),
        "band_high": (HORIZON,),
    }
    for name, shape in shapes.items():
        try:
            values = np.asarray(params.get(name), dtype=float)
        except (TypeError, ValueError):
            raise ModelMismatch(f"the model's {name} is not numbers") from None
        if values.shape != shape or not np.isfinite(values).all():
            raise ModelMismatch(f"the model's {name} has the wrong shape or holds a non-number")
    if not (np.asarray(params["std"], dtype=float) > 0).all():
        raise ModelMismatch("the model's std must be positive")
    if (np.asarray(params["band_low"], dtype=float) > np.asarray(params["band_high"], dtype=float)).any():
        # The 10% quantile of the errors above the 90% one: no fit gives that.
        raise ModelMismatch("the model's band is upside down")
    for name in ("clip", "flat_margin"):
        value = params.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ModelMismatch(f"the model's {name} is not a positive number")


def _predict_rows(params: dict[str, Any], features: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Forecast moves (rows x HORIZON, ATR units) and the probability that the last one is up."""
    scaled = np.clip(
        (features - np.asarray(params["mean"], dtype=float)) / np.asarray(params["std"], dtype=float),
        -float(params["clip"]),
        float(params["clip"]),
    )
    design = np.column_stack([np.ones(len(scaled)), scaled])
    moves = design @ np.asarray(params["coef"], dtype=float).T
    lean = np.clip(design @ np.asarray(params["up_coef"], dtype=float), -30.0, 30.0)
    return moves, 1.0 / (1.0 + np.exp(-lean))


def direction(up_probability: float, margin: float = FLAT_MARGIN) -> str:
    """"up", "down", or "flat" when the probability is too close to a coin toss to call."""
    if up_probability > 0.5 + margin:
        return "up"
    if up_probability < 0.5 - margin:
        return "down"
    return "flat"


def call(up_probability: float, move: float, margin: float = FLAT_MARGIN) -> str:
    """The direction word that is shown. It never contradicts the drawn line.

    The probability and the line come from two separate fits. ``move`` is
    the line's move to the last close. A call is made only when both say the
    same: "up" needs a probability above the flat zone and a line that ends
    above the base price, "down" the reverse. Anything else is "flat".
    """
    side = direction(up_probability, margin)
    if side == "up" and not move > 0:
        return "flat"
    if side == "down" and not move < 0:
        return "flat"
    return side


def fit(candles: pd.DataFrame) -> dict[str, Any]:
    """Fit the model on every closed candle whose four later closes are known.

    Returns plain numbers that json can store. Raises ValueError when there
    are too few candles.
    """
    data = dataset(candles)
    rows = np.flatnonzero(data.scored)
    return _fit_rows(data.features[rows], data.targets[rows], rows)


def predict(params: dict[str, Any], candles: pd.DataFrame) -> dict[str, Any] | None:
    """The forecast made at the close of the last closed candle, or None with too few candles.

    Raises ModelMismatch for a model of another version or with other features.
    """
    check(params)
    data = dataset(candles)
    if not len(data.usable) or not data.usable[-1]:
        return None
    moves, chance = _predict_rows(params, data.features[-1:])
    base, atr, made_at = float(data.base[-1]), float(data.atr[-1]), int(data.made_at[-1])
    points = []
    for step in range(HORIZON):
        move = float(moves[0, step])
        # The band is the model's usual error around its own line; it always holds the line.
        low = move + min(float(params["band_low"][step]), 0.0)
        high = move + max(float(params["band_high"][step]), 0.0)
        points.append(
            {
                "time": made_at + step * STEP,  # open time of that future candle
                "close": base + move * atr,
                "low": base + low * atr,
                "high": base + high * atr,
            }
        )
    up = float(chance[0])
    return {
        "made_at": made_at,
        "base_price": base,
        "points": points,
        "up_probability": up,
        "direction": call(up, float(moves[0, HORIZON - 1]), float(params["flat_margin"])),
        "atr": atr,  # the unit the model works in; lets the caller judge how far the points lie
    }


def own_calls(params: dict[str, Any], candles: pd.DataFrame) -> dict[str, int]:
    """How often a model itself says "up" or "down" on the candles a fit on ``candles`` uses.

    Counted with the rule of ``call``, on every such candle. This is not a
    record: the model may have been fitted on these very candles, so nothing
    here says whether a call was right. It says only how often the shown word
    is not "flat". The walk-forward record cannot say that about the final
    model: every fit chooses its own ridge strength, and a fit with another
    strength leaves the flat zone more or less often.

    Raises ModelMismatch for a model of another version or with other features.
    """
    check(params)
    data = dataset(candles)
    rows = np.flatnonzero(data.scored)
    if not len(rows):
        return {"rows": 0, "up": 0, "down": 0, "open_market_rows": 0, "open_market_calls": 0}
    moves, chance = _predict_rows(params, data.features[rows])
    margin = float(params["flat_margin"])
    line = moves[:, HORIZON - 1]
    up = (chance > 0.5 + margin) & (line > 0)
    down = (chance < 0.5 - margin) & (line < 0)
    open_market = data.features[rows, FEATURE_NAMES.index("market_closed")] < 0.5
    return {
        "rows": int(len(rows)),
        "up": int(up.sum()),
        "down": int(down.sum()),
        "open_market_rows": int(open_market.sum()),
        "open_market_calls": int(((up | down) & open_market).sum()),
    }


# --- the measured record --------------------------------------------------------------


def splits(
    positions: np.ndarray, *, min_train: int = MIN_TRAIN, block: int = BLOCK, gap: int = HORIZON
) -> list[tuple[np.ndarray, np.ndarray]]:
    """Walk-forward blocks over rows whose candle numbers are ``positions`` (oldest first).

    Each pair is (rows to fit on, rows to score). The scored rows are the
    next ``block`` rows; the fit takes every earlier row that lies more than
    ``gap`` candles before the first scored one. With a gap of HORIZON the
    last close a training row looks at is older than the first scored candle,
    so no target overlaps what is scored. The window only grows.
    """
    if gap < HORIZON:
        raise ValueError(f"the gap must be at least the horizon ({HORIZON} candles), got {gap}")
    if min_train < 1 or block < 1:
        raise ValueError("min_train and block must be positive")
    positions = np.asarray(positions)
    out = []
    for start in range(min_train, len(positions), block):
        test = np.arange(start, min(start + block, len(positions)))
        train = np.flatnonzero(positions < positions[start] - gap)
        out.append((train, test))
    return out


def hit_rate_floor(hits: int, calls: int, overlap: int = HORIZON) -> float:
    """Lower 95% bound (Wilson) of a hit rate, with overlapping calls counted as fewer.

    Calls made one candle apart share three of their four candles, so they
    are not separate tries. They are counted as ``calls / overlap`` tries.
    """
    tries = calls / max(overlap, 1)
    if tries <= 0:
        return 0.0
    rate = hits / calls
    z = Z_TWO_SIDED_95
    centre = rate + z * z / (2 * tries)
    spread = z * math.sqrt(rate * (1 - rate) / tries + z * z / (4 * tries * tries))
    return (centre - spread) / (1 + z * z / tries)


def mae_gain_floor(model_errors: np.ndarray, baseline_errors: np.ndarray, overlap: int = HORIZON) -> float:
    """Lower 95% bound of the mean of (baseline error - model error), overlap counted as above."""
    gain = np.asarray(baseline_errors, dtype=float) - np.asarray(model_errors, dtype=float)
    tries = len(gain) / max(overlap, 1)
    if tries < 2:
        return -math.inf
    return float(gain.mean() - Z_ONE_SIDED_95 * gain.std(ddof=1) / math.sqrt(tries))


def judge(*, model_errors: np.ndarray, baseline_errors: np.ndarray, hits: int, calls: int) -> bool:
    """Whether the model measurably beat "the price stays where it is".

    All three must hold, on forecasts the model had not been fitted on:

    1. It made at least MIN_CALLS calls outside the flat zone. Fewer say nothing.
    2. Its error at the fourth close is lower than the baseline's, and by
       more than chance explains: the lower 95% bound of the mean gain is
       above zero.
    3. Its calls were right more often than a coin: the lower 95% bound of
       the hit rate is above 0.5.

    Why so strict: price changes are close to noise, and a model with thirty
    weights will look a little better than nothing on some stretch by luck
    alone. Forecasts made one candle apart also overlap, so both bounds count
    them as a quarter as many tries. A model that fails this is still shown,
    but marked as no better than doing nothing.
    """
    if calls < MIN_CALLS or len(model_errors) == 0:
        return False
    if not float(np.mean(model_errors)) < float(np.mean(baseline_errors)):
        return False
    if not mae_gain_floor(model_errors, baseline_errors) > 0.0:
        return False
    return hit_rate_floor(hits, calls) > 0.5


def _record(
    *,
    made_at: np.ndarray,
    real: np.ndarray,
    move: np.ndarray,
    chance: np.ndarray,
    margin: np.ndarray,
    low: np.ndarray,
    high: np.ndarray,
    atr: np.ndarray,
) -> dict[str, Any] | None:
    """Score one set of forecasts, oldest first. None when the set is empty.

    The first nine keys are the skill block as it always was. The rest say
    how the calls split, and what the best constant call ("always up" or
    "always down") would have scored on the very same calls: a hit rate
    means little without it in a market that mostly rose or mostly fell.
    """
    if not len(real):
        return None
    last = HORIZON - 1
    errors = np.abs(real - move) * atr[:, None]  # price units
    baseline = np.abs(real) * atr[:, None]
    went_up, went_down = real[:, last] > 0, real[:, last] < 0
    # A call needs the probability and the line to agree, exactly as ``call`` decides what is shown.
    called_up = (chance > 0.5 + margin) & (move[:, last] > 0)
    called_down = (chance < 0.5 - margin) & (move[:, last] < 0)
    called = called_up | called_down
    calls = int(called.sum())
    # A close exactly at the base price counts against the call.
    hits = int((called_up & went_up).sum() + (called_down & went_down).sum())
    always_up, always_down = int((called & went_up).sum()), int((called & went_down).sum())
    miss = real[:, last] - move[:, last]
    inside = (miss >= np.minimum(low, 0.0)) & (miss <= np.maximum(high, 0.0))
    return {
        "samples": int(len(real)),
        "from": int(made_at[0]),
        "to": int(made_at[-1]),
        "hit_rate": hits / calls if calls else 0.0,
        "calls": calls,
        "mae": float(errors[:, last].mean()),
        "baseline_mae": float(baseline[:, last].mean()),
        "coverage": float(inside.mean()),
        "better_than_baseline": judge(
            model_errors=errors[:, last], baseline_errors=baseline[:, last], hits=hits, calls=calls
        ),
        "calls_up": int(called_up.sum()),
        "calls_down": int(called_down.sum()),
        "hit_rate_baseline": max(always_up, always_down) / calls if calls else 0.0,
        "hit_rate_baseline_call": "up" if always_up >= always_down else "down",
        "hit_rate_floor": hit_rate_floor(hits, calls),
        "mae_gain_floor": mae_gain_floor(errors[:, last], baseline[:, last]),
        "mae_by_step": [float(v) for v in errors.mean(axis=0)],
        "baseline_mae_by_step": [float(v) for v in baseline.mean(axis=0)],
        "up_share": float(went_up.mean()),
    }


SKILL_KEYS = (
    "samples", "from", "to", "hit_rate", "calls", "mae", "baseline_mae", "coverage", "better_than_baseline",
    "calls_up", "calls_down", "hit_rate_baseline", "hit_rate_baseline_call",
)  # fmt: skip


def walk_forward(
    candles: pd.DataFrame, *, min_train: int = MIN_TRAIN, block: int = BLOCK, gap: int = HORIZON
) -> Evaluation | None:
    """Measure the model out of sample: fit on the past, forecast the next block, move on.

    Returns None when the history is too short: for a single block, or for a
    single open-market forecast from a fit of a comparable size.
    """
    data = dataset(candles)
    rows = np.flatnonzero(data.scored)
    features, targets = data.features[rows], data.targets[rows]
    blocks = splits(rows, min_train=min_train, block=block, gap=gap)
    if not blocks:
        return None
    moves, chances, lows, highs, margins, tested, fitted_on = [], [], [], [], [], [], []
    for train, test in blocks:
        params = _fit_rows(features[train], targets[train], rows[train], gap)
        move, chance = _predict_rows(params, features[test])
        moves.append(move)
        chances.append(chance)
        lows.append(np.full(len(test), params["band_low"][HORIZON - 1]))
        highs.append(np.full(len(test), params["band_high"][HORIZON - 1]))
        margins.append(np.full(len(test), params["flat_margin"]))
        tested.append(test)
        fitted_on.append(np.full(len(test), len(train)))
    test = np.concatenate(tested)
    fitted = np.concatenate(fitted_on)
    parts = {
        "made_at": data.made_at[rows[test]],
        "real": targets[test],
        "move": np.concatenate(moves),
        "chance": np.concatenate(chances),
        "margin": np.concatenate(margins),
        "low": np.concatenate(lows),
        "high": np.concatenate(highs),
        "atr": data.atr[rows[test]],
    }

    def record(keep: np.ndarray) -> dict[str, Any] | None:
        return _record(**{name: values[keep] for name, values in parts.items()})

    # The final model is fitted on every scored row: a block is comparable when its fit saw
    # at least COMPARABLE_TRAIN of that.
    comparable = fitted >= COMPARABLE_TRAIN * len(rows)
    market_closed = features[test, FEATURE_NAMES.index("market_closed")] > 0.5
    shown = record(comparable & ~market_closed)
    if shown is None:
        return None
    skill = {name: shown[name] for name in SKILL_KEYS}
    skill["scope"] = SCOPE
    skill["train_rows"] = int(fitted[comparable].min())  # the smallest fit behind a scored forecast
    return Evaluation(
        skill=skill,
        blocks=len(blocks),
        scored_blocks=int(len(np.unique(fitted[comparable]))),
        closed_market=record(comparable & market_closed),
        all_forecasts=record(np.ones(len(test), dtype=bool)),
        mae_by_step=tuple(shown["mae_by_step"]),
        baseline_mae_by_step=tuple(shown["baseline_mae_by_step"]),
        up_share=shown["up_share"],
        hit_floor=shown["hit_rate_floor"],
        mae_gain_floor=shown["mae_gain_floor"],
    )


def evaluate(
    candles: pd.DataFrame, *, min_train: int = MIN_TRAIN, block: int = BLOCK, gap: int = HORIZON
) -> dict[str, Any] | None:
    """The skill block of the API: the model's measured walk-forward record, or None."""
    result = walk_forward(candles, min_train=min_train, block=block, gap=gap)
    return None if result is None else result.skill
