"""Binance USD-M futures, XAUUSDT perpetual: public market data, no API key.

Only two public read-only endpoints are used: ``/fapi/v1/klines`` (candles,
also for older history in pages) and ``/fapi/v1/ticker/bookTicker`` (best bid
and ask). No account endpoints,
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
from typing import Any, Callable

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
INTERVALS = {
    "M1": "1m",
    "M3": "3m",
    "M5": "5m",
    "M15": "15m",
    "M30": "30m",
    "H1": "1h",
    "H2": "2h",
    "H4": "4h",
    "D1": "1d",
    "W1": "1w",  # opens on Monday 00:00 UTC
    "MN1": "1M",  # opens on the first of the month, 00:00 UTC
}
MAX_LIMIT = 1500
# Reading history: one page is the largest request the exchange allows (it
# costs 10 of the 2400 request weights a minute). A pause between pages keeps
# the download far below that limit, next to a live feed on the same address.
HISTORY_PAUSE = 0.5
HISTORY_MAX_PAGES = 400  # 600,000 candles: a wall against a loop that never ends


class BinanceSource(DataSource):
    name = "binance"
    chart_timeframes = tuple(INTERVALS)

    def __init__(
        self,
        symbol: str = "XAUUSDT",
        get_json: JsonGetter = http.get_json,
        clock: Clock = time.time,
        base_url: str = BASE_URL,
        sleep: Callable[[float], None] = time.sleep,
        history_pause: float = HISTORY_PAUSE,
    ) -> None:
        self.symbol = symbol
        self._get_json = get_json
        self._clock = clock
        self._base_url = base_url
        self._sleep = sleep
        self._history_pause = history_pause

    def fetch_candles(self, timeframe: str, limit: int = 500) -> pd.DataFrame:
        check_limit(limit, MAX_LIMIT)
        if timeframe not in INTERVALS:
            raise ValueError(f"Binance source does not support timeframe {timeframe!r}")
        records = self._klines({"symbol": self.symbol, "interval": INTERVALS[timeframe], "limit": str(limit)})
        return self._frame(records, timeframe, newest_closed=False)

    def _klines(self, params: dict[str, str]) -> list[dict[str, Any]]:
        """One klines request, parsed. The rows carry no ``closed`` mark yet."""
        rows = self._get_json(f"{self._base_url}/fapi/v1/klines", params)
        if not isinstance(rows, list):
            raise DataSourceError(f"Binance klines: unexpected response {str(rows)[:200]}")
        records = []
        for row in rows:
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
                }
            )
        return records

    def _frame(self, records: list[dict[str, Any]], timeframe: str, *, newest_closed: bool) -> pd.DataFrame:
        frame = pd.DataFrame(records, columns=list(cd.CANDLE_COLUMNS))
        # Closed only when the exchange listed a newer candle.
        frame["closed"] = True
        if len(frame) and not newest_closed:
            frame.loc[frame.index[-1], "closed"] = False
        try:
            return cd.validate(frame, timeframe)
        except ValueError as exc:
            raise DataSourceError(f"Binance klines failed validation: {exc}") from None

    def fetch_history(self, timeframe: str, start: int | None = None, end: int | None = None) -> pd.DataFrame:
        """All candles from ``start`` (default: the oldest the exchange has) to ``end`` (default: now).

        Read oldest first in pages of 1500 with a pause between them. Each
        page starts just after the last candle of the page before, so pages
        cannot overlap; a page that does not move forward is an error, not
        something to patch over. A refusal (HTTP 418 or 429) or any other
        failed request ends the download at once with DataSourceError: no
        retry here, and no partial history is returned.
        """
        if timeframe not in INTERVALS:
            raise ValueError(f"Binance source does not support timeframe {timeframe!r}")
        if start is not None and end is not None and end < start:
            raise ValueError("end is before start")
        records: list[dict[str, Any]] = []
        cursor = 0 if start is None else int(start) * 1000
        newer_listed = False  # the exchange listed a candle after the last one kept
        for page in range(HISTORY_MAX_PAGES):
            if page:
                self._sleep(self._history_pause)
            rows = self._klines(
                {
                    "symbol": self.symbol,
                    "interval": INTERVALS[timeframe],
                    "startTime": str(cursor),
                    "limit": str(MAX_LIMIT),
                }
            )
            if rows and rows[0]["time"] * 1000 < cursor:
                raise DataSourceError("Binance klines: a history page starts before the time asked for")
            if end is not None:
                kept = [row for row in rows if row["time"] <= end]
                newer_listed = len(kept) < len(rows)
                records += kept
            else:
                records += rows
            if len(rows) < MAX_LIMIT or newer_listed:
                break  # a short page: the exchange has nothing newer
            cursor = rows[-1]["time"] * 1000 + 1
        else:
            raise DataSourceError(f"Binance klines: history did not end within {HISTORY_MAX_PAGES} pages")
        return self._frame(records, timeframe, newest_closed=newer_listed)

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
            timed_by_source=stamp is not None,
        )
