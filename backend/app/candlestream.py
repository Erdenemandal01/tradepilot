"""Live candle streams for the chart: one thread per origin timeframe that somebody is watching.

The chart (backend/app/chart.py) asks for a stream with ``watch`` each time
a page's chart stream turns. The first ask starts a thread for that origin
timeframe; a thread that nobody has asked for during ``idle_seconds`` ends.
Each thread opens the source's stream, tells the chart it is connected (the
chart then refills its candles over REST, so that what was missed while
disconnected is there, once and in order), and hands every candle update to
the chart. When the stream breaks, it is opened again after a growing pause
(1, 2, 5, 10, 30 s), and a refusal by the source is waited out. The pause
starts again at 1 s only after a connection that stayed up for a minute: one
that breaks soon after its first update (a middlebox that cuts it, a message
that cannot be read) waits longer each time, like one that never opened.

Nothing here touches the signal path: the live feed polls closed candles over
REST and never reads a stream. When a stream is down the chart polls as it
did before; a stream only makes it faster.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from backend.app.datasource.base import CandleStream, DataSource, DataSourceError, StreamCandle

log = logging.getLogger("tradepilot.chart")

PAUSES = (1.0, 2.0, 5.0, 10.0, 30.0)  # seconds before the 1st, 2nd, ... attempt after a failure
IDLE_SECONDS = 30.0  # a stream nobody has watched for this long is closed
READ_TIMEOUT = 1.0  # seconds one read waits; also how soon a thread notices it should stop
SILENT_SECONDS = 120.0  # no candle for this long on an open stream: it is opened again
STABLE_SECONDS = 60.0  # a connection that delivered and stayed up this long starts the pauses again at the first
REFUSED = (418, 429)
REFUSED_WAIT = 60.0  # seconds after a refusal that named no wait
MAX_WAIT = 3600.0


@dataclass
class _Worker:
    timeframe: str
    stop: threading.Event = field(default_factory=threading.Event)
    watched_at: float = 0.0
    thread: threading.Thread | None = None
    stream: CandleStream | None = None
    connected: bool = False
    leaving: bool = False  # the thread has decided to end: a new watch starts a new one
    connects: int = 0
    candles: int = 0
    last_error: str | None = None


class StreamManager:
    """Starts, feeds and stops the live candle streams of one price source."""

    def __init__(
        self,
        source: DataSource,
        on_candle: Callable[[str, StreamCandle], None],
        on_connect: Callable[[str], None],
        on_disconnect: Callable[[str], None],
        *,
        idle_seconds: float = IDLE_SECONDS,
        pauses: tuple[float, ...] = PAUSES,
        read_timeout: float = READ_TIMEOUT,
        silent_seconds: float = SILENT_SECONDS,
        stable_seconds: float = STABLE_SECONDS,
        timer: Callable[[], float] = time.monotonic,
    ) -> None:
        self._source = source
        self._on_candle = on_candle
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        self._idle = float(idle_seconds)
        self._pauses = tuple(float(p) for p in pauses) or (1.0,)
        self._read_timeout = float(read_timeout)
        self._silent = float(silent_seconds)
        self._stable = float(stable_seconds)
        self._timer = timer
        self._timeframes = frozenset(getattr(source, "stream_timeframes", ()) or ())
        self._lock = threading.Lock()
        self._workers: dict[str, _Worker] = {}
        self._closed = False

    def can_stream(self, timeframe: str) -> bool:
        return timeframe in self._timeframes

    def watch(self, timeframe: str) -> bool:
        """Somebody looks at ``timeframe`` now. Starts its stream when none runs. False: no stream for it."""
        if not self.can_stream(timeframe):
            return False
        with self._lock:
            if self._closed:
                return False
            worker = self._workers.get(timeframe)
            if worker is None or worker.leaving or worker.thread is None or not worker.thread.is_alive():
                worker = _Worker(timeframe, watched_at=self._timer())
                worker.thread = threading.Thread(
                    target=self._run, args=(worker,), name=f"chart-stream-{timeframe}", daemon=True
                )
                self._workers[timeframe] = worker
                worker.thread.start()
            worker.watched_at = self._timer()
        return True

    def connected(self, timeframe: str) -> bool:
        with self._lock:
            worker = self._workers.get(timeframe)
            return bool(worker is not None and worker.connected)

    def status(self) -> dict[str, dict[str, object]]:
        """For tests and the log: each running stream's state."""
        with self._lock:
            return {
                tf: {"connected": w.connected, "connects": w.connects, "candles": w.candles, "last_error": w.last_error}
                for tf, w in self._workers.items()
            }

    def threads(self) -> list[threading.Thread]:
        with self._lock:
            return [w.thread for w in self._workers.values() if w.thread is not None and w.thread.is_alive()]

    def close(self, timeout: float = 5.0) -> None:
        """Stop every stream and wait for the threads, at most ``timeout`` seconds in all.

        A thread that reads wakes within one read timeout. One that is still
        opening its connection (a TCP connect or handshake that hangs) cannot
        be woken: it is a daemon thread, ends by itself when the open returns,
        and never holds the process at exit.
        """
        with self._lock:
            self._closed = True
            workers = list(self._workers.values())
        for worker in workers:
            worker.stop.set()
            stream = worker.stream
            if stream is not None:
                try:
                    stream.close()  # wakes a read that is waiting
                except Exception:  # noqa: BLE001 - closing must not fail the shutdown
                    pass
        deadline = time.monotonic() + timeout
        for worker in workers:
            if worker.thread is not None:
                worker.thread.join(max(deadline - time.monotonic(), 0.0))

    # --- one thread per timeframe ---------------------------------------------------------

    def _idle_now(self, worker: _Worker) -> bool:
        return self._timer() - worker.watched_at > self._idle

    def _pause(self, failures: int, error: DataSourceError | None) -> float:
        wait = self._pauses[min(failures, len(self._pauses) - 1)]
        if error is not None and error.status in REFUSED:
            asked = error.retry_after if error.retry_after and error.retry_after > 0 else REFUSED_WAIT
            wait = max(wait, min(float(asked), MAX_WAIT))
        return wait

    def _run(self, worker: _Worker) -> None:
        failures = 0  # attempts in a row that failed, or connections that did not stay up and deliver
        tf = worker.timeframe
        try:
            while not worker.stop.is_set() and not self._idle_now(worker):
                error: DataSourceError | None = None
                try:
                    stream = self._source.open_candle_stream(tf)
                except DataSourceError as failed:
                    error = failed
                except Exception as failed:  # noqa: BLE001 - a bug in a source must not end the thread silently
                    log.exception("chart stream %s: opening failed", tf)
                    error = DataSourceError(f"{type(failed).__name__}: {failed}")
                else:
                    opened = self._timer()
                    delivered = self._read(worker, stream)
                    if delivered and self._timer() - opened >= self._stable:
                        failures = 0  # a connection that worked for a while: the next pause is the first again
                    error = worker_error(worker)
                if worker.stop.is_set() or self._idle_now(worker):
                    break
                wait = self._pause(failures, error)
                failures += 1
                if error is not None:
                    worker.last_error = str(error)
                    log.info("chart stream %s: %s; again in %.0f s", tf, error, wait)
                if worker.stop.wait(wait):
                    break
        finally:
            with self._lock:
                worker.leaving = True
                if self._workers.get(tf) is worker:
                    del self._workers[tf]

    def _read(self, worker: _Worker, stream: CandleStream) -> bool:
        """Read one connection until it breaks, goes silent, nobody watches, or the thread is stopped."""
        tf = worker.timeframe
        delivered = False
        worker.stream = stream
        worker.last_error = None
        if worker.stop.is_set():  # close() came between opening and here
            stream.close()
            worker.stream = None
            return False
        worker.connected = True
        worker.connects += 1
        try:
            try:
                self._on_connect(tf)  # the chart refills over REST before the first update is laid
            except Exception:  # noqa: BLE001
                log.exception("chart stream %s: the refill after connecting failed", tf)
            heard = self._timer()
            while not worker.stop.is_set() and not self._idle_now(worker):
                candle = stream.read(self._read_timeout)
                now = self._timer()
                if candle is None:
                    if now - heard > self._silent:
                        raise DataSourceError(f"no candle for {self._silent:.0f} s")
                    continue
                heard = now
                delivered = True
                worker.candles += 1
                try:
                    self._on_candle(tf, candle)
                except Exception:  # noqa: BLE001 - one bad update must not end the stream
                    log.exception("chart stream %s: an update could not be laid", tf)
        except DataSourceError as error:
            worker.last_error = str(error)
        except Exception as error:  # noqa: BLE001 - a bug in reading must not end the thread and skip the pauses
            log.exception("chart stream %s: reading failed", tf)
            worker.last_error = f"{type(error).__name__}: {error}"
        finally:
            worker.connected = False
            worker.stream = None
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                self._on_disconnect(tf)
            except Exception:  # noqa: BLE001
                log.exception("chart stream %s: after the disconnect", tf)
        return delivered


def worker_error(worker: _Worker) -> DataSourceError | None:
    return DataSourceError(worker.last_error) if worker.last_error else None
