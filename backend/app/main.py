"""FastAPI application: login, the dashboard pages, the API and the live stream.

Everything except ``/api/health``, the login page and what that page needs
to render requires a valid session cookie. The interactive API docs stay
switched off. There is no route that sends, changes or closes an order.
"""

from __future__ import annotations

import asyncio
import json
import logging
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

log = logging.getLogger("tradepilot.web")


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


def end_streams_on_exit(feed: Any) -> Callable[[], None]:
    """End the open streams the moment the server is asked to stop.

    The server waits for open connections before it runs any shutdown code,
    and a dashboard's stream never ends by itself: without this, Ctrl-C would
    hang for as long as a page is open. Returns the call that undoes it.
    """
    previous: dict[int, Any] = {}

    def on_signal(number: int, frame: Any) -> None:
        feed.end_streams()
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
        price_source, feed, refresh_seconds=settings.poll_seconds, history=settings.history_candles
    )
    throttle = auth.Throttle()
    login_lock = threading.Lock()
    secret = settings.session_secret.get_secret_value()
    password_hash = settings.password_hash.get_secret_value()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        stop = threading.Event()
        thread = None
        if settings.live_enabled if run_feed is None else run_feed:
            thread = threading.Thread(target=feed.run, args=(stop,), name="live-feed", daemon=True)
            thread.start()
        undo = end_streams_on_exit(feed)
        try:
            yield
        finally:
            undo()
            stop.set()
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
        return auth.token_valid(request.cookies.get(auth.COOKIE), secret, time.time())

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
        response = JSONResponse({"ok": True})
        response.set_cookie(
            auth.COOKIE,
            auth.new_token(secret, now, settings.session_hours),
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
