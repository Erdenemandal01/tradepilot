"""Candles from a CSV file, for tests and backtests. No network.

Columns: ``time``, ``open``, ``high``, ``low``, ``close``; optional ``volume``
and ``closed``. ``time`` is the candle open time, either UTC Unix seconds or
a date-time text such as ``2026-11-11 07:45:00`` (read as UTC).

Higher timeframes are built from the file's timeframe with ``aggregate``.
The quote is the last closed candle's close, timed at that candle's close:
historical data, never presented as a live price.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from backend.app.core import candles as cd
from backend.app.datasource.base import DataSource, DataSourceError, Quote, check_limit

_TRUE = {"true", "1", "yes"}
_FALSE = {"false", "0", "no"}


class CsvSource(DataSource):
    name = "csv"

    def __init__(self, path: str | Path, timeframe: str = "M15", symbol: str = "XAUUSD") -> None:
        cd.timeframe_seconds(timeframe)
        self.path = Path(path)
        self.timeframe = timeframe
        self.symbol = symbol
        self._frame: pd.DataFrame | None = None

    def _load(self) -> pd.DataFrame:
        if self._frame is not None:
            return self._frame
        try:
            raw = pd.read_csv(self.path)
        except (OSError, ValueError) as exc:
            raise DataSourceError(f"cannot read {self.path}: {exc}") from None
        if "time" in raw.columns and not pd.api.types.is_numeric_dtype(raw["time"]):
            stamps = pd.to_datetime(raw["time"], utc=True)
            raw["time"] = (stamps - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(seconds=1)
        if "closed" in raw.columns and not pd.api.types.is_bool_dtype(raw["closed"]):
            text = raw["closed"].astype(str).str.strip().str.lower()
            if not text.isin(_TRUE | _FALSE).all():
                raise DataSourceError(f"{self.path}: closed column must be true or false")
            raw["closed"] = text.isin(_TRUE)
        try:
            self._frame = cd.validate(raw, self.timeframe)
        except ValueError as exc:
            raise DataSourceError(f"{self.path}: {exc}") from None
        return self._frame

    def fetch_candles(self, timeframe: str, limit: int = 500) -> pd.DataFrame:
        check_limit(limit, 10_000_000)
        frame = self._load()
        if timeframe != self.timeframe:
            frame = cd.aggregate(frame, self.timeframe, timeframe)
        return frame.tail(limit).reset_index(drop=True)

    def fetch_quote(self) -> Quote:
        closed = self._load()
        closed = closed[closed["closed"]]
        if closed.empty:
            raise DataSourceError(f"{self.path}: no closed candles")
        last = closed.iloc[-1]
        return Quote(
            source=self.name,
            symbol=self.symbol,
            time=float(last["time"] + cd.timeframe_seconds(self.timeframe)),
            price=float(last["close"]),
        )
