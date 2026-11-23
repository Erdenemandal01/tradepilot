"""The live feed against a fake price source, a fake clock and a stopwatch moved by hand.

No test touches the network, sleeps or sends email. The candles come from
``trend`` in test_filters, on which the rule sends BUY signals in waves (SELL
with a falling slope). A test that needs "the next signal" walks the fake
source forward one candle at a time instead of relying on a candle number
picked by hand.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import math
import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import numpy as np
import pandas as pd
import pytest

from backend.app import live, notify
from backend.app.config import Settings
from backend.app.core import candles as cd
from backend.app.core import filters as fl
from backend.app.datasource.base import DataSource, DataSourceError, Quote
from backend.app.live import LiveFeed
from backend.tests.test_filters import M15, START, trend

HISTORY = 1300  # candles kept in memory here: enough closed H1 candles for the H1 score
REPLAY = 12  # candles judged again on a (re)load in most tests: the real 192 cost half a second each time
REAL_REPLAY = live.REPLAY
PC_AHEAD = 7777.0  # the PC clock is wrong by more than two hours in every test
SIGNAL_CODES = ("signal", "signal_emailed")
ALERT_SUBJECT = notify.alert_message("", "", 0, 0)[0]  # how every system alert's subject starts

# Changed on purpose (second page review): "poll_seconds" moved from the snapshot's own keys into
# the tick. The page's "server lost" limit follows it, and a page whose /api/state fails must
# still know it. The snapshot starts with a tick, so it carries the key as before.
TICK_KEYS = {"v", "now", "feed", "age", "price", "bid", "ask", "market_closed", "bars", "card", "poll_seconds"}
SNAPSHOT_KEYS = TICK_KEYS | {
    "symbol", "timeframe", "source", "reasons", "log", "today", "alerts", "email", "utc_offset_hours", "rules",
    "alert_after_seconds",
}  # fmt: skip
ROW_KEYS = {
    "time", "side", "entry", "zone_low", "zone_high", "sl", "tp", "sl_distance", "tp_distance",
    "expires_at", "valid_seconds", "grade", "run", "score", "h1_score",
}  # fmt: skip


def turning(count: int, turn: int, fall: float = 1.2) -> pd.DataFrame:
    """Like ``trend``, but the rise ends at candle ``turn``: M15 turns down while H1 still points up."""
    rows = []
    previous = 4000.0
    for i in range(count):
        close = 4000.0 + 0.5 * min(i, turn) - fall * max(i - turn, 0) + 3.0 * math.sin(i / 3.0)
        rows.append(
            {
                "time": START + i * M15,
                "open": previous,
                "high": max(previous, close) + 0.4,
                "low": min(previous, close) - 0.4,
                "close": close,
                "volume": 1.0,
                "closed": True,
            }
        )
        previous = close
    return cd.validate(pd.DataFrame(rows), "M15")


UP = trend(1600)
DOWN = trend(1600, slope=-0.5)
TURN = turning(1600, 1400)


def opens_at(index: int) -> int:
    """Open time of the candle with this row number (its close is the next one's open)."""
    return START + index * M15


class FakeClock:
    """A clock, or a stopwatch, that moves only when a test moves it."""

    def __init__(self, now: float) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now


class FakeSource(DataSource):
    """Every candle that will ever exist, and a cursor saying how many exist so far.

    ``fetch_candles`` returns the newest ``limit`` of them with the last one
    marked forming; ``fetch_quote`` is timed by the fake clock. While ``error``
    is set, the calls named in ``error_in`` raise it. As over a real network,
    the candle error comes back only after the quote request has left; set
    ``fails_at_once`` for an error that comes back before that.
    """

    name = "fake"
    symbol = "XAUTEST"

    def __init__(self, candles: pd.DataFrame, cursor: int, clock: FakeClock) -> None:
        self.candles = candles
        self.cursor = cursor
        self.clock = clock
        self.price: float | None = None  # None: the forming candle's open, which is the last close
        self.timed = True
        self.error: Exception | None = None
        self.error_in: tuple[str, ...] = ("candles", "quote")
        self.fails_at_once = False
        self.on_candles: Callable[[], None] | None = None
        self.calls: list[int] = []  # the limit of every candle request
        self.missing: set[int] = set()  # row numbers this answer lacks (a copy of the source without them)
        self.quotes = 0
        self.quote_left = threading.Event()

    def seen(self) -> pd.DataFrame:
        """All candles that exist so far; the last one is still forming."""
        frame = self.candles.iloc[: self.cursor].reset_index(drop=True).copy()
        frame.loc[frame.index[-1], "closed"] = False
        return frame.drop(index=[row for row in self.missing if row < self.cursor - 1]).reset_index(drop=True)

    def fetch_candles(self, timeframe: str, limit: int = 500) -> pd.DataFrame:
        assert timeframe == "M15"
        self.calls.append(limit)
        if self.on_candles is not None:
            self.on_candles()
        if self.error is not None and "candles" in self.error_in:
            if not self.fails_at_once:
                assert self.quote_left.wait(timeout=5.0), "the quote request never left"
            raise self.error
        return self.seen().iloc[-limit:].reset_index(drop=True)

    def fetch_history(self, timeframe: str, start: int | None = None, end: int | None = None) -> pd.DataFrame:
        assert timeframe == "M15"
        return self.seen()

    def fetch_quote(self) -> Quote:
        self.quote_left.set()
        self.quotes += 1
        if self.error is not None and "quote" in self.error_in:
            raise self.error
        price = self.price if self.price is not None else float(self.candles["open"].iloc[self.cursor - 1])
        return Quote(self.name, self.symbol, self.clock(), price, price - 0.05, price + 0.05, self.timed)


class World:
    """A feed with everything around it faked. Time passes only when a test says so."""

    def __init__(
        self, feed: LiveFeed, source: FakeSource, clock: FakeClock, pc: FakeClock, timer: FakeClock,
        notifier: notify.Notifier,
    ) -> None:  # fmt: skip
        self.feed, self.source, self.notifier = feed, source, notifier
        self.clock = clock  # the source's clock: right
        self.pc = pc  # the PC's clock: wrong by PC_AHEAD
        self.timer = timer  # the stopwatch the feed measures silence with

    def poll(self) -> float:
        self.source.quote_left.clear()  # a new cycle: see FakeSource
        return self.feed.poll()

    def pass_seconds(self, seconds: float) -> None:
        for clock in (self.clock, self.pc, self.timer):
            clock.now += seconds

    def close_candles(self, count: int = 1) -> None:
        """The source lists ``count`` more candles; it is three seconds into the newest one."""
        self.source.cursor += count
        ahead = opens_at(self.source.cursor - 1) + 3 - self.clock.now
        assert ahead > 0, "the clock is already past that candle"
        self.pass_seconds(ahead)

    def walk_to(self, cursor: int) -> None:
        """One cycle per candle until the source's cursor is ``cursor``."""
        while self.source.cursor < cursor:
            self.close_candles()
            self.poll()

    def until(self, wanted: Callable[[dict[str, Any]], bool], limit: int = 40) -> dict[str, Any]:
        """One cycle per candle until ``wanted(snapshot)`` holds; that snapshot."""
        for _ in range(limit):
            self.close_candles()
            self.poll()
            snap = self.feed.snapshot()
            if wanted(snap):
                return snap
        raise AssertionError(f"not reached within {limit} candles")

    def until_signal(self) -> dict[str, Any]:
        """One cycle per candle until the feed announces a new signal; that snapshot."""
        last = newest_signal(self.feed.snapshot())
        return self.until(lambda snap: newest_signal(snap) != last)

    def signal_emails(self) -> list[tuple[str, str]]:
        return [mail for mail in self.notifier.sent if not mail[0].startswith(ALERT_SUBJECT)]


def make_settings(**values: Any) -> Settings:
    base = {
        "symbol": "XAUUSD",
        "signal_timeframe": "M15",
        "poll_seconds": 2.0,
        "history_candles": HISTORY,
        "stale_after_seconds": 60,
        "alert_after_seconds": 300,
        "display_utc_offset_hours": 8,
    }
    return Settings(_env_file=None, **{**base, **values})


@pytest.fixture(autouse=True)
def no_exported_settings(monkeypatch):
    # Settings built here must not pick up variables exported in the shell that runs the tests.
    for name in [name for name in os.environ if name.upper().startswith("TRADEPILOT_")]:
        monkeypatch.delenv(name)


@pytest.fixture
def make_world(monkeypatch):
    feeds: list[LiveFeed] = []

    def make(
        cursor: int = 1400,
        *,
        candles: pd.DataFrame = UP,
        notifier: notify.Notifier | None = None,
        replay: int = REPLAY,
        closed_hours: Callable[[float], bool] = lambda t: False,
        executor: Any = None,
        **settings: Any,
    ) -> World:
        monkeypatch.setattr(live, "REPLAY", replay)
        clock = FakeClock(opens_at(cursor - 1) + 3)
        pc = FakeClock(clock.now + PC_AHEAD)
        timer = FakeClock(5000.0)
        source = FakeSource(candles, cursor, clock)
        notifier = notify.MemoryNotifier() if notifier is None else notifier
        feed = LiveFeed(
            source, make_settings(**settings), notifier, clock=pc, timer=timer, closed_hours=closed_hours,
            executor=executor,
        )  # fmt: skip
        feeds.append(feed)
        return World(feed, source, clock, pc, timer, notifier)

    yield make
    for feed in feeds:
        feed.close()


def strict_json(value: Any) -> Any:
    """Through JSON and back with NaN and Infinity refused, as a browser's JSON.parse refuses them."""
    return json.loads(json.dumps(value, allow_nan=False))


def codes(snap: dict[str, Any]) -> list[str]:
    return [alert["code"] for alert in snap["alerts"]]


def log_times(snap: dict[str, Any]) -> list[int]:
    return [row["time"] for row in snap["log"]]


def newest_signal(snap: dict[str, Any]) -> int:
    return max((alert["id"] for alert in snap["alerts"] if alert["code"] in SIGNAL_CODES), default=0)


def alert_subject(code: str, **values: object) -> str:
    return ALERT_SUBJECT + notify.alert_text(code, **values)


def bar(row: Any) -> dict[str, float | int]:
    return {
        "time": int(row.time), "open": row.open, "high": row.high, "low": row.low, "close": row.close,
        "closed": bool(row.closed),
    }  # fmt: skip


def hour_bar(frame: pd.DataFrame, start: int) -> dict[str, float | int]:
    """The H1 candle opening at ``start``, built by hand from the M15 candles inside it."""
    inside = frame[(frame["time"] >= start) & (frame["time"] < start + 3600)]
    return {
        "time": start,
        "open": float(inside["open"].iloc[0]),
        "high": float(inside["high"].max()),
        "low": float(inside["low"].min()),
        "close": float(inside["close"].iloc[-1]),
        "closed": len(inside) == 4 and bool(inside["closed"].all()),  # all four quarters there and closed
    }


def first_touch(row: dict[str, Any], closed: pd.DataFrame, now: float) -> dict[str, Any]:
    """The outcome docs/WEB.md promises for a log row, worked out candle by candle."""
    if now < row["expires_at"]:
        return {"kind": "valid"}
    buy = row["side"] == "BUY"
    for candle in closed[closed["time"] >= row["time"]].itertuples():
        stopped = candle.low <= row["sl"] if buy else candle.high >= row["sl"]
        reached = candle.high >= row["tp"] if buy else candle.low <= row["tp"]
        if stopped:  # one candle touching both counts as the stop
            return {"kind": "sl", "time": int(candle.time)}
        if reached:
            return {"kind": "tp", "time": int(candle.time)}
    return {"kind": "none"}


_SIGNAL_CURSORS: list[int] = []


def signal_cursors(make_world) -> list[int]:
    """Cursors at which a default world, polled once per candle, announces its next signals.

    Found once with a throwaway twin, so that a test can stop one candle short
    of a signal, or jump over one, without a candle number picked by hand.
    """
    if not _SIGNAL_CURSORS:
        twin = make_world()
        twin.poll()
        for _ in range(8):
            before = newest_signal(twin.feed.snapshot())
            twin.close_candles()
            twin.poll()
            if newest_signal(twin.feed.snapshot()) != before:
                _SIGNAL_CURSORS.append(twin.source.cursor)
        # The tests walk to four candles before the last of them, and use the candle after it as one without a signal.
        assert len(_SIGNAL_CURSORS) >= 2 and 1404 <= _SIGNAL_CURSORS[-1] < twin.source.cursor, _SIGNAL_CURSORS
    return _SIGNAL_CURSORS


# --- starting up -------------------------------------------------------------------


def test_before_the_first_answer_the_feed_is_starting_and_shows_no_price(make_world):
    world = make_world()
    snap = world.feed.snapshot()
    assert snap["feed"] == "starting" and snap["card"] == {"kind": "starting"}
    assert (snap["price"], snap["bid"], snap["ask"]) == (None, None, None)
    assert snap["bars"] == {} and snap["log"] == [] and snap["alerts"] == [] and snap["reasons"] is None
    assert snap["today"] == {"total": 0, "buy": 0, "sell": 0}
    assert world.feed.candles("M15", 10) == []
    assert world.source.calls == [] and world.notifier.sent == []
    assert strict_json(snap) == snap


def test_the_first_load_replays_history_and_announces_nothing_from_it(make_world):
    world = make_world(1500, replay=REAL_REPLAY, history_candles=1500)
    assert world.poll() == 2.0
    snap = world.feed.snapshot()

    # The newest decision is itself a sent signal that is still valid: the most tempting one to announce.
    newest_close = opens_at(world.source.cursor - 1)
    assert (snap["card"]["kind"], snap["card"]["state"], snap["card"]["time"]) == ("signal", "valid", newest_close)
    # The last 192 closed candles (48 hours) are in the log, nothing older.
    times = [row["time"] for row in snap["log"]]
    window_start = opens_at(world.source.cursor - REAL_REPLAY)
    assert len(times) > 30 and window_start <= min(times) < window_start + 6 * 3600
    assert max(times) == newest_close
    # Exactly the "started" alert, and its email is the only one.
    assert codes(snap) == ["started"]
    assert [subject for subject, _ in world.notifier.sent] == [alert_subject("started")]
    assert world.signal_emails() == []
    assert world.source.calls == [1500]  # one request for the whole history


def test_polling_on_after_the_first_load_still_announces_nothing_old(make_world):
    world = make_world()
    world.poll()
    for _ in range(3):
        world.pass_seconds(2)
        world.poll()
    assert codes(world.feed.snapshot()) == ["started"]
    assert world.signal_emails() == []


# --- a normal cycle -------------------------------------------------------------------


def test_a_normal_cycle_costs_three_candles_and_one_quote_and_waits_the_poll_interval(make_world):
    world = make_world()
    world.poll()
    world.pass_seconds(2)
    assert world.poll() == 2.0
    assert world.source.calls == [HISTORY, live.TAIL] and live.TAIL == 3
    assert world.source.quotes == 2


def test_the_poll_interval_is_never_shorter_than_half_a_second(make_world):
    world = make_world(poll_seconds=0.01)
    assert world.poll() == 0.5


def test_a_newly_closed_candle_with_a_signal_gives_one_alert_and_one_email(make_world):
    world = make_world()
    world.poll()
    snap = world.until_signal()

    closed = world.source.candles.iloc[world.source.cursor - 2]  # the candle that has just closed
    row, card = snap["log"][0], snap["card"]
    assert card == {"kind": "signal", "state": "valid", **{key: row[key] for key in ROW_KEYS}}
    assert row["time"] == int(closed["time"]) + M15 and row["entry"] == closed["close"]
    assert row["side"] == "BUY" and row["sl"] < row["zone_low"] < row["entry"] < row["zone_high"] < row["tp"]
    assert row["sl_distance"] == pytest.approx(row["entry"] - row["sl"]) and row["sl_distance"] > 0
    assert row["tp_distance"] == pytest.approx(row["tp"] - row["entry"]) and row["tp_distance"] > 0
    assert row["expires_at"] == row["time"] + 2 * M15 and row["valid_seconds"] == 1800
    assert row["grade"] == "B" and row["outcome"] == {"kind": "valid"}

    alert = snap["alerts"][0]
    assert (alert["code"], alert["level"]) == ("signal_emailed", "ok")
    assert "BUY" in alert["text"] and alert["time"] == world.clock.now  # stamped by the source's clock
    assert len([code for code in codes(snap) if code in SIGNAL_CODES]) == 1

    ((subject, body),) = world.signal_emails()
    assert "XAUUSD" in subject and " BUY " in subject and f"{row['entry']:.2f}" in subject
    stop, target = (next(text for text in body.split("\n") if text.startswith(name)) for name in ("SL:", "TP:"))
    assert f"−{row['sl_distance']:.2f}" in stop and f"{row['sl']:.2f}" in stop
    assert f"+{row['tp_distance']:.2f}" in target and f"{row['tp']:.2f}" in target
    assert notify.clock_text(row["expires_at"], 8) in body and "fake" in body


def test_a_sell_signal_email_has_the_signs_the_other_way_round(make_world):
    world = make_world(candles=DOWN)
    world.poll()
    snap = world.until_signal()

    row = snap["log"][0]
    assert row["side"] == "SELL" and row["tp"] < row["zone_low"] < row["entry"] < row["zone_high"] < row["sl"]
    assert row["sl_distance"] == pytest.approx(row["sl"] - row["entry"])
    assert row["tp_distance"] == pytest.approx(row["entry"] - row["tp"])
    ((subject, body),) = world.signal_emails()
    assert " SELL " in subject and "BUY" not in subject
    stop, target = (next(text for text in body.split("\n") if text.startswith(name)) for name in ("SL:", "TP:"))
    assert f"+{row['sl_distance']:.2f}" in stop and f"−{row['tp_distance']:.2f}" in target
    assert snap["today"]["sell"] >= 1 and snap["today"]["buy"] == 0
    assert "SELL" in snap["alerts"][0]["text"]


def test_polling_again_without_a_new_candle_decides_nothing(make_world, monkeypatch):
    decided: list[int] = []
    real_replay = fl.replay

    def counting_replay(candles, timeframe, count, **options):
        decided.append(count)
        return real_replay(candles, timeframe, count, **options)

    monkeypatch.setattr(fl, "replay", counting_replay)
    world = make_world()
    world.poll()
    world.until_signal()
    judged = list(decided)
    assert judged[0] == REPLAY and set(judged[1:]) == {1}  # the history once, then one candle at a time
    before = world.feed.snapshot()

    for _ in range(5):
        world.pass_seconds(2)
        world.poll()
    after = world.feed.snapshot()
    assert decided == judged  # the rule did not run again
    assert after["v"] == before["v"] and after["log"] == before["log"] and after["alerts"] == before["alerts"]
    assert len(world.signal_emails()) == 1
    assert after["now"] == before["now"] + 10  # only the clock and the price moved on


def test_over_a_wave_every_sent_signal_is_emailed_exactly_once_and_nothing_else_is(make_world):
    world = make_world()
    world.poll()
    loaded_at = world.clock.now
    for _ in range(20):
        world.close_candles()
        world.poll()

    snap = world.feed.snapshot()
    new_rows = [row for row in reversed(snap["log"]) if row["time"] > loaded_at]  # oldest first
    emails = world.signal_emails()
    assert len(emails) == len(new_rows) >= 4
    for (subject, _), row in zip(emails, new_rows):
        assert f"{row['entry']:.2f}" in subject
    assert len({row["time"] for row in new_rows}) == len(new_rows)
    for earlier, later in zip(new_rows, new_rows[1:]):
        assert later["time"] >= earlier["expires_at"]  # never two valid signals of one side at once
    assert [row["run"] for row in new_rows][:3] == [new_rows[0]["run"] + i for i in range(3)]


def test_without_email_a_signal_is_still_announced_in_the_page(make_world):
    world = make_world(notifier=notify.NullNotifier())
    world.poll()
    snap = world.until_signal()
    assert snap["email"] is False
    assert snap["alerts"][0]["code"] == "signal" and "signal_emailed" not in codes(snap)
    assert snap["card"]["kind"] == "signal"


def test_two_candles_closed_between_cycles_are_both_decided_without_a_reload(make_world):
    target = signal_cursors(make_world)[-1]
    world = make_world()
    world.poll()
    world.walk_to(target - 2)
    emails = len(world.signal_emails())

    world.close_candles(2)
    world.poll()
    snap = world.feed.snapshot()
    assert world.source.calls[-1] == live.TAIL and "reloaded" not in codes(snap)
    assert snap["log"][0]["time"] == opens_at(target - 1)  # the newer of the two candles carried the signal
    assert len(world.signal_emails()) == emails + 1


def test_the_same_candle_is_never_announced_twice(make_world):
    world = make_world()
    world.poll()
    announced = world.until_signal()["log"][0]["time"]

    world.source.cursor -= 1  # the source answers with older data for a moment
    world.pass_seconds(2)
    world.poll()
    assert codes(world.feed.snapshot())[0] == "reloaded"
    world.source.cursor += 1  # and then lists the same candle as closed again
    world.pass_seconds(2)
    world.poll()

    snap = world.feed.snapshot()
    assert snap["log"][0]["time"] == announced  # it is decided and shown again
    assert len(world.signal_emails()) == 1  # and not sent again
    assert len([code for code in codes(snap) if code in SIGNAL_CODES]) == 1


# --- late, down and back -------------------------------------------------------------------


def test_a_failed_fetch_keeps_the_last_real_price_and_the_feed_goes_late_then_down_by_the_timer_only(make_world):
    world = make_world()
    world.poll()
    good = world.feed.tick()
    quote_time, start = world.clock.now, world.timer.now
    world.source.error = DataSourceError("no route to host")

    # Neither the number of failures nor a jumping wall clock makes a feed late: only the stopwatch does.
    for _ in range(25):
        world.poll()
    world.pc.now += 86400
    world.poll()
    assert world.feed.tick()["feed"] == "ok"

    for seconds, status in ((9.5, "ok"), (10, "late"), (59.5, "late")):
        world.timer.now = start + seconds
        world.poll()
        tick = world.feed.tick()
        assert (tick["feed"], tick["age"]) == (status, seconds)
        assert (tick["price"], tick["bid"], tick["ask"]) == (good["price"], good["bid"], good["ask"])
        assert tick["card"] == good["card"] and tick["bars"] == good["bars"]  # still shown, with its age

    world.timer.now = start + 60
    world.poll()
    tick = world.feed.tick()
    assert tick["feed"] == "down" and tick["bars"] == {}
    assert (tick["price"], tick["bid"], tick["ask"]) == (good["price"], good["bid"], good["ask"])
    assert tick["card"] == {"kind": "feed_down", "last_price": good["price"], "last_time": quote_time, "age": 60.0, "emailed": False}
    assert tick["now"] == quote_time + 60  # the source's time carried on by the stopwatch, not the PC's
    assert strict_json(tick) == tick

    for _ in range(3):  # and it is reported once, not on every cycle
        world.timer.now += 5
        world.poll()
    snap = world.feed.snapshot()
    assert codes(snap) == ["feed_down", "started"]
    assert snap["alerts"][0]["level"] == "down" and "60" in snap["alerts"][0]["text"]


def test_the_feed_down_email_waits_for_alert_after_seconds_and_goes_out_once(make_world):
    world = make_world()
    world.poll()
    start = world.timer.now
    world.source.error = DataSourceError("no route to host")
    sent = world.notifier.sent

    for seconds in (60, 120, 299.5):
        world.timer.now = start + seconds
        world.poll()
    assert world.feed.tick()["feed"] == "down" and len(sent) == 1  # only the "started" email so far

    for seconds in (300, 305, 600):
        world.timer.now = start + seconds
        world.poll()
    assert [subject for subject, _ in sent] == [alert_subject("started"), alert_subject("feed_down", seconds=300)]
    assert codes(world.feed.snapshot()) == ["feed_down", "started"]


@pytest.mark.parametrize(("outage", "emailed"), [(100, False), (400, True)])
def test_recovery_is_announced_and_emailed_only_if_the_down_email_went_out(make_world, outage, emailed):
    world = make_world()
    world.poll()
    world.source.error = DataSourceError("no route to host")
    world.pass_seconds(outage)
    world.poll()
    assert world.feed.tick()["feed"] == "down"

    world.source.error = None
    world.pass_seconds(2)
    assert world.poll() == 2.0
    snap = world.feed.snapshot()
    assert snap["feed"] == "ok" and snap["age"] == 0 and snap["bars"] and snap["card"]["kind"] != "feed_down"
    assert codes(snap) == ["feed_back", "feed_down", "started"] and snap["alerts"][0]["level"] == "ok"
    subjects = [subject for subject, _ in world.notifier.sent]
    if emailed:
        assert subjects == [
            alert_subject("started"), alert_subject("feed_down", seconds=outage), alert_subject("feed_back"),
        ]  # fmt: skip
    else:
        assert subjects == [alert_subject("started")]


def test_a_second_outage_is_announced_again(make_world):
    world = make_world()
    world.poll()
    for _ in range(2):
        world.source.error = DataSourceError("no route to host")
        world.pass_seconds(400)
        world.poll()
        world.source.error = None
        world.pass_seconds(2)
        world.poll()
    assert codes(world.feed.snapshot()) == ["feed_back", "feed_down", "feed_back", "feed_down", "started"]
    assert len(world.notifier.sent) == 5  # started, and down and back for each outage


def test_a_source_that_keeps_repeating_the_identical_answer_goes_down_too(make_world):
    world = make_world()
    world.poll()
    start = world.timer.now
    for seconds, status in ((9.5, "ok"), (10, "late"), (59.5, "late"), (60, "down")):
        world.timer.now = start + seconds  # the stopwatch runs; the source's clock, price and candles stand still
        assert world.poll() == 2.0
        assert world.feed.tick()["feed"] == status
    assert codes(world.feed.snapshot())[0] == "feed_down"

    world.source.price = world.feed.tick()["price"] + 0.01  # a number moved, but the price's own time did not
    world.poll()
    assert world.feed.tick()["feed"] == "down"  # a price stamped a minute ago is still an old price

    world.clock.now += 60  # the source's clock runs again: now the answer is fresh data
    world.poll()
    snap = world.feed.snapshot()
    assert snap["feed"] == "ok" and codes(snap)[0] == "feed_back"


def test_the_fetch_time_of_an_untimed_quote_does_not_count_as_fresh_data(make_world):
    world = make_world()
    world.source.timed = False  # like a source whose price carries no time of its own
    world.poll()
    for _ in range(3):
        world.pass_seconds(20)  # each quote is "timed" 20 s later, but not one price has changed
        world.poll()
    assert world.feed.tick()["feed"] == "down"


def test_a_frozen_source_that_reports_no_volume_goes_down_too(make_world):
    world = make_world(candles=UP.assign(volume=np.nan))
    world.poll()
    world.timer.now += 120  # two minutes of the identical answer
    world.poll()
    assert world.feed.tick()["feed"] == "down"


def test_a_source_that_never_answers_goes_from_starting_to_down_without_a_price(make_world):
    world = make_world()
    world.source.error = DataSourceError("name resolution failed")
    start = world.timer.now
    for seconds, status in ((0, "starting"), (59.5, "starting"), (60, "down")):
        world.timer.now = start + seconds
        world.poll()
        assert world.feed.tick()["feed"] == status
    tick = world.feed.tick()
    assert tick["card"] == {"kind": "feed_down", "last_price": None, "last_time": None, "age": 60.0, "emailed": False}
    assert (tick["price"], tick["bid"], tick["ask"], tick["bars"]) == (None, None, None, {})
    assert tick["now"] == world.pc.now  # no time from the source yet: the PC clock is all there is
    assert strict_json(world.feed.snapshot())["log"] == [] and world.notifier.sent == []

    world.source.error = None
    world.poll()
    snap = world.feed.snapshot()
    assert snap["feed"] == "ok" and codes(snap) == ["started", "feed_back", "feed_down"]


def test_no_signal_is_decided_while_the_feed_is_down_and_a_still_valid_one_is_announced_on_recovery(make_world):
    target = signal_cursors(make_world)[-1]
    world = make_world()
    world.poll()
    world.walk_to(target - 1)  # the candle now forming will carry a signal when it closes
    world.pass_seconds(880)
    world.poll()
    before = world.feed.snapshot()
    emails = len(world.signal_emails())

    world.source.error = DataSourceError("connection reset")
    world.pass_seconds(20)
    world.source.cursor += 1  # the candle closes while the source cannot be reached
    world.pass_seconds(60)
    world.poll()
    down = world.feed.snapshot()
    assert down["feed"] == "down" and down["card"]["kind"] == "feed_down"
    assert log_times(down) == log_times(before) and down["reasons"] == before["reasons"]
    assert len(world.signal_emails()) == emails and newest_signal(down) == newest_signal(before)

    world.source.error = None
    world.pass_seconds(2)
    world.poll()
    back = world.feed.snapshot()
    assert back["log"][0]["time"] == opens_at(target - 1) and back["card"]["kind"] == "signal"
    assert codes(back)[:3] == ["signal_emailed", "feed_back", "feed_down"]
    assert len(world.signal_emails()) == emails + 1


def test_a_cycle_without_a_quote_decides_nothing_until_both_answers_are_there(make_world):
    target = signal_cursors(make_world)[-1]
    world = make_world()
    world.poll()
    world.walk_to(target - 1)
    before = world.feed.snapshot()
    emails = len(world.signal_emails())

    world.source.error, world.source.error_in = DataSourceError("quote timed out"), ("quote",)
    world.close_candles()
    assert world.poll() == 2.0
    half = world.feed.snapshot()
    assert log_times(half) == log_times(before) and half["reasons"] == before["reasons"]
    assert half["price"] == before["price"]
    assert len(world.signal_emails()) == emails

    world.source.error = None
    world.pass_seconds(2)
    world.poll()
    assert world.feed.snapshot()["log"][0]["time"] == opens_at(target - 1)
    assert len(world.signal_emails()) == emails + 1


def test_an_error_in_the_candles_alone_fails_the_cycle_too(make_world):
    world = make_world()
    world.poll()
    price = world.feed.tick()["price"]
    world.source.error, world.source.error_in = DataSourceError("klines timed out"), ("candles",)
    world.source.price = price + 5  # the quote that did arrive is not shown without its candles
    world.pass_seconds(2)
    assert world.poll() == 2.0
    assert world.feed.tick()["price"] == price


def test_a_candle_request_that_fails_at_once_is_still_handled_as_a_failed_fetch(make_world):
    world = make_world()
    world.poll()
    world.source.error = DataSourceError("refused at once", status=429, retry_after=120.0)
    world.source.fails_at_once = True
    interval = sys.getswitchinterval()
    sys.setswitchinterval(10.0)  # keeps the idle quote worker waiting, so the order is the same on every run
    try:
        wait = world.poll()
    finally:
        sys.setswitchinterval(interval)
    assert wait == 120.0


# --- how long to wait after a failure ---------------------------------------------------------


def test_failed_cycles_back_off_and_the_first_good_one_resets_the_wait(make_world):
    world = make_world()
    world.poll()
    world.source.error = DataSourceError("timed out")
    assert [world.poll() for _ in range(6)] == [2.0, 4.0, 8.0, 15.0, 15.0, 15.0]
    world.source.error = None
    world.pass_seconds(1)
    assert world.poll() == 2.0
    world.source.error = DataSourceError("HTTP 500", status=500)  # not a refusal: the ordinary back-off
    assert [world.poll() for _ in range(2)] == [2.0, 4.0]


@pytest.mark.parametrize(
    ("status", "retry_after", "expected"),
    [
        (429, 120.0, 120.0),
        (429, 1.0, 2.0),  # never sooner than the normal interval
        (418, 99999.0, 3600.0),  # and never longer than an hour
        (429, None, 60.0),
        (418, None, 60.0),
        (429, 0.0, 60.0),
        (429, -5.0, 60.0),
    ],
)
def test_a_refusal_sets_the_wait_from_retry_after_or_starts_at_a_minute(make_world, status, retry_after, expected):
    world = make_world()
    world.source.error = DataSourceError(f"HTTP {status}", status=status, retry_after=retry_after)
    assert world.poll() == expected


def test_refusals_without_retry_after_double_up_to_fifteen_minutes_and_a_good_cycle_resets_them(make_world):
    world = make_world()
    world.poll()
    world.source.error = DataSourceError("HTTP 429", status=429)
    assert [world.poll() for _ in range(6)] == [60.0, 120.0, 240.0, 480.0, 900.0, 900.0]
    world.source.error = None
    world.pass_seconds(1)
    assert world.poll() == 2.0
    world.source.error = DataSourceError("HTTP 429", status=429)
    assert world.poll() == 60.0


# --- whose clock ------------------------------------------------------------------------------


def test_now_follows_the_sources_clock_not_the_pcs(make_world):
    world = make_world()
    assert world.feed.now() == world.pc.now  # nothing from the source yet
    world.poll()
    assert world.feed.now() == world.clock.now == world.feed.tick()["now"]
    assert world.feed.now() != world.pc.now

    world.pc.now += 86400  # the PC clock jumps a day
    world.timer.now += 5  # five seconds pass on the stopwatch
    assert world.feed.now() == world.clock.now + 5 == world.feed.tick()["now"]


def test_a_quote_without_a_time_of_its_own_leaves_only_the_pc_clock(make_world):
    world = make_world()
    world.source.timed = False
    world.poll()
    assert world.feed.now() == world.pc.now == world.feed.tick()["now"]


def test_the_started_alert_is_stamped_by_the_sources_clock_like_every_other(make_world):
    world = make_world()
    world.poll()
    (started,) = world.feed.snapshot()["alerts"]
    ((_, body),) = world.notifier.sent
    assert started["time"] == world.clock.now
    assert notify.clock_text(world.clock.now, 8) in body


# --- market hours and the H1 filter ----------------------------------------------------------


def test_closed_hours_block_a_signal_with_market_closed(make_world):
    target = signal_cursors(make_world)[0]  # with the market open, a signal is sent here
    closes_at = opens_at(target - 1)
    world = make_world(closed_hours=lambda t: t >= closes_at)
    world.poll()
    world.walk_to(target)

    snap = world.feed.snapshot()
    card = snap["card"]
    assert (card["kind"], card["code"], card["side"]) == ("blocked", "MARKET_CLOSED", "BUY")
    assert (card["candle_close"], card["next_check"]) == (closes_at, closes_at + M15)
    assert card["score"] >= 3 and snap["reasons"]["blocked"] == "MARKET_CLOSED"
    assert snap["market_closed"] is True
    assert all(row["time"] < closes_at for row in snap["log"])
    assert world.signal_emails() == [] and newest_signal(snap) == 0


def test_while_the_market_is_closed_nothing_is_sent_not_even_from_history(make_world):
    world = make_world(closed_hours=lambda t: True)
    world.poll()
    for _ in range(3):
        world.close_candles()
        world.poll()
    snap = world.feed.snapshot()
    assert snap["log"] == [] and snap["today"]["total"] == 0 and snap["market_closed"] is True
    assert snap["card"]["kind"] == "blocked" and snap["card"]["code"] == "MARKET_CLOSED"
    assert codes(snap) == ["started"] and world.signal_emails() == []


def test_a_sell_against_the_h1_trend_is_blocked_with_htf_conflict_and_not_sent(make_world):
    world = make_world(1440, candles=TURN)
    world.poll()
    loaded_at = world.clock.now
    snap = world.until(lambda s: s["card"]["kind"] == "blocked")

    card = snap["card"]
    assert (card["code"], card["side"]) == ("HTF_CONFLICT", "SELL")
    assert card["score"] <= -3 and card["h1_score"] > -1  # M15 says sell, H1 does not agree
    assert snap["reasons"]["blocked"] == "HTF_CONFLICT" and snap["reasons"]["h1_score"] == card["h1_score"]
    assert card["candle_close"] == opens_at(world.source.cursor - 1)
    # The blocked candle is not in the log, and no email went out beyond those of the signals that were sent.
    sent_since_load = [row for row in snap["log"] if row["time"] > loaded_at]
    assert all(row["time"] < card["candle_close"] for row in snap["log"])
    assert len(world.signal_emails()) == len(sent_since_load)


# --- the card ------------------------------------------------------------------------------------


def test_a_hold_card_names_the_candle_and_the_next_check(make_world):
    world = make_world(candles=DOWN)
    world.poll()
    snap = world.feed.snapshot()
    close = opens_at(world.source.cursor - 1)
    assert snap["card"] == {
        "kind": "hold",
        "candle_close": close,
        "next_check": close + M15,
        "score": snap["reasons"]["score"],
        "side": "HOLD",
        "h1_score": snap["reasons"]["h1_score"],
    }
    assert -3 < snap["card"]["score"] < 3 and snap["reasons"]["blocked"] is None


def test_the_signal_card_is_valid_inside_the_entry_zone_and_outside_zone_beyond_it(make_world):
    world = make_world()
    world.poll()
    card = world.until_signal()["card"]
    assert card["state"] == "valid"
    for price, state in (
        (card["zone_high"], "valid"),  # the edge counts as inside
        (card["zone_high"] + 0.01, "outside_zone"),
        (card["zone_low"], "valid"),
        (card["zone_low"] - 0.01, "outside_zone"),
        (card["entry"], "valid"),
    ):
        world.source.price = price
        world.pass_seconds(1)
        world.poll()
        tick = world.feed.tick()
        assert tick["price"] == price
        assert tick["card"] == {**card, "state": state}


def test_the_card_turns_expired_when_the_signal_runs_out_and_no_new_candle_came(make_world):
    # Thirty minutes without a new candle is a broken source (see the half-frozen tests below). Here the
    # feed is told to bear it, so that only the clock can end the signal.
    world = make_world(stale_after_seconds=3600)
    world.poll()
    card = world.until_signal()["card"]

    world.pass_seconds(card["expires_at"] - world.clock.now - 1)
    world.poll()
    snap = world.feed.snapshot()
    assert snap["card"] == card and snap["log"][0]["outcome"] == {"kind": "valid"}  # valid up to the last second
    world.pass_seconds(1)
    world.poll()
    snap = world.feed.snapshot()
    assert snap["card"] == {
        "kind": "expired",
        "candle_close": card["time"],
        "next_check": card["time"] + M15,
        "score": card["score"],
        "side": "BUY",
        "h1_score": card["h1_score"],
    }
    assert snap["log"][0]["outcome"] == {"kind": "none"}  # no closed candle after it yet
    assert snap["feed"] == "late"  # the candles are overdue; with the usual limit the feed would be down


# --- gaps -----------------------------------------------------------------------------------------


def test_a_gap_reloads_the_history_and_only_the_newest_signal_is_announced(make_world):
    target = signal_cursors(make_world)[-1]
    world = make_world()
    world.poll()
    world.walk_to(target - 4)
    before = world.feed.snapshot()
    emails = len(world.signal_emails())

    world.close_candles(4)  # four candles appear at once: the three newest do not connect
    world.poll()
    snap = world.feed.snapshot()
    assert world.source.calls[-2:] == [live.TAIL, HISTORY]  # the tail was tried, then everything asked again
    assert codes(snap)[:2] == ["signal_emailed", "reloaded"] and snap["alerts"][1]["level"] == "warn"
    missed = [row for row in snap["log"] if row["time"] > before["log"][0]["time"]]
    assert len(missed) == 2 and missed[0]["time"] == opens_at(target - 1)  # two signals came in the gap
    new_emails = world.signal_emails()[emails:]
    assert len(new_emails) == 1 and f"{missed[0]['entry']:.2f}" in new_emails[0][0]  # only the newest is sent


def test_after_a_gap_a_signal_from_the_candle_before_the_newest_is_announced_while_it_is_valid(make_world):
    target = signal_cursors(make_world)[-1] + 1  # one candle after a signal: the newest candle itself sends nothing
    world = make_world()
    world.poll()
    world.walk_to(target - 4)
    before = world.feed.snapshot()
    emails = len(world.signal_emails())

    world.close_candles(4)
    world.poll()
    snap = world.feed.snapshot()
    # A signal lives for two candles: the one from the candle before the newest has 15 minutes left,
    # it is on the card, and it is announced exactly as it would be without the reload.
    assert snap["card"]["kind"] == "signal" and snap["card"]["time"] == snap["log"][0]["time"]
    assert snap["card"]["expires_at"] - world.clock.now > 800
    assert codes(snap)[:2] == ["signal_emailed", "reloaded"] and newest_signal(snap) != newest_signal(before)
    new_emails = world.signal_emails()[emails:]
    assert len(new_emails) == 1 and f"{snap['card']['entry']:.2f}" in new_emails[0][0]
    # Older signals from the gap have run out: they are in the log, and nobody is told about them now.
    assert len([row for row in snap["log"] if row["time"] > before["log"][0]["time"]]) >= 1

    world.pass_seconds(2)
    world.poll()
    assert len(world.signal_emails()) == emails + 1  # and it is not announced twice


def test_after_a_gap_a_newest_signal_that_has_already_expired_is_not_announced(make_world):
    target = signal_cursors(make_world)[-1]
    world = make_world(stale_after_seconds=3600)  # a source allowed to be this late with its candles
    world.poll()
    world.walk_to(target - 4)
    emails = len(world.signal_emails())
    before = world.feed.snapshot()

    world.close_candles(4)
    world.pass_seconds(2 * M15)  # the source's clock is already past the signal's 30 minutes
    world.poll()
    snap = world.feed.snapshot()
    assert snap["log"][0]["time"] == opens_at(target - 1) and world.clock.now >= snap["log"][0]["expires_at"]
    assert codes(snap)[0] == "reloaded" and newest_signal(snap) == newest_signal(before)
    assert len(world.signal_emails()) == emails
    assert snap["card"]["kind"] == "expired"


# --- what the page gets ------------------------------------------------------------------------------


def test_snapshot_and_tick_have_the_shape_docs_web_md_promises(make_world):
    world = make_world()
    world.poll()
    snap, tick = world.feed.snapshot(), world.feed.tick()
    assert set(tick) == TICK_KEYS and set(snap) == SNAPSHOT_KEYS
    assert {key: snap[key] for key in TICK_KEYS} == tick
    assert (snap["symbol"], snap["timeframe"]) == ("XAUUSD", "M15")
    assert snap["source"] == {"name": "fake", "symbol": "XAUTEST"}
    assert snap["email"] is True and snap["utc_offset_hours"] == 8
    assert tick["poll_seconds"] == snap["poll_seconds"] == 2.0
    # Plan: every signal is "experimental" until a backtest exists, and the news filter is not applied yet.
    assert snap["rules"] == {"status": "experimental", "filters_off": ["NEWS_WINDOW"], "market_hours": "approximate"}
    assert set(snap["card"]) == ROW_KEYS | {"kind", "state"}
    assert all(set(row) == ROW_KEYS | {"outcome"} for row in snap["log"]) and snap["log"]
    assert set(snap["reasons"]) == {"candle_close", "score", "side", "blocked", "conditions", "h1_score", "rsi", "atr"}
    assert [c["id"] for c in snap["reasons"]["conditions"]] == [
        "trend_ema200", "trend_ema20_50", "macd_vs_signal", "macd_vs_zero", "rsi_zone",
    ]  # fmt: skip
    assert sum(c["value"] for c in snap["reasons"]["conditions"]) == snap["reasons"]["score"]
    assert set(snap["alerts"][0]) == {"id", "time", "level", "code", "text"}
    assert (tick["bid"], tick["ask"]) == (tick["price"] - 0.05, tick["price"] + 0.05)


@pytest.mark.parametrize("volume", [1.0, np.nan])
def test_snapshot_and_tick_are_strict_json_in_every_state(make_world, volume):
    world = make_world(candles=UP.assign(volume=volume))  # NaN: a source that reports no volume

    def check() -> str:
        snap, tick = world.feed.snapshot(), world.feed.tick()
        assert strict_json(snap) == snap and strict_json(tick) == tick
        return snap["card"]["kind"]

    kinds = [check()]
    world.poll()
    kinds.append(check())
    world.until_signal()
    kinds.append(check())
    world.until(lambda snap: snap["card"]["kind"] == "hold")
    kinds.append(check())
    world.source.error = DataSourceError("gone")
    world.pass_seconds(60)
    world.poll()
    kinds.append(check())
    assert kinds == ["starting", "signal", "signal", "hold", "feed_down"]


def test_v_grows_exactly_when_the_snapshot_changed(make_world):
    world = make_world()
    world.poll()
    v = world.feed.tick()["v"]

    world.pass_seconds(2)
    world.poll()
    assert world.feed.tick()["v"] == v  # only the price ticked
    world.close_candles()
    world.poll()
    assert world.feed.tick()["v"] > v  # a candle closed and was decided
    v = world.feed.tick()["v"]
    world.source.error = DataSourceError("gone")
    world.timer.now += 10
    world.poll()
    assert world.feed.tick()["v"] > v  # the feed turned late
    v = world.feed.tick()["v"]
    world.poll()
    assert world.feed.tick()["v"] == v


def test_the_tick_carries_the_two_newest_candles_of_m15_and_h1(make_world):
    world = make_world(1401)
    world.poll()
    for _ in range(4):  # every position of the forming candle within its hour
        seen = world.source.seen()
        bars = world.feed.tick()["bars"]
        assert set(bars) == {"M15", "H1"}
        assert bars["M15"] == [bar(row) for row in seen.tail(2).itertuples()]
        hour = int(seen["time"].iloc[-1]) // 3600 * 3600
        assert bars["H1"] == [hour_bar(seen, hour - 3600), hour_bar(seen, hour)]
        world.close_candles()
        world.poll()


def test_candles_gives_the_newest_rows_oldest_first_and_builds_h1_from_m15(make_world):
    world = make_world(1402)
    world.poll()
    seen = world.source.seen()

    assert world.feed.candles("M15", 5) == [bar(row) for row in seen.tail(5).itertuples()]
    assert len(world.feed.candles("M15", 99999)) == HISTORY
    hour = int(seen["time"].iloc[-1]) // 3600 * 3600
    newest_hours = [hour_bar(seen, start) for start in (hour - 7200, hour - 3600, hour)]
    assert world.feed.candles("H1", 3) == newest_hours
    # Asked for more than there is: every full hour of the candles in memory, and the hour still forming.
    kept = seen.tail(HISTORY)
    per_hour = (kept["time"] // 3600 * 3600).value_counts()
    full_hours = sorted(int(start) for start, count in per_hour.items() if count == 4 or start == hour)
    everything = world.feed.candles("H1", 99999)
    assert [b["time"] for b in everything] == full_hours and len(full_hours) < len(per_hour)
    assert strict_json(everything) == everything


@pytest.mark.parametrize("timeframe", ["M5", "H4", "D1", "m15", ""])
def test_candles_refuses_a_timeframe_it_does_not_keep(make_world, timeframe):
    world = make_world()
    world.poll()
    with pytest.raises(ValueError):
        world.feed.candles(timeframe, 10)


# --- the log and today's counts -----------------------------------------------------------------------


def test_log_outcomes_are_the_first_level_the_feeds_closed_candles_reached(make_world):
    world = make_world(replay=24)  # six hours back: signals of every outcome
    world.poll()
    snap = world.feed.snapshot()
    closed = world.source.seen().iloc[:-1]

    times = [row["time"] for row in snap["log"]]
    assert times == sorted(times, reverse=True) and len(times) >= 5  # newest first
    for row in snap["log"]:
        assert row["outcome"] == first_touch(row, closed, snap["now"])
    assert {row["outcome"]["kind"] for row in snap["log"]} == {"valid", "none", "tp", "sl"}

    world.pass_seconds(M15 + 100)  # later, within the newest signal's 30 minutes: nothing has changed yet
    world.poll()
    later = world.feed.snapshot()
    assert later["log"] == snap["log"] and later["log"][0]["outcome"] == {"kind": "valid"}


def test_an_open_outcome_is_settled_once_a_later_candle_reaches_a_level(make_world):
    world = make_world()
    world.poll()
    waiting = next(row for row in world.feed.snapshot()["log"] if row["outcome"] == {"kind": "none"})

    def outcome(snap: dict[str, Any]) -> dict[str, Any]:
        return next(row["outcome"] for row in snap["log"] if row["time"] == waiting["time"])

    snap = world.until(lambda s: outcome(s)["kind"] != "none")
    assert outcome(snap) == first_touch(waiting, world.source.seen().iloc[:-1], snap["now"])
    assert outcome(snap)["kind"] in ("tp", "sl") and outcome(snap)["time"] >= waiting["time"]
    world.close_candles()
    world.poll()
    assert outcome(world.feed.snapshot()) == outcome(snap)  # settled once, it stays


def test_todays_counts_start_at_midnight_of_the_display_zone(make_world):
    # Candle 1436 opens at 20:00 UTC: 04:00 the next day in UTC+8, whose midnight was 16:00 UTC.
    world = make_world(1437, replay=100, history_candles=1500, display_utc_offset_hours=8)
    world.poll()
    snap = world.feed.snapshot()
    moment = datetime.fromtimestamp(snap["now"], timezone.utc)
    assert (moment.hour, moment.minute) == (20, 0)

    def since_midnight(zone: timezone) -> int:
        midnight = datetime.fromtimestamp(snap["now"], zone).replace(hour=0, minute=0, second=0, microsecond=0)
        return sum(row["time"] >= midnight.timestamp() for row in snap["log"])

    local, utc = since_midnight(timezone(timedelta(hours=8))), since_midnight(timezone.utc)
    assert 0 < local < utc  # the two midnights give different answers here
    assert snap["today"] == {"total": local, "buy": local, "sell": 0}


def test_todays_counts_follow_the_configured_offset(make_world):
    world = make_world(1437, replay=100, history_candles=1500, display_utc_offset_hours=0)
    world.poll()
    snap = world.feed.snapshot()
    midnight = int(snap["now"]) // 86400 * 86400
    expected = sum(row["time"] >= midnight for row in snap["log"])
    assert snap["utc_offset_hours"] == 0 and snap["today"]["total"] == expected > 5


# --- email that fails or is slow ----------------------------------------------------------------------


def test_a_failing_notifier_gives_the_email_failed_alert_and_never_an_exception_out_of_poll(make_world):
    world = make_world(notifier=notify.MemoryNotifier(fail=True))
    assert world.poll() == 2.0  # the "started" email failed; the cycle did not
    snap = world.feed.snapshot()
    assert codes(snap) == ["email_failed", "started"] and snap["alerts"][0]["level"] == "warn"

    snap = world.until_signal()
    assert codes(snap)[:2] == ["email_failed", "signal_emailed"]
    assert snap["feed"] == "ok" and snap["card"]["kind"] == "signal" and world.notifier.sent == []

    world.source.error = DataSourceError("gone")
    world.pass_seconds(400)
    assert world.poll() == 2.0  # the feed-down email fails as well, quietly
    assert codes(world.feed.snapshot())[:2] == ["email_failed", "feed_down"]


class HeldExecutor:
    """Keeps what is submitted until the test runs it, like a mail thread that is busy."""

    def __init__(self) -> None:
        self.held: list[tuple[Callable[..., Any], tuple[Any, ...]]] = []

    def submit(self, function: Callable[..., Any], *args: Any) -> None:
        self.held.append((function, args))

    def run_all(self) -> None:
        while self.held:
            function, args = self.held.pop(0)
            function(*args)


def test_with_an_executor_a_cycle_hands_its_email_over_and_does_not_wait_for_the_mail_server(make_world):
    executor = HeldExecutor()
    world = make_world(executor=executor)
    world.poll()
    snap = world.until_signal()
    assert world.notifier.sent == [] and len(executor.held) == 2  # "started" and the signal are queued
    assert codes(snap)[0] == "signal_emailed"

    executor.run_all()
    assert [subject.startswith(ALERT_SUBJECT) for subject, _ in world.notifier.sent] == [True, False]


def test_an_email_that_fails_later_on_the_executor_still_raises_the_alert(make_world):
    executor = HeldExecutor()
    world = make_world(executor=executor, notifier=notify.MemoryNotifier(fail=True))
    world.poll()
    assert codes(world.feed.snapshot()) == ["started"]
    executor.run_all()
    assert codes(world.feed.snapshot()) == ["email_failed", "started"]


def test_the_test_email_reports_whether_it_went_out(make_world):
    working = make_world()
    assert working.feed.send_test_email() is True
    assert working.notifier.sent[-1][0] == alert_subject("email_test")
    assert codes(working.feed.snapshot()) == ["email_test"]

    failing = make_world(notifier=notify.MemoryNotifier(fail=True))
    assert failing.feed.send_test_email() is False
    assert codes(failing.feed.snapshot()) == ["email_failed"]

    without = make_world(notifier=notify.NullNotifier())
    assert without.feed.send_test_email() is False and without.feed.snapshot()["alerts"] == []


# --- health and the loop ---------------------------------------------------------------------------------


class FakeStop:
    """Stands in for the loop's stop event: never sleeps, ends the loop after a number of cycles."""

    def __init__(self, cycles: int, between: Callable[[int], None]) -> None:
        self.cycles, self.between = cycles, between
        self.waits: list[float] = []

    def is_set(self) -> bool:
        return len(self.waits) >= self.cycles

    def wait(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.between(len(self.waits))


def test_health_says_not_started_until_the_loop_runs(make_world):
    world = make_world()
    world.poll()
    assert world.feed.health() == {
        "feed": "not_started", "worker": "not_running", "source": "fake", "last_candle_age": None,
    }  # fmt: skip


def test_the_loop_reports_health_and_counts_a_cycles_own_duration_into_the_interval(make_world):
    world = make_world()
    world.source.on_candles = lambda: setattr(world.timer, "now", world.timer.now + 0.5)  # each cycle takes 0.5 s
    seen = []

    def between(cycle: int) -> None:
        seen.append(world.feed.health())
        world.pass_seconds(2)

    stop = FakeStop(3, between)
    world.feed.run(stop)
    assert stop.waits == [1.5, 1.5, 1.5]
    # Three seconds into the forming candle, the last closed candle is three seconds old.
    assert seen[0] == {"feed": "ok", "worker": "running", "source": "fake", "last_candle_age": 3.0}
    assert [health["last_candle_age"] for health in seen] == [3.0, 5.0, 7.0]
    assert world.feed.health()["worker"] == "not_running" and world.source.quotes == 3


def test_the_loop_outlives_an_unexpected_error_and_never_spins(make_world, caplog):
    world = make_world()
    beats: list[int] = []
    beat = world.feed.beat
    world.feed.beat = lambda: (beats.append(len(stop.waits)), beat())

    def between(wait: int) -> None:
        world.pass_seconds(2)
        world.source.error = RuntimeError("a bug in the source") if wait == 1 else None
        # The cycle after the fourth wait is slower than the whole interval.
        world.source.on_candles = (lambda: setattr(world.timer, "now", world.timer.now + 30)) if wait == 4 else None

    stop = FakeStop(6, between)
    world.feed.run(stop)
    # Normal; 5 s after the error, waited in slices of the poll interval; the floor after the slow cycle; normal.
    assert stop.waits == pytest.approx([2.0, 2.0, 2.0, 1.0, 0.2, 2.0])
    # During the long wait open pages still hear from the server, so they do not report a lost connection.
    assert beats == [2, 3]
    assert "unexpected error" in caplog.text
    assert world.feed.tick()["feed"] == "ok" and world.feed.running is False


def test_a_page_that_asks_between_cycles_is_told_the_feeds_real_age(make_world):
    # While the loop backs off nothing polls; a page load or a reconnect must still not be told "ok".
    world = make_world()
    world.poll()
    assert world.feed.tick()["feed"] == "ok"
    world.pass_seconds(30)
    assert (world.feed.tick()["feed"], world.feed.snapshot()["feed"]) == ("late", "late")
    world.pass_seconds(40)
    tick = world.feed.tick()
    assert tick["feed"] == "down" and tick["card"]["kind"] == "feed_down" and tick["bars"] == {}


# --- pushing ticks to open pages ---------------------------------------------------------------------------


def test_every_cycle_pushes_one_tick_to_each_open_page(make_world):
    world = make_world()
    world.poll()

    async def scenario():
        loop = asyncio.get_running_loop()
        first, second = world.feed.subscribe(loop), world.feed.subscribe(loop)
        world.pass_seconds(2)
        await asyncio.to_thread(world.poll)  # the feed runs in a thread of its own
        got = [await asyncio.wait_for(queue.get(), timeout=5) for queue in (first, second)]
        world.feed.unsubscribe(second)
        world.pass_seconds(2)
        await asyncio.to_thread(world.poll)
        later = await asyncio.wait_for(first.get(), timeout=5)
        await asyncio.sleep(0)
        return got, later, first.empty(), second.empty()

    got, later, first_empty, second_empty = asyncio.run(scenario())
    assert got[0] == got[1] and set(got[0]) == TICK_KEYS
    assert later == world.feed.tick() and later["now"] == got[0]["now"] + 2
    assert strict_json(later) == later
    assert first_empty and second_empty  # one tick per cycle, none after unsubscribing


def test_a_slow_page_gets_the_newest_ticks_not_a_backlog(make_world):
    world = make_world()
    world.poll()

    async def scenario():
        queue = world.feed.subscribe(asyncio.get_running_loop())
        for _ in range(23):  # the page reads nothing while 23 cycles go by
            world.pass_seconds(2)
            world.poll()
        await asyncio.sleep(0)
        return [queue.get_nowait() for _ in range(queue.qsize())]

    ticks = asyncio.run(scenario())
    newest = world.feed.tick()
    assert len(ticks) == 20 and ticks[-1] == newest
    assert [tick["now"] for tick in ticks] == [newest["now"] - 2 * back for back in range(19, -1, -1)]


def test_close_tells_every_open_stream_to_end(make_world):
    world = make_world()

    async def scenario():
        loop = asyncio.get_running_loop()
        queues = [world.feed.subscribe(loop), world.feed.subscribe(loop)]
        world.feed.close()
        return [await asyncio.wait_for(queue.get(), timeout=5) for queue in queues]

    assert asyncio.run(scenario()) == [None, None]


def test_a_page_whose_event_loop_is_gone_does_not_break_the_cycle(make_world):
    world = make_world()
    loop = asyncio.new_event_loop()
    world.feed.subscribe(loop)
    loop.close()
    assert world.poll() == 2.0
    world.pass_seconds(2)
    assert world.poll() == 2.0
    assert world.feed.tick()["feed"] == "ok"


# --- half-frozen sources ----------------------------------------------------------------------------


def test_candles_that_stop_coming_take_the_feed_down_although_the_price_is_alive(make_world):
    world = make_world()
    world.poll()
    base = world.feed.tick()["price"]
    due = opens_at(world.source.cursor)  # when the forming candle should be followed by a newer one
    world.pass_seconds(due - world.clock.now + 59)
    world.source.price = base + 0.01
    world.poll()
    assert world.feed.tick()["feed"] == "late"  # overdue, but for less than the minute that takes the feed down

    seen = set()
    for step in range(60):  # two more minutes: the price moves on every cycle, no candle is listed
        world.pass_seconds(2)
        world.source.price = base + 0.01 * (step % 7)
        world.poll()
        seen.add(world.feed.tick()["feed"])
    snap = world.feed.snapshot()
    assert seen == {"down"} and snap["card"]["kind"] == "feed_down" and codes(snap)[0] == "feed_down"
    assert snap["bars"] == {}

    world.source.cursor += 1  # the source lists the overdue candle at last
    world.pass_seconds(2)
    world.poll()
    snap = world.feed.snapshot()
    assert snap["feed"] == "ok" and "feed_back" in codes(snap)[:2]


def test_a_price_that_is_repeated_unchanged_takes_the_feed_down_although_candles_keep_closing(make_world):
    target = signal_cursors(make_world)[-1]
    world = make_world()
    world.poll()
    world.walk_to(target - 1)  # the candle now forming carries a signal when it closes
    emails = len(world.signal_emails())
    before = world.feed.snapshot()

    frozen = world.source.fetch_quote()
    world.source.fetch_quote = lambda: (world.source.quote_left.set(), frozen)[1]  # one old answer, again and again
    start = world.clock.now
    world.pass_seconds(61)
    world.poll()
    tick = world.feed.tick()
    assert tick["feed"] == "down" and tick["card"]["kind"] == "feed_down"
    assert tick["now"] == pytest.approx(start + 61)  # the clock does not stand still with the price
    assert tick["card"]["last_time"] == frozen.time  # and the last price is dated by its own time

    world.close_candles()  # the signal candle closes while the price is a minute old and older
    world.poll()
    snap = world.feed.snapshot()
    assert snap["feed"] == "down" and len(world.signal_emails()) == emails
    assert newest_signal(snap) == newest_signal(before) and log_times(snap) == log_times(before)
    assert snap["reasons"]["blocked"] == fl.STALE_FEED  # decided, and stopped for the stale price

    del world.source.fetch_quote  # the price endpoint is alive again
    world.pass_seconds(2)
    world.poll()
    snap = world.feed.snapshot()
    assert snap["feed"] == "ok" and snap["card"] == {**snap["card"], "kind": "blocked", "code": fl.STALE_FEED}
    assert len(world.signal_emails()) == emails  # a signal held back is not sent late


def test_any_error_out_of_the_notifier_gives_the_email_failed_alert(make_world):
    class Broken(notify.MemoryNotifier):
        def send(self, subject: str, body: str) -> None:
            raise UnicodeEncodeError("ascii", "нууц", 0, 4, "ordinal not in range(128)")

    world = make_world(notifier=Broken())
    world.poll()  # the "started" email fails in a way no NotifyError describes
    assert codes(world.feed.snapshot())[0] == "email_failed"
    assert world.feed.send_test_email() is False
    assert codes(world.feed.snapshot())[:2] == ["email_failed", "email_failed"]


# --- findings of the independent review ----------------------------------------------------------------


def test_a_hole_in_the_sources_own_history_is_said_once_and_does_not_reload_every_cycle(make_world):
    holed = UP.drop(UP.index[1200]).reset_index(drop=True)  # the source itself lacks one candle, two days ago
    world = make_world(1400, candles=holed)
    world.poll()
    assert world.source.calls == [HISTORY] and codes(world.feed.snapshot())[:2] == ["history_gaps", "started"]
    for _ in range(5):
        world.pass_seconds(2)
        assert world.poll() == 2.0
    world.close_candles()
    world.poll()
    snap = world.feed.snapshot()
    # Changed on purpose with finding P4 of the second review: the hole moved from 5 hours back to
    # 2 days back. A hole younger than a day is now asked for once per closed candle (next test).
    assert world.source.calls == [HISTORY] + [live.TAIL] * 6  # tails only: the old hole is not news
    assert snap["feed"] == "ok" and codes(snap).count("history_gaps") == 1 and "reloaded" not in codes(snap)


def test_a_reload_while_the_feed_is_down_does_not_rewrite_a_signal_that_was_already_sent(make_world):
    world = make_world()
    world.poll()
    world.walk_to(1402)  # a BUY is decided, announced and emailed
    assert codes(world.feed.snapshot())[0] == "signal_emailed"
    sent = world.feed.snapshot()["log"][0]
    emails = len(world.signal_emails())
    world.close_candles()  # the next candle proposes the same side: a duplicate, nothing is sent
    world.poll()
    assert world.feed.snapshot()["reasons"]["blocked"] == fl.DUPLICATE

    world.pass_seconds(100)
    world.source.cursor -= 1  # one answer from a copy of the source that is a candle behind, and overdue
    world.poll()
    assert world.feed.snapshot()["feed"] == "down" and "reloaded" in codes(world.feed.snapshot())[:2]
    world.source.cursor += 1  # the source is right again
    world.pass_seconds(2)
    world.poll()

    snap = world.feed.snapshot()
    assert snap["feed"] == "ok" and snap["log"][0] == {**sent, "outcome": snap["log"][0]["outcome"]}
    assert snap["card"]["time"] == sent["time"] and len(world.signal_emails()) == emails
    assert [d.blocked for d in world.feed._decisions[-2:]] == [None, fl.DUPLICATE]  # as they were decided


def test_an_error_between_two_cycles_does_not_end_the_loop(make_world, caplog):
    world = make_world()
    beat = world.feed.beat
    beats: list[int] = []

    def failing_once() -> None:
        beats.append(len(beats))
        if len(beats) == 1:
            raise RuntimeError("one bad moment while only reporting")
        beat()

    world.feed.beat = failing_once

    def between(wait: int) -> None:
        world.pass_seconds(2)
        # After the first wait the source refuses: the loop backs off and beats in between.
        world.source.error = DataSourceError("slow down", status=429, retry_after=5) if wait == 1 else None

    stop = FakeStop(6, between)
    world.feed.run(stop)
    assert len(beats) >= 2 and "between cycles" in caplog.text  # the error was logged, the next beat came
    assert world.source.quotes >= 3 and world.feed.tick()["feed"] == "ok"  # and the source was asked again



# --- findings of the second review: decisions are kept, holes are known by name -----------------------


def sent_then_duplicate(world: World) -> tuple[dict[str, Any], int]:
    """A BUY decided, announced and emailed on row 1400; row 1401 after it is a DUPLICATE."""
    world.poll()
    world.walk_to(1402)
    assert codes(world.feed.snapshot())[0] == "signal_emailed"
    sent = world.feed.snapshot()["log"][0]
    assert sent["time"] == opens_at(1401)
    world.close_candles()
    world.poll()
    assert world.feed.snapshot()["reasons"]["blocked"] == fl.DUPLICATE
    return sent, len(world.signal_emails())


def decided(world: World) -> list[tuple[int | None, str | None, str | None]]:
    return [(d.result.signal_time, d.result.signal, d.blocked) for d in world.feed._decisions]


def test_one_answer_without_a_candle_does_not_rewrite_the_signal_sent_on_it(make_world):
    world = make_world()
    sent, emails = sent_then_duplicate(world)
    before = decided(world)

    world.pass_seconds(2)
    world.source.missing = {1400}  # one answer from a copy of the source that lacks the signal's candle
    world.poll()
    snap = world.feed.snapshot()
    assert world.source.calls[-2:] == [live.TAIL, HISTORY] and codes(snap)[:2] == ["history_gaps", "reloaded"]
    assert snap["feed"] == "ok" and decided(world) == before  # the reload decided nothing again

    world.source.missing = set()  # the candle is back
    world.pass_seconds(2)
    world.poll()
    snap = world.feed.snapshot()
    assert decided(world) == before and world.feed._holes == set()
    assert snap["log"][0] == {**sent, "outcome": snap["log"][0]["outcome"]} and snap["card"]["time"] == sent["time"]
    assert snap["reasons"]["blocked"] == fl.DUPLICATE and len(world.signal_emails()) == emails


def test_a_copy_that_catches_up_while_the_feed_is_down_does_not_rewrite_a_sent_signal(make_world):
    world = make_world()
    sent, emails = sent_then_duplicate(world)
    before = decided(world)

    world.pass_seconds(100)
    seen = []
    for behind in (2, 1, 0):  # two candles behind (a reload), one behind (the tail connects), right
        world.source.cursor = 1403 - behind
        world.pass_seconds(2)
        world.poll()
        seen.append((world.feed.tick()["feed"], world.source.calls[-1]))
        assert decided(world) == before
    assert seen == [("down", HISTORY), ("down", live.TAIL), ("ok", live.TAIL)]
    snap = world.feed.snapshot()
    assert snap["log"][0] == {**sent, "outcome": snap["log"][0]["outcome"]} and snap["card"]["time"] == sent["time"]
    assert len(world.signal_emails()) == emails and not hasattr(world.feed, "_stale_times")


def test_a_candle_stopped_for_a_stale_price_stays_stopped_through_later_reloads(make_world):
    target = signal_cursors(make_world)[-1]
    world = make_world()
    world.poll()
    world.walk_to(target - 1)
    emails = len(world.signal_emails())
    frozen = world.source.fetch_quote()
    world.source.fetch_quote = lambda: (world.source.quote_left.set(), frozen)[1]
    world.pass_seconds(61)
    world.poll()
    world.close_candles()  # the signal candle closes under a price that is a minute old
    world.poll()
    stopped = decided(world)[-1]
    assert stopped == (opens_at(target - 1), "BUY", fl.STALE_FEED)
    del world.source.fetch_quote

    world.close_candles(4)  # candles are missed: a whole reload, with the feed fine again
    world.poll()
    assert world.source.calls[-1] == HISTORY and world.feed.tick()["feed"] == "ok"
    assert stopped in decided(world) and decided(world)[-5] == stopped  # still stopped, four new ones after it
    assert opens_at(target - 1) not in log_times(world.feed.snapshot())
    assert len(world.signal_emails()) <= emails + 1  # at most the newest valid one of the missed candles


def test_a_hole_is_known_by_its_place_a_new_one_is_said_and_a_healed_one_forgotten(make_world):
    world = make_world()
    world.poll()
    world.close_candles()
    world.source.missing = {1399}  # the candle that just closed is not listed
    world.poll()
    assert world.source.calls[-2:] == [live.TAIL, HISTORY]
    assert world.feed._holes == {(opens_at(1398), opens_at(1400))}
    assert codes(world.feed.snapshot()).count("history_gaps") == 1

    world.source.missing = set()  # two seconds later the source has it: the tail heals the hole
    world.pass_seconds(2)
    world.poll()
    assert world.feed._holes == set() and world.source.calls[-1] == live.TAIL

    world.walk_to(1405)  # more than a minute later
    world.close_candles()
    world.source.missing = {1404}  # another hole: as many holes as were said before, but not that one
    world.poll()
    snap = world.feed.snapshot()
    assert world.feed._holes == {(opens_at(1403), opens_at(1405))}
    assert codes(snap).count("history_gaps") == 2  # said, although the count of holes is 1 as before
    for _ in range(3):
        world.pass_seconds(2)
        world.poll()
    assert codes(world.feed.snapshot()).count("history_gaps") == 2  # and said once, not on every cycle


def test_a_reload_for_missed_candles_does_not_say_an_old_hole_again(make_world):
    holed = UP.drop(UP.index[[110, 1250]]).reset_index(drop=True)
    world = make_world(1400, candles=holed)
    world.poll()
    assert codes(world.feed.snapshot()).count("history_gaps") == 1 and len(world.feed._holes) == 2
    world.close_candles(95)  # the older hole leaves the kept candles, and candles were missed
    world.poll()
    snap = world.feed.snapshot()
    assert world.source.calls[-1] == HISTORY and codes(snap).count("reloaded") == 1
    assert len(world.feed._holes) == 1 and codes(snap).count("history_gaps") == 1  # no new hole: nothing said


def test_a_young_hole_that_the_source_fills_later_is_picked_up_when_a_candle_closes(make_world):
    world = make_world()
    world.source.missing = {1395}  # not listed for an hour, so it is out of reach of the three-candle tail
    world.poll()
    assert world.feed._holes == {(opens_at(1394), opens_at(1396))}
    world.close_candles()
    world.poll()
    # A candle closed and the hole is younger than a day: one longer tail, from the candle before the hole on.
    assert world.source.calls == [HISTORY, live.TAIL, 7] and len(world.feed._holes) == 1

    world.source.missing = set()  # now the source lists it
    for _ in range(3):
        world.pass_seconds(2)
        world.poll()
    assert world.source.calls[-3:] == [live.TAIL] * 3 and len(world.feed._holes) == 1  # not on every cycle
    decisions = len(world.feed._decisions)
    world.close_candles()
    world.poll()
    snap = world.feed.snapshot()
    frame = world.feed.frame()
    assert world.source.calls[-2:] == [live.TAIL, 8] and world.source.calls.count(HISTORY) == 1
    assert world.feed._holes == set() and cd.find_gaps(frame, "M15") == [] and len(frame) == HISTORY
    assert frame["time"].tolist() == world.source.seen()["time"].tolist()[-HISTORY:]
    assert "reloaded" not in codes(snap) and snap["feed"] == "ok"
    assert len(world.feed._decisions) == decisions + 1  # the candle that closed now; the filled one is history

    world.close_candles()
    world.poll()
    assert world.source.calls[-1] == live.TAIL  # no hole left: the usual tail again


def test_of_two_young_holes_the_longer_tail_reaches_the_older_one_too(make_world):
    world = make_world()
    world.source.missing = {1390, 1395}
    world.poll()
    assert world.feed._holes == {(opens_at(1389), opens_at(1391)), (opens_at(1394), opens_at(1396))}
    world.source.missing = {1395}  # the source fills the older hole; the younger one stays
    world.close_candles()
    world.poll()
    # From the candle before the OLDER hole on: rows 1389 to 1400 are 12 candles. A tail that only
    # reached the younger hole (7 candles) would leave the filled one out for good.
    assert world.source.calls == [HISTORY, live.TAIL, 12]
    assert world.feed._holes == {(opens_at(1394), opens_at(1396))}
    assert opens_at(1390) in world.feed.frame()["time"].tolist() and len(world.feed.frame()) == HISTORY
    snap = world.feed.snapshot()
    assert "reloaded" not in codes(snap) and codes(snap).count("history_gaps") == 1 and snap["feed"] == "ok"


def test_a_hole_the_source_fills_in_part_is_picked_up_and_is_not_said_as_a_new_hole(make_world):
    # Inside the usual tail: two candles were not listed, then the newer of them is.
    world = make_world()
    world.source.missing = {1396, 1397}
    world.poll()
    assert world.feed._holes == {(opens_at(1395), opens_at(1398))}
    world.source.missing = {1396}
    world.pass_seconds(2)
    world.poll()
    snap = world.feed.snapshot()
    assert world.source.calls == [HISTORY, live.TAIL]  # no reload: nothing the feed had was lost
    assert world.feed._holes == {(opens_at(1395), opens_at(1397))} and opens_at(1397) in world.feed.frame()["time"].tolist()
    assert codes(snap).count("history_gaps") == 1 and "reloaded" not in codes(snap)  # the hole only became smaller

    # Out of the tail's reach: the longer request at the next closed candle brings the part that was filled.
    world = make_world()
    world.source.missing = {1390, 1391}
    world.poll()
    world.source.missing = {1390}
    world.close_candles()
    world.poll()
    snap = world.feed.snapshot()
    assert world.source.calls == [HISTORY, live.TAIL, 12]
    assert world.feed._holes == {(opens_at(1389), opens_at(1391))} and opens_at(1391) in world.feed.frame()["time"].tolist()
    assert codes(snap).count("history_gaps") == 1 and "reloaded" not in codes(snap)
    assert world.feed.frame()["time"].tolist() == world.source.seen()["time"].tolist()[-len(world.feed.frame()) :]


def test_the_longer_tail_is_used_only_when_it_fits_and_a_failure_costs_nothing(make_world):
    world = make_world()
    world.source.missing = {1395}
    world.poll()
    hole = {(opens_at(1394), opens_at(1396))}
    fetch = world.source.fetch_candles

    def longer_tail_fails(timeframe: str, limit: int = 500) -> pd.DataFrame:
        if limit not in (live.TAIL, HISTORY):
            world.source.calls.append(limit)
            raise DataSourceError("the longer request timed out")
        return fetch(timeframe, limit)

    world.source.fetch_candles = longer_tail_fails
    world.source.missing = set()  # the source has the candle now
    world.close_candles()
    assert world.poll() == 2.0  # the cycle itself is fine
    snap = world.feed.snapshot()
    assert world.source.calls[-2:] == [live.TAIL, 7] and world.feed._holes == hole  # nothing gained, nothing lost
    assert snap["feed"] == "ok" and "reloaded" not in codes(snap)
    assert world.feed._decisions[-1].result.signal_time == opens_at(1400)  # the closed candle was decided

    def longer_tail_lacks_a_candle(timeframe: str, limit: int = 500) -> pd.DataFrame:
        frame = fetch(timeframe, limit)
        if limit not in (live.TAIL, HISTORY):  # this answer opens a hole the stored candles do not have
            frame = frame[frame["time"] != opens_at(1398)].reset_index(drop=True)
        return frame

    world.source.fetch_candles = longer_tail_lacks_a_candle
    world.close_candles()
    world.poll()
    assert world.source.calls[-2:] == [live.TAIL, 8] and world.feed._holes == hole  # not used: it would cost a candle
    assert opens_at(1398) in world.feed.frame()["time"].tolist()

    del world.source.fetch_candles  # the source answers properly: the next closed candle brings the fill
    world.close_candles()
    world.poll()
    assert world.source.calls[-2:] == [live.TAIL, 9] and world.feed._holes == set()
    assert world.source.calls.count(HISTORY) == 1 and "reloaded" not in codes(world.feed.snapshot())


def test_a_refusal_of_the_longer_tail_is_obeyed_after_the_cycle_it_came_in(make_world):
    world = make_world()
    world.source.missing = {1395}
    world.poll()
    fetch = world.source.fetch_candles

    def refused(timeframe: str, limit: int = 500) -> pd.DataFrame:
        if limit not in (live.TAIL, HISTORY):
            world.source.calls.append(limit)
            raise DataSourceError("HTTP 429", status=429, retry_after=90.0)
        return fetch(timeframe, limit)

    world.source.fetch_candles = refused
    world.close_candles()
    assert world.poll() == 90.0  # the source asked for quiet: the next cycle waits as long as it said
    # The candle that closed is decided in this very cycle: the usual answers were good.
    assert world.feed._decisions[-1].result.signal_time == opens_at(1400) and world.feed.tick()["feed"] == "ok"
    assert world.source.calls[-2:] == [live.TAIL, 7]

    del world.source.fetch_candles
    world.source.missing = set()  # the source answers again, and lists the candle now
    world.pass_seconds(90)
    assert world.poll() == 2.0 and world.source.calls[-1] == live.TAIL  # no candle closed since: the usual tail
    world.close_candles()
    assert world.poll() == 2.0 and world.feed._holes == set()  # and the next closed candle brings the fill


def test_a_hole_that_comes_and_goes_reloads_the_history_once_a_minute_at_most(make_world):
    world = make_world()
    world.poll()
    world.close_candles()
    for cycle in range(20):  # two copies of the source answer in turn; one lacks the newest closed candle
        world.source.missing = {1399} if cycle % 2 == 0 else set()
        world.poll()
        world.pass_seconds(2)
    snap = world.feed.snapshot()
    assert world.source.calls.count(HISTORY) == 2  # the first load, and one reload for that hole
    assert codes(snap).count("reloaded") == 1 and codes(snap).count("history_gaps") == 1
    assert snap["feed"] == "ok" and [d.result.signal_time for d in world.feed._decisions].count(opens_at(1400)) == 1

    world.pass_seconds(30)  # a minute after the reload the same hole may cause one more
    for cycle in range(6):
        world.source.missing = {1399} if cycle % 2 == 0 else set()
        world.poll()
        world.pass_seconds(2)
    assert world.source.calls.count(HISTORY) == 3
    assert codes(world.feed.snapshot()).count("history_gaps") == 1  # the hole itself is still said once


def test_after_a_reload_only_candles_a_signal_can_still_live_on_are_announced_whatever_the_clock_says(make_world):
    target = signal_cursors(make_world)[-1]  # eight candles are missed; the newest of them carries a signal
    world = make_world(target - 8, stale_after_seconds=10**6)
    world.source.timed = False  # a quote without a time: the feed has only the PC's clock
    world.pc.now = world.clock.now - 7800  # and that clock is more than two hours slow
    world.poll()
    emails = len(world.signal_emails())
    before = world.feed.snapshot()

    world.close_candles(8)
    world.poll()
    snap = world.feed.snapshot()
    newest_close = opens_at(world.source.cursor - 1)
    missed = [row for row in snap["log"] if row["time"] > before["now"] + 7800]
    still_valid = [row for row in missed if row["time"] > newest_close - 2 * M15]
    # By the slow clock none of the missed signals has expired, so the clock alone would send them all.
    assert len(missed) >= 2 and all(snap["now"] < row["expires_at"] for row in missed)
    assert world.source.calls[-1] == HISTORY and 1 <= len(still_valid) < len(missed)
    new_emails = world.signal_emails()[emails:]
    assert len(new_emails) == len(still_valid)
    assert all(f"{row['entry']:.2f}" in mail[0] for row, mail in zip(reversed(still_valid), new_emails))


@pytest.mark.parametrize("behind", [1, 4])
def test_a_first_answer_from_a_copy_that_is_behind_does_not_announce_what_closed_before_the_start(make_world, behind):
    target = signal_cursors(make_world)[-1]
    world = make_world(target + 1, stale_after_seconds=10**6)  # started one candle after a signal's candle closed
    world.source.cursor -= behind  # the first answer is behind; the clock is right
    world.poll()
    assert codes(world.feed.snapshot()) == ["started"]
    world.source.cursor += behind
    world.pass_seconds(2)
    world.poll()
    snap = world.feed.snapshot()
    assert world.source.calls[-1] == (live.TAIL if behind == 1 else HISTORY)
    # The signal is decided and shown, as after any restart. Nobody is told: it closed before the feed started.
    assert snap["log"][0]["time"] == opens_at(target - 1) and snap["card"]["kind"] == "signal"
    assert newest_signal(snap) == 0 and world.signal_emails() == []

    world.until_signal()  # the next one is announced as usual
    assert len(world.signal_emails()) == 1


@pytest.mark.parametrize("poll", [0.5, 2.0, 15.0, 60.0])
def test_every_tick_says_how_often_ticks_come(make_world, poll):
    """The page calls the server lost after a few poll intervals without a tick. It must know the
    interval from the tick itself: a page whose /api/state keeps failing has nothing else."""
    world = make_world(poll_seconds=poll)
    before = world.feed.tick()  # before the first cycle: a page that connects while the feed starts
    world.poll()
    after = world.feed.tick()
    world.source.error = DataSourceError("no route to host")
    world.pass_seconds(120)
    world.poll()
    down = world.feed.tick()  # and while the feed is down
    assert down["feed"] == "down"
    for tick in (before, after, down, world.feed.snapshot()):
        assert tick["poll_seconds"] == poll and type(tick["poll_seconds"]) is float


@pytest.mark.parametrize("poll, late_after", [(2.0, 10.0), (15.0, 37.5), (30.0, 45.0), (100.0, 45.0)])
def test_late_comes_before_down_also_with_a_slow_poll(make_world, poll, late_after):
    world = make_world(poll_seconds=poll)
    world.poll()
    start = world.timer.now
    world.source.error = DataSourceError("no route to host")
    seen = []
    for seconds in (late_after - 0.5, late_after, 59.5, 60):
        world.timer.now = start + seconds
        world.poll()
        seen.append(world.feed.tick()["feed"])
    assert seen == ["ok", "late", "late", "down"]
