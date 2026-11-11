"""The interface every price source implements.

Sources return candle frames in the shape described in ``core/candles.py`` and
quotes as ``Quote``. When a source cannot deliver real data it raises
``DataSourceError``; it never returns a guessed or repeated price.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Mapping

import pandas as pd

# Fetch a URL with query parameters and return the decoded JSON body.
JsonGetter = Callable[[str, Mapping[str, str]], Any]
Clock = Callable[[], float]


class DataSourceError(RuntimeError):
    """The source could not deliver real data (network, limits, bad response)."""


@dataclass(frozen=True)
class Quote:
    source: str
    symbol: str
    time: float  # UTC Unix seconds of the price (fetch time if the source gives none)
    price: float  # mid of bid and ask when both are known, otherwise the last price
    bid: float | None = None
    ask: float | None = None

    @property
    def spread(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid


class DataSource(ABC):
    name: str
    symbol: str

    @abstractmethod
    def fetch_candles(self, timeframe: str, limit: int = 500) -> pd.DataFrame:
        """The latest ``limit`` candles, oldest first. The last one may still be forming."""

    @abstractmethod
    def fetch_quote(self) -> Quote:
        """The latest price."""


def check_limit(limit: int, maximum: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= maximum:
        raise ValueError(f"limit must be an integer from 1 to {maximum}, got {limit!r}")


def to_float(value: Any, what: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise DataSourceError(f"bad {what} in response: {value!r}") from None


def to_int(value: Any, what: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise DataSourceError(f"bad {what} in response: {value!r}") from None
