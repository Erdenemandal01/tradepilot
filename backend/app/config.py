"""Application settings, read from environment variables and an optional .env file."""

from __future__ import annotations

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Every variable is prefixed with TRADEPILOT_, for example TRADEPILOT_SYMBOL."""

    model_config = SettingsConfigDict(
        env_prefix="TRADEPILOT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    symbol: str = "XAUUSD"
    signal_timeframe: str = "M15"
    min_bars: int = 300
    stale_after_seconds: int = 60

    # Price source (Phase 3): binance, twelvedata or csv.
    price_source: str = "binance"
    binance_symbol: str = "XAUUSDT"
    twelvedata_symbol: str = "XAU/USD"
    twelvedata_api_key: SecretStr = SecretStr("")
    csv_path: str = "data/xauusd_m15.csv"
    csv_timeframe: str = "M15"


@lru_cache
def get_settings() -> Settings:
    return Settings()
