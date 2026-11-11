"""Price sources against fake HTTP responses. No test touches the network."""

from __future__ import annotations

import http.client
import io
import math
from urllib.error import HTTPError, URLError

import pandas as pd
import pytest

from backend.app.config import Settings
from backend.app.datasource import DataSourceError, make_source
from backend.app.datasource import http as http_mod
from backend.app.datasource.binance_source import BinanceSource
from backend.app.datasource.csv_source import CsvSource
from backend.app.datasource.twelvedata_source import TwelveDataSource

T0 = 1_793_998_800  # an H1 boundary
M15 = 900


class FakeHttp:
    """Answers by URL suffix and records every call."""

    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, params):
        self.calls.append((url, dict(params)))
        for suffix, answer in self.responses.items():
            if url.endswith(suffix):
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise AssertionError(f"unexpected URL {url}")


def kline(start: int, o: float, h: float, low: float, c: float, v: float = 10.0) -> list:
    close_ms = (start + M15) * 1000 - 1
    return [start * 1000, f"{o:.2f}", f"{h:.2f}", f"{low:.2f}", f"{c:.2f}", f"{v:.3f}", close_ms, "0", 1]


# --- Binance ----------------------------------------------------------------


def binance_klines(n: int = 4) -> list:
    return [kline(T0 + i * M15, 4000 + i, 4002 + i, 3999 + i, 4001 + i) for i in range(n)]


def test_binance_candles_parse_and_mark_the_forming_candle():
    fake = FakeHttp({"/fapi/v1/klines": binance_klines(4)})
    # The newest row is the forming candle; the three before it are closed.
    source = BinanceSource(get_json=fake, clock=lambda: T0 + 3 * M15 + 100)
    frame = source.fetch_candles("M15", limit=4)

    assert frame["time"].tolist() == [T0 + i * M15 for i in range(4)]
    assert frame["closed"].tolist() == [True, True, True, False]
    assert frame.iloc[0][["open", "high", "low", "close", "volume"]].tolist() == [4000, 4002, 3999, 4001, 10]
    url, params = fake.calls[0]
    assert url == "https://fapi.binance.com/fapi/v1/klines"
    assert params == {"symbol": "XAUUSDT", "interval": "15m", "limit": "4"}


@pytest.mark.parametrize("clock_offset", [-3600, 0, 3 * M15 + 100, 4 * M15 + 5, 86400])
def test_binance_closed_flag_ignores_the_pc_clock(clock_offset):
    # A candle is closed only when the response lists a newer one. For a few
    # seconds after a close the exchange can still serve the old candle in a
    # pre-close state, and a PC clock can be wrong, so neither may promote
    # the last row or hold back the earlier ones.
    fake = FakeHttp({"/fapi/v1/klines": binance_klines(4)})
    source = BinanceSource(get_json=fake, clock=lambda: T0 + clock_offset)
    assert source.fetch_candles("M15", limit=4)["closed"].tolist() == [True, True, True, False]


def test_binance_single_candle_is_still_forming():
    fake = FakeHttp({"/fapi/v1/klines": binance_klines(1)})
    source = BinanceSource(get_json=fake, clock=lambda: T0 + 10 * M15)
    assert source.fetch_candles("M15", limit=1)["closed"].tolist() == [False]


def test_binance_quote_uses_bid_ask_and_exchange_time():
    fake = FakeHttp(
        {"/fapi/v1/ticker/bookTicker": {"symbol": "XAUUSDT", "bidPrice": "4066.90", "askPrice": "4066.91", "time": 1794383462732}}
    )
    quote = BinanceSource(get_json=fake, clock=lambda: 0).fetch_quote()
    assert quote.source == "binance"
    assert quote.bid == 4066.90 and quote.ask == 4066.91
    assert quote.price == pytest.approx(4066.905)
    assert quote.spread == pytest.approx(0.01)
    assert quote.time == pytest.approx(1794383462.732)


@pytest.mark.parametrize(
    "payload",
    [
        {"code": -1121, "msg": "Invalid symbol."},
        [["not", "a", "kline"]],
        [["soon", "1", "2", "0.5", "1.5", "1", 0]],  # open time is not a number
        [kline(T0, 4000, 3990, 3999, 4001)],  # high below open: fails validation
    ],
)
def test_binance_bad_kline_responses_raise(payload):
    source = BinanceSource(get_json=FakeHttp({"/fapi/v1/klines": payload}), clock=lambda: T0 + 10 * M15)
    with pytest.raises(DataSourceError):
        source.fetch_candles("M15", limit=1)


@pytest.mark.parametrize(
    "payload",
    [{"code": -1121, "msg": "Invalid symbol."}, {"bidPrice": "4067", "askPrice": "4066"}, {"bidPrice": "x", "askPrice": "1"}],
)
def test_binance_bad_quote_responses_raise(payload):
    source = BinanceSource(get_json=FakeHttp({"/fapi/v1/ticker/bookTicker": payload}))
    with pytest.raises(DataSourceError):
        source.fetch_quote()


def test_binance_network_errors_propagate_as_datasource_errors():
    source = BinanceSource(get_json=FakeHttp({"/fapi/v1/klines": DataSourceError("cannot reach")}))
    with pytest.raises(DataSourceError):
        source.fetch_candles("M15")


def test_binance_rejects_bad_arguments():
    source = BinanceSource(get_json=FakeHttp({}))
    with pytest.raises(ValueError):
        source.fetch_candles("M7")
    with pytest.raises(ValueError):
        source.fetch_candles("M15", limit=0)
    with pytest.raises(ValueError):
        source.fetch_candles("M15", limit=1501)


# --- Twelve Data ------------------------------------------------------------


def td_values(n: int = 3) -> list:
    rows = []
    for i in range(n):
        stamp = pd.Timestamp(T0 + i * M15, unit="s", tz="UTC").strftime("%Y-%m-%d %H:%M:%S")
        rows.append({"datetime": stamp, "open": f"{4000 + i}", "high": f"{4002 + i}", "low": f"{3999 + i}", "close": f"{4001 + i}"})
    return list(reversed(rows))  # Twelve Data answers newest first


def test_twelvedata_candles_parse_sort_and_mark_forming():
    fake = FakeHttp({"/time_series": {"meta": {}, "values": td_values(3), "status": "ok"}})
    source = TwelveDataSource("key123", get_json=fake, clock=lambda: T0 + 2 * M15 + 5)
    frame = source.fetch_candles("M15", limit=3)

    assert frame["time"].tolist() == [T0, T0 + M15, T0 + 2 * M15]
    assert frame["closed"].tolist() == [True, True, False]
    assert frame["volume"].isna().all()
    assert frame.iloc[2]["close"] == 4003
    _, params = fake.calls[0]
    assert params == {"symbol": "XAU/USD", "interval": "15min", "outputsize": "3", "timezone": "UTC", "apikey": "key123"}


@pytest.mark.parametrize("now", [T0 - 3600, T0 + 2 * M15 + 5, T0 + 86400])
def test_twelvedata_closed_flag_ignores_the_pc_clock(now):
    fake = FakeHttp({"/time_series": {"meta": {}, "values": td_values(3), "status": "ok"}})
    frame = TwelveDataSource("k", get_json=fake, clock=lambda: now).fetch_candles("M15", limit=3)
    assert frame["closed"].tolist() == [True, True, False]


def test_twelvedata_error_payload_raises_without_the_key():
    fake = FakeHttp({"/time_series": {"code": 404, "message": "symbol not available on your plan", "status": "error"}})
    source = TwelveDataSource("secret-key", get_json=fake, clock=lambda: T0)
    with pytest.raises(DataSourceError) as info:
        source.fetch_candles("M15")
    assert "404" in str(info.value)
    assert "secret-key" not in str(info.value)


def test_twelvedata_needs_a_key():
    with pytest.raises(DataSourceError, match="API key"):
        TwelveDataSource("")


def test_twelvedata_quote_is_timed_at_fetch():
    fake = FakeHttp({"/price": {"price": "4062.50"}})
    quote = TwelveDataSource("k", get_json=fake, clock=lambda: 123.0).fetch_quote()
    assert (quote.price, quote.bid, quote.ask, quote.time, quote.spread) == (4062.5, None, None, 123.0, None)


# --- CSV --------------------------------------------------------------------


def write_csv(tmp_path, text: str):
    path = tmp_path / "candles.csv"
    path.write_text(text, encoding="utf-8")
    return path


def test_csv_source_reads_unix_times_and_aggregates(tmp_path):
    lines = ["time,open,high,low,close,volume"]
    lines += [f"{T0 + i * M15},{100 + i},{101 + i},{99 + i},{100.5 + i},1" for i in range(8)]
    source = CsvSource(write_csv(tmp_path, "\n".join(lines)))

    m15 = source.fetch_candles("M15", limit=3)
    assert m15["time"].tolist() == [T0 + 5 * M15, T0 + 6 * M15, T0 + 7 * M15]
    h1 = source.fetch_candles("H1")
    assert h1["time"].tolist() == [T0, T0 + 3600]
    assert h1["closed"].all()

    quote = source.fetch_quote()
    assert quote.price == 107.5
    assert quote.time == T0 + 8 * M15


def test_csv_source_reads_text_times_and_closed_flags(tmp_path):
    text = (
        "time,open,high,low,close,closed\n"
        "2026-11-11 07:00:00,1,2,0.5,1.5,true\n"
        "2026-11-11 07:15:00,1.5,2,1,1.8,false\n"
    )
    source = CsvSource(write_csv(tmp_path, text))
    frame = source.fetch_candles("M15")
    assert frame["time"].iloc[0] == int(pd.Timestamp("2026-11-11 07:00:00", tz="UTC").timestamp())
    assert frame["closed"].tolist() == [True, False]
    assert source.fetch_quote().price == 1.5  # last closed candle only


def test_csv_source_bad_file_raises(tmp_path):
    with pytest.raises(DataSourceError):
        CsvSource(tmp_path / "missing.csv").fetch_candles("M15")
    bad = write_csv(tmp_path, "time,open,high,low,close\n100,1,2,0.5,1.5\n")  # not on an M15 boundary
    with pytest.raises(DataSourceError, match="aligned"):
        CsvSource(bad).fetch_candles("M15")


# --- factory and HTTP client -------------------------------------------------


def test_make_source_builds_each_source():
    settings = Settings(_env_file=None, twelvedata_api_key="k")
    assert make_source(settings).name == "binance"
    assert make_source(settings, "twelvedata").name == "twelvedata"
    assert make_source(settings, "csv").name == "csv"
    with pytest.raises(ValueError):
        make_source(settings, "mt5")


def test_api_key_is_hidden_in_settings_repr():
    settings = Settings(_env_file=None, twelvedata_api_key="very-secret")
    assert "very-secret" not in repr(settings)


def test_http_errors_do_not_leak_query_parameters(monkeypatch):
    def refuse(request, timeout):
        raise URLError("connection refused")

    monkeypatch.setattr(http_mod, "urlopen", refuse)
    with pytest.raises(DataSourceError) as info:
        http_mod.get_json("https://api.example.test/price", {"apikey": "secret-key"})
    assert "secret-key" not in str(info.value)

    def http_error(request, timeout):
        raise HTTPError(request.full_url, 429, "Too Many Requests", {}, io.BytesIO(b'{"msg":"slow down"}'))

    monkeypatch.setattr(http_mod, "urlopen", http_error)
    with pytest.raises(DataSourceError) as info:
        http_mod.get_json("https://api.example.test/price", {"apikey": "secret-key"})
    assert "429" in str(info.value) and "secret-key" not in str(info.value)


@pytest.mark.parametrize(
    "error", [http.client.IncompleteRead(b"partial"), http.client.BadStatusLine("garbled"), TimeoutError("slow")]
)
def test_http_transport_errors_become_datasource_errors(monkeypatch, error):
    # A connection cut in mid-response is not an OSError; it must still arrive
    # as DataSourceError so a long-running collector logs it and carries on.
    class Broken:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def read(self):
            raise error

    monkeypatch.setattr(http_mod, "urlopen", lambda request, timeout: Broken())
    with pytest.raises(DataSourceError):
        http_mod.get_json("https://api.example.test/klines", {})


def test_http_client_does_not_name_the_project():
    assert "tradepilot" not in http_mod.USER_AGENT.lower()


def test_quote_spread_is_none_without_bid_and_ask():
    fake = FakeHttp({"/price": {"price": "1"}})
    assert TwelveDataSource("k", get_json=fake).fetch_quote().spread is None
    assert not math.isnan(BinanceSource(get_json=FakeHttp({"/fapi/v1/ticker/bookTicker": {"bidPrice": "1", "askPrice": "1"}})).fetch_quote().price)
