"""Price sources behind one interface: Binance XAUUSDT, Twelve Data XAU/USD, CSV."""

from __future__ import annotations

from backend.app.config import Settings
from backend.app.datasource.base import DataSource, DataSourceError, Quote

SOURCES = ("binance", "twelvedata", "csv")

__all__ = ["DataSource", "DataSourceError", "Quote", "SOURCES", "make_source"]


def make_source(settings: Settings, name: str | None = None) -> DataSource:
    """Build the source named in settings (TRADEPILOT_PRICE_SOURCE) or by ``name``."""
    name = name or settings.price_source
    if name == "binance":
        from backend.app.datasource.binance_source import BinanceSource

        return BinanceSource(symbol=settings.binance_symbol)
    if name == "twelvedata":
        from backend.app.datasource.twelvedata_source import TwelveDataSource

        return TwelveDataSource(
            api_key=settings.twelvedata_api_key.get_secret_value(),
            symbol=settings.twelvedata_symbol,
        )
    if name == "csv":
        from backend.app.datasource.csv_source import CsvSource

        return CsvSource(settings.csv_path, timeframe=settings.csv_timeframe, symbol=settings.symbol)
    raise ValueError(f"unknown price source {name!r}; choose one of {', '.join(SOURCES)}")
