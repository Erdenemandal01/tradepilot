"""The chart's live candle stream: the manager's threads, the chart laying updates, the route, and the signals.

The streams here are scripted fakes (``Script``) handed out by a fake source
in order. The manager runs real threads with very short pauses; every wait
in a test has a time limit. The signal path is checked to be untouched: the
live feed decides the same with a stream running as without one.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from types import SimpleNamespace
from typing import Any, Callable

import pandas as pd
import pytest
from fastapi import HTTPException

from backend.app import auth, live, main, notify
from backend.app import chart as ch
from backend.app.core import candles as cd
from backend.app.datasource import binance_source as bs
from backend.app.datasource.base import DataSourceError, StreamCandle
from backend.app.live import LiveFeed
from backend.tests.test_chart import NOW, Clock, Native, feed_of, pass_seconds
from backend.tests.test_live import PC_AHEAD, UP, FakeClock, FakeSource, make_settings, opens_at
from backend.tests.test_web import SECRET, site, walk  # noqa: F401  (site: the fixture)

FAST = {"pauses": (0.02, 0.05, 0.1), "idle_seconds": 30.0, "read_timeout": 0.02}
EVENT_KEYS = {"timeframe", "bars", "mode", "at", "age", "rev", "gaps", "error", "retry_after"}


def wait_until(condition: Callable[[], Any], timeout: float = 5.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("not reached in time")


class Script:
    """A stream that hands out its items in order: a candle, an error (the stream breaks), a callable
    (run, then the next item), or None (a read that times out). Then it stays open and silent."""

    def __init__(self, *items: Any) -> None:
        self.items = list(items)
        self.closed = threading.Event()

    def read(self, timeout: float) -> StreamCandle | None:
        while True:
            if self.closed.is_set():
                raise DataSourceError("the stream was closed")
            if not self.items:
                self.closed.wait(timeout)
                return None
            item = self.items.pop(0)
            if isinstance(item, BaseException):
                self.closed.set()
                raise item
            if item is None:
                time.sleep(min(timeout, 0.01))
                return None
            if callable(item) and not isinstance(item, StreamCandle):
                item()
                continue
            return item

    def close(self) -> None:
        self.closed.set()


class Streaming(Native):
    """The chart tests' source, with a live stream of three of its intervals."""

    stream_timeframes = ("M1", "M5", "M15")

    def __init__(self, clock: Clock, **options: Any) -> None:
        super().__init__(clock, **options)
        self.scripts: list[Any] = []
        self.opened: list[tuple[str, float]] = []

    def open_candle_stream(self, timeframe: str) -> Script:
        self.opened.append((timeframe, time.monotonic()))
        if not self.scripts:
            raise DataSourceError("no stream to give")
        item = self.scripts.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def update(self, timeframe: str, start: int, close_up: float, *, closed: bool = False) -> StreamCandle:
        """A stream update of the candle opening at ``start``, with the source's own open, high and low."""
        opened = 4000.0 + start / 86400 % 50
        return StreamCandle(start, opened, opened + 3.0, opened - 2.0, opened + close_up, 10.0, closed, float(NOW))


def forming(timeframe: str, now: float = NOW) -> int:
    return int(cd.opens(pd.Series([int(now)]), timeframe).iloc[0])


def streaming_chart(refresh: float = 2.0, **options: Any) -> tuple[ch.ChartCandles, Streaming, Clock]:
    clock = Clock(NOW)
    source = Streaming(clock)
    chart = ch.ChartCandles(
        source, feed_of(clock), refresh_seconds=refresh, stream=True, stream_options={**FAST, **options}
    )
    return chart, source, clock


@pytest.fixture
def charts():
    made: list[ch.ChartCandles] = []

    def make(*args: Any, **options: Any) -> tuple[ch.ChartCandles, Streaming, Clock]:
        chart, source, clock = streaming_chart(*args, **options)
        made.append(chart)
        return chart, source, clock

    yield make
    for chart in made:
        chart.close()
        assert chart.stream_threads() == []


def frame_of(chart: ch.ChartCandles, timeframe: str) -> pd.DataFrame:
    return chart._kept[timeframe].frame


# --- the manager and the chart, with a fake stream -------------------------------------------------------------


def test_a_stream_moves_the_forming_candle_and_closes_it_without_rest_requests(charts):
    chart, source, _ = charts()
    start = forming("M1")
    chart.candles("M1", 1500)
    source.scripts.append(
        Script(
            source.update("M1", start, 0.5),
            source.update("M1", start, 0.7, closed=True),  # the exchange's final values
            source.update("M1", start + 60, 0.1),
        )
    )
    chart.watch("M1")
    live_event = wait_until(lambda: (e := chart.live("M1"))["bars"][-1]["time"] == start + 60 and e)
    assert [(b["time"], b["closed"]) for b in live_event["bars"]] == [(start - 60, True), (start, True), (start + 60, False)]
    assert live_event["bars"][1]["close"] == 4000.0 + start / 86400 % 50 + 0.7
    assert live_event["mode"] == "stream" and live_event["error"] is None and live_event["gaps"] == 0
    # One full load, one tail for the refill after connecting; then the stream carries it.
    assert source.calls == [("M1", 1500), ("M1", 3)]
    times = frame_of(chart, "M1")["time"]
    assert times.is_monotonic_increasing and not times.duplicated().any() and len(times) == 1500


def test_a_candle_closed_only_by_the_next_one_is_confirmed_over_rest(charts, monkeypatch):
    monkeypatch.setattr(ch, "CONFIRM_AFTER", 0.2)
    chart, source, clock = charts()
    start = forming("M1")
    chart.candles("M1", 1500)

    def next_minute() -> None:
        clock.now = start + 63  # REST lists the next candle now; the earlier one has its final values there

    source.scripts.append(Script(source.update("M1", start, 0.4), next_minute, source.update("M1", start + 60, 0.1)))
    chart.watch("M1")
    wait_until(lambda: int(frame_of(chart, "M1")["time"].iloc[-1]) == start + 60)
    assert float(frame_of(chart, "M1")["close"].iloc[-2]) == 4000.0 + start / 86400 % 50 + 0.4  # the stream's last word
    final = 4000.0 + start / 86400 % 50 + 1.0  # what REST says the candle closed at
    wait_until(lambda: chart.live("M1")["bars"][-2]["close"] == final)
    assert chart._kept["M1"].confirm is None and source.calls[-1] == ("M1", 3)


def test_duplicates_and_updates_out_of_order_change_nothing(charts):
    chart, source, _ = charts()
    start = forming("M1")
    chart.candles("M1", 1500)
    before = frame_of(chart, "M1").copy()
    source.scripts.append(
        Script(
            source.update("M1", start, 0.4),
            source.update("M1", start, 0.4),  # the same update twice
            source.update("M1", start - 120, 2.5, closed=True),  # an older candle
            source.update("M1", start, 0.9),
        )
    )
    chart.watch("M1")
    wait_until(lambda: float(frame_of(chart, "M1")["close"].iloc[-1]) == 4000.0 + start / 86400 % 50 + 0.9)
    after = frame_of(chart, "M1")
    assert len(after) == len(before) and after["time"].tolist() == before["time"].tolist()
    assert after.iloc[:-1].equals(before.iloc[:-1])  # nothing but the forming candle moved


def test_a_jump_over_a_hole_is_refilled_over_rest_not_built_over(charts):
    chart, source, clock = charts(refresh=0.5)
    start = forming("M1")
    chart.candles("M1", 1500)
    seen: list[pd.DataFrame] = []
    original = chart._lay

    def lay(origin: str, bar: dict, event_time: float) -> str:
        outcome = original(origin, bar, event_time)
        seen.append(frame_of(chart, "M1").copy())
        return outcome

    chart._lay = lay  # type: ignore[method-assign]

    def later() -> None:
        clock.now += 180  # REST now lists the three minutes as well

    source.scripts.append(Script(source.update("M1", start + 180, 0.3), None, later, *[None] * 40,
                                 source.update("M1", start + 180, 0.6)))  # fmt: skip
    chart.watch("M1")
    # An open page's stream asks for the candles every half second, as the route does.
    wait_until(lambda: chart.live("M1")["bars"][-1]["time"] == start + 180, timeout=8)
    for frame in seen:  # never a frame with the new candle laid over the missing ones
        assert cd.count_breaks(frame, "M1") == 0
    final = frame_of(chart, "M1")
    assert final["time"].tolist()[-4:] == [start, start + 60, start + 120, start + 180]
    assert not final["time"].duplicated().any()


def test_after_a_reconnect_the_candles_missed_meanwhile_are_there_once_and_in_order(charts):
    chart, source, clock = charts()
    start = forming("M1")
    first = chart.candles("M1", 1500)
    rev = first["rev"]

    def five_minutes_pass() -> None:
        clock.now += 300

    source.scripts += [
        Script(source.update("M1", start, 0.4), five_minutes_pass, DataSourceError("connection reset")),
        Script(source.update("M1", start + 300, 0.2)),
    ]
    chart.watch("M1")
    wait_until(lambda: int(frame_of(chart, "M1")["time"].iloc[-1]) == start + 300)
    frame = frame_of(chart, "M1")
    assert cd.count_breaks(frame, "M1") == 0 and not frame["time"].duplicated().any()
    assert frame["time"].tolist()[-6:] == [start + 60 * i for i in range(6)]
    assert [tf for tf, _ in source.opened] == ["M1", "M1"]
    assert source.opened[1][1] - source.opened[0][1] >= FAST["pauses"][0]
    # The tail after the reconnect did not reach the kept candles: the history was loaded again, and
    # the page is told (older candles changed: the old forming one is final now, five more came).
    assert source.calls[-1] == ("M1", 1500) or source.calls[-2] == ("M1", 1500)
    assert chart.candles("M1", 10)["rev"] > rev
    assert chart.live("M1")["rev"] > rev


def test_a_refusal_is_waited_out_and_failures_wait_longer_each_time(charts):
    chart, source, _ = charts(pauses=(0.05, 0.15, 0.3))
    chart.candles("M1", 3)
    source.scripts += [DataSourceError("HTTP 429", status=429, retry_after=0.6), DataSourceError("refused")]
    chart.watch("M1")
    wait_until(lambda: len(source.opened) >= 4, timeout=6)
    gaps = [b[1] - a[1] for a, b in zip(source.opened, source.opened[1:])]
    assert gaps[0] >= 0.55  # the refusal's own wait, longer than the first pause
    assert gaps[1] >= 0.14 and gaps[2] >= 0.28 and gaps[2] > gaps[1]  # 0.15, then 0.3: growing


def test_a_stream_nobody_watches_stops_and_close_leaves_no_thread(charts):
    chart, source, _ = charts(idle_seconds=0.3)
    chart.candles("M1", 3)
    source.scripts.append(Script())
    chart.watch("M1")
    assert len(chart.stream_threads()) == 1
    wait_until(lambda: chart.stream_threads() == [], timeout=4)  # nobody asked for 0.3 s
    source.scripts += [Script(), Script()]
    chart.watch("M1")
    chart.watch("M5")
    assert len(chart.stream_threads()) == 2
    chart.close()
    assert chart.stream_threads() == []
    assert not [t for t in threading.enumerate() if t.name.startswith("chart-stream")]


def test_a_timeframe_without_a_stream_is_polled_as_before(charts):
    chart, source, _ = charts()
    chart.watch("H1")  # Streaming streams M1, M5, M15 only
    assert chart.stream_threads() == [] and chart.live("H1")["mode"] == "poll"
    assert chart.stream_info() == {
        "available": True,
        "timeframes": ["M1", "M2", "M5", "M10", "M15", "M45"],  # M2 from M1, M10 from M5, M45 from M15
    }


def test_the_setting_switches_the_stream_off():
    clock = Clock(NOW)
    chart = ch.ChartCandles(Streaming(clock), feed_of(clock), stream=False)
    chart.watch("M1")
    assert chart.stream_threads() == [] and chart.stream_info() == {"available": False, "timeframes": []}
    assert chart.live("M1")["mode"] == "poll"
    chart.close()


def test_built_timeframes_move_with_the_stream_of_their_origin(charts):
    chart, source, _ = charts()
    start = forming("M1")  # 13:07: the second minute of the 13:06 M2 candle
    source.scripts.append(Script(source.update("M1", start, 2.9)))
    chart.watch("M2")
    event = wait_until(lambda: (e := chart.live("M2"))["mode"] == "stream" and e)
    minutes = {b["time"]: b for b in chart.live("M1")["bars"]}
    newest = event["bars"][-1]
    first, second = minutes[start - 60], minutes[start]
    assert newest == {
        "time": start - 60, "open": first["open"], "high": max(first["high"], second["high"]),
        "low": min(first["low"], second["low"]), "close": second["close"],
        "volume": first["volume"] + second["volume"], "closed": False,
    }  # fmt: skip
    assert second["close"] == 4000.0 + start / 86400 % 50 + 2.9  # the stream's state is in it


# --- the signal timeframe: the feed's candles with the stream's newer forming candle ---------------------------


def test_the_signal_timeframe_shows_the_feeds_candles_and_lays_only_the_forming_one_over_them(charts):
    chart, source, clock = charts()
    feed_frame = chart._feed.frame()
    kept = feed_frame.copy()
    start = forming("M15")
    source.scripts.append(
        Script(
            source.update("M15", start - 900, 2.0, closed=True),  # a closed candle of the feed: never touched
            source.update("M15", start, 1.3),
        )
    )
    chart.watch("M15")
    event = wait_until(lambda: (e := chart.live("M15"))["mode"] == "stream" and e)
    closed_bar = event["bars"][-2]
    assert closed_bar["close"] == float(kept["close"].iloc[-2])  # the feed's value, what the rule ran on
    assert event["bars"][-1]["close"] == 4000.0 + start / 86400 % 50 + 1.3
    assert chart._feed.frame() is feed_frame and feed_frame.equals(kept)  # the feed's memory is not changed


def test_a_candle_the_stream_closed_before_the_feed_polled_it_is_shown_closed_with_the_next_one(charts):
    chart, source, _ = charts()
    start = forming("M15")
    source.scripts.append(Script(source.update("M15", start, 1.1, closed=True), source.update("M15", start + 900, 0.2)))
    chart.watch("M15")
    event = wait_until(lambda: (e := chart.live("M15"))["bars"][-1]["time"] == start + 900 and e)
    assert [(b["time"], b["closed"]) for b in event["bars"]] == [(start - 900, True), (start, True), (start + 900, False)]
    assert chart.live("M45")["bars"][-1]["time"] == forming("M45", start + 900)  # built from the same candles


# --- found by the verifiers on 2026-11-23: no candle reopened, no stale candle shown closed or as live --------------
#
# These run without the manager's threads: the stream's updates are handed to the chart by hand
# (``_on_candle`` / ``_lay``, what the thread calls), on a timer the test moves.


class FeedStub:
    timeframe = "M15"

    def __init__(self, frame: pd.DataFrame | None) -> None:
        self.current = frame

    def frame(self) -> pd.DataFrame | None:
        return self.current


def signal_chart(monkeypatch, count: int = 300) -> tuple[ch.ChartCandles, FeedStub, Clock, Clock, int]:
    """A chart over a feed whose newest M15 candle F is forming; a stream counted as connected."""
    clock, timer = Clock(NOW), Clock(1000.0)
    source = Streaming(clock)
    feed = FeedStub(Native(clock, ("M15",), listed=int(NOW) - 900 * (count - 1)).fetch_candles("M15", count))
    chart = ch.ChartCandles(source, feed, timer=timer, clock=clock, stream=True, stream_options=FAST)
    monkeypatch.setattr(chart._streams, "connected", lambda origin: True)  # as while the thread is connected
    return chart, feed, clock, timer, forming("M15")


def candle(start: int, close: float, *, closed: bool = False, volume: float = 10.0, at: float = NOW) -> StreamCandle:
    return StreamCandle(start, 4100.0, max(close, 4100.0) + 1.0, min(close, 4100.0) - 1.0, close, volume, closed, at)


def shape(event: dict[str, Any], base: int) -> list[tuple]:
    return [((b["time"] - base) // 900, b["close"], b["closed"]) for b in event["bars"]]


def test_a_signal_candle_the_feed_has_forming_is_not_closed_by_the_streams_next_candle(monkeypatch):
    chart, feed, clock, timer, start = signal_chart(monkeypatch)
    feeds_close = float(feed.current["close"].iloc[-1])
    chart.live("M15")
    pass_seconds(30, clock, timer)  # the feed has not polled for 30 s; the stream connected after F closed
    chart._on_candle("M15", candle(start + 900, 4120.5))  # F's final values never came over the stream
    event = chart.live("M15")
    assert shape(event, start)[-1] == (0, feeds_close, False)  # F as the feed has it: still forming, not closed
    assert event["mode"] == "poll" and event["age"] == 30.0  # the feed's freshness, not the stream's
    # The feed polls: F closed (the source listed F + 1). Now the stream's F + 1 is laid, live.
    feed.current = Native(Clock(start + 905), ("M15",), listed=int(NOW) - 900 * 299).fetch_candles("M15", 301)
    event = chart.live("M15")
    assert shape(event, start)[-2:] == [(0, float(feed.current["close"].iloc[-2]), True), (1, 4120.5, False)]
    assert event["mode"] == "stream" and event["age"] == 0.0


def test_a_feed_that_is_stuck_is_never_shown_as_live_however_far_the_stream_runs(monkeypatch):
    chart, feed, clock, timer, start = signal_chart(monkeypatch)
    first = chart.live("M15")
    pass_seconds(1800 + 60, clock, timer)  # REST refused for half an hour; the WebSocket still works
    for step in range(5):
        chart._on_candle("M15", candle(start + 1800, 4120.5 + step / 10, at=clock.now))
    event = chart.live("M15")
    assert shape(event, start)[-1] == (0, float(feed.current["close"].iloc[-1]), False)  # the feed's own F
    assert event["mode"] == "poll" and event["at"] == first["at"] and event["age"] == 1860.0
    built = chart.live("M45")
    assert built["mode"] == "poll" and built["age"] == 1860.0


def test_final_values_of_the_exchange_stay_while_the_feed_is_behind_and_no_candle_reopens(monkeypatch):
    chart, feed, clock, timer, start = signal_chart(monkeypatch)
    chart.live("M15")
    shown: dict[int, tuple] = {}  # every candle once sent as closed, with its values
    steps = [
        (candle(start, 4130.0, closed=True, volume=50.0), "stream"),  # F closes with the exchange's flag
        (candle(start + 900, 4131.0), "stream"),
        (candle(start + 900, 4132.0, closed=True, volume=40.0), "stream"),
        (candle(start + 1800, 4133.0), "poll"),  # two candles past the feed's own: not laid, not live
        (candle(start + 2700, 4134.0), "poll"),
    ]
    for update, mode in steps:
        pass_seconds(0.5, clock, timer)
        chart._on_candle("M15", update)
        event = chart.live("M15")
        assert event["mode"] == mode, (update, event["mode"])
        for bar in event["bars"]:
            if bar["time"] in shown:
                assert (bar["close"], bar["volume"], bar["closed"]) == shown[bar["time"]], bar  # never reopened, never changed
            elif bar["closed"]:
                shown[bar["time"]] = (bar["close"], bar["volume"], True)
    assert shape(event, start)[-2:] == [(0, 4130.0, True), (1, 4132.0, True)]
    assert chart._feed.frame() is feed.current and not feed.current["closed"].iloc[-1]  # the feed is never touched


def test_a_stream_that_stalled_does_not_put_an_older_forming_state_over_the_feeds(monkeypatch):
    chart, feed, clock, timer, start = signal_chart(monkeypatch)
    newer = feed.current.copy()
    newer.loc[newer.index[-1], ["close", "volume"]] = [float(newer["high"].iloc[-1]), 99.0]  # more traded since
    feed.current = newer
    chart._on_candle("M15", candle(start, 4100.4, volume=20.0))  # the stream's last word, before those trades
    assert chart.live("M15")["bars"][-1]["volume"] == 99.0


def test_a_rest_tail_from_a_copy_that_lags_never_reopens_a_candle_the_exchange_closed():
    a = 1795600800  # the candle that closes
    rows = [
        {"time": a - 120, "open": 4100.0, "high": 4101.0, "low": 4099.0, "close": 4100.5, "volume": 5.0, "closed": True},
        {"time": a - 60, "open": 4100.5, "high": 4102.0, "low": 4100.0, "close": 4101.0, "volume": 6.0, "closed": True},
        {"time": a, "open": 4101.0, "high": 4103.0, "low": 4100.8, "close": 4102.2, "volume": 7.0, "closed": False},
    ]
    source = Scripted(rows)
    clock, timer = Clock(a + 50), Clock(1000.0)
    chart = ch.ChartCandles(source, FeedStub(None), timer=timer, clock=clock)
    chart.live("M1")
    chart._lay("M1", candle(a, 4102.6, volume=7.2).bar(), a + 59.0)
    pass_seconds(1.0, clock, timer)
    chart._lay("M1", candle(a, 4102.9, closed=True, volume=7.4).bar(), a + 60.05)  # the exchange's final values
    pass_seconds(2.5, clock, timer)  # no trade yet in the new minute: the stream is quiet, REST refreshes
    source.rows = rows[:2] + [{**rows[2], "high": 4102.5, "close": 4102.0, "volume": 6.5}]  # still pre-close
    event = chart.live("M1")
    assert len(source.calls) == 2  # the REST tail was asked for
    assert (event["bars"][-1]["close"], event["bars"][-1]["volume"], event["bars"][-1]["closed"]) == (4102.9, 7.4, True)
    chart._lay("M1", candle(a + 60, 4102.9, volume=0.1).bar(), a + 63.1)
    assert [(b["close"], b["closed"]) for b in chart.live("M1")["bars"][-2:]] == [(4102.9, True), (4102.9, False)]


class Scripted(Native):
    """A source of M1 candles that answers with the rows it is given (the last one forming)."""

    stream_timeframes = ("M1",)

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        super().__init__(Clock(NOW), ("M1",))
        self.rows = rows

    def fetch_candles(self, timeframe: str, limit: int = 500) -> pd.DataFrame:
        self.calls.append((timeframe, limit))
        return cd.validate(pd.DataFrame(self.rows[-limit:]), timeframe)


def test_a_candle_closed_only_by_the_next_one_is_not_sent_as_closed_until_rest_confirms_it(monkeypatch):
    monkeypatch.setattr(ch, "CONFIRM_AFTER", 1.0)
    a = 1795600860  # the second minute of an M2 candle
    rows = [
        {"time": a - 60 * k, "open": 4100.0, "high": 4101.0, "low": 4099.0, "close": 4100.5, "volume": 5.0, "closed": True}
        for k in range(10, 0, -1)
    ] + [{"time": a, "open": 4101.0, "high": 4102.5, "low": 4100.8, "close": 4102.0, "volume": 6.5, "closed": False}]
    source = Scripted(rows)  # a copy that lags: the candle A with values from before its close
    clock, timer = Clock(a + 61), Clock(1000.0)
    chart = ch.ChartCandles(source, FeedStub(None), timer=timer, clock=clock)
    chart.live("M1")
    chart._lay("M1", candle(a + 60, 4103.0, volume=0.2).bar(), a + 61.0)  # the stream's first word: the next candle
    for event in (chart.live("M1"), chart.candles("M1", 3)):
        assert [(b["time"] - a, b["closed"]) for b in event["bars"]][-2:] == [(0, False), (60, False)]
    assert chart.live("M2")["bars"][-2]["closed"] is False  # the M2 candle that holds A is not final either
    pass_seconds(2.1, clock, timer)
    chart.live("M1")  # REST asked again: still the lagging copy, A still listed as the newest
    assert source.calls[-1] == ("M1", 3) and chart.live("M1")["bars"][-2]["closed"] is False
    final = {**rows[-1], "high": 4103.0, "close": 4102.8, "volume": 7.9, "closed": True}
    source.rows = rows[:-1] + [final, {"time": a + 60, "open": 4102.8, "high": 4103.2, "low": 4102.7, "close": 4103.0,
                                        "volume": 0.3, "closed": False}]  # fmt: skip
    pass_seconds(2.1, clock, timer)
    event = chart.live("M1")
    assert (event["bars"][-2]["close"], event["bars"][-2]["volume"], event["bars"][-2]["closed"]) == (4102.8, 7.9, True)
    assert chart._kept["M1"].confirm is None and chart.live("M2")["bars"][-2]["closed"] is True


def test_a_stream_that_breaks_after_one_update_again_and_again_waits_longer_each_time(charts):
    chart, source, _ = charts(pauses=(0.05, 0.15, 0.3), stable_seconds=60.0)
    start = forming("M1")
    chart.candles("M1", 1500)
    source.scripts += [Script(source.update("M1", start, 0.1 * k), DataSourceError("cut")) for k in range(1, 5)]
    chart.watch("M1")
    wait_until(lambda: len(source.opened) >= 4, timeout=6)
    gaps = [b[1] - a[1] for a, b in zip(source.opened, source.opened[1:])]
    assert gaps[0] >= 0.04 and gaps[1] >= 0.14 and gaps[2] >= 0.28  # 0.05, 0.15, 0.3: one update is no recovery


def test_a_reading_error_that_is_not_a_data_source_error_does_not_end_the_thread_or_skip_the_pauses(charts):
    chart, source, _ = charts(pauses=(0.05, 0.15, 0.3))
    chart.candles("M1", 3)
    source.scripts += [Script(OverflowError("int too large")) for _ in range(4)]
    chart.watch("M1")
    thread = chart.stream_threads()[0]
    wait_until(lambda: len(source.opened) >= 4, timeout=6)
    gaps = [b[1] - a[1] for a, b in zip(source.opened, source.opened[1:])]
    assert gaps[0] >= 0.04 and gaps[1] >= 0.14 and gaps[2] >= 0.28
    assert chart.stream_threads() == [thread]  # the same thread all along
    assert source.scripts == []  # each of the four broke with OverflowError


def test_close_does_not_wait_long_for_a_thread_stuck_in_a_connect():
    release = threading.Event()

    class Hanging(Streaming):
        def open_candle_stream(self, timeframe: str) -> Script:
            release.wait(10)  # a TCP connect or handshake that gets no answer
            raise DataSourceError("no answer")

    clock = Clock(NOW)
    chart = ch.ChartCandles(Hanging(clock), feed_of(clock), stream=True, stream_options=FAST)
    chart.watch("M1")
    started = time.monotonic()
    chart.close(timeout=0.3)
    assert time.monotonic() - started < 1.0
    release.set()
    wait_until(lambda: chart.stream_threads() == [], timeout=3)  # it ends by itself once the connect returns


def test_a_chart_stream_is_woken_only_by_changes_of_its_own_origin():
    clock = Clock(NOW)
    chart = ch.ChartCandles(Streaming(clock), feed_of(clock))
    loop = asyncio.new_event_loop()
    try:
        m2, m5, everything = chart.subscribe(loop, "M2"), chart.subscribe(loop, "M5"), chart.subscribe(loop)
        chart._notify("M1")
        loop.run_until_complete(asyncio.sleep(0))
        assert (m2.wake.is_set(), m5.wake.is_set(), everything.wake.is_set()) == (True, False, True)  # M2 is built from M1
    finally:
        loop.close()


def test_the_daemon_pool_reuses_a_few_daemon_threads():
    pool = main.DaemonPool(4, "test-pool")
    names: set[str] = set()

    async def scenario() -> list[int]:
        results = []
        for value in range(20):  # one at a time, as one open stream asks
            results.append(await pool.run(lambda v=value: (names.add(threading.current_thread().name), v * 2)[1]))
        together = await asyncio.gather(*[pool.run(time.sleep, 0.2) for _ in range(4)])  # four pages at once
        assert together == [None] * 4
        return results

    started = time.monotonic()
    assert asyncio.run(scenario()) == [v * 2 for v in range(20)]
    assert time.monotonic() - started < 2.0  # the four waits ran side by side
    assert len(names) == 1  # the calls one after another all ran on the same thread
    assert all(t.daemon for t in threading.enumerate() if t.name.startswith("test-pool"))


# --- the Binance stream ---------------------------------------------------------------------------------------------


class FakeSocket:
    def __init__(self, *texts: str) -> None:
        self.texts = list(texts)
        self.closed = False

    def recv(self, timeout: float) -> str | None:
        return self.texts.pop(0) if self.texts else None

    def close(self) -> None:
        self.closed = True


def kline(start: int, values: tuple, closed: bool, *, symbol: str = "XAUUSDT", interval: str = "1m") -> str:
    o, h, low, c, v = values
    return json.dumps(
        {"e": "kline", "E": start * 1000 + 59_000, "s": symbol,
         "k": {"t": start * 1000, "T": start * 1000 + 59_999, "s": symbol, "i": interval, "o": o, "h": h, "l": low,
               "c": c, "v": v, "x": closed}}  # fmt: skip
    )


ROWS = [  # open time, then the strings the exchange sends: open, high, low, close, base volume
    (1795600800, ("4100.10", "4101.00", "4099.50", "4100.80", "12.345")),
    (1795600860, ("4100.80", "4102.25", "4100.00", "4101.90", "8.001")),
    (1795600920, ("4101.90", "4103.00", "4101.10", "4102.40", "3.5")),
]


def test_rest_and_stream_candles_have_one_shape():
    rest_rows = [[t * 1000, *values, t * 1000 + 59_999, "0", 1, "0", "0", "0"] for t, values in ROWS]
    seen: list[str] = []

    def connect(url: str) -> FakeSocket:
        seen.append(url)
        texts = [kline(ROWS[1][0], ROWS[1][1], True), json.dumps({"result": None, "id": 1}), kline(*ROWS[2], False)]
        return FakeSocket(*texts)

    source = bs.BinanceSource(get_json=lambda url, params: rest_rows[: int(params["limit"])], connect_stream=connect)
    rest = source.fetch_candles("M1", 3)
    stream = source.open_candle_stream("M1")
    assert seen == ["wss://fstream.binance.com/market/ws/xauusdt@kline_1m"]
    laid = source.fetch_candles("M1", 2)  # what REST said before: the second minute still forming
    while (candle := stream.read(0.1)) is not None:
        laid, outcome = cd.lay_stream(laid, candle.bar(), "M1")
        assert outcome in (cd.STREAM_SAME, cd.STREAM_NEXT)
    pd.testing.assert_frame_equal(laid, rest)  # time in UTC seconds, O H L C, volume, closed: equal
    assert list(laid.dtypes) == list(rest.dtypes)


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        kline(ROWS[0][0], ROWS[0][1], False, symbol="BTCUSDT"),
        kline(ROWS[0][0], ROWS[0][1], False, interval="5m"),
        kline(ROWS[0][0] + 30, ROWS[0][1], False),  # not on a minute boundary
        kline(ROWS[0][0], ("4100", "4099", "4098", "4100.5", "1"), False),  # high below the close
        kline(ROWS[0][0], ("4100", "4101", "4099", "-1", "1"), False),
        json.dumps({"e": "kline", "E": 1, "s": "XAUUSDT", "k": {"t": ROWS[0][0] * 1000, "i": "1m", "x": "yes"}}),
        # Python's json reads Infinity, NaN and 1e400 (as infinity): none of them is a time or a number.
        kline(ROWS[0][0], ROWS[0][1], False).replace(f'"t": {ROWS[0][0] * 1000}', '"t": Infinity'),
        kline(ROWS[0][0], ROWS[0][1], False).replace(f'"t": {ROWS[0][0] * 1000}', '"t": 1e400'),
        kline(ROWS[0][0], ROWS[0][1], False).replace(f'"t": {ROWS[0][0] * 1000}', '"t": NaN'),
        kline(ROWS[0][0], ROWS[0][1], False).replace(f'"E": {ROWS[0][0] * 1000 + 59_000}', '"E": Infinity'),
        kline(ROWS[0][0], ("4100", "4101", "4099", "4100.5", "Infinity"), False),  # volume
        kline(ROWS[0][0], ("4100", "4101", "4099", "4100.5", "-5"), False),
        kline(ROWS[0][0], ("4100", "4101", "4099", "4100.5", "NaN"), False),
        kline(ROWS[0][0], ("4100", "1e400", "4099", "4100.5", "1"), False),
    ],
)
def test_a_broken_stream_message_breaks_the_stream_and_is_never_a_candle(text):
    socket = FakeSocket(text)
    stream = bs.BinanceCandleStream(socket, "XAUUSDT", "M1")  # type: ignore[arg-type]
    with pytest.raises(DataSourceError):
        stream.read(0.1)
    assert socket.closed


# --- the route ------------------------------------------------------------------------------------------------------


def chart_stream_endpoint(app: Any) -> Any:
    (route,) = [r for m, p, r in walk(app.routes) if (m, p) == ("GET", "/api/candles/stream")]
    return route.endpoint


def member_request(s: Any, hours: float = 12) -> Any:
    return SimpleNamespace(cookies={auth.COOKIE: auth.new_token(SECRET, s.clock.now, hours)})


def event(chunk: str) -> tuple[str, dict[str, Any]]:
    assert chunk.endswith("\n\n")
    name, data = chunk[:-2].split("\n")
    assert name == "event: candles" and data.startswith("data: ")
    return name[len("event: ") :], json.loads(data[len("data: ") :])


def test_the_route_needs_a_session_and_a_timeframe_this_source_can_show(site):  # noqa: F811
    s = site()
    s.load()
    assert s.visitor().get("/api/candles/stream?timeframe=M15").status_code == 401
    member = s.member()
    for query in ("timeframe=M5", "timeframe=M10", "timeframe=MN2", "timeframe=m15", "timeframe="):
        assert member.get(f"/api/candles/stream?{query}").status_code == 422


def test_the_first_event_comes_at_once_in_the_shape_of_the_candles_route(site, monkeypatch):  # noqa: F811
    monkeypatch.setattr(main, "CHART_HEARTBEAT", 0.3)
    s = site()
    s.load()
    candles = s.member().get("/api/candles?timeframe=H1&limit=3").json()

    async def scenario() -> list[str]:
        response = await chart_stream_endpoint(s.app)(member_request(s), timeframe="H1")
        events = response.body_iterator
        got = [response.media_type, await asyncio.wait_for(anext(events), timeout=2)]
        got.append(await asyncio.wait_for(anext(events), timeout=2))  # nothing changed: the heartbeat
        await events.aclose()
        return got

    media_type, first, beat = asyncio.run(scenario())
    assert media_type == "text/event-stream"
    _, data = event(first)
    assert set(data) == EVENT_KEYS
    assert data["timeframe"] == "H1" and data["bars"] == candles["bars"] and data["rev"] == candles["rev"]
    assert data["mode"] == "poll" and data["error"] is None and data["gaps"] == 0
    assert data["age"] is not None and data["age"] < 5 and data["at"] is not None
    assert event(beat)[1]["bars"] == data["bars"]
    assert s.app.state.chart.open_streams() == 0  # the page left: nothing is kept for it


def test_before_the_feed_has_candles_the_event_says_unavailable_and_shows_nothing(site):  # noqa: F811
    s = site()

    async def scenario() -> str:
        response = await chart_stream_endpoint(s.app)(member_request(s), timeframe="M15")
        first = await asyncio.wait_for(anext(response.body_iterator), timeout=2)
        await response.body_iterator.aclose()
        return first

    data = event(asyncio.run(scenario()))[1]
    assert data["bars"] == [] and data["error"] == "unavailable" and data["retry_after"] > 0


def test_a_new_candle_is_sent_and_the_stream_ends_with_the_session(site, monkeypatch):  # noqa: F811
    monkeypatch.setattr(main, "CHART_CHECK", 0.05)
    s = site()
    s.load()

    async def scenario() -> list[str]:
        response = await chart_stream_endpoint(s.app)(member_request(s, hours=1), timeframe="M15")
        events = response.body_iterator
        got = [await asyncio.wait_for(anext(events), timeout=2)]
        s.source.cursor += 1
        s.source.clock.now += 900
        await asyncio.to_thread(s.feed.poll)
        got.append(await asyncio.wait_for(anext(events), timeout=2))
        s.clock.now += 3600  # the session's hour is over
        with pytest.raises(StopAsyncIteration):
            while True:
                await asyncio.wait_for(anext(events), timeout=6)
        return got

    first, second = (event(chunk)[1] for chunk in asyncio.run(scenario()))
    assert second["bars"][-1]["time"] == first["bars"][-1]["time"] + 900
    assert second["bars"][-2]["closed"] is True
    assert s.app.state.chart.open_streams() == 0


def test_shutdown_ends_an_open_chart_stream_at_once_and_too_many_are_refused(site, monkeypatch):  # noqa: F811
    monkeypatch.setattr(main, "CHART_STREAMS_MAX", 1)
    s = site()
    s.load()
    endpoint = chart_stream_endpoint(s.app)

    async def scenario() -> float:
        response = await endpoint(member_request(s), timeframe="M15")
        events = response.body_iterator
        await asyncio.wait_for(anext(events), timeout=2)
        with pytest.raises(HTTPException) as full:
            await endpoint(member_request(s), timeframe="M15")
        assert full.value.status_code == 503 and full.value.headers == {"Retry-After": "10"}
        started = time.monotonic()
        s.app.state.chart.end_streams()  # what Ctrl-C does first (main.end_streams_on_exit)
        with pytest.raises(StopAsyncIteration):
            while True:
                await asyncio.wait_for(anext(events), timeout=3)
        return time.monotonic() - started

    assert asyncio.run(scenario()) < 1.0


def test_streams_asked_for_at_the_same_moment_cannot_pass_the_cap(site, monkeypatch):  # noqa: F811
    monkeypatch.setattr(main, "CHART_STREAMS_MAX", 3)
    s = site()
    s.load()
    endpoint = chart_stream_endpoint(s.app)

    async def scenario() -> tuple[list[Any], int]:
        answers = await asyncio.gather(
            *[endpoint(member_request(s), timeframe="M15") for _ in range(12)], return_exceptions=True
        )
        counted = s.app.state.chart.open_streams()
        for answer in answers:
            if not isinstance(answer, BaseException):
                await answer.body_iterator.aclose()
        return answers, counted

    answers, counted = asyncio.run(scenario())
    refused = [a for a in answers if isinstance(a, HTTPException)]
    assert len(answers) - len(refused) == 3 and counted == 3  # the slot is taken when the request is accepted
    assert all(a.status_code == 503 and a.headers == {"Retry-After": "10"} for a in refused)


def test_a_chart_stream_that_never_starts_gives_its_slot_back(site, monkeypatch):  # noqa: F811
    s = site()
    s.load()

    async def scenario() -> tuple[int, int]:
        response = await chart_stream_endpoint(s.app)(member_request(s), timeframe="M15")
        taken = s.app.state.chart.open_streams()

        async def receive() -> dict[str, Any]:
            return {"type": "http.disconnect"}  # the page left before the first byte

        async def send(message: dict[str, Any]) -> None:
            pass

        await response({"type": "http", "asgi": {"version": "3.0", "spec_version": "2.0"}}, receive, send)
        return taken, s.app.state.chart.open_streams()

    assert asyncio.run(scenario()) == (1, 0)


def test_an_event_with_a_number_that_is_not_finite_is_never_sent(site, monkeypatch):  # noqa: F811
    monkeypatch.setattr(main, "CHART_CHECK", 0.05)
    s = site()
    s.load()
    chart = s.app.state.chart
    real = chart.live
    calls = []

    def live_(timeframe: str) -> dict[str, Any]:
        data = real(timeframe)
        calls.append(1)
        if len(calls) == 1:
            return {**data, "at": float("nan"), "bars": []}  # would leave as bare NaN: no page can parse it
        return data

    monkeypatch.setattr(chart, "live", live_)

    async def scenario() -> str:
        response = await chart_stream_endpoint(s.app)(member_request(s), timeframe="M15")
        first = await asyncio.wait_for(anext(response.body_iterator), timeout=3)
        await response.body_iterator.aclose()
        return first

    data = event(asyncio.run(scenario()))[1]  # strict JSON (json.loads would take NaN; checked below)
    assert data["bars"] and data["at"] is not None and "NaN" not in json.dumps(data)


def test_logging_out_ends_the_session_on_the_server_and_its_open_chart_stream(site, monkeypatch):  # noqa: F811
    monkeypatch.setattr(main, "CHART_CHECK", 0.05)
    s = site()
    s.load()
    request = member_request(s)
    token = request.cookies[auth.COOKIE]

    async def scenario() -> None:
        response = await chart_stream_endpoint(s.app)(request, timeframe="M15")
        events = response.body_iterator
        await asyncio.wait_for(anext(events), timeout=2)
        out = await asyncio.to_thread(s.with_token(token).post, "/api/logout")
        assert out.status_code == 200
        with pytest.raises(StopAsyncIteration):
            while True:
                await asyncio.wait_for(anext(events), timeout=6)

    asyncio.run(scenario())
    assert s.with_token(token).get("/api/state").status_code == 401  # a copy of the token works no more
    s.clock.now += 1
    assert s.member().get("/api/state").status_code == 200  # a new session does


def test_a_stop_signal_ends_the_chart_streams_too():
    import signal

    order: list[str] = []
    feed = SimpleNamespace(end_streams=lambda: order.append("ticks ended"))
    chart = SimpleNamespace(end_streams=lambda: order.append("chart streams ended"))
    before = signal.signal(signal.SIGINT, lambda number, frame: order.append("server stops"))
    try:
        undo = main.end_streams_on_exit(feed, chart)
        signal.raise_signal(signal.SIGINT)
        assert order == ["ticks ended", "chart streams ended", "server stops"]
        undo()
    finally:
        signal.signal(signal.SIGINT, before)


# --- the signals never notice ----------------------------------------------------------------------------------------


class StreamingFeedSource(FakeSource):
    """test_live's fake source with an M15 stream that keeps pushing the forming candle's final values."""

    stream_timeframes = ("M15",)

    def open_candle_stream(self, timeframe: str) -> Any:
        source = self

        class Endless:
            def __init__(self) -> None:
                self.closed = threading.Event()

            def read(self, timeout: float) -> StreamCandle | None:
                if self.closed.wait(0.002):
                    raise DataSourceError("closed")
                row = source.candles.iloc[source.cursor - 1]
                return StreamCandle(int(row.time), float(row.open), float(row.high), float(row.low), float(row.close),
                                    float(row.volume), False, float(row.time))  # fmt: skip

            def close(self) -> None:
                self.closed.set()

        return Endless()


def decide(with_stream: bool, monkeypatch) -> tuple[list[tuple], list[dict], list[str], bool]:
    monkeypatch.setattr(live, "REPLAY", 12)
    clock = FakeClock(opens_at(1399) + 3)
    pc, timer = FakeClock(clock.now + PC_AHEAD), FakeClock(5000.0)
    source = (StreamingFeedSource if with_stream else FakeSource)(UP, 1400, clock)
    notifier = notify.MemoryNotifier()
    feed = LiveFeed(source, make_settings(), notifier, clock=pc, timer=timer, closed_hours=lambda t: False)
    chart = ch.ChartCandles(source, feed, stream=with_stream, stream_options=FAST)
    streamed = False
    try:
        feed.poll()
        for _ in range(30):
            source.cursor += 1
            for clocked in (clock, pc, timer):
                clocked.now += 900
            chart.watch("M15")
            feed.poll()
            if with_stream:
                streamed = wait_until(lambda: chart.live("M15")["mode"] == "stream", timeout=3) or streamed
            chart.live("M45")
        decisions = [
            (d.result.signal_time, d.result.signal, d.result.score, d.blocked, d.sent, d.run) for d in feed._decisions
        ]
        return decisions, feed.snapshot()["log"], [subject for subject, _ in notifier.sent], streamed
    finally:
        chart.close()
        feed.close()


def test_the_live_feed_decides_exactly_the_same_with_a_stream_running(monkeypatch):
    without = decide(False, monkeypatch)
    with_stream = decide(True, monkeypatch)
    assert with_stream[3] is True  # the stream really ran and was laid over the chart
    assert with_stream[0] == without[0] and len(without[0]) > 30
    assert with_stream[1] == without[1] and with_stream[2] == without[2]
    assert any(subject for subject in without[2])  # signals were sent in the run, so there was something to compare
