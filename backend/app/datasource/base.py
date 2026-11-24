"""The interface every price source implements.

Sources return candle frames in the shape described in ``core/candles.py`` and
quotes as ``Quote``. When a source cannot deliver real data it raises
``DataSourceError``; it never returns a guessed or repeated price.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

import pandas as pd

# Fetch a URL with query parameters and return the decoded JSON body.
JsonGetter = Callable[[str, Mapping[str, str]], Any]
Clock = Callable[[], float]


class DataSourceError(RuntimeError):
    """The source could not deliver real data (network, limits, bad response).

    ``status`` is the HTTP status when there was one; ``retry_after`` is the
    number of seconds the service asked the client to wait (HTTP 429 / 418).
    """

    def __init__(self, message: str, *, status: int | None = None, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


@dataclass(frozen=True)
class Quote:
    source: str
    symbol: str
    time: float  # UTC Unix seconds of the price (fetch time if the source gives none)
    price: float  # mid of bid and ask when both are known, otherwise the last price
    bid: float | None = None
    ask: float | None = None
    # False when the source gave no time: ``time`` is then only the moment of
    # the fetch, and the price's real age is unknown (never treat it as fresh).
    timed_by_source: bool = True

    @property
    def spread(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid


@dataclass(frozen=True)
class StreamCandle:
    """One update of a candle from a live stream, in the shape of a row of a candle frame.

    ``closed`` is the exchange's own flag: True once the candle has ended and
    these are its final values. ``event_time`` is when the exchange sent the
    update, UTC seconds by its clock.
    """

    time: int  # open time, UTC seconds
    open: float
    high: float
    low: float
    close: float
    volume: float  # NaN when the source gives none
    closed: bool
    event_time: float

    def bar(self) -> dict[str, Any]:
        """The row for ``core.candles.lay_stream``."""
        return {
            "time": self.time,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "closed": self.closed,
        }


class CandleStream(Protocol):
    """An open live stream of one timeframe's candles. Used by one thread."""

    def read(self, timeout: float) -> StreamCandle | None:
        """The next update, or None when none came within ``timeout`` seconds.

        Raises DataSourceError when the stream is broken; it is then closed
        and a new one must be opened.
        """

    def close(self) -> None:
        """End the stream. Safe to call from another thread and more than once."""


class DataSource(ABC):
    name: str
    symbol: str
    # Candle lengths the chart may ask this source for directly, besides the
    # signal timeframe. Empty: the chart is built from the signal candles only.
    chart_timeframes: tuple[str, ...] = ()
    # Candle lengths this source can push live (``open_candle_stream``). Empty:
    # no stream, the chart is refreshed by polling. Never used for signals.
    stream_timeframes: tuple[str, ...] = ()

    def open_candle_stream(self, timeframe: str) -> CandleStream:
        """Open a live stream of ``timeframe`` candles. DataSourceError when it cannot be opened."""
        raise NotImplementedError(f"the {self.name} source has no live candle stream")

    @abstractmethod
    def fetch_candles(self, timeframe: str, limit: int = 500) -> pd.DataFrame:
        """The latest ``limit`` candles, oldest first. The last one may still be forming."""

    @abstractmethod
    def fetch_quote(self) -> Quote:
        """The latest price."""

    def fetch_history(self, timeframe: str, start: int | None = None, end: int | None = None) -> pd.DataFrame:
        """Every candle the source has from ``start`` to ``end`` (open times, UTC seconds), oldest first.

        Without ``start`` it begins at the oldest candle the source has;
        without ``end`` it runs to now. The shape is that of ``fetch_candles``:
        the newest candle is marked closed only when the source listed a newer
        one. A source may need many requests for this and must pace them. It
        is for fitting and backtests, never for the live loop. A source that
        cannot do it raises NotImplementedError.
        """
        raise NotImplementedError(f"the {self.name} source cannot read older candles in pages")


def check_limit(limit: int, maximum: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= maximum:
        raise ValueError(f"limit must be an integer from 1 to {maximum}, got {limit!r}")


def to_float(value: Any, what: str) -> float:
    """A finite number from a response. NaN and infinity are never data (Python's json reads both)."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise DataSourceError(f"bad {what} in response: {str(value)[:50]!r}") from None
    if not math.isfinite(number):
        raise DataSourceError(f"bad {what} in response: {str(value)[:50]!r}")
    return number


def to_int(value: Any, what: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):  # OverflowError: infinity, or 1e400 read as a float
        raise DataSourceError(f"bad {what} in response: {str(value)[:50]!r}") from None
