"""The live feed: ask the price source every few seconds, decide, notify.

One background thread calls ``poll`` in a loop. Each cycle fetches the newest
few candles and the quote, lays them over the candles kept in memory, and
when a candle has closed runs the rule and the filters on it once. Nothing
is recomputed for a price tick: a tick only moves the forming candle, the
price and the state of the current signal.

Rules kept here:
- Never a made-up price. When the source gives nothing, the last real values
  stay with their age, the feed is reported late and then down, and no signal
  is decided until real data is back.
- Closed candles only: the source marks a candle closed when it lists a newer one.
- A signal is announced once, when its candle closes while the feed is
  running. After a restart the history is replayed for display, not re-sent.
- A candle is decided once. Whatever the source answers later (a copy that is
  behind, a candle that is missing from one answer, a whole-history reload),
  a decision already made stays exactly as it was made.
- An answer without candles, or with candles that end before the ones the
  feed has, is not fresh data: the cycle counts as failed and the stored
  candles stay.
- The feed's clock follows the source forward. An old quote does not turn it
  back: such a quote shows with its age.
- Signals only: this module reads prices and sends messages, nothing else.
- The forecast (backend/app/forecast.py) is made after the signals of a cycle
  are decided and announced. It never changes them, and an error in it costs
  the forecast only.
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from collections import deque
from concurrent.futures import Executor, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any, Callable

import numpy as np
import pandas as pd

from backend.app import forecast as fcast
from backend.app import notify
from backend.app.config import Settings
from backend.app.core import candles as cd
from backend.app.core import filters as fl
from backend.app.core.pricecheck import probably_closed
from backend.app.core.signal import params_for
from backend.app.datasource.base import DataSource, DataSourceError, Quote

log = logging.getLogger("tradepilot.live")

TAIL = 3  # candles asked for on a normal cycle: the forming one and two before it
REPLAY = 192  # closed candles judged again after a (re)load: 48 hours of M15
KEEP_DECISIONS = 600
LOG_ROWS = 60
LATE_AFTER = 10.0  # seconds without fresh data before the feed is shown as late
LATE_POLLS = 2.5  # with a slow poll: this many poll intervals instead, when that is longer
LATE_CAP = 0.75  # but never later than this share of stale_after_seconds, so "late" always comes before "down"
HOLE_QUIET = 60.0  # seconds: the same hole is not reloaded for, or said, again within this time
HOLE_AGE = 86400  # seconds: a hole younger than this is asked for again each time a candle closes
BEHIND_QUIET = 60.0  # seconds of nothing but answers that end before the stored candles before the source is believed
CLOCK_PERIOD = 3600.0  # seconds in which no quote was ahead of the clock before the clock is set back
REFUSED = (418, 429)
HIGHER = fl.HIGHER_TIMEFRAME


class SourceBehind(DataSourceError):
    """The answer's candles end before the candles the feed already has: a copy of the source that is behind."""


def _bar(row: Any) -> dict[str, float | int | None]:
    return {
        "time": int(row.time),
        "open": float(row.open),
        "high": float(row.high),
        "low": float(row.low),
        "close": float(row.close),
        "volume": None if math.isnan(row.volume) else float(row.volume),  # None: the source gave none
        "closed": bool(row.closed),  # False: the candle is still forming
    }


class LiveFeed:
    def __init__(
        self,
        source: DataSource,
        settings: Settings,
        notifier: notify.Notifier,
        *,
        clock: Callable[[], float] = time.time,
        timer: Callable[[], float] = time.monotonic,
        closed_hours: Callable[[float], bool] = probably_closed,
        executor: Executor | None = None,
        forecaster: fcast.Forecaster | None = None,
    ) -> None:
        self._source = source
        self._forecaster = forecaster  # None: no forecast is made or shown
        self._notifier = notifier
        self._clock = clock
        self._timer = timer
        self._closed_hours = closed_hours
        self._executor = executor  # emails leave on it so a slow mail server never delays the feed
        self._side_request = ThreadPoolExecutor(max_workers=1, thread_name_prefix="quote")

        self.symbol = settings.symbol
        self.timeframe = settings.signal_timeframe
        self._step = cd.timeframe_seconds(self.timeframe)
        self._poll_seconds = max(float(settings.poll_seconds), 0.5)
        self._history = int(settings.history_candles)
        self._stale_after = float(settings.stale_after_seconds)
        # Fresh data comes once per poll: with a slow poll the feed is not late between two answers.
        # Capped below the stale limit, so a feed always reads late before it reads down.
        self._late_after = min(max(LATE_AFTER, LATE_POLLS * self._poll_seconds), LATE_CAP * self._stale_after)
        self._valid_bars = int(params_for(self.timeframe).valid_bars)  # how many candles a signal lives
        self._deep_tail = min(HOLE_AGE // self._step + TAIL, self._history)
        self._alert_after = float(settings.alert_after_seconds)
        self._offset_hours = int(settings.display_utc_offset_hours)

        self._lock = threading.RLock()
        self._candles: pd.DataFrame | None = None
        self._decisions: list[fl.Decision] = []
        self._last_sent: fl.Decision | None = None
        self._decided = 0  # close time of the newest candle that has been decided: it is never decided again
        self._announced = 0  # signal time of the newest decision that needs no announcement any more
        self._outcomes: dict[int, tuple[str, int]] = {}
        # Places where the stored history lacks candles, as (open time before, open time after).
        self._holes: set[tuple[int, int]] = set()
        self._hole_said: dict[tuple[int, int], float] = {}  # when each hole was last there (the feed's stopwatch)
        self._hole_reloads: dict[tuple[int, int], float] = {}  # when the history was last reloaded for each hole
        self._heal_refused: DataSourceError | None = None  # the source refused the longer tail in this cycle
        self._behind_since: float | None = None  # when the answers began to end before the stored candles
        self._behind_logged = -math.inf  # when that was last written to the server log
        self._quote: Quote | None = None
        self._quote_at = 0.0
        self._source_time: float | None = None  # a time the source reported: what the clock runs on from
        self._source_time_at = 0.0  # when that quote arrived (the feed's stopwatch)
        # The newest time stamp among the quotes behind the clock since it was last set: a quote that is
        # not newer than that is a repeat and says nothing new about the time.
        self._clock_newest = -math.inf
        # The freshest quote since the clock was last set, as (its time, when it arrived), and when
        # the first quote that was behind the clock arrived: see _set_clock.
        self._clock_fresh: tuple[float, float] | None = None
        self._clock_period_at: float | None = None
        self._fingerprint: tuple | None = None
        self._started_at = timer()
        self._activity_at: float | None = None
        self._status = "starting"
        self._age = 0.0
        self._down = False
        self._down_emailed = False
        self._failures = 0
        self._refusals = 0
        self._alerts: deque[dict[str, Any]] = deque(maxlen=30)
        self._alert_id = 0
        self._version = 0
        self._subscribers: dict[asyncio.Queue, asyncio.AbstractEventLoop] = {}
        self.running = False

    # --- time ---------------------------------------------------------------

    def now(self) -> float:
        """Current UTC time, by the source's own clock when it gives one.

        A time the source reported plus the time passed since. A quote that
        is ahead of this clock moves it forward. When the source repeats an
        old answer, or the first quote after a silence is an old one, the
        clock keeps running and the quote's age shows (see ``_set_clock``).
        """
        if self._source_time is not None:
            return self._source_time + (self._timer() - self._source_time_at)
        return self._clock()

    # --- the loop -------------------------------------------------------------

    def run(self, stop: threading.Event) -> None:
        """Poll until ``stop`` is set, and keep open pages informed in between.

        A cycle runs on its own thread. While the source is slow to answer, or
        while the loop backs off after refusals, a tick still goes out every
        poll interval, so a page can tell a silent source from a lost server.
        """
        self.running = True
        cycles = ThreadPoolExecutor(max_workers=1, thread_name_prefix="feed-cycle")
        try:
            while not stop.is_set():
                began = self._timer()
                cycle = cycles.submit(self.poll)
                wait: float | None = None
                while wait is None and not stop.is_set():
                    try:
                        wait = cycle.result(timeout=self._poll_seconds)
                    except Exception as error:
                        if isinstance(error, FutureTimeout) and not cycle.done():
                            self._beat_safely()
                            continue
                        # The loop must outlive any single bad cycle.
                        log.exception("live feed: unexpected error")
                        wait = max(self._poll_seconds, 5.0)
                if wait is None:
                    break
                # The cycle's own duration is part of the interval, not added to it.
                due = max(began + wait, self._timer() + 0.2)
                while not stop.is_set():
                    remaining = due - self._timer()
                    if remaining <= 0 or stop.wait(min(remaining, self._poll_seconds)):
                        break
                    if due - self._timer() > 0:
                        self._beat_safely()
        finally:
            self.running = False
            cycles.shutdown(wait=False, cancel_futures=True)

    def beat(self) -> None:
        """Judge the feed and tell open pages, without asking the source."""
        self._publish(self.tick())

    def _beat_safely(self) -> None:
        # The loop must outlive an error between two cycles as it outlives one inside a cycle.
        try:
            self.beat()
        except Exception:
            log.exception("live feed: unexpected error between cycles")

    def poll(self) -> float:
        """One cycle. Returns the seconds to wait before the next one."""
        wait = self._poll_seconds
        try:
            try:
                frame, quote, reload = self._fetch()
            except DataSourceError as error:
                wait = self._failed(error)
            else:
                with self._lock:
                    self._apply(frame, quote, reload)
                refused, self._heal_refused = self._heal_refused, None
                if refused is not None:
                    wait = self._failed(refused)
        finally:
            with self._lock:
                self._judge_feed()
                tick = self._tick()
            self._publish(tick)
        return wait

    def _fetch(self) -> tuple[pd.DataFrame, Quote, bool]:
        """Network part of a cycle, done without holding the lock."""
        stored = self._candles
        reload = stored is None
        self._heal_refused = None
        # The quote is asked for at the same moment as the candles, not after them.
        quote = self._side_request.submit(self._source.fetch_quote)
        try:
            frame = self._ask(self._history if reload else TAIL)
            if stored is not None:
                self._bear_behind(stored, frame)
                merged, news = cd.lay_over(stored, frame, self.timeframe)
                pairs = [(gap.after, gap.before) for gap in news]
                if merged is None or (pairs and not self._reloaded_lately(pairs)):
                    # The tail does not connect, or a candle is missing that was not missing before.
                    reload = True
                    self._note_reload(pairs)
                    frame = self._ask(self._history)
                    self._behind_since = None  # the stored candles are the source's own again
                else:
                    # A hole the history was reloaded for less than a minute ago is borne as the
                    # source gives it: its answers disagree, and asking for everything again
                    # on every change would not settle that.
                    frame = self._heal(stored, merged).iloc[-self._history :].reset_index(drop=True)
        except BaseException:
            if not quote.cancel():  # already running: wait, so two cycles never overlap
                quote.exception()
            raise
        return frame, quote.result(), reload

    def _ask(self, limit: int) -> pd.DataFrame:
        """The newest ``limit`` candles. An answer without a single candle is not data: it fails the cycle."""
        frame = self._source.fetch_candles(self.timeframe, limit)
        if frame.empty:
            raise DataSourceError(f"the source answered a request for {limit} candles with none")
        return frame

    def _bear_behind(self, stored: pd.DataFrame, frame: pd.DataFrame) -> None:
        """Fail the cycle when the tail ends before the stored candles, instead of following it back.

        Copies of a source can answer in turn, one of them a candle behind.
        Such an answer brings nothing new: the stored candles stay, the cycle
        counts as failed, and the feed's age runs on from the last answer
        that was right. Only when nothing else has come for a minute is the
        source believed: the caller then loads the history as it is now.
        """
        if int(frame["time"].iloc[-1]) >= int(stored["time"].iloc[-1]):
            self._behind_since = None
            return
        now = self._timer()
        if self._behind_since is None:
            self._behind_since = now
        if now - self._behind_since < BEHIND_QUIET:
            raise SourceBehind("the source answered with candles that end before the ones it gave earlier")

    def _reloaded_lately(self, pairs: list[tuple[int, int]]) -> bool:
        now = self._timer()
        return all(now - self._hole_reloads.get(pair, -math.inf) < HOLE_QUIET for pair in pairs)

    def _note_reload(self, pairs: list[tuple[int, int]]) -> None:
        now = self._timer()
        self._hole_reloads = {pair: at for pair, at in self._hole_reloads.items() if now - at < HOLE_QUIET}
        self._hole_reloads.update((pair, now) for pair in pairs)

    def _heal(self, stored: pd.DataFrame, merged: pd.DataFrame) -> pd.DataFrame:
        """Ask once per closed candle for a tail that reaches the young holes, if there are any.

        A source can list a candle late. Once that candle is older than the
        usual tail nothing would ever pick it up. So when a candle closes and
        the stored history has a hole younger than a day, one longer tail is
        asked for, from the candle before the oldest such hole on. What it
        brings is used only when it connects and costs no candle the feed
        has: a hole that became smaller is taken, a new one is not. Otherwise
        the frame stays as it is. Candles decided before are not decided again.
        """
        if self._last_closed(merged) <= self._last_closed(stored):
            return merged
        reach = cd.hole_reach(merged, self.timeframe, self._deep_tail)
        if reach <= TAIL:
            return merged
        try:
            healed, news = cd.lay_over(merged, self._ask(reach), self.timeframe)
        except (DataSourceError, ValueError) as error:
            log.info("live feed: the longer tail for a hole gave nothing: %s", error)
            if isinstance(error, DataSourceError) and error.status in REFUSED:
                # Asked to slow down. This cycle's own answers are good and are used; the wait
                # before the next cycle is the one a refusal gets (see poll).
                self._heal_refused = error
            return merged
        return merged if healed is None or news else healed

    def _failed(self, error: DataSourceError) -> float:
        with self._lock:
            self._failures += 1
            loud = self._failures in (1, 10) or self._failures % 100 == 0
            if isinstance(error, SourceBehind):
                # Copies that answer in turn would write this line every few seconds: once a minute is enough.
                now = self._timer()
                loud = now - self._behind_logged >= BEHIND_QUIET
                if loud:
                    self._behind_logged = now
            if loud:
                log.warning("live feed: fetch failed (%d in a row): %s", self._failures, error)
            if error.status in REFUSED:
                self._refusals += 1
                if error.retry_after and error.retry_after > 0:
                    return max(self._poll_seconds, min(float(error.retry_after), 3600.0))
                return min(60.0 * 2 ** (self._refusals - 1), 900.0)
            return min(self._poll_seconds * 2 ** min(self._failures - 1, 3), 15.0)

    def _last_closed(self, frame: pd.DataFrame | None) -> int:
        if frame is None:
            return 0
        closed = frame.loc[frame["closed"], "time"]
        return int(closed.iloc[-1]) if len(closed) else 0

    def _apply(self, frame: pd.DataFrame, quote: Quote, reload: bool) -> None:
        first_load = self._candles is None
        self._candles = frame
        # The quote first: every alert below is then stamped by the source's clock.
        self._quote = quote
        self._quote_at = self._timer()
        if quote.timed_by_source:
            self._set_clock(quote.time, self._quote_at)

        last = frame.iloc[-1]
        volume = float(last["volume"])
        fingerprint = (
            quote.time if quote.timed_by_source else None,
            quote.bid,
            quote.ask,
            quote.price,
            int(last["time"]),
            float(last["close"]),
            None if volume != volume else volume,  # NaN (no volume) never equals itself
        )
        if fingerprint != self._fingerprint:
            # Only data that moved counts as alive: a source that repeats one
            # frozen answer must not look healthy.
            self._fingerprint = fingerprint
            self._activity_at = self._timer()
        self._failures = 0
        self._refusals = 0

        # Judged before deciding: a candle that closes while the price is stale
        # proposes nothing.
        self._judge_feed()
        stale = self._status == "down"

        closes = frame.loc[frame["closed"], "time"] + self._step  # when each closed candle closed
        newest = int(closes.iloc[-1]) if len(closes) else 0
        fresh: list[fl.Decision] = []
        if first_load:
            # The history is judged for the log and the chart. Only the newest candle can have
            # closed under a stale price here: the others closed before this process looked.
            marks = {newest} if stale and newest else set()
            self._decisions = fl.replay(
                frame, self.timeframe, REPLAY, closed_hours=self._closed_hours, stale_times=marks
            )
            self._last_sent = next((d for d in reversed(self._decisions) if d.sent), None)
            self._decided = newest
            # Nothing that closed before this first answer is announced, also when the answer
            # came from a copy of the source that is behind: the clock sets the line as well.
            boundary = int(cd.opens(pd.Series([int(self.now())]), self.timeframe).iloc[0])
            self._announced = max(newest, boundary)
            self._alert("ok", "started", email=True)
            self._version += 1
        else:
            if reload:
                self._alert("warn", "reloaded")
            # Only candles not decided before are decided, after a reload as on any other cycle.
            # What was decided, and maybe announced, stays as it was decided: neither a reload
            # nor an answer from a copy that is behind can rewrite it.
            new = closes[closes > self._decided]
            if len(new):
                marks = {int(at) for at in new} if stale else set()
                fresh = fl.replay(
                    frame,
                    self.timeframe,
                    min(len(new), REPLAY),
                    previous=self._last_sent,
                    closed_hours=self._closed_hours,
                    stale_times=marks,
                )
                self._decisions = (self._decisions + fresh)[-KEEP_DECISIONS:]
                for decision in fresh:
                    if decision.sent:
                        self._last_sent = decision
                self._decided = newest
                self._version += 1
        self._note_holes(frame)

        now = self.now()
        # By count, not by the clock alone: a signal lives for ``valid_bars`` candles, so a
        # decision older than that many closed candles has run out whatever the clock says.
        oldest_valid = newest - self._valid_bars * self._step
        for decision in fresh:
            at = decision.result.signal_time or 0
            if at <= self._announced:
                continue
            self._announced = at
            if decision.sent and at > oldest_valid and fl.signal_state(decision, None, now) != fl.EXPIRED:
                self._announce(decision)
        if len(self._outcomes) > KEEP_DECISIONS:
            kept = {d.result.signal_time for d in self._decisions}
            self._outcomes = {key: value for key, value in self._outcomes.items() if key in kept}
        self._forecast(frame)

    def _set_clock(self, time: float, at: float) -> None:
        """Let a quote's own time move the feed's clock: forward at once, back only on long evidence.

        A quote ahead of the running clock sets it. An older one does not:
        after a silence the first quote can be minutes old (a copy of the
        source that lags), and a clock set back by it would make that price
        look fresh and an expired signal look valid. The quote's age is
        judged against the clock as it ran before.

        A clock that could only go forward would stay wrong for good after
        one quote dated ahead, and the stopwatch of a PC that runs a little
        fast would carry it ahead of the source over the days. So the clock
        does go back, but only to the freshest quote of a whole
        ``CLOCK_PERIOD`` in which not one quote was ahead of it. A quote that
        is repeated says nothing new about the time and does not count.

        The period starts with the first quote behind the clock, judged
        against the quotes since the clock was last set, not against every
        quote ever seen: after one quote dated ahead (a glitch), the right
        quotes that follow are all older than it, and judged against it they
        would not count until real time had caught up with the glitch. So the
        feed reads down for an hour after such a quote, whatever its size.
        """
        if self._source_time is None:
            self._source_time, self._source_time_at = time, at
            return
        if time >= self._source_time + (at - self._source_time_at):
            self._source_time, self._source_time_at = time, at
            # The evidence against the clock starts anew.
            self._clock_fresh, self._clock_period_at, self._clock_newest = None, None, -math.inf
            return
        if time <= self._clock_newest:
            return  # a repeat
        self._clock_newest = time
        fresh = self._clock_fresh
        if fresh is None or time - at >= fresh[0] - fresh[1]:
            self._clock_fresh = (time, at)
        if self._clock_period_at is None:
            self._clock_period_at = at
        if at - self._clock_period_at >= CLOCK_PERIOD:
            self._source_time, self._source_time_at = self._clock_fresh
            self._clock_fresh, self._clock_period_at, self._clock_newest = None, None, -math.inf

    def _note_holes(self, frame: pd.DataFrame) -> None:
        """Keep the holes of the stored history by name, and say a new one once.

        A hole is known by the two candles around it. One that was not there
        before is said; one that healed is forgotten. A hole that comes and
        goes with the source's answers is said once, not on every change. A
        hole that only became smaller, because the source filled a part of
        it, is not a new one.
        """
        times = frame["time"].to_numpy()
        holes = {(int(times[i]), int(times[i + 1])) for i in np.nonzero(np.diff(times) > self._step)[0]}
        now = self._timer()
        news = [
            pair
            for pair in holes - self._holes
            if not cd.within(pair, self._holes) and now - self._hole_said.get(pair, -math.inf) >= HOLE_QUIET
        ]
        self._hole_said = {pair: at for pair, at in self._hole_said.items() if now - at < HOLE_QUIET}
        self._hole_said.update((pair, now) for pair in holes)
        self._holes = holes
        if news:
            # The source's own history lacks candles.
            self._alert("warn", "history_gaps", count=len(holes))

    def _forecast(self, frame: pd.DataFrame) -> None:
        """One forecast per newly closed candle, after everything about signals is done."""
        if self._forecaster is None:
            return
        try:
            if self._forecaster.update(frame):
                self._version += 1  # open pages fetch the snapshot again and get the new forecast
        except Exception:  # the forecast is an extra: it must never cost a cycle
            log.exception("live feed: the forecast failed")

    def _judge_feed(self) -> None:
        reference = self._activity_at if self._activity_at is not None else self._started_at
        age = self._timer() - reference
        now = self.now()
        quote = self._quote
        if quote is not None and quote.timed_by_source:
            # The price's own age: a quote that is repeated unchanged is old even
            # while the candles keep moving.
            age = max(age, now - quote.time)
        frame = self._candles
        if frame is not None and len(frame):
            # The candles' side: how long ago the forming candle should have been
            # followed by a newer one. A live price does not make up for it.
            age = max(age, now - (int(frame["time"].iloc[-1]) + self._step))
        if age >= self._stale_after:
            status = "down"
        elif self._candles is None:
            status = "starting"
        else:
            status = "ok" if age < self._late_after else "late"
        if status == "down" and not self._down:
            self._down = True
            self._alert("down", "feed_down", seconds=int(self._stale_after))
        if self._down and status == "down" and not self._down_emailed and age >= self._alert_after:
            self._down_emailed = True
            self._email(*self._alert_email("feed_down", seconds=int(age)))
        if self._down and status == "ok":
            self._down = False
            self._alert("ok", "feed_back", email=self._down_emailed)
            self._down_emailed = False
        if status != self._status:
            self._version += 1
        self._status = status
        self._age = age

    # --- alerts and email -------------------------------------------------------

    def _alert_email(self, code: str, **values: object) -> tuple[str, str]:
        text = notify.alert_text(code, **values)
        return notify.alert_message(text, self.symbol, self.now(), self._offset_hours)

    def _alert(self, level: str, code: str, email: bool = False, **values: object) -> None:
        self._alert_id += 1
        self._alerts.appendleft(
            {
                "id": self._alert_id,
                "time": self.now(),
                "level": level,
                "code": code,
                "text": notify.alert_text(code, **values),
            }
        )
        self._version += 1
        if email:
            self._email(*self._alert_email(code, **values))

    def _announce(self, decision: fl.Decision) -> None:
        emailed = self._notifier.enabled
        self._alert(
            "ok",
            "signal_emailed" if emailed else "signal",
            side=decision.result.signal,
            grade=notify.GRADE_WITHOUT_DIGEST,
        )
        if emailed:
            self._email(
                *notify.signal_message(
                    decision, self.symbol, self.timeframe, self._source.name, self._offset_hours
                )
            )

    def _email(self, subject: str, body: str) -> None:
        if not self._notifier.enabled:
            return
        if self._executor is None:
            self._deliver(subject, body)
        else:
            self._executor.submit(self._deliver, subject, body)

    def _deliver(self, subject: str, body: str) -> bool:
        try:
            self._notifier.send(subject, body)
        except Exception as error:  # whatever the reason, a lost email must show on the page
            # Only a NotifyError's text is known to be free of addresses and passwords.
            reason = error if isinstance(error, notify.NotifyError) else type(error).__name__
            log.warning("live feed: email not sent: %s", reason)
            with self._lock:
                self._alert("warn", "email_failed")
            return False
        return True

    def send_test_email(self) -> bool:
        """Send one test message now. False when email is not configured or it failed."""
        if not self._notifier.enabled:
            return False
        if not self._deliver(*self._alert_email("email_test")):
            return False
        with self._lock:
            self._alert("ok", "email_test")
        return True

    # --- what the site reads ------------------------------------------------------

    def _row(self, decision: fl.Decision) -> dict[str, Any]:
        result = decision.result
        levels = result.levels
        assert levels is not None and result.signal_time is not None and result.expires_at is not None
        return {
            "time": result.signal_time,
            "side": result.signal,
            "entry": levels.entry,
            "zone_low": levels.zone_low,
            "zone_high": levels.zone_high,
            "sl": levels.stop_loss,
            "tp": levels.take_profit,
            "sl_distance": levels.sl_distance,
            "tp_distance": levels.tp_distance,
            "expires_at": result.expires_at,
            "valid_seconds": result.expires_at - result.signal_time,
            "grade": notify.GRADE_WITHOUT_DIGEST,
            "run": decision.run,
            "score": result.score,
            "h1_score": decision.h1_score,
        }

    def _card(self, now: float, price: float | None) -> dict[str, Any]:
        if self._status == "down":
            quote = self._quote
            return {
                "kind": "feed_down",
                "last_price": None if quote is None else quote.price,
                "last_time": None if quote is None else self._price_time(quote, now),
                "age": self._age,
                "emailed": self._down_emailed,
            }
        latest = self._decisions[-1] if self._decisions else None
        if self._candles is None or latest is None:
            return {"kind": "starting"}
        sent = self._last_sent
        if sent is not None:
            state = fl.signal_state(sent, price, now)
            if state != fl.EXPIRED:
                return {"kind": "signal", "state": state, **self._row(sent)}
        at = latest.result.signal_time or 0
        base = {
            "candle_close": at,
            "next_check": at + self._step,
            "score": latest.result.score,
            "side": latest.result.signal,
            "h1_score": latest.h1_score,
        }
        if latest.sent:
            return {"kind": "expired", **base}
        if latest.blocked and latest.blocked != fl.DUPLICATE:
            return {"kind": "blocked", "code": latest.blocked, **base}
        return {"kind": "hold", **base}

    def _price_time(self, quote: Quote, now: float) -> float:
        """When the shown price was true: its own time, or the moment it arrived."""
        if quote.timed_by_source:
            return quote.time
        return now - (self._timer() - self._quote_at)

    def _bars(self) -> dict[str, list[dict[str, float | int]]]:
        frame = self._candles
        if frame is None or frame.empty:
            return {}
        bars = {self.timeframe: [_bar(row) for row in frame.tail(2).itertuples()]}
        ratio = cd.timeframe_seconds(HIGHER) // self._step
        higher = cd.aggregate(frame.tail(3 * ratio), self.timeframe, HIGHER)
        bars[HIGHER] = [_bar(row) for row in higher.tail(2).itertuples()]
        return bars

    def _tick(self) -> dict[str, Any]:
        # Judged at the moment of asking: a page that loads or reconnects while the
        # loop is backing off must not be told the feed is fine.
        self._judge_feed()
        quote = self._quote
        now = self.now()
        live = quote is not None and self._status != "down"
        price = quote.price if quote is not None else None
        return {
            "v": self._version,
            "now": now,
            "feed": self._status,
            "age": round(self._age, 1),
            "price": price,
            "bid": None if quote is None else quote.bid,
            "ask": None if quote is None else quote.ask,
            "card": self._card(now, price if live else None),
            # A rule of thumb for the broker's hours, not its calendar (see pricecheck).
            "market_closed": bool(self._closed_hours(now)),
            "bars": self._bars() if live else {},
            # Ticks come this often. The page's "server lost" limit follows it, and it must be
            # known from the tick: a page whose /api/state fails has nothing else.
            "poll_seconds": self._poll_seconds,
        }

    def tick(self) -> dict[str, Any]:
        with self._lock:
            return self._tick()

    def _outcome(self, decision: fl.Decision, now: float) -> dict[str, Any]:
        result = decision.result
        if now < (result.expires_at or 0):
            return {"kind": "valid"}
        key = result.signal_time or 0
        if key not in self._outcomes and self._candles is not None:
            kind, at = fl.outcome(result, self._candles)
            if kind is None or at is None:
                return {"kind": "none"}
            self._outcomes[key] = (kind, at)
        if key not in self._outcomes:
            return {"kind": "none"}
        kind, at = self._outcomes[key]
        return {"kind": kind, "time": at}

    def _reasons(self) -> dict[str, Any] | None:
        latest = self._decisions[-1] if self._decisions else None
        if latest is None or latest.result.score is None:
            return None
        result = latest.result
        return {
            "candle_close": result.signal_time,
            "score": result.score,
            "side": result.signal,
            "blocked": latest.blocked,
            "conditions": [{"id": c.id, "value": c.value} for c in result.conditions],
            "h1_score": latest.h1_score,
            "rsi": result.indicators.get("rsi"),
            "atr": result.indicators.get("atr"),
        }

    def snapshot(self) -> dict[str, Any]:
        """Everything the dashboard shows except the candles."""
        with self._lock:
            tick = self._tick()
            now = tick["now"]
            sent = [d for d in self._decisions if d.sent]
            offset = self._offset_hours * 3600
            day_start = (int(now) + offset) // 86400 * 86400 - offset
            today = [d for d in sent if (d.result.signal_time or 0) >= day_start]
            log_rows = [{**self._row(d), "outcome": self._outcome(d, now)} for d in reversed(sent[-LOG_ROWS:])]
            return {
                **tick,
                "symbol": self.symbol,
                "timeframe": self.timeframe,
                "source": {"name": self._source.name, "symbol": self._source.symbol},
                "reasons": self._reasons(),
                "log": log_rows,
                "today": {
                    "total": len(today),
                    "buy": sum(d.result.signal == "BUY" for d in today),
                    "sell": sum(d.result.signal == "SELL" for d in today),
                },
                "alerts": list(self._alerts),
                "email": self._notifier.enabled,
                "utc_offset_hours": self._offset_hours,
                "alert_after_seconds": self._alert_after,
                "rules": {
                    "status": "experimental",
                    "filters_off": [fl.NEWS_WINDOW],
                    "market_hours": "approximate",
                },
            }

    def prediction(self) -> dict[str, Any]:
        """The forecast block of /api/state (docs/WEB.md). Never raises."""
        if self._forecaster is None:
            return fcast.unavailable(fcast.NO_MODEL)
        try:
            with self._lock:
                frame = self._candles
                last = self._last_closed(frame)
                return self._forecaster.block(self._status, last + self._step if last else None)
        except Exception:
            log.exception("live feed: the forecast block failed")
            return fcast.unavailable(None)

    def frame(self) -> pd.DataFrame | None:
        """The candles kept in memory, newest last. Shared, never to be changed by the caller."""
        with self._lock:
            return self._candles

    def candles(self, timeframe: str, limit: int) -> list[dict[str, float | int]]:
        """The newest ``limit`` candles of the signal timeframe or of H1, oldest first."""
        with self._lock:
            frame = self._candles
        if frame is None:
            return []
        if timeframe == HIGHER and timeframe != self.timeframe:
            frame = cd.aggregate(frame, self.timeframe, HIGHER)
        elif timeframe != self.timeframe:
            raise ValueError(f"timeframe must be {self.timeframe} or {HIGHER}")
        return [_bar(row) for row in frame.tail(limit).itertuples()]

    def health(self) -> dict[str, Any]:
        with self._lock:
            frame = self._candles
            age = None if frame is None else round(self.now() - self._last_closed(frame) - self._step, 1)
            return {
                "feed": self._status if self.running else "not_started",
                "worker": "running" if self.running else "not_running",
                "source": self._source.name,
                "last_candle_age": age if self.running else None,
            }

    # --- push to open pages -----------------------------------------------------------

    def subscribe(self, loop: asyncio.AbstractEventLoop) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=20)
        with self._lock:
            self._subscribers[queue] = loop
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers.pop(queue, None)

    @staticmethod
    def _offer(queue: asyncio.Queue, item: Any) -> None:
        if queue.full():  # a slow page gets the newest state, not a backlog
            queue.get_nowait()
        queue.put_nowait(item)

    def _publish(self, item: Any) -> None:
        with self._lock:
            subscribers = list(self._subscribers.items())
        for queue, loop in subscribers:
            try:
                loop.call_soon_threadsafe(self._offer, queue, item)
            except RuntimeError:  # that page's event loop is gone
                self.unsubscribe(queue)

    def end_streams(self) -> None:
        """Tell every open stream to end; the feed itself keeps running."""
        self._publish(None)

    def close(self) -> None:
        """Server shutdown: end the streams and stop asking the source."""
        self.end_streams()
        self._side_request.shutdown(wait=False, cancel_futures=True)
