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
- Signals only: this module reads prices and sends messages, nothing else.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from concurrent.futures import Executor, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any, Callable

import pandas as pd

from backend.app import notify
from backend.app.config import Settings
from backend.app.core import candles as cd
from backend.app.core import filters as fl
from backend.app.core.pricecheck import probably_closed
from backend.app.datasource.base import DataSource, DataSourceError, Quote

log = logging.getLogger("tradepilot.live")

TAIL = 3  # candles asked for on a normal cycle: the forming one and two before it
REPLAY = 192  # closed candles judged again after a (re)load: 48 hours of M15
KEEP_DECISIONS = 600
LOG_ROWS = 60
LATE_AFTER = 10.0  # seconds without fresh data before the feed is shown as late
REFUSED = (418, 429)
HIGHER = fl.HIGHER_TIMEFRAME


def _bar(row: Any) -> dict[str, float | int]:
    return {
        "time": int(row.time),
        "open": float(row.open),
        "high": float(row.high),
        "low": float(row.low),
        "close": float(row.close),
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
    ) -> None:
        self._source = source
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
        self._late_after = max(LATE_AFTER, 2.5 * self._poll_seconds)
        self._alert_after = float(settings.alert_after_seconds)
        self._offset_hours = int(settings.display_utc_offset_hours)

        self._lock = threading.RLock()
        self._candles: pd.DataFrame | None = None
        self._decisions: list[fl.Decision] = []
        self._last_sent: fl.Decision | None = None
        self._announced = 0  # signal time of the newest decision that needs no announcement any more
        self._outcomes: dict[int, tuple[str, int]] = {}
        self._stale_times: set[int] = set()  # close times of candles that closed while the price was stale
        self._holes = 0  # places where the loaded history itself lacks candles
        self._quote: Quote | None = None
        self._quote_at = 0.0
        self._source_time: float | None = None  # newest time the source has reported
        self._source_time_at = 0.0
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

        The newest time the source has reported plus the time passed since.
        Only a newer time moves it: when the source repeats an old answer this
        clock keeps running, and the answer's age shows.
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
        finally:
            with self._lock:
                self._judge_feed()
                tick = self._tick()
            self._publish(tick)
        return wait

    def _fetch(self) -> tuple[pd.DataFrame, Quote, bool]:
        """Network part of a cycle, done without holding the lock."""
        reload = self._candles is None
        # The quote is asked for at the same moment as the candles, not after them.
        quote = self._side_request.submit(self._source.fetch_quote)
        try:
            frame = self._source.fetch_candles(self.timeframe, self._history if reload else TAIL)
            if not reload:
                merged = cd.merge(self._candles, frame, self.timeframe, keep=self._history)
                if merged is None:  # the tail does not connect: candles were missed
                    reload = True
                    frame = self._source.fetch_candles(self.timeframe, self._history)
                else:
                    frame = merged
        except BaseException:
            if not quote.cancel():  # already running: wait, so two cycles never overlap
                quote.exception()
            raise
        return frame, quote.result(), reload

    def _failed(self, error: DataSourceError) -> float:
        with self._lock:
            self._failures += 1
            if self._failures in (1, 10) or self._failures % 100 == 0:
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
        before = self._last_closed(self._candles)
        self._candles = frame
        # The quote first: every alert below is then stamped by the source's clock.
        self._quote = quote
        self._quote_at = self._timer()
        if quote.timed_by_source and (self._source_time is None or quote.time > self._source_time):
            self._source_time, self._source_time_at = quote.time, self._quote_at

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
        fresh: list[fl.Decision] = []
        if reload:
            if stale and len(closes):
                # Only candles not decided before are held back. What was decided, and maybe
                # announced, while the feed was fine stays as it was decided.
                undecided = closes.iloc[-1:] if first_load else closes[closes > self._announced]
                self._stale_times.update(int(at) for at in undecided)
            decisions = fl.replay(
                frame, self.timeframe, REPLAY, closed_hours=self._closed_hours, stale_times=self._stale_times
            )
            self._decisions = decisions
            self._last_sent = next((d for d in reversed(self._decisions) if d.sent), None)
            self._outcomes.clear()
            if first_load:
                self._announced = self._last_closed(frame) + self._step
                self._alert("ok", "started", email=True)
            else:
                # Candles were missed. Every decision not looked at yet is looked at now: a
                # signal lives for two candles, so the one before the newest can still be valid.
                fresh = [d for d in decisions if (d.result.signal_time or 0) > self._announced]
                self._alert("warn", "reloaded")
            holes = len(cd.find_gaps(frame, self.timeframe))
            if holes != self._holes:
                # The source's own history lacks candles. Said once, not on every cycle.
                self._holes = holes
                if holes:
                    self._alert("warn", "history_gaps", count=holes)
            self._version += 1
        else:
            new = closes[closes > before + self._step]
            if len(new):
                if stale:
                    self._stale_times.update(int(at) for at in new)
                fresh = fl.replay(
                    frame,
                    self.timeframe,
                    len(new),
                    previous=self._last_sent,
                    closed_hours=self._closed_hours,
                    stale_times=self._stale_times,
                )
                self._decisions = (self._decisions + fresh)[-KEEP_DECISIONS:]
                for decision in fresh:
                    if decision.sent:
                        self._last_sent = decision
                self._version += 1
        if self._stale_times:
            oldest = int(frame["time"].iloc[0])
            self._stale_times = {at for at in self._stale_times if at > oldest}

        now = self.now()
        for decision in fresh:
            at = decision.result.signal_time or 0
            if at <= self._announced:
                continue
            self._announced = at
            if decision.sent and fl.signal_state(decision, None, now) != fl.EXPIRED:
                self._announce(decision)

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
                "poll_seconds": self._poll_seconds,  # ticks come this often: the page's "lost" limit follows it
                "rules": {
                    "status": "experimental",
                    "filters_off": [fl.NEWS_WINDOW],
                    "market_hours": "approximate",
                },
            }

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
