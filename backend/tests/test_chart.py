"""Candles for the chart: where each timeframe comes from, how it is refreshed, what a failure costs."""

from __future__ import annotations

import calendar
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

from backend.app import chart as ch
from backend.app.core import candles as cd
from backend.app.datasource.base import DataSource, DataSourceError, Quote
from backend.tests.test_web import site  # noqa: F401  (the fixture: an app over fakes)

BINANCE_LIKE = ("M1", "M3", "M5", "M15", "M30", "H1", "H2", "H4", "D1", "W1", "MN1")


def at(year: int, month: int = 1, day: int = 1, hour: int = 0, minute: int = 0, second: int = 0) -> int:
    return calendar.timegm((year, month, day, hour, minute, second))


NOW = at(2026, 11, 20, 13, 7, 30)  # a Friday afternoon, seven and a half minutes past the hour


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Native(DataSource):
    """A source with its own candle of every listed length, from ``listed`` up to the clock's present."""

    name = "fake"
    symbol = "XAUTEST"

    def __init__(self, clock: Clock, timeframes: tuple[str, ...] = BINANCE_LIKE, listed: int = at(2020)) -> None:
        self.chart_timeframes = timeframes
        self.clock = clock
        self.listed = listed
        self.calls: list[tuple[str, int]] = []
        self.error: Exception | None = None

    def fetch_candles(self, timeframe: str, limit: int = 500) -> pd.DataFrame:
        assert timeframe in self.chart_timeframes, timeframe
        self.calls.append((timeframe, limit))
        if self.error is not None:
            raise self.error
        now = int(self.clock())
        first, newest = (int(cd.opens(pd.Series([moment]), timeframe).iloc[0]) for moment in (self.listed, now))
        if timeframe in cd.MONTH_TIMEFRAMES:
            times = [first]
            while times[-1] < newest:
                times.append(int(cd.following(pd.Series([times[-1]]), timeframe).iloc[0]))
            times = times[-limit:]
        else:
            step = cd.timeframe_seconds(timeframe)
            count = min(limit, (newest - first) // step + 1)
            times = [newest - step * back for back in range(count - 1, -1, -1)]
        frame = pd.DataFrame({"time": times})
        frame["open"] = 4000.0 + frame["time"] / 86400 % 50
        frame["high"] = frame["open"] + 3.0
        frame["low"] = frame["open"] - 2.0
        frame["close"] = frame["open"] + 1.0
        frame["volume"] = 10.0
        frame["closed"] = True
        last = frame.index[-1]
        frame.loc[last, "close"] = frame.loc[last, "open"] + (now - times[-1]) % 20 * 0.1  # the forming candle moves
        frame.loc[last, "closed"] = False
        return cd.validate(frame, timeframe)

    def fetch_quote(self) -> Quote:  # the chart never asks for one
        raise AssertionError("not used")


def feed_of(clock: Clock, count: int = 1500) -> Any:
    """What the chart needs of the live feed: its timeframe and the candles it keeps."""
    frame = Native(clock, ("M15",), listed=int(clock()) - 900 * (count - 1)).fetch_candles("M15", count)
    return SimpleNamespace(timeframe="M15", frame=lambda: frame)


def make(timeframes: tuple[str, ...] = BINANCE_LIKE, listed: int = at(2020), **options: Any):
    clock, timer = Clock(NOW), Clock(1000.0)
    source = Native(clock, timeframes, listed)
    chart = ch.ChartCandles(source, feed_of(clock), timer=timer, **options)
    return chart, source, clock, timer


def pass_seconds(seconds: float, *clocks: Clock) -> None:
    for clock in clocks:
        clock.now += seconds


# --- where a timeframe comes from -----------------------------------------------------------------


def test_a_source_with_binances_intervals_can_show_the_whole_menu():
    chart, source, *_ = make()
    assert chart.timeframes() == list(cd.CHART_TIMEFRAMES)
    built = {tf: chart.candles(tf, 5)["built_from"] for tf in chart.timeframes()}
    assert {tf: origin for tf, origin in built.items() if origin} == {
        "M2": "M1", "M10": "M5", "M45": "M15", "H3": "H1", "MN3": "MN1", "MN6": "MN1", "MN12": "MN1",
    }  # fmt: skip
    # The signal timeframe, and what is built from it, never costs a request.
    assert {tf for tf, _ in source.calls} == set(BINANCE_LIKE) - {"M15"}


def test_a_source_without_intervals_of_its_own_shows_what_the_signal_candles_can_build():
    chart, source, *_ = make(timeframes=())
    assert chart.timeframes() == ["M15", "M30", "M45", "H1", "H2", "H3", "H4", "D1", "W1", "MN1", "MN3", "MN6", "MN12"]
    hours = chart.candles("H1", 1500)
    assert hours["built_from"] == "M15" and hours["history"] == "full" and hours["gaps"] == 0
    assert len(hours["bars"]) == 375 and hours["bars"][-1]["closed"] is False  # 1500 quarters of an hour
    with pytest.raises(ValueError):
        chart.candles("M5", 10)
    assert source.calls == []


def test_the_answer_names_its_source_and_marks_the_forming_candle():
    chart, *_ = make()
    answer = chart.candles("H4", 3)
    assert {key: value for key, value in answer.items() if key != "bars"} == {
        "timeframe": "H4", "source": "fake", "symbol": "XAUTEST", "built_from": None,
        "history": "full", "partial_first": False, "gaps": 0,
    }  # fmt: skip
    assert [item["time"] for item in answer["bars"]] == [at(2026, 11, 20, 4), at(2026, 11, 20, 8), at(2026, 11, 20, 12)]
    assert [item["closed"] for item in answer["bars"]] == [True, True, False]
    assert set(answer["bars"][0]) == {"time", "open", "high", "low", "close", "volume", "closed"}


def test_longer_candles_follow_the_rule_first_open_highest_high_lowest_low_last_close_summed_volume():
    chart, source, *_ = make()
    minutes = {item["time"]: item for item in chart.candles("M1", 1500)["bars"]}
    two = chart.candles("M2", 3)["bars"][0]
    first, second = minutes[two["time"]], minutes[two["time"] + 60]
    assert two == {
        "time": two["time"],
        "open": first["open"],
        "high": max(first["high"], second["high"]),
        "low": min(first["low"], second["low"]),
        "close": second["close"],
        "volume": first["volume"] + second["volume"],
        "closed": True,
    }
    assert two["time"] % 120 == 0 and source.calls == [("M1", 1500)]  # one request served both


# --- history ----------------------------------------------------------------------------------------


def test_a_young_instrument_shows_a_short_history_and_a_marked_partial_first_candle():
    chart, source, *_ = make(listed=at(2026, 3, 10))  # listed in March 2026
    months = chart.candles("MN1", 1500)
    assert months["history"] == "short" and months["partial_first"] is False
    assert [item["time"] for item in months["bars"]] == [at(2026, month) for month in range(3, 12)]

    year = chart.candles("MN12", 1500)
    assert year["history"] == "short" and year["partial_first"] is True and year["built_from"] == "MN1"
    assert [(item["time"], item["closed"]) for item in year["bars"]] == [(at(2026), False)]
    quarters = chart.candles("MN3", 1500)
    assert [item["time"] for item in quarters["bars"]] == [at(2026, 1), at(2026, 4), at(2026, 7), at(2026, 10)]
    assert quarters["partial_first"] is True
    # A page that asks for fewer candles than exist is not told the history ends at its first one.
    cut = chart.candles("MN3", 2)
    assert (cut["history"], cut["partial_first"], len(cut["bars"])) == ("full", False, 2)
    assert {call for call in source.calls} == {("MN1", 240)}  # months are asked for whole, and not 1500 of them


def test_a_hole_in_the_sources_candles_is_counted_not_filled(monkeypatch):
    chart, source, *_ = make()
    whole = source.fetch_candles

    def holed(timeframe: str, limit: int = 500) -> pd.DataFrame:
        frame = whole(timeframe, limit)
        return frame.drop(frame.index[[-40, -39, -10]]).reset_index(drop=True)  # three hours never came

    monkeypatch.setattr(source, "fetch_candles", holed)
    assert chart.candles("H1", 1500)["gaps"] == 2
    three = chart.candles("H3", 1500)
    assert three["built_from"] == "H1" and three["gaps"] == 2  # the buckets with a hole are left out


# --- refreshing ---------------------------------------------------------------------------------------


def test_a_timeframe_is_loaded_once_then_refreshed_with_a_tail_no_more_often_than_the_interval():
    chart, source, clock, timer = make(refresh_seconds=2.0)
    first = chart.candles("M5", 3)["bars"]
    assert source.calls == [("M5", 1500)]

    pass_seconds(1.9, clock, timer)
    assert chart.candles("M5", 3)["bars"] == first and len(source.calls) == 1  # too soon: answered from memory

    pass_seconds(0.6, clock, timer)  # 13:07:32.5
    moved = chart.candles("M5", 3)["bars"]
    assert source.calls[-1] == ("M5", 3)
    assert [item["time"] for item in moved] == [item["time"] for item in first]
    assert moved[-1]["close"] != first[-1]["close"] and moved[:-1] == first[:-1]  # only the forming candle moved

    pass_seconds(150, clock, timer)  # past 13:10: a new candle has opened
    later = chart.candles("M5", 3)["bars"]
    assert source.calls[-1] == ("M5", 3) and len(source.calls) == 3
    assert later[-1]["time"] == at(2026, 11, 20, 13, 10) and [item["closed"] for item in later] == [True, True, False]
    assert later[-2]["time"] == first[-1]["time"] and later[-2]["close"] == later[-2]["open"] + 1.0  # got its close
    assert len(chart.candles("M5", 1500)["bars"]) == 1500  # the kept history does not grow past what was asked


def test_a_tail_that_no_longer_connects_loads_the_history_again():
    chart, source, clock, timer = make()
    chart.candles("M1", 3)
    pass_seconds(600, clock, timer)  # ten minutes unwatched: three newest candles do not reach the kept ones
    bars = chart.candles("M1", 1500)["bars"]
    assert source.calls == [("M1", 1500), ("M1", 3), ("M1", 1500)]
    assert bars[-1]["time"] == at(2026, 11, 20, 13, 17) and cd.count_breaks(pd.DataFrame(bars), "M1") == 0


# --- failures -------------------------------------------------------------------------------------------


def test_a_source_that_fails_costs_the_chart_and_is_left_alone_for_a_while():
    chart, source, clock, timer = make()
    source.error = DataSourceError("connection reset")
    with pytest.raises(ch.ChartUnavailable) as failure:
        chart.candles("H1", 100)
    assert failure.value.retry_after == 5.0 and source.calls == [("H1", 1500)]
    with pytest.raises(ch.ChartUnavailable) as failure:
        chart.candles("H3", 100)  # built from the same candles: the same wait
    assert 0 < failure.value.retry_after <= 5.0 and len(source.calls) == 1  # not asked again yet
    assert chart.candles("M45", 5)["bars"]  # what comes from the feed's memory is not touched

    source.error = None
    pass_seconds(5, clock, timer)
    assert len(chart.candles("H1", 100)["bars"]) == 100 and len(source.calls) == 2


def test_a_refusal_keeps_the_chart_away_for_as_long_as_the_source_asks():
    chart, source, clock, timer = make()
    chart.candles("D1", 5)
    source.error = DataSourceError("too many requests", status=429, retry_after=120)
    pass_seconds(3, clock, timer)
    with pytest.raises(ch.ChartUnavailable) as failure:
        chart.candles("D1", 5)
    assert failure.value.retry_after == 120.0  # kept candles are not passed off as current either
    source.error = None
    pass_seconds(119, clock, timer)
    with pytest.raises(ch.ChartUnavailable):
        chart.candles("D1", 5)
    assert source.calls == [("D1", 1000), ("D1", 3)]
    pass_seconds(1, clock, timer)
    assert chart.candles("D1", 5)["bars"][-1]["closed"] is False

    source.error = DataSourceError("banned", status=418)
    pass_seconds(3, clock, timer)
    with pytest.raises(ch.ChartUnavailable) as failure:
        chart.candles("D1", 5)
    assert failure.value.retry_after == 60.0


# --- the route ------------------------------------------------------------------------------------------


def test_the_route_answers_503_with_a_wait_when_the_source_gives_no_candles(site):  # noqa: F811
    s = site()
    s.load()
    member = s.member()

    def unavailable(timeframe: str, limit: int) -> dict[str, Any]:
        raise ch.ChartUnavailable(7.2)

    s.app.state.chart.candles = unavailable
    response = member.get("/api/candles?timeframe=H1&limit=3")
    assert response.status_code == 503 and response.headers["retry-after"] == "8"
    assert response.json() == {"detail": "the price source gave no candles"}
    assert member.get("/api/state").status_code == 200  # the rest of the site is not affected
