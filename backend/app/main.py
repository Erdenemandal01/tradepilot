"""FastAPI application: login, the dashboard pages, the API and the live stream.

Everything except ``/api/health``, the login page and what that page needs
to render requires a valid session cookie. The interactive API docs stay
switched off. There is no route that sends, changes or closes an order.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import queue
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel

from backend.app import __version__, auth
from backend.app.chart import ChartCandles, ChartUnavailable
from backend.app.config import Settings, get_settings
from backend.app.datasource import DataSource, make_source
from backend.app.forecast import Forecaster
from backend.app.live import HIGHER, LiveFeed
from backend.app.notify import Notifier, make_notifier

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
STATIC_DIR = FRONTEND_DIR / "static"
# What the login page needs before anyone has logged in. Nothing else is public.
PUBLIC_STATIC = {"styles.css", "login.js", "favicon.svg"}
MEDIA_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".txt": "text/plain; charset=utf-8",
}
SECURITY_HEADERS = {
    "content-security-policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "cache-control": "no-store",  # prices and the session must never be served from a cache
}
KEEP_ALIVE_SECONDS = 15.0
# The chart's candle stream (GET /api/candles/stream).
CHART_STREAMS_MAX = 8  # open chart streams at once; one more is answered 503
CHART_EVENT_SPACING = 0.2  # seconds: at most about five events a second per page, the newest state wins
CHART_HEARTBEAT = 5.0  # seconds: an event at least this often, so a page can tell "late" from "live"
CHART_CHECK = 0.5  # seconds between looks at the candles when nothing woke the stream

log = logging.getLogger("tradepilot.web")


class DaemonPool:
    """A few daemon threads that run blocking calls for the event loop and hand back the results.

    Not the event loop's default pool: the interpreter waits for those
    threads at exit, and a REST request that hangs would then hold Ctrl-C up
    for its whole timeout. Not a new thread per call either: an open chart
    stream asks several times a second. Threads are started as needed, never
    fewer than the calls waiting (up to ``size``), so one call that waits on
    the source does not hold up another page's.
    """

    def __init__(self, size: int, name: str) -> None:
        self._size = max(int(size), 1)
        self._name = name
        self._jobs: queue.SimpleQueue = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._threads = 0
        self._waiting = 0  # calls handed in and not finished

    def run(self, function: Callable[..., Any], *args: Any) -> asyncio.Future:
        """Run ``function(*args)`` on one of the threads; await the returned future for its result."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        with self._lock:
            self._waiting += 1
            start = self._threads < min(self._size, self._waiting)
            if start:
                self._threads += 1
        if start:
            threading.Thread(target=self._work, name=f"{self._name}-{self._threads}", daemon=True).start()
        self._jobs.put((loop, future, function, args))
        return future

    def _work(self) -> None:
        while True:
            loop, future, function, args = self._jobs.get()
            try:
                result, error = function(*args), None
            except BaseException as failure:  # noqa: BLE001 - handed to the awaiting side
                result, error = None, failure
            with self._lock:
                self._waiting -= 1
            try:
                loop.call_soon_threadsafe(_settle, future, result, error)
            except RuntimeError:  # the loop is gone: the server stopped
                pass


def _settle(future: asyncio.Future, result: Any, error: BaseException | None) -> None:
    if future.done():
        return
    if error is not None:
        future.set_exception(error)
    else:
        future.set_result(result)


class ChartStreamResponse(StreamingResponse):
    """The chart stream's response: its slot among the open chart streams is given back however it ends.

    The slot is taken when the request is accepted (so that streams opened at
    the same moment cannot pass the limit together) and released here, also
    when the stream never starts (the page left before the first byte).
    """

    def __init__(self, content: Any, release: Callable[[], None], **options: Any) -> None:
        super().__init__(content, **options)
        self._release = release

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._release()


class SecurityHeaders:
    """Adds the security headers to every response (plain ASGI, safe for streams).

    An error nothing else handled is answered here as well, so that even a 500
    leaves with the headers.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = False

        async def send_with_headers(message: Any) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                present = {name.lower() for name, _ in message.get("headers", [])}
                extra = [
                    (name.encode(), value.encode())
                    for name, value in SECURITY_HEADERS.items()
                    if name.encode() not in present
                ]
                message = {**message, "headers": [*message.get("headers", []), *extra]}
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        except Exception:
            if started:  # too late to answer: the connection is cut by the server
                raise
            log.exception("unhandled error on %s %r", scope.get("method"), scope.get("path"))
            headers = [(b"content-type", b"text/plain; charset=utf-8")]
            headers += [(name.encode(), value.encode()) for name, value in SECURITY_HEADERS.items()]
            await send({"type": "http.response.start", "status": 500, "headers": headers})
            await send({"type": "http.response.body", "body": b"Internal Server Error"})


class LoginBody(BaseModel):
    password: str


def end_streams_on_exit(*owners: Any) -> Callable[[], None]:
    """End the open streams the moment the server is asked to stop.

    The server waits for open connections before it runs any shutdown code,
    and a dashboard's streams (the feed's ticks, the chart's candles) never
    end by themselves: without this, Ctrl-C would hang for as long as a page
    is open. ``owners`` are the objects whose ``end_streams`` ends them.
    Returns the call that undoes it.
    """
    previous: dict[int, Any] = {}

    def on_signal(number: int, frame: Any) -> None:
        for owner in owners:
            try:
                owner.end_streams()
            except Exception:  # noqa: BLE001 - the server must stop whatever happens here
                log.exception("ending the open streams failed")
        earlier = previous.get(number)
        if callable(earlier):
            earlier(number, frame)  # the server's own handler: it starts the shutdown

    if threading.current_thread() is threading.main_thread():  # signals can only be set from there
        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            number = getattr(signal, name, None)
            if number is not None:
                try:
                    previous[number] = signal.signal(number, on_signal)
                except (ValueError, OSError):
                    pass

    def undo() -> None:
        for number, earlier in previous.items():
            if signal.getsignal(number) is on_signal:
                signal.signal(number, earlier)

    return undo


def plain_static_name(path: str) -> bool:
    """Whether ``path`` is plain ``name`` or ``folder/name`` text.

    Checked before any file call: a backslash, a drive or a network path must
    never reach the file system, where Windows would act on it.
    """
    if not path or any(mark in path for mark in ("\\", ":", "\0")):
        return False
    return all(part not in ("", ".", "..") for part in path.split("/"))


def create_app(
    settings: Settings | None = None,
    *,
    source: DataSource | None = None,
    notifier: Notifier | None = None,
    run_feed: bool | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="email")
    price_source = source or make_source(settings)
    feed = LiveFeed(
        price_source,
        settings,
        notifier if notifier is not None else make_notifier(settings),
        executor=executor,
        # The fitted forecast model, when the training script has written one.
        # Only a model fitted on this very source and instrument is used.
        forecaster=Forecaster.from_file(
            settings.forecast_model_path, settings.signal_timeframe, (price_source.name, price_source.symbol)
        ),
    )
    chart = ChartCandles(
        price_source,
        feed,
        refresh_seconds=settings.poll_seconds,
        history=settings.history_candles,
        # The chart's own live stream. The feed never reads it: signals stay on REST polling.
        stream=settings.chart_stream,
    )
    throttle = auth.Throttle()
    login_lock = threading.Lock()
    secret = settings.session_secret.get_secret_value()
    password_hash = settings.password_hash.get_secret_value()
    # Sessions ended by "log out" before their time, with that time. Kept in memory: a restart of the
    # server forgets them (docs/WEB.md, known limits). Pruned as they run out.
    revoked: dict[str, float] = {}
    revoked_lock = threading.Lock()
    chart_pool = DaemonPool(CHART_STREAMS_MAX, "chart-live")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        stop = threading.Event()
        thread = None
        if settings.live_enabled if run_feed is None else run_feed:
            thread = threading.Thread(target=feed.run, args=(stop,), name="live-feed", daemon=True)
            thread.start()
        undo = end_streams_on_exit(feed, chart)
        try:
            yield
        finally:
            undo()
            stop.set()
            chart.close(timeout=0.5)  # a stream thread stuck in a connect does not hold Ctrl-C up
            feed.close()
            if thread is not None:
                thread.join(timeout=5)
            executor.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(
        title="TradePilot AI",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(SecurityHeaders)
    app.state.feed = feed
    app.state.chart = chart

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError) -> JSONResponse:
        # The default answer repeats what was sent, and on the login route that is
        # the typed password. Say what is wrong and where, never the value.
        details = [{"type": str(item.get("type")), "loc": list(item.get("loc", ()))} for item in error.errors()]
        return JSONResponse({"detail": details}, status_code=422)

    def logged_in(request: Request) -> bool:
        token = request.cookies.get(auth.COOKIE)
        if not auth.token_valid(token, secret, time.time()):
            return False
        with revoked_lock:
            return token not in revoked

    def require_login(request: Request) -> None:
        if not logged_in(request):
            raise HTTPException(status_code=401, detail="login required")

    # --- public ---------------------------------------------------------------

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        """Liveness check: the only API route that needs no login."""
        return {"status": "ok", "version": __version__, "symbol": settings.symbol, **feed.health()}

    @app.post("/api/login")
    def login(body: LoginBody, request: Request) -> Response:
        if not secret or not password_hash:
            raise HTTPException(status_code=503, detail="no password is set on the server")
        # One password is checked at a time. Without the lock, tries sent together
        # would all pass the brake before any of them is counted.
        with login_lock:
            now = time.time()
            wait = throttle.wait(now)
            if wait > 0:
                raise HTTPException(
                    status_code=429, detail="too many attempts", headers={"Retry-After": str(int(wait) + 1)}
                )
            if not auth.verify_password(body.password, password_hash):
                throttle.failed(now)
                raise HTTPException(status_code=401, detail="wrong password")
            throttle.succeeded()
        token = auth.new_token(secret, now, settings.session_hours)
        with revoked_lock:
            # A token names its expiry second: logged out and in again within one second, the new
            # session has the very token that was logged out. The new login counts.
            revoked.pop(token, None)
        response = JSONResponse({"ok": True})
        response.set_cookie(
            auth.COOKIE,
            token,
            max_age=int(settings.session_hours * 3600),
            httponly=True,
            samesite="strict",
            secure=settings.cookie_secure,
            path="/",
        )
        return response

    @app.post("/api/logout")
    def logout(request: Request) -> Response:
        response = JSONResponse({"ok": True})
        # Only the browser that holds the session can end it. A form on another
        # site is sent without the SameSite=Strict cookie and changes nothing.
        if logged_in(request):
            token = request.cookies.get(auth.COOKIE) or ""
            now = time.time()
            with revoked_lock:
                for old in [old for old, until in revoked.items() if until <= now]:
                    del revoked[old]
                # Ended on the server too: a copy of the token, and the streams opened with it, stop working.
                revoked[token] = float(token.split(".", 1)[0])
            response.delete_cookie(
                auth.COOKIE, path="/", httponly=True, samesite="strict", secure=settings.cookie_secure
            )
        return response

    @app.get("/login")
    def login_page(request: Request) -> Response:
        if logged_in(request):
            return RedirectResponse("/", status_code=303)
        return FileResponse(FRONTEND_DIR / "login.html", media_type="text/html; charset=utf-8")

    @app.get("/")
    def dashboard(request: Request) -> Response:
        if not logged_in(request):
            return RedirectResponse("/login", status_code=303)
        return FileResponse(FRONTEND_DIR / "index.html", media_type="text/html; charset=utf-8")

    @app.get("/static/{path:path}")
    def static(path: str, request: Request) -> Response:
        if path not in PUBLIC_STATIC:
            require_login(request)
        if not plain_static_name(path):
            raise HTTPException(status_code=404, detail="not found")
        try:
            target = (STATIC_DIR / path).resolve()
            inside = STATIC_DIR.resolve() in target.parents and target.is_file()
        except (OSError, ValueError):
            inside = False
        if not inside or target.suffix not in MEDIA_TYPES:
            raise HTTPException(status_code=404, detail="not found")
        return FileResponse(target, media_type=MEDIA_TYPES[target.suffix])

    # --- behind login -----------------------------------------------------------

    api = APIRouter(prefix="/api", dependencies=[Depends(require_login)])

    @api.get("/state")
    def state() -> dict[str, Any]:
        return {
            **feed.snapshot(),
            # What the chart can show. Its timeframe is a view: signals stay on
            # the signal timeframe with the higher one as their filter.
            "chart": {
                "timeframes": chart.timeframes(),
                "signal_timeframe": feed.timeframe,
                "filter_timeframe": HIGHER,
                # Each menu timeframe this source cannot show, with the reason as a code.
                "missing": chart.missing(),
                "shortest": chart.shortest(),
                # Whether the chart has a live candle stream here, and which timeframes it moves.
                "stream": chart.stream_info(),
            },
            # The forecast of the next four M15 closes with its measured record, or why there is none.
            "prediction": feed.prediction(),
        }

    @api.get("/quote")
    def quote() -> dict[str, Any]:
        snap = feed.snapshot()
        return {key: snap[key] for key in ("price", "bid", "ask", "now", "feed", "age", "source")}

    @api.get("/candles")
    def candles(timeframe: str = feed.timeframe, limit: int = 500) -> dict[str, Any]:
        if timeframe not in chart.timeframes() or not 1 <= limit <= 1500:
            raise HTTPException(status_code=422, detail="a timeframe this source can show, limit 1 to 1500")
        try:
            return chart.candles(timeframe, limit)
        except ChartUnavailable as error:
            raise HTTPException(
                status_code=503,
                detail="the price source gave no candles",
                headers={"Retry-After": str(int(error.retry_after) + 1)},
            ) from None

    @api.get("/candles/stream")
    async def candle_stream(request: Request, timeframe: str = feed.timeframe) -> StreamingResponse:
        """Server-sent events: the chart timeframe's newest bars, live, for as long as the session lasts.

        One ``candles`` event at once, then one whenever the bars (or the
        history counter, the gaps, the mode) changed, at most one every
        CHART_EVENT_SPACING seconds with the newest state, and at least one
        every CHART_HEARTBEAT seconds. Signals never depend on any of this.
        """
        if timeframe not in chart.timeframes():
            raise HTTPException(status_code=422, detail="a timeframe this source can show")
        loop = asyncio.get_running_loop()
        # The slot is taken here, counted and checked in one step, and given back when the response
        # ends however it ends (ChartStreamResponse), also when it never starts.
        subscriber = chart.subscribe(loop, timeframe, limit=CHART_STREAMS_MAX)
        if subscriber is None:
            raise HTTPException(
                status_code=503, detail="too many open chart streams", headers={"Retry-After": "10"}
            )

        def release() -> None:
            chart.unsubscribe(subscriber)

        async def compute(subscriber: Any) -> dict[str, Any] | None:
            """The event's data, or None when the server began to stop while it was being made."""
            work = asyncio.ensure_future(chart_pool.run(chart.live, timeframe))
            ending = asyncio.ensure_future(subscriber.end.wait())
            try:
                await asyncio.wait({work, ending}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                ending.cancel()
            if not work.done():
                work.cancel()  # the thread finishes on its own; nobody waits for it
                return None
            return work.result()

        async def events() -> AsyncIterator[str]:
            last: Any = None
            sent_at = -math.inf
            try:
                # Checked on every turn: an open stream must not outlive its session or the server.
                while logged_in(request) and not chart.ending:
                    chart.watch(timeframe)
                    subscriber.wake.clear()
                    data = await compute(subscriber)
                    if data is None:
                        break
                    key = [data[name] for name in ("bars", "mode", "rev", "gaps", "error")]
                    if key != last or loop.time() - sent_at >= CHART_HEARTBEAT:
                        spare = CHART_EVENT_SPACING - (loop.time() - sent_at)
                        if spare > 0:  # too soon after the last event: wait, then send the newest state
                            await asyncio.sleep(spare)
                            data = await compute(subscriber)
                            if data is None:
                                break
                            key = [data[name] for name in ("bars", "mode", "rev", "gaps", "error")]
                        try:
                            # Strict JSON: a number that is not finite would leave as bare NaN or
                            # Infinity, which no page can read. Never expected (the candles are checked).
                            chunk: str | None = f"event: candles\ndata: {json.dumps(data, allow_nan=False)}\n\n"
                        except ValueError:
                            log.warning("chart stream %s: an event with a number that is not finite was not sent", timeframe)
                            chunk = None
                        if chunk is not None:
                            yield chunk
                        last, sent_at = key, loop.time()
                    pause = min(CHART_CHECK, max(CHART_HEARTBEAT - (loop.time() - sent_at), 0.0))
                    try:
                        await asyncio.wait_for(subscriber.wake.wait(), timeout=pause)
                    except asyncio.TimeoutError:
                        pass
            finally:
                release()

        return ChartStreamResponse(events(), release, media_type="text/event-stream")

    @api.get("/signal/latest")
    def latest_signal() -> dict[str, Any]:
        return feed.snapshot()["card"]

    @api.get("/signals")
    def signals(limit: int = 100) -> list[dict[str, Any]]:
        return feed.snapshot()["log"][: max(1, min(limit, 100))]

    @api.get("/news")
    def news() -> dict[str, Any]:
        return {"available": False, "events": [], "digest": None}  # calendar: Phase 5, digest: Phase 8

    @api.get("/stats")
    def stats() -> dict[str, Any]:
        return {"available": False, "backtest": None, "live": None}  # backtest: Phase 4

    @api.post("/notify/test")
    def notify_test() -> dict[str, Any]:
        snap = feed.snapshot()
        if not snap["email"]:
            raise HTTPException(status_code=409, detail="email is not configured")
        return {"ok": feed.send_test_email()}

    @api.get("/stream")
    async def stream(request: Request) -> StreamingResponse:
        """Server-sent events: one ``tick`` per feed cycle, for as long as the session lasts."""
        queue = feed.subscribe(asyncio.get_running_loop())

        async def events() -> AsyncIterator[str]:
            try:
                yield f"event: tick\ndata: {json.dumps(feed.tick())}\n\n"
                # Checked on every turn: an open stream must not outlive its session.
                while logged_in(request):
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=KEEP_ALIVE_SECONDS)
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
                        continue
                    if item is None:  # the server is shutting down
                        break
                    yield f"event: tick\ndata: {json.dumps(item)}\n\n"
            finally:
                feed.unsubscribe(queue)

        return StreamingResponse(events(), media_type="text/event-stream")

    app.include_router(api)
    return app


app = create_app()
