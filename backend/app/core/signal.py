"""Rule-based signal engine, rules_version 1.

Five conditions each score +1, 0 or -1; the sum runs from -5 to +5.
A sum of +3 or more proposes BUY, -3 or less proposes SELL, anything else HOLD.
BUY and SELL come with ATR-based levels: an entry zone, a stop loss and a
take profit.

This module only proposes. The filters that can still block a proposal
(higher-timeframe conflict, news window, market closed, stale feed,
duplicates) live in ``filters.py`` from Phase 5.

The thresholds and multipliers are starting values, not a validated strategy.
Change them only together with a backtest, and bump RULES_VERSION when you do.

Input candles: oldest first, columns ``open``, ``high``, ``low``, ``close``;
optionally ``time`` (candle open time, UTC Unix seconds) and ``closed`` (bool).
Rows with ``closed == False`` are ignored: signals use closed candles only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import pandas as pd

from backend.app.core import indicators

RULES_VERSION = 1

BUY = "BUY"
SELL = "SELL"
HOLD = "HOLD"

INSUFFICIENT_BARS = "INSUFFICIENT_BARS"

REQUIRED_COLUMNS = ("open", "high", "low", "close")

CONDITION_IDS = (
    "trend_ema200",
    "trend_ema20_50",
    "macd_vs_signal",
    "macd_vs_zero",
    "rsi_zone",
)


@dataclass(frozen=True)
class SignalParams:
    """Tunable numbers of the rule set. Defaults are the plan's starting values."""

    min_bars: int = 300
    buy_threshold: int = 3
    sell_threshold: int = -3

    ema_fast: int = 20
    ema_mid: int = 50
    ema_slow: int = 200
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    rsi_period: int = 14
    rsi_lower: float = 30.0
    rsi_mid: float = 50.0
    rsi_upper: float = 70.0
    atr_period: int = 14

    sl_atr_mult: float = 1.5
    tp_atr_mult: float = 2.0
    zone_atr_mult: float = 0.25

    valid_bars: int = 2
    timeframe_seconds: int = 900  # M15

    def __post_init__(self) -> None:
        if self.sell_threshold >= self.buy_threshold:
            raise ValueError("sell_threshold must be below buy_threshold")
        if not self.rsi_lower < self.rsi_mid < self.rsi_upper:
            raise ValueError("rsi bounds must satisfy lower < mid < upper")
        if min(self.sl_atr_mult, self.tp_atr_mult, self.zone_atr_mult) <= 0:
            raise ValueError("ATR multipliers must be positive")


@dataclass(frozen=True)
class Condition:
    id: str
    value: int  # +1, 0 or -1


@dataclass(frozen=True)
class Levels:
    """Price levels for one proposal. Distances are positive price amounts.

    The feed's price can differ from a broker's. Apply ``sl_distance`` and
    ``tp_distance`` to the real fill price instead of copying absolute levels.
    """

    entry: float
    zone_low: float
    zone_high: float
    stop_loss: float
    take_profit: float
    sl_distance: float
    tp_distance: float


@dataclass(frozen=True)
class SignalResult:
    signal: str | None  # BUY, SELL, HOLD, or None when no evaluation was possible
    score: int | None
    strength: float | None  # abs(score) / number of conditions
    conditions: tuple[Condition, ...]
    indicators: dict[str, float]
    levels: Levels | None
    candle_time: int | None  # open time of the last closed candle
    signal_time: int | None  # close time of that candle
    expires_at: int | None  # signal_time + valid_bars candles, BUY and SELL only
    reason: str | None  # why signal is None
    rules_version: int = RULES_VERSION

    def to_dict(self) -> dict[str, Any]:
        """Plain, JSON-serializable form."""
        return {
            "signal": self.signal,
            "score": self.score,
            "strength": self.strength,
            "conditions": [{"id": c.id, "value": c.value} for c in self.conditions],
            "indicators": dict(self.indicators),
            "levels": None if self.levels is None else dict(vars(self.levels)),
            "candle_time": self.candle_time,
            "signal_time": self.signal_time,
            "expires_at": self.expires_at,
            "reason": self.reason,
            "rules_version": self.rules_version,
        }


def compare(a: float, b: float) -> int:
    """+1 if a is above b, -1 if below, 0 if equal within floating-point tolerance."""
    if math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9):
        return 0
    return 1 if a > b else -1


def rsi_zone(value: float, params: SignalParams) -> int:
    """+1 between mid and upper, -1 between lower and mid, 0 elsewhere.

    Overbought (>= upper) and oversold (<= lower) score 0: no trend point
    is given at the extremes.
    """
    if params.rsi_mid < value < params.rsi_upper:
        return 1
    if params.rsi_lower < value < params.rsi_mid:
        return -1
    return 0


def score_conditions(
    *,
    close: float,
    ema_fast: float,
    ema_mid: float,
    ema_slow: float,
    macd: float,
    macd_signal: float,
    rsi: float,
    params: SignalParams | None = None,
) -> tuple[Condition, ...]:
    """Score the five conditions for one candle, in the order of CONDITION_IDS."""
    params = params or SignalParams()
    values = (
        compare(close, ema_slow),
        compare(ema_fast, ema_mid),
        compare(macd, macd_signal),
        compare(macd, 0.0),
        rsi_zone(rsi, params),
    )
    return tuple(Condition(id=cid, value=value) for cid, value in zip(CONDITION_IDS, values))


def classify(score: int, params: SignalParams | None = None) -> str:
    """Map a total score to BUY, SELL or HOLD."""
    params = params or SignalParams()
    if score >= params.buy_threshold:
        return BUY
    if score <= params.sell_threshold:
        return SELL
    return HOLD


def compute_levels(side: str, entry: float, atr: float, params: SignalParams | None = None) -> Levels:
    """Entry zone, stop loss and take profit for a BUY or SELL at ``entry``."""
    params = params or SignalParams()
    if side not in (BUY, SELL):
        raise ValueError(f"levels exist only for BUY or SELL, got {side!r}")
    if not (math.isfinite(entry) and math.isfinite(atr)) or atr < 0:
        raise ValueError(f"entry and atr must be finite and atr non-negative, got {entry!r}, {atr!r}")
    direction = 1.0 if side == BUY else -1.0
    sl_distance = params.sl_atr_mult * atr
    tp_distance = params.tp_atr_mult * atr
    half_zone = params.zone_atr_mult * atr
    return Levels(
        entry=entry,
        zone_low=entry - half_zone,
        zone_high=entry + half_zone,
        stop_loss=entry - direction * sl_distance,
        take_profit=entry + direction * tp_distance,
        sl_distance=sl_distance,
        tp_distance=tp_distance,
    )


def closed_candles(candles: pd.DataFrame) -> pd.DataFrame:
    """Validate the frame and drop candles that are still forming."""
    missing = [column for column in REQUIRED_COLUMNS if column not in candles.columns]
    if missing:
        raise ValueError(f"candles are missing columns: {', '.join(missing)}")
    frame = candles
    if "closed" in frame.columns:
        frame = frame[frame["closed"].astype(bool)]
    if "time" in frame.columns and not frame["time"].is_monotonic_increasing:
        raise ValueError("candles must be ordered oldest first")
    return frame


def evaluate(candles: pd.DataFrame, params: SignalParams | None = None) -> SignalResult:
    """Evaluate the rule set on the last closed candle."""
    params = params or SignalParams()
    frame = closed_candles(candles)
    candle_time = int(frame["time"].iloc[-1]) if "time" in frame.columns and len(frame) else None
    signal_time = None if candle_time is None else candle_time + params.timeframe_seconds

    def no_signal(reason: str) -> SignalResult:
        return SignalResult(
            signal=None,
            score=None,
            strength=None,
            conditions=(),
            indicators={},
            levels=None,
            candle_time=candle_time,
            signal_time=signal_time,
            expires_at=None,
            reason=reason,
        )

    if len(frame) < params.min_bars:
        return no_signal(INSUFFICIENT_BARS)

    close = frame["close"].astype(float)
    high = frame["high"].astype(float)
    low = frame["low"].astype(float)
    macd_frame = indicators.macd(close, params.macd_fast, params.macd_slow, params.macd_signal)
    latest = {
        "close": close.iloc[-1],
        "ema_fast": indicators.ema(close, params.ema_fast).iloc[-1],
        "ema_mid": indicators.ema(close, params.ema_mid).iloc[-1],
        "ema_slow": indicators.ema(close, params.ema_slow).iloc[-1],
        "macd": macd_frame["macd"].iloc[-1],
        "macd_signal": macd_frame["signal"].iloc[-1],
        "rsi": indicators.rsi(close, params.rsi_period).iloc[-1],
        "atr": indicators.atr(high, low, close, params.atr_period).iloc[-1],
    }
    latest = {name: float(value) for name, value in latest.items()}
    if not all(math.isfinite(value) for value in latest.values()):
        # min_bars was set below what the indicators need to warm up.
        return no_signal(INSUFFICIENT_BARS)

    conditions = score_conditions(
        close=latest["close"],
        ema_fast=latest["ema_fast"],
        ema_mid=latest["ema_mid"],
        ema_slow=latest["ema_slow"],
        macd=latest["macd"],
        macd_signal=latest["macd_signal"],
        rsi=latest["rsi"],
        params=params,
    )
    score = sum(condition.value for condition in conditions)
    signal = classify(score, params)
    actionable = signal in (BUY, SELL)

    return SignalResult(
        signal=signal,
        score=score,
        strength=abs(score) / len(conditions),
        conditions=conditions,
        indicators=latest,
        levels=compute_levels(signal, latest["close"], latest["atr"], params) if actionable else None,
        candle_time=candle_time,
        signal_time=signal_time,
        expires_at=(
            signal_time + params.valid_bars * params.timeframe_seconds
            if actionable and signal_time is not None
            else None
        ),
        reason=None,
    )
