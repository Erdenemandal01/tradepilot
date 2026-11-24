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

    # Live feed (the web app's own loop): how often the source is asked, how
    # many candles are kept in memory, and when a silent feed is emailed about.
    live_enabled: bool = True
    poll_seconds: float = 2.0
    history_candles: int = 1500
    alert_after_seconds: int = 300
    # The chart's live candle stream (Binance's public kline WebSocket). Off: the chart is
    # refreshed by polling only. Signals never depend on it either way.
    chart_stream: bool = True
    # Times in emails are shown in this zone (UTC+8 is Ulaanbaatar).
    display_utc_offset_hours: int = 8

    # The forecast model written by `python -m backend.scripts.train_forecast`.
    # No file: no forecast is shown.
    forecast_model_path: str = "data/forecast_model.json"

    # Login. Both are written by `python -m backend.scripts.set_password`.
    password_hash: SecretStr = SecretStr("")
    session_secret: SecretStr = SecretStr("")
    session_hours: int = 12
    cookie_secure: bool = False  # set true once the site is served over HTTPS

    # Email (SMTP). Empty host or recipients: no email is sent.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: SecretStr = SecretStr("")
    email_from: str = ""
    email_to: str = ""  # comma-separated addresses


@lru_cache
def get_settings() -> Settings:
    return Settings()
