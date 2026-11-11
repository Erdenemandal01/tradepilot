"""Twelve Data REST API, XAU/USD. Needs a free API key in TRADEPILOT_TWELVEDATA_API_KEY.

Free Basic plan: 8 credits per minute, 800 per day; one call here costs one
credit. Its terms allow "internal non-display usage" only; check them before
showing these prices to anyone else.

``/price`` returns a last price without bid, ask or timestamp, so the quote's
time is the moment it was fetched.
"""

from __future__ import annotations

import time
from typing import Any

import numpy as np
import pandas as pd

from backend.app.core import candles as cd
from backend.app.datasource import http
from backend.app.datasource.base import (
    Clock,
    DataSource,
    DataSourceError,
    JsonGetter,
    Quote,
    check_limit,
    to_float,
)

BASE_URL = "https://api.twelvedata.com"
INTERVALS = {
    "M1": "1min",
    "M5": "5min",
    "M15": "15min",
    "M30": "30min",
    "H1": "1h",
    "H4": "4h",
    "D1": "1day",
}
MAX_OUTPUTSIZE = 5000


class TwelveDataSource(DataSource):
    name = "twelvedata"

    def __init__(
        self,
        api_key: str,
        symbol: str = "XAU/USD",
        get_json: JsonGetter = http.get_json,
        clock: Clock = time.time,
        base_url: str = BASE_URL,
    ) -> None:
        if not api_key:
            raise DataSourceError("Twelve Data needs an API key: set TRADEPILOT_TWELVEDATA_API_KEY in .env")
        self.symbol = symbol
        self._api_key = api_key
        self._get_json = get_json
        self._clock = clock
        self._base_url = base_url

    def _call(self, path: str, params: dict[str, str]) -> Any:
        data = self._get_json(f"{self._base_url}{path}", {**params, "apikey": self._api_key})
        if not isinstance(data, dict):
            raise DataSourceError(f"Twelve Data {path}: unexpected response {str(data)[:200]}")
        if data.get("status") == "error":
            raise DataSourceError(f"Twelve Data {path}: error {data.get('code')}: {data.get('message')}")
        return data

    def fetch_candles(self, timeframe: str, limit: int = 500) -> pd.DataFrame:
        check_limit(limit, MAX_OUTPUTSIZE)
        if timeframe not in INTERVALS:
            raise ValueError(f"Twelve Data source does not support timeframe {timeframe!r}")
        data = self._call(
            "/time_series",
            {
                "symbol": self.symbol,
                "interval": INTERVALS[timeframe],
                "outputsize": str(limit),
                "timezone": "UTC",
            },
        )
        values = data.get("values")
        if not isinstance(values, list):
            raise DataSourceError(f"Twelve Data /time_series: no values in {str(data)[:200]}")
        records = []
        for value in values:
            try:
                start = int(pd.Timestamp(value["datetime"], tz="UTC").timestamp())
            except (KeyError, TypeError, ValueError):
                raise DataSourceError(f"Twelve Data /time_series: bad row {str(value)[:200]}") from None
            records.append(
                {
                    "time": start,
                    "open": to_float(value.get("open"), "open"),
                    "high": to_float(value.get("high"), "high"),
                    "low": to_float(value.get("low"), "low"),
                    "close": to_float(value.get("close"), "close"),
                    "volume": to_float(value["volume"], "volume") if "volume" in value else np.nan,
                }
            )
        frame = pd.DataFrame(records, columns=list(cd.CANDLE_COLUMNS))
        frame = frame.sort_values("time", kind="stable").reset_index(drop=True)
        # Closed only when the response lists a newer candle; the PC clock is
        # not used (see binance_source.py for why).
        frame["closed"] = np.arange(len(frame)) < len(frame) - 1
        try:
            return cd.validate(frame, timeframe)
        except ValueError as exc:
            raise DataSourceError(f"Twelve Data candles failed validation: {exc}") from None

    def fetch_quote(self) -> Quote:
        data = self._call("/price", {"symbol": self.symbol})
        price = to_float(data.get("price"), "price")
        if price <= 0:
            raise DataSourceError(f"Twelve Data /price: bad price {price}")
        # /price carries no time of its own: the quote's age is unknown.
        return Quote(source=self.name, symbol=self.symbol, time=self._clock(), price=price, timed_by_source=False)
