"""Candles for the chart, in whatever length the page asks for.

The chart's timeframe is a way of looking at prices, nothing more. The rule
keeps running on the signal timeframe and its H1 filter whatever is shown.

Where the candles of a chart timeframe come from:

- the signal timeframe: the live feed's memory, so the chart shows exactly
  the candles the rule ran on;
- a length the price source has itself (``DataSource.chart_timeframes``):
  asked from the source and kept here, refreshed with a short tail request;
- any other length: built from the longest shorter candles that fill it
  exactly (``core.candles.resample``), for example M45 from M15 and MN12
  from MN1.

Nothing here touches the signal path: a failure in this module costs a
chart, never a signal. No candle is made up: when the source gives nothing,
the request fails and the page says so.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

import pandas as pd

from backend.app.core import candles as cd
from backend.app.datasource.base import DataSource, DataSourceError

TAIL = 3  # candles asked for on a refresh: the forming one and two before it
# A hole among this many newest candles is asked for again each time a candle closes: the
# source may list a candle late, and the short tail would never reach it.
HOLE_REACH = 100
REFUSED = (418, 429)
# How many candles a full load asks for. Long candles need fewer: the request
# is cheaper, and the instrument's history ends long before 1500 weeks.
ASK = {"D1": 1000, "W1": 499, "MN1": 240}


class ChartUnavailable(RuntimeError):
    """The source gave no candles for the chart; ``retry_after`` seconds until it is asked again."""

    def __init__(self, retry_after: float) -> None:
        super().__init__("the price source gave no candles")
        self.retry_after = retry_after


class Feed(Protocol):
    timeframe: str

    def frame(self) -> pd.DataFrame | None: ...


@dataclass
class _Kept:
    frame: pd.DataFrame | None = None
    fetched_at: float = 0.0
    short: bool = False  # the source had fewer candles than asked for: nothing older exists
    retry_at: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)


def _bars(frame: pd.DataFrame) -> list[dict[str, Any]]:
    return [
        {
            "time": int(row.time),
            "open": float(row.open),
            "high": float(row.high),
            "low": float(row.low),
            "close": float(row.close),
            "volume": None if math.isnan(row.volume) else float(row.volume),
            "closed": bool(row.closed),  # False: the candle is still forming
        }
        for row in frame.itertuples()
    ]


class ChartCandles:
    def __init__(
        self,
        source: DataSource,
        feed: Feed,
        *,
        refresh_seconds: float = 2.0,
        history: int = 1500,
        timer: Callable[[], float] = time.monotonic,
    ) -> None:
        self._source = source
        self._feed = feed
        self._refresh = max(float(refresh_seconds), 0.5)
        self._history = int(history)
        self._timer = timer
        self._native = tuple(tf for tf in getattr(source, "chart_timeframes", ()) if tf in cd.CHART_TIMEFRAMES)
        self._kept: dict[str, _Kept] = {}
        self._guard = threading.Lock()
        self._origins = {tf: self._origin(tf) for tf in cd.CHART_TIMEFRAMES}

    def _origin(self, timeframe: str) -> str | None:
        """The candles a chart timeframe is read or built from; None when the source cannot give it."""
        base = self._feed.timeframe
        if timeframe == base or timeframe in self._native:
            return timeframe
        for candidate in reversed(cd.CHART_TIMEFRAMES):  # the longest that fits: the fewest candles to combine
            if (candidate == base or candidate in self._native) and cd.can_resample(candidate, timeframe):
                return candidate
        return None

    def timeframes(self) -> list[str]:
        """The chart timeframes this source can show, shortest first."""
        return [tf for tf in cd.CHART_TIMEFRAMES if self._origins[tf] is not None]

    def candles(self, timeframe: str, limit: int) -> dict[str, Any]:
        """The newest ``limit`` candles of ``timeframe`` and what is known about their history.

        Raises ValueError for a timeframe the source cannot show and
        ChartUnavailable when the source gives nothing right now.
        """
        origin = self._origins.get(timeframe)
        if origin is None:
            raise ValueError(f"timeframe {timeframe!r} is not available from this source")
        if origin == self._feed.timeframe:
            frame, short = self._feed.frame(), False  # the feed keeps a window, not the whole history
        else:
            frame, short = self._from_source(origin)
        partial = False
        if frame is None:
            frame = cd.validate(pd.DataFrame(columns=list(cd.CANDLE_COLUMNS)), origin)
        if origin != timeframe:
            frame, partial = cd.resample(frame, origin, timeframe, history_starts=short)
        whole = len(frame) <= limit  # nothing older was cut off by the limit
        frame = frame.iloc[-limit:]
        return {
            "timeframe": timeframe,
            "source": self._source.name,
            "symbol": self._source.symbol,
            "built_from": None if origin == timeframe else origin,
            # "short": the first candle is the oldest one the source has.
            "history": "short" if short and whole else "full",
            # The first candle was built from a bucket whose beginning does not exist.
            "partial_first": bool(partial and whole),
            "gaps": cd.count_breaks(frame, timeframe),
            "bars": _bars(frame),
        }

    def _ask(self, timeframe: str) -> int:
        return min(self._history, ASK.get(timeframe, self._history))

    def _heal(self, timeframe: str, before: pd.DataFrame, frame: pd.DataFrame, ask: int) -> pd.DataFrame:
        """When a candle has closed and a young hole is kept: one longer tail that reaches it.

        The longer request is an extra. When it fails, or what it brings does
        not fit, the chart keeps what the usual tail gave. Only a refusal
        (asked to slow down) is passed on, so that the source is left alone.
        """
        if len(frame) < 2 or len(before) < 2 or int(frame["time"].iloc[-1]) == int(before["time"].iloc[-1]):
            return frame
        reach = cd.hole_reach(frame, timeframe, min(HOLE_REACH, ask))
        if reach <= TAIL:
            return frame
        try:
            healed, news = cd.lay_over(frame, self._source.fetch_candles(timeframe, reach), timeframe)
        except ValueError:
            return frame
        except DataSourceError as error:
            if error.status in REFUSED:
                raise
            return frame
        if healed is None or news:
            return frame
        return healed.iloc[-ask:].reset_index(drop=True)

    def _from_source(self, timeframe: str) -> tuple[pd.DataFrame, bool]:
        with self._guard:
            kept = self._kept.setdefault(timeframe, _Kept())
        with kept.lock:  # one request at a time per timeframe, however many pages ask
            now = self._timer()
            if now < kept.retry_at:
                raise ChartUnavailable(kept.retry_at - now)
            if kept.frame is not None and now - kept.fetched_at < self._refresh:
                return kept.frame, kept.short
            ask = self._ask(timeframe)
            try:
                frame = None
                if kept.frame is not None and timeframe not in cd.MONTH_TIMEFRAMES:
                    tail = self._source.fetch_candles(timeframe, min(TAIL, ask))
                    try:
                        frame = cd.merge(kept.frame, tail, timeframe, keep=ask)
                    except ValueError:
                        frame = None  # the two do not fit together: load the whole history again
                    if frame is not None:
                        frame = self._heal(timeframe, kept.frame, frame, ask)
                    if frame is not None and len(frame) >= ask:
                        kept.short = False  # the oldest candles have been cut off since
                if frame is None:
                    frame = self._source.fetch_candles(timeframe, ask)
                    kept.short = len(frame) < ask
            except DataSourceError as error:
                if error.status in REFUSED:  # asked to slow down: stay away, as the live feed does
                    wait = min(float(error.retry_after), 3600.0) if error.retry_after and error.retry_after > 0 else 60.0
                else:
                    wait = 5.0
                kept.retry_at = self._timer() + wait
                raise ChartUnavailable(wait) from None
            kept.frame, kept.fetched_at = frame, self._timer()
            return frame, kept.short
