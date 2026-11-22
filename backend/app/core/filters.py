"""Filters that can stop a proposal of ``signal.py``, and what follows a sent signal.

``signal.evaluate`` proposes BUY, SELL or HOLD for one closed candle. Here a
BUY or SELL proposal is checked against the plan's filters (section 5.2):

- stale feed, market closed (both decided by the caller and passed in),
- H1 trend: BUY needs an H1 score of +1 or more, SELL of -1 or less,
- duplicate: a signal of the same side is still valid.

The news window is not here yet: it needs the economic calendar.

``replay`` runs proposal and filters over the last closed candles exactly as
the live feed does one candle at a time, so the history shown after a restart
is what would have been sent. Nothing here reads a clock, a file or the
network: the caller passes times and prices in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Collection

import pandas as pd

from backend.app.core.candles import aggregate, timeframe_seconds
from backend.app.core.signal import BUY, SELL, INSUFFICIENT_BARS, SignalParams, SignalResult, evaluate, params_for

HTF_CONFLICT = "HTF_CONFLICT"
NEWS_WINDOW = "NEWS_WINDOW"  # not applied yet (needs the calendar)
MARKET_CLOSED = "MARKET_CLOSED"
STALE_FEED = "STALE_FEED"
DUPLICATE = "DUPLICATE"

HIGHER_TIMEFRAME = "H1"

VALID = "valid"
OUTSIDE_ZONE = "outside_zone"
EXPIRED = "expired"


@dataclass(frozen=True)
class Decision:
    """One closed candle's proposal after the filters."""

    result: SignalResult
    h1_score: int | None
    blocked: str | None  # reason code when the evaluation or a BUY / SELL proposal was stopped
    run: int = 0  # position in a run of back-to-back sent signals of one side; 0 when not sent

    @property
    def sent(self) -> bool:
        return self.blocked is None and self.result.signal in (BUY, SELL)


def htf_conflict(side: str, h1_score: int) -> bool:
    """BUY while the H1 score is below +1, SELL while it is above -1."""
    return h1_score < 1 if side == BUY else h1_score > -1


def decide(
    result: SignalResult,
    *,
    h1_score: int | None,
    previous: Decision | None,
    market_closed: bool = False,
    stale: bool = False,
) -> Decision:
    """Apply the filters to one proposal. ``previous`` is the last sent signal."""
    if result.signal is None:
        return Decision(result, h1_score, result.reason)
    if result.signal not in (BUY, SELL):
        return Decision(result, h1_score, None)
    side = result.signal
    if stale:
        return Decision(result, h1_score, STALE_FEED)
    if market_closed:
        return Decision(result, h1_score, MARKET_CLOSED)
    if h1_score is None:
        # The H1 trend cannot be confirmed, so it is not treated as confirmed.
        return Decision(result, h1_score, INSUFFICIENT_BARS)
    if htf_conflict(side, h1_score):
        return Decision(result, h1_score, HTF_CONFLICT)

    run = 1
    if previous is not None and previous.sent and previous.result.signal == side:
        expires = previous.result.expires_at
        if result.signal_time is not None and expires is not None:
            if expires > result.signal_time:
                return Decision(result, h1_score, DUPLICATE)
            validity = expires - (previous.result.signal_time or expires)
            if result.signal_time <= expires + validity:
                run = previous.run + 1
    return Decision(result, h1_score, None, run)


def signal_state(decision: Decision, price: float | None, now: float) -> str:
    """Where a sent signal stands: valid, outside the entry zone, or expired.

    Without a price the zone cannot be judged, so only the clock decides.
    """
    result = decision.result
    if result.expires_at is None or now >= result.expires_at:
        return EXPIRED
    levels = result.levels
    if price is not None and levels is not None and not levels.zone_low <= price <= levels.zone_high:
        return OUTSIDE_ZONE
    return VALID


def outcome(result: SignalResult, candles: pd.DataFrame) -> tuple[str | None, int | None]:
    """Which level the feed's price reached first after a signal: ``tp`` or ``sl``.

    Looks at closed candles that opened at or after the signal time and
    returns the level with that candle's open time, or ``(None, None)``.
    When one candle touches both levels the stop counts first. The levels
    are the feed's, measured from the reference entry, not a real fill.
    """
    levels = result.levels
    if levels is None or result.signal_time is None or candles.empty:
        return None, None
    frame = candles[candles["time"] >= result.signal_time]
    if "closed" in frame.columns:
        frame = frame[frame["closed"].astype(bool)]
    if frame.empty:
        return None, None
    if result.signal == BUY:
        stopped = frame["low"] <= levels.stop_loss
        reached = frame["high"] >= levels.take_profit
    else:
        stopped = frame["high"] >= levels.stop_loss
        reached = frame["low"] <= levels.take_profit
    touched = frame[stopped | reached]
    if touched.empty:
        return None, None
    first = touched.index[0]
    return ("sl" if bool(stopped.loc[first]) else "tp"), int(touched.loc[first, "time"])


def replay(
    candles: pd.DataFrame,
    timeframe: str,
    count: int,
    *,
    params: SignalParams | None = None,
    previous: Decision | None = None,
    closed_hours: Callable[[float], bool] | None = None,
    stale_times: Collection[int] = (),
) -> list[Decision]:
    """Decisions for the last ``count`` closed candles, oldest first.

    Each candle is judged on the candles up to it only, and on the H1 candles
    that had closed by its close. ``previous`` is the last signal sent before
    this stretch; ``closed_hours`` tells whether the broker's market is
    closed at a given UTC time; ``stale_times`` are the close times of
    candles that closed while the price feed was stale: a proposal there is
    stopped as STALE_FEED and does not count as a sent signal for the
    candles after it.
    """
    params = params_for(timeframe, params)
    step = timeframe_seconds(timeframe)
    closed = candles[candles["closed"].astype(bool)] if "closed" in candles.columns else candles
    closed = closed.reset_index(drop=True)
    if closed.empty or count < 1:
        return []

    higher_step = timeframe_seconds(HIGHER_TIMEFRAME)
    higher_params = params_for(HIGHER_TIMEFRAME, params)
    higher = aggregate(closed, timeframe, HIGHER_TIMEFRAME)
    higher = higher[higher["closed"]]
    higher_close = higher["time"] + higher_step
    cache: dict[int, int | None] = {}

    def h1_score(at: int) -> int | None:
        known = int((higher_close <= at).sum())
        if known not in cache:
            cache[known] = evaluate(higher.iloc[:known], higher_params).score if known else None
        return cache[known]

    decisions: list[Decision] = []
    for end in range(max(len(closed) - count, 0) + 1, len(closed) + 1):
        result = evaluate(closed.iloc[:end], params)
        at = int(closed["time"].iloc[end - 1]) + step
        decision = decide(
            result,
            h1_score=h1_score(at),
            previous=previous,
            market_closed=bool(closed_hours(at)) if closed_hours else False,
            stale=at in stale_times,
        )
        if decision.sent:
            previous = decision
        decisions.append(decision)
    return decisions
