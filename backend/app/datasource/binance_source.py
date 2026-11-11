"""Binance USD-M futures, XAUUSDT perpetual: public market data, no API key.

Only two public read-only endpoints are used: ``/fapi/v1/klines`` (candles)
and ``/fapi/v1/ticker/bookTicker`` (best bid and ask). No account endpoints,
no keys, no signed requests.

The contract is quoted in USDT and trades 24/7, so its price differs from a
broker's spot XAUUSD, and it keeps trading while XAUUSD is closed at the
weekend. The market-hours filter (Phase 5) handles the second point; the
price comparison script measures the first.

A candle is reported closed only when the same response lists a newer one.
The PC clock is not used for this: for a few seconds after a close Binance
can still serve the candle in a pre-close state, and a clock that runs ahead
would pass off a forming candle as closed. The price of this rule is a short
delay: a candle is picked up on the first poll after the next one appears.
"""

from __future__ import annotations

import time

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
    to_int,
)

BASE_URL = "https://fapi.binance.com"
INTERVALS = {"M1": "1m", "M5": "5m", "M15": "15m", "M30": "30m", "H1": "1h", "H4": "4h", "D1": "1d"}
MAX_LIMIT = 1500


class BinanceSource(DataSource):
    name = "binance"

    def __init__(
        self,
        symbol: str = "XAUUSDT",
        get_json: JsonGetter = http.get_json,
        clock: Clock = time.time,
        base_url: str = BASE_URL,
    ) -> None:
        self.symbol = symbol
        self._get_json = get_json
        self._clock = clock
        self._base_url = base_url

    def fetch_candles(self, timeframe: str, limit: int = 500) -> pd.DataFrame:
        check_limit(limit, MAX_LIMIT)
        if timeframe not in INTERVALS:
            raise ValueError(f"Binance source does not support timeframe {timeframe!r}")
        rows = self._get_json(
            f"{self._base_url}/fapi/v1/klines",
            {"symbol": self.symbol, "interval": INTERVALS[timeframe], "limit": str(limit)},
        )
        if not isinstance(rows, list):
            raise DataSourceError(f"Binance klines: unexpected response {str(rows)[:200]}")
        records = []
        newest = len(rows) - 1
        for index, row in enumerate(rows):
            if not isinstance(row, list) or len(row) < 6:
                raise DataSourceError(f"Binance klines: bad row {str(row)[:200]}")
            records.append(
                {
                    "time": to_int(row[0], "open time") // 1000,
                    "open": to_float(row[1], "open"),
                    "high": to_float(row[2], "high"),
                    "low": to_float(row[3], "low"),
                    "close": to_float(row[4], "close"),
                    "volume": to_float(row[5], "volume"),
                    # Closed only when the response lists a newer candle.
                    "closed": index < newest,
                }
            )
        try:
            return cd.validate(pd.DataFrame(records, columns=list(cd.CANDLE_COLUMNS)), timeframe)
        except ValueError as exc:
            raise DataSourceError(f"Binance klines failed validation: {exc}") from None

    def fetch_quote(self) -> Quote:
        data = self._get_json(f"{self._base_url}/fapi/v1/ticker/bookTicker", {"symbol": self.symbol})
        if not isinstance(data, dict) or "bidPrice" not in data or "askPrice" not in data:
            raise DataSourceError(f"Binance bookTicker: unexpected response {str(data)[:200]}")
        bid = to_float(data["bidPrice"], "bid")
        ask = to_float(data["askPrice"], "ask")
        if not 0 < bid <= ask:
            raise DataSourceError(f"Binance bookTicker: bad bid/ask {bid}/{ask}")
        stamp = data.get("time")
        quote_time = to_int(stamp, "time") / 1000 if stamp is not None else self._clock()
        return Quote(
            source=self.name,
            symbol=self.symbol,
            time=quote_time,
            price=(bid + ask) / 2,
            bid=bid,
            ask=ask,
        )
