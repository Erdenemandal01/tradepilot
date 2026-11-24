"""Candles for the chart, in whatever length the page asks for.

The chart's timeframe is a way of looking at prices, nothing more. The rule
keeps running on the signal timeframe and its H1 filter whatever is shown.

Where the candles of a chart timeframe come from (its "origin"):

- the signal timeframe: the live feed's memory, so the chart shows exactly
  the candles the rule ran on, with the live stream's newer state of the
  forming candle laid over them (never over a closed one);
- a length the price source has itself (``DataSource.chart_timeframes``):
  asked from the source and kept here, refreshed with a short tail request,
  and moved by the source's live stream while one runs;
- any other length: built from the longest shorter candles that fill it
  exactly (``core.candles.resample``), for example M45 from M15 and MN12
  from MN1.

One origin per timeframe, history and live alike: the live updates of a
timeframe (``live``) come from the same candles as its history (``candles``).

The live stream (backend/app/candlestream.py) only makes the chart faster.
While it is fresh, the REST tail refresh of its origin is skipped; when it
has been silent for longer than the refresh interval, or is down, REST works
exactly as without it. After every (re)connect the origin is refilled over
REST. Nothing here touches the signal path: a failure in this module costs a
chart, never a signal. No candle is made up: when the source gives nothing,
the request fails and the page says so.
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, NamedTuple, Protocol

import pandas as pd

from backend.app.candlestream import StreamManager
from backend.app.core import candles as cd
from backend.app.datasource.base import DataSource, DataSourceError, StreamCandle

log = logging.getLogger("tradepilot.chart")

TAIL = 3  # candles asked for on a refresh: the forming one and two before it
# A hole among this many newest candles is asked for again each time a candle closes: the
# source may list a candle late, and the short tail would never reach it.
HOLE_REACH = 100
REFUSED = (418, 429)
# How many candles a full load asks for. Long candles need fewer: the request
# is cheaper, and the instrument's history ends long before 1500 weeks.
ASK = {"D1": 1000, "W1": 499, "MN1": 240}
LIVE_BARS = 3  # bars in a live event: the forming one and two before it
STREAM_LIVE = 10.0  # seconds: a stream that delivered within this is the chart's "stream" mode
CONFIRM_AFTER = 2.0  # seconds after the stream closed a candle without saying so: REST confirms it
CONFIRM_TRIES = 5
SIGNAL_BARS = 4  # stream candles kept for the signal timeframe: the feed's newest, the next one, and two for a feed that is behind
NO_SHORTER = "no_shorter_candles"  # the source's shortest candle is longer than this timeframe
CANNOT_BUILD = "cannot_build"  # no candle of the source fills this one exactly


class ChartUnavailable(RuntimeError):
    """The source gave no candles for the chart; ``retry_after`` seconds until it is asked again."""

    def __init__(self, retry_after: float) -> None:
        super().__init__("the price source gave no candles")
        self.retry_after = retry_after


class Feed(Protocol):
    timeframe: str

    def frame(self) -> pd.DataFrame | None: ...


@dataclass
class _Kept:
    """What is kept of one origin timeframe asked from the source."""

    frame: pd.DataFrame | None = None
    fetched_at: float = 0.0  # timer: the last REST answer that was used
    short: bool = False  # the source had fewer candles than asked for: nothing older exists
    retry_at: float = 0.0
    fetch: threading.Lock = field(default_factory=threading.Lock)  # one REST request at a time
    rev: int = 0  # moves when candles older than the newest three changed
    at: float | None = None  # source time of the newest confirmation of the candles (UTC seconds)
    seen_at: float | None = None  # timer: when that confirmation arrived
    stream_at: float | None = None  # timer: the last stream update laid (None: no stream, or it went down)
    dirty: bool = False  # the stream found a hole: REST must refill before the stream is trusted again
    # Open time of a candle that is closed in ``frame`` although neither the exchange's flag nor a REST
    # answer listing a newer candle said so (the stream's next candle closed it). Its values may lack the
    # last trades: it is sent as not closed (``closed: false``) until REST confirms it.
    confirm: int | None = None
    confirm_at: float = 0.0
    confirm_tries: int = 0


@dataclass
class _Signal:
    """The signal timeframe: the feed's candles as last seen, and the stream's newer bars."""

    frame: pd.DataFrame | None = None
    rev: int = 0
    at: float | None = None
    seen_at: float | None = None
    # The stream's newest state of each candle it moved, by open time: the newest few only. A candle
    # the exchange closed keeps its final values here until the feed has polled past it.
    bars: dict[int, dict[str, Any]] = field(default_factory=dict)
    newest: int | None = None  # open time of the stream's newest candle
    stream_at: float | None = None
    event_time: float | None = None
    version: int = 0  # moves whenever ``bars`` changed
    laid: tuple | None = None  # (feed frame, version, delivering, the frame laid from them): the last overlay


class _View(NamedTuple):
    frame: pd.DataFrame
    short: bool
    rev: int
    at: float | None
    seen_at: float | None
    streaming: bool  # the stream delivered within STREAM_LIVE and its candles are the ones shown
    confirm: int | None = None  # a candle closed without confirmation (see ``_Kept.confirm``)


@dataclass
class _Subscriber:
    loop: asyncio.AbstractEventLoop
    wake: asyncio.Event
    end: asyncio.Event
    origin: str | None = None  # woken by changes of this origin's candles only (None: by every change)


def _bars(frame: pd.DataFrame, timeframe: str | None = None, confirm: int | None = None) -> list[dict[str, Any]]:
    """Bars for the page. A bar that holds the candle ``confirm`` (closed without confirmation) is sent as not closed."""
    bars = [
        {
            "time": int(row.time),
            "open": float(row.open),
            "high": float(row.high),
            "low": float(row.low),
            "close": float(row.close),
            "volume": None if math.isnan(row.volume) else float(row.volume),
            "closed": bool(row.closed),  # False: the candle is still forming, or its final values are not confirmed yet
        }
        for row in frame.itertuples()
    ]
    if confirm is not None and timeframe is not None:
        for bar in reversed(bars):  # it is one of the newest
            if bar["time"] > confirm:
                continue
            if confirm < _next_open(bar["time"], timeframe):
                bar["closed"] = False
            break
    return bars


def _drop_forming(sig: _Signal) -> None:
    """Forget the stream's forming states (a connection ended or began); the exchange's final values stay."""
    sig.bars = {start: bar for start, bar in sig.bars.items() if bar["closed"]}
    sig.newest = max(sig.bars) if sig.bars else None
    sig.version += 1


def _next_open(time_: int, timeframe: str) -> int:
    return int(cd.following(pd.Series([int(time_)]), timeframe).iloc[0])


def _volume_ahead(frame: pd.DataFrame, bar: dict[str, Any]) -> bool:
    """Whether the frame's newest candle has more volume traded than the stream's state of the same candle.

    Volume only grows while a candle forms, so the frame's state is then the
    newer one (a stream that stalled while trades went on). Unknown volumes
    decide nothing.
    """
    mine, theirs = float(frame["volume"].iloc[-1]), bar.get("volume")
    if theirs is None or math.isnan(mine) or math.isnan(float(theirs)):
        return False
    return mine > float(theirs)


def _length(timeframe: str) -> int:
    """A candle's length in seconds; a month counts as 31 days (only used to order lengths)."""
    months = cd.MONTH_TIMEFRAMES.get(timeframe)
    return months * 31 * 86400 if months else cd.timeframe_seconds(timeframe)


def _parts(origin: str, timeframe: str) -> int:
    """At most how many ``origin`` candles fill one ``timeframe`` candle."""
    return max(1, -(-_length(timeframe) // _length(origin)))


class ChartCandles:
    def __init__(
        self,
        source: DataSource,
        feed: Feed,
        *,
        refresh_seconds: float = 2.0,
        history: int = 1500,
        timer: Callable[[], float] = time.monotonic,
        clock: Callable[[], float] = time.time,
        stream: bool = False,
        stream_options: dict[str, Any] | None = None,
    ) -> None:
        self._source = source
        self._feed = feed
        self._refresh = max(float(refresh_seconds), 0.5)
        self._history = int(history)
        self._timer = timer
        self._clock = clock
        self._native = tuple(tf for tf in getattr(source, "chart_timeframes", ()) if tf in cd.CHART_TIMEFRAMES)
        self._kept: dict[str, _Kept] = {}
        self._signal = _Signal()
        self._state = threading.RLock()  # every change of what is kept; held only for short pure work
        self._origins = {tf: self._origin(tf) for tf in cd.CHART_TIMEFRAMES}
        self._gap_cache: dict[str, tuple[tuple, int]] = {}
        self._subscribers: dict[asyncio.Event, _Subscriber] = {}
        self._ending = False
        self._streams: StreamManager | None = None
        if stream and getattr(source, "stream_timeframes", ()):
            self._streams = StreamManager(
                source, self._on_candle, self._on_connect, self._on_disconnect, timer=timer, **(stream_options or {})
            )

    def _origin(self, timeframe: str) -> str | None:
        """The candles a chart timeframe is read or built from; None when the source cannot give it."""
        base = self._feed.timeframe
        if timeframe == base or timeframe in self._native:
            return timeframe
        for candidate in reversed(cd.CHART_TIMEFRAMES):  # the longest that fits: the fewest candles to combine
            if (candidate == base or candidate in self._native) and cd.can_resample(candidate, timeframe):
                return candidate
        return None

    # --- what the page is told about the menu ---------------------------------------------------------

    def timeframes(self) -> list[str]:
        """The chart timeframes this source can show, shortest first."""
        return [tf for tf in cd.CHART_TIMEFRAMES if self._origins[tf] is not None]

    def shortest(self) -> str:
        """The source's shortest candle (its own or the signal timeframe's)."""
        own = [tf for tf in cd.CHART_TIMEFRAMES if tf == self._feed.timeframe or tf in self._native]
        return min(own, key=_length)

    def missing(self) -> dict[str, str]:
        """Why each chart timeframe this source cannot show is missing, as a reason code."""
        shortest = _length(self.shortest())
        return {
            tf: NO_SHORTER if _length(tf) < shortest else CANNOT_BUILD
            for tf in cd.CHART_TIMEFRAMES
            if self._origins[tf] is None
        }

    def stream_info(self) -> dict[str, Any]:
        """Whether a live candle stream exists for this source, and which chart timeframes it moves."""
        if self._streams is None:
            return {"available": False, "timeframes": []}
        return {
            "available": True,
            "timeframes": [tf for tf in self.timeframes() if self._streams.can_stream(self._origins[tf] or "")],
        }

    # --- history (GET /api/candles) ------------------------------------------------------------------------

    def candles(self, timeframe: str, limit: int) -> dict[str, Any]:
        """The newest ``limit`` candles of ``timeframe`` and what is known about their history.

        Raises ValueError for a timeframe the source cannot show and
        ChartUnavailable when the source gives nothing right now (an empty
        answer included: it is never shown as an empty chart).
        """
        origin = self._origins.get(timeframe)
        if origin is None:
            raise ValueError(f"timeframe {timeframe!r} is not available from this source")
        view = self._view(origin)
        frame, partial = view.frame, False
        if origin != timeframe:
            frame, partial = cd.resample(frame, origin, timeframe, history_starts=view.short)
        whole = len(frame) <= limit  # nothing older was cut off by the limit
        frame = frame.iloc[-limit:]
        return {
            "timeframe": timeframe,
            "source": self._source.name,
            "symbol": self._source.symbol,
            "built_from": None if origin == timeframe else origin,
            # "short": the first candle is the oldest one the source has.
            "history": "short" if view.short and whole else "full",
            # The first candle was built from a bucket whose beginning does not exist.
            "partial_first": bool(partial and whole),
            "gaps": cd.count_breaks(frame, timeframe),
            # Moves when candles older than the newest three changed (see ``live``).
            "rev": view.rev,
            "bars": _bars(frame, timeframe, view.confirm),
        }

    # --- live (GET /api/candles/stream) ---------------------------------------------------------------------

    def watch(self, timeframe: str) -> None:
        """A page shows ``timeframe`` live now: keep its origin's stream running."""
        origin = self._origins.get(timeframe)
        if self._streams is not None and origin is not None:
            self._streams.watch(origin)

    def live(self, timeframe: str) -> dict[str, Any]:
        """One chart stream event: the newest bars of ``timeframe`` and how fresh they are.

        Never raises for a source that gives nothing: the event then has no
        bars and says ``error: "unavailable"``.
        """
        origin = self._origins[timeframe]
        assert origin is not None, timeframe
        try:
            view = self._view(origin)
        except ChartUnavailable as failure:
            with self._state:
                rev, at, seen_at = self._freshness(origin)
            return {
                "timeframe": timeframe,
                "bars": [],
                "mode": self._mode(origin, False),
                "at": at,
                "age": None if seen_at is None else round(max(self._timer() - seen_at, 0.0), 1),
                "rev": rev,
                "gaps": self._gap_cache.get(timeframe, ((), 0))[1],
                "error": "unavailable",
                "retry_after": round(failure.retry_after, 1),
            }
        frame = view.frame
        if origin == timeframe:
            shown = frame.iloc[-LIVE_BARS:]
        else:
            rows = min(len(frame), _parts(origin, timeframe) * (LIVE_BARS + 1))
            built, _ = cd.resample(frame.iloc[-rows:], origin, timeframe, history_starts=view.short and rows == len(frame))
            shown = built.iloc[-LIVE_BARS:]
        return {
            "timeframe": timeframe,
            "bars": _bars(shown, timeframe, view.confirm),
            "mode": self._mode(origin, view.streaming),
            "at": view.at,
            "age": None if view.seen_at is None else round(max(self._timer() - view.seen_at, 0.0), 1),
            "rev": view.rev,
            "gaps": self._gaps(timeframe, origin, view),
            "error": None,
            "retry_after": None,
        }

    def _mode(self, origin: str, streaming: bool) -> str:
        return "stream" if streaming and self._streams is not None and self._streams.connected(origin) else "poll"

    def _gaps(self, timeframe: str, origin: str, view: _View) -> int:
        """Breaks in the whole kept history of ``timeframe``; rebuilt only when the candles' shape changed."""
        frame = view.frame
        key = (len(frame), int(frame["time"].iloc[0]), int(frame["time"].iloc[-1]), view.rev, view.short)
        cached = self._gap_cache.get(timeframe)
        if cached is not None and cached[0] == key:
            return cached[1]
        built = frame if origin == timeframe else cd.resample(frame, origin, timeframe, history_starts=view.short)[0]
        gaps = cd.count_breaks(built, timeframe)
        self._gap_cache[timeframe] = (key, gaps)
        return gaps

    def _freshness(self, origin: str) -> tuple[int, float | None, float | None]:
        if origin == self._feed.timeframe:
            sig = self._signal
            return sig.rev, sig.at, sig.seen_at
        kept = self._kept.get(origin) or _Kept()
        return kept.rev, kept.at, kept.seen_at

    # --- the origin's candles ---------------------------------------------------------------------------------

    def _view(self, origin: str) -> _View:
        if origin == self._feed.timeframe:
            return self._signal_view()
        frame, short = self._from_source(origin)
        now = self._timer()
        with self._state:
            kept = self._kept[origin]
            streaming = kept.stream_at is not None and now - kept.stream_at < STREAM_LIVE
            return _View(frame, short, kept.rev, kept.at, kept.seen_at, streaming, kept.confirm)

    def _signal_view(self) -> _View:
        """The feed's own candles, with the stream's newer state of its newest candle laid over them.

        The rule ran on the feed's candles, so they are what is shown. The
        stream may add: its newer state of the feed's newest candle, that
        candle's final values once the exchange closed it, and the candle
        after it, but only once the one before is closed (in the feed, or by
        the exchange's flag): the next candle never closes a candle the feed
        still has as forming. Nothing further ahead is laid. The stream counts
        for the freshness (``at``, ``age``, mode "stream") only while its
        newest candle is the newest one shown; otherwise the event tells the
        feed's own freshness, so a stuck feed is never passed off as live.
        """
        feed = self._feed.frame()
        now = self._timer()
        with self._state:
            sig = self._signal
            if feed is not sig.frame:
                if feed is not None and not feed.empty:
                    if cd.older_changed(sig.frame, feed):
                        sig.rev += 1
                    sig.at, sig.seen_at = self._clock(), now  # a new answer: the feed's cycle succeeded
                sig.frame = feed
            if feed is None or feed.empty:
                raise ChartUnavailable(self._refresh)  # before the feed's first answer: nothing to show yet
            at, seen_at = sig.at, sig.seen_at
            newest = int(feed["time"].iloc[-1])
            old = [t for t in sig.bars if t < newest]
            for time_ in old:
                del sig.bars[time_]  # the feed has polled past it: its own candle is what the rule ran on
            if old:
                sig.version += 1
            # A forming state counts only while the stream delivers; a closed one is final.
            delivering = sig.stream_at is not None and now - sig.stream_at < STREAM_LIVE
            cached = sig.laid
            if cached is not None and cached[0] is feed and cached[1:3] == (sig.version, delivering):
                frame = cached[3]  # nothing changed since: an open page asks several times a second
            else:
                frame = self._lay_signal(feed, sig.bars, delivering)
                sig.laid = (feed, sig.version, delivering, frame)
            in_step = sig.newest is not None and int(frame["time"].iloc[-1]) == sig.newest
            streaming = in_step and delivering
            if streaming:
                if sig.event_time is not None and (at is None or sig.event_time > at):
                    at = sig.event_time
                seen_at = max(seen_at or 0.0, sig.stream_at or 0.0)
            return _View(frame, False, sig.rev, at, seen_at, streaming)  # the feed keeps a window, not the whole history

    def _lay_signal(self, feed: pd.DataFrame, bars: dict[int, dict[str, Any]], delivering: bool) -> pd.DataFrame:
        """The stream's candles laid over the feed's, by the rules of ``_signal_view``."""
        tf = self._feed.timeframe
        frame = feed
        following = _next_open(int(feed["time"].iloc[-1]), tf)
        for time_ in sorted(bars):
            if time_ > following:
                break  # never more than one candle past the feed's own
            bar = bars[time_]
            last_closed = bool(frame["closed"].iloc[-1])
            if not bar["closed"]:
                if not delivering:
                    break
                if time_ == int(frame["time"].iloc[-1]) and _volume_ahead(frame, bar):
                    break  # the feed's own state of this candle is newer than the stream's (more volume traded)
            if time_ == following and not last_closed:
                break  # the feed's newest is still forming: the feed or the exchange's flag must close it first
            try:
                laid, outcome = cd.lay_stream(frame, bar, tf, keep_closed=True)
            except ValueError:
                break
            if outcome in (cd.STREAM_SAME, cd.STREAM_NEXT):
                frame = laid
        return frame

    def _ask(self, timeframe: str) -> int:
        return min(self._history, ASK.get(timeframe, self._history))

    def _fetch(self, timeframe: str, limit: int) -> pd.DataFrame:
        """The source's newest candles. An answer without a single candle is not data: it is a failure."""
        frame = self._source.fetch_candles(timeframe, limit)
        if frame.empty:
            raise DataSourceError(f"the source answered a request for {limit} {timeframe} candles with none")
        return frame

    def _heal(self, timeframe: str, before: pd.DataFrame, frame: pd.DataFrame, ask: int) -> pd.DataFrame:
        """When a candle has closed and a young hole is kept: one longer tail that reaches it.

        The longer request is an extra. When it fails, or what it brings does
        not fit, the chart keeps what the usual tail gave. Only a refusal
        (asked to slow down) is passed on, so that the source is left alone.
        """
        if len(frame) < 2 or len(before) < 2 or int(frame["time"].iloc[-1]) == int(before["time"].iloc[-1]):
            return frame
        reach = cd.hole_reach(frame, timeframe, min(HOLE_REACH, ask))
        if reach <= TAIL:
            return frame
        try:
            healed, news = cd.lay_over(frame, self._fetch(timeframe, reach), timeframe)
        except ValueError:
            return frame
        except DataSourceError as error:
            if error.status in REFUSED:
                raise
            return frame
        if healed is None or news:
            return frame
        return healed.iloc[-ask:].reset_index(drop=True)

    def _kept_for(self, timeframe: str) -> _Kept:
        with self._state:
            return self._kept.setdefault(timeframe, _Kept())

    def _from_source(self, timeframe: str, *, force: bool = False) -> tuple[pd.DataFrame, bool]:
        """The kept candles of a source timeframe, refreshed over REST when that is due.

        Due: nothing kept yet; ``force`` (the stream has just connected); the
        stream found a hole or closed a candle without the exchange's flag;
        or the refresh interval has passed and the stream is not fresh. A
        stream that keeps breaking cannot make the forced refills hammer the
        source: its reconnects wait longer each time (candlestream.PAUSES).
        """
        kept = self._kept_for(timeframe)
        with kept.fetch:  # one request at a time per timeframe, however many pages ask
            now = self._timer()
            with self._state:
                frame, short = kept.frame, kept.short
                fresh = kept.stream_at is not None and now - kept.stream_at < self._refresh
                confirm = kept.confirm is not None and now >= kept.confirm_at
                needed = kept.dirty or confirm
            if frame is not None and fresh and not needed and not force:
                return frame, short  # the stream keeps it current: no REST request
            if now < kept.retry_at:
                if frame is not None and fresh:
                    return frame, short
                raise ChartUnavailable(kept.retry_at - now)
            if frame is not None and now - kept.fetched_at < self._refresh and not force:
                return frame, short  # a refill the stream asks for waits for the interval too: no hammering
            ask = self._ask(timeframe)
            try:
                if frame is None:
                    loaded = self._fetch(timeframe, ask)
                    return self._store(kept, timeframe, loaded, ask, int(loaded["time"].iloc[-1]), short=len(loaded) < ask)
                tail = self._fetch(timeframe, min(TAIL, ask))
                with self._state:
                    current = kept.frame
                    try:
                        merged = cd.lay_rest(current, tail, timeframe, keep=ask)
                    except ValueError:
                        merged = None  # the two do not fit together: load the whole history again
                if merged is not None:
                    merged = self._heal(timeframe, current, merged, ask)
                    return self._store(kept, timeframe, merged, ask, int(tail["time"].iloc[-1]))
                loaded = self._fetch(timeframe, ask)
                return self._store(kept, timeframe, loaded, ask, int(loaded["time"].iloc[-1]), short=len(loaded) < ask)
            except DataSourceError as error:
                if error.status in REFUSED:  # asked to slow down: stay away, as the live feed does
                    wait = min(float(error.retry_after), 3600.0) if error.retry_after and error.retry_after > 0 else 60.0
                else:
                    wait = 5.0
                kept.retry_at = self._timer() + wait
                with self._state:
                    if kept.frame is not None and kept.stream_at is not None and self._timer() - kept.stream_at < self._refresh:
                        return kept.frame, kept.short  # the stream still carries it: the failed refresh costs nothing
                raise ChartUnavailable(wait) from None

    def _store(
        self,
        kept: _Kept,
        timeframe: str,
        frame: pd.DataFrame,
        ask: int,
        rest_end: int,
        *,
        short: bool | None = None,
    ) -> tuple[pd.DataFrame, bool]:
        """Keep a REST answer. Stream updates that came while it was asked for are laid on it again.

        ``rest_end`` is the open time of the newest candle the REST answer
        itself listed. A candle that is kept closed at the time of the
        answer's forming newest one is not reopened by it: a copy of the
        source that lags still lists it as forming, with values from before
        its close.
        """
        with self._state:
            current = kept.frame
            if current is not None and len(current) and len(frame):
                last = int(frame["time"].iloc[-1])
                at_last = current[current["time"] == last]
                start = last + 1  # the stream moved on while REST answered: keep that
                if len(at_last) and bool(at_last["closed"].iloc[0]) and not bool(frame["closed"].iloc[-1]):
                    start = last
                for row in current[current["time"] >= start].itertuples():
                    try:
                        laid, outcome = cd.lay_stream(frame, row._asdict(), timeframe)
                    except ValueError:
                        break
                    if outcome not in (cd.STREAM_SAME, cd.STREAM_NEXT):
                        break
                    frame = laid
            frame = frame.iloc[-ask:].reset_index(drop=True)
            if cd.older_changed(current, frame):
                kept.rev += 1
            kept.frame = frame
            if short is not None:
                kept.short = short
            elif len(frame) >= ask:
                kept.short = False  # the oldest candles have been cut off since
            kept.fetched_at = kept.seen_at = self._timer()
            kept.at = self._clock()
            kept.dirty = False
            if kept.confirm is not None:
                if rest_end > kept.confirm:
                    kept.confirm = None  # REST listed a newer candle: the closed one has its final values now
                else:
                    kept.confirm_tries += 1
                    kept.confirm_at = self._timer() + CONFIRM_AFTER
                    if kept.confirm_tries >= CONFIRM_TRIES:
                        kept.confirm = None
            result = (kept.frame, kept.short)
        self._notify(timeframe)
        return result

    # --- what the stream hands over (on its own thread) -------------------------------------------------------

    def _on_connect(self, origin: str) -> None:
        """A stream (re)connected: refill over REST so that what was missed is there, in order, once."""
        if origin == self._feed.timeframe:
            with self._state:
                _drop_forming(self._signal)  # the feed has its own candles; only final values stay
            return
        try:
            self._from_source(origin, force=True)
        except ChartUnavailable:
            with self._state:
                self._kept_for(origin).dirty = True  # the next update tries again, after the source's wait

    def _on_disconnect(self, origin: str) -> None:
        with self._state:
            if origin == self._feed.timeframe:
                _drop_forming(self._signal)
                self._signal.stream_at = None
            elif origin in self._kept:
                self._kept[origin].stream_at = None  # REST takes over at once
        self._notify(origin)

    def _on_candle(self, origin: str, candle: StreamCandle) -> None:
        bar = candle.bar()
        now = self._timer()
        if origin == self._feed.timeframe:
            with self._state:
                sig = self._signal
                start = int(bar["time"])
                if sig.newest is not None and start < sig.newest:
                    return  # an update older than the newest one: nothing new
                kept = sig.bars.get(start)
                if kept is None or not kept["closed"] or bar["closed"]:
                    sig.bars[start] = bar  # a candle the exchange closed is not reopened by a late update
                sig.newest = start
                for extra in sorted(sig.bars)[:-SIGNAL_BARS]:
                    del sig.bars[extra]
                sig.version += 1
                sig.stream_at, sig.event_time = now, candle.event_time
            self._notify(origin)
            return
        if self._lay(origin, bar, candle.event_time) == cd.STREAM_HOLE:
            try:
                self._from_source(origin)  # refill over REST (no more often than the refresh interval)
            except ChartUnavailable:
                return
            self._lay(origin, bar, candle.event_time)

    def _lay(self, origin: str, bar: dict[str, Any], event_time: float) -> str:
        kept = self._kept_for(origin)
        with self._state:
            if kept.frame is None or kept.frame.empty:
                kept.dirty = True
                return cd.STREAM_HOLE
            closed_before = bool(kept.frame["closed"].iloc[-1])
            try:
                frame, outcome = cd.lay_stream(kept.frame, bar, origin)
            except ValueError as error:
                log.info("chart stream %s: an update that is not a candle: %s", origin, error)
                return cd.STREAM_OLD
            if outcome == cd.STREAM_HOLE:
                kept.dirty = True
                return outcome
            now = self._timer()
            kept.stream_at, kept.seen_at, kept.at = now, now, event_time
            if outcome in (cd.STREAM_SAME, cd.STREAM_NEXT):
                if outcome == cd.STREAM_NEXT and not closed_before:
                    # The candle before was closed by the next one, not by the exchange's flag: its
                    # values may lack the last trades. A REST tail confirms them shortly.
                    kept.confirm = int(frame["time"].iloc[-2])
                    kept.confirm_at, kept.confirm_tries = now + CONFIRM_AFTER, 0
                kept.frame = frame.iloc[-self._ask(origin) :].reset_index(drop=True)
        self._notify(origin)
        return outcome

    # --- open chart streams, shutdown ---------------------------------------------------------------------------

    def subscribe(
        self, loop: asyncio.AbstractEventLoop, timeframe: str | None = None, *, limit: int | None = None
    ) -> _Subscriber | None:
        """Register an open chart stream of ``timeframe``; None when ``limit`` streams are open already.

        Counted and checked in one step, so streams opened at the same moment
        cannot pass the limit together.
        """
        origin = None if timeframe is None else self._origins.get(timeframe)
        subscriber = _Subscriber(loop, asyncio.Event(), asyncio.Event(), origin)
        with self._state:
            if limit is not None and len(self._subscribers) >= limit:
                return None
            self._subscribers[subscriber.wake] = subscriber
            if self._ending:
                subscriber.end.set()
        return subscriber

    def unsubscribe(self, subscriber: _Subscriber) -> None:
        with self._state:
            self._subscribers.pop(subscriber.wake, None)

    def open_streams(self) -> int:
        with self._state:
            return len(self._subscribers)

    def _notify(self, origin: str | None = None, *, end: bool = False) -> None:
        """Wake the open chart streams whose candles come from ``origin`` (every one when None)."""
        with self._state:
            subscribers = [
                item
                for item in self._subscribers.values()
                if origin is None or item.origin is None or item.origin == origin
            ]
        for item in subscribers:
            try:
                item.loop.call_soon_threadsafe(item.wake.set)
                if end:
                    item.loop.call_soon_threadsafe(item.end.set)
            except RuntimeError:  # that page's event loop is gone
                self.unsubscribe(item)

    @property
    def ending(self) -> bool:
        return self._ending

    def end_streams(self) -> None:
        """Tell every open chart stream to end now (the server is stopping)."""
        self._ending = True
        self._notify(end=True)

    def close(self, timeout: float = 5.0) -> None:
        """Server shutdown: end the chart streams and stop the live candle streams.

        Waits at most ``timeout`` seconds for the stream threads. They are
        daemon threads: one that is still inside a connect when the time is
        up ends by itself and does not hold the process.
        """
        self.end_streams()
        if self._streams is not None:
            self._streams.close(timeout)

    def stream_threads(self) -> list[threading.Thread]:
        return [] if self._streams is None else self._streams.threads()
