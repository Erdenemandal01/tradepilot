"""The web app: who may see what, the login, and what the API hands the dashboard.

Every app here is built over stand-in frontend files in a temporary folder
(the real frontend/ is never read), the fake price source of test_live and a
clock moved by hand. The endless /api/stream response is never read through
the test client: its generator is driven directly, with a time limit on
every step.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import threading
from http.cookies import SimpleCookie
from types import SimpleNamespace
from typing import Any, Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app import __version__, auth, live, main, notify
from backend.app.config import Settings
from backend.scripts import set_password
from backend.tests.test_live import (
    HISTORY,
    SNAPSHOT_KEYS,
    TICK_KEYS,
    UP,
    FakeClock,
    FakeSource,
    bar,
    hour_bar,
    opens_at,
    strict_json,
)

PASSWORD = "correct horse battery staple"
HASH = auth.hash_password(PASSWORD)  # hashed once: scrypt is slow on purpose
SECRET = "session-secret-for-the-web-tests-0123456789"
# The app keeps the real market-hours rule of thumb, and the test candles start on a Friday evening:
# with 1324 of them it is the Friday afternoon two weeks on, with 1400 the Saturday after it.
FRIDAY, SATURDAY = 1324, 1400

# docs/WEB.md, "Login": what answers without a session. Everything else must not.
PUBLIC_ROUTES = {("GET", "/api/health"), ("GET", "/login"), ("POST", "/api/login"), ("POST", "/api/logout")}
PUBLIC_FILES = {"styles.css": "text/css", "login.js": "text/javascript", "favicon.svg": "image/svg+xml"}

FILES = {
    "index.html": "<!doctype html><title>dashboard stand-in</title>",
    "login.html": "<!doctype html><title>login stand-in</title>",
    "secret.txt": "a file next to the static folder, not inside it",
    "static/styles.css": "body { color: black }",
    "static/login.js": "// login stand-in",
    "static/favicon.svg": "<svg xmlns='http://www.w3.org/2000/svg'/>",
    "static/app.js": "// dashboard stand-in",
    "static/notes.txt": "notes stand-in",
    "static/vendor/chart.js": "// chart library stand-in",
    "static/page.html": "<p>html is not a kind of file the static route serves</p>",
    "static/data.json": '{"served": "never"}',
    "static/.env": "TRADEPILOT_SESSION_SECRET=never-served",
}
NEVER_IN_A_RESPONSE = ("not inside it", "is not a kind of file", '"served"', "never-served")


class FakeTime:
    """Stands in for the ``time`` module inside main.py: sessions and login pauses run on this clock."""

    def __init__(self, now: float = 1_795_300_000.0) -> None:
        self.now = now

    def time(self) -> float:
        return self.now


class Site:
    def __init__(self, app: Any, source: FakeSource, notifier: notify.Notifier, clock: FakeTime, frontend: Any) -> None:
        self.app, self.source, self.notifier, self.clock, self.frontend = app, source, notifier, clock, frontend
        self.feed = app.state.feed
        self.loaded = False

    def visitor(self, **options: Any) -> TestClient:
        """A browser without a session."""
        return TestClient(self.app, **options)

    def member(self) -> TestClient:
        """A browser holding a valid session cookie."""
        return self.with_token(auth.new_token(SECRET, self.clock.now, 12))

    def with_token(self, token: str) -> TestClient:
        client = TestClient(self.app)
        client.cookies.set(auth.COOKIE, token)
        return client

    def load(self) -> None:
        """One feed cycle by hand: the history comes in from the fake source."""
        self.loaded = True
        self.feed.poll()


@pytest.fixture
def site(tmp_path, monkeypatch):
    for name in [name for name in os.environ if name.upper().startswith("TRADEPILOT_")]:
        monkeypatch.delenv(name)  # settings exported in the shell that runs the tests stay out of these apps
    frontend = tmp_path / "frontend"
    for name, text in FILES.items():
        path = frontend / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(main, "FRONTEND_DIR", frontend)
    monkeypatch.setattr(main, "STATIC_DIR", frontend / "static")
    clock = FakeTime()
    monkeypatch.setattr(main, "time", clock)
    monkeypatch.setattr(live, "REPLAY", 12)  # the real 192 candles cost half a second per load
    sites: list[Site] = []

    def make(
        notifier: notify.Notifier | None = None, settings: Settings | None = None, cursor: int = FRIDAY, **values: Any
    ) -> Site:
        if settings is None:
            base = {
                "password_hash": HASH,
                "session_secret": SECRET,
                "history_candles": HISTORY,
                # Never the model a training run left in data/ on this PC.
                "forecast_model_path": str(tmp_path / "no_forecast_model.json"),
            }
            settings = Settings(_env_file=None, **{**base, **values})
        source = FakeSource(UP, cursor, FakeClock(opens_at(cursor - 1) + 3))
        notifier = notify.MemoryNotifier() if notifier is None else notifier
        app = main.create_app(settings, source=source, notifier=notifier, run_feed=False)
        sites.append(Site(app, source, notifier, clock, frontend))
        return sites[-1]

    yield make
    for made in sites:
        if made.loaded:
            with TestClient(made.app):  # start-up and shutdown: ends the app's email and quote threads
                pass
        made.feed.close()


def walk(routes: Any, prefix: str = "") -> Iterator[tuple[str, str, Any]]:
    """(method, path, route) for everything the app can answer.

    An unknown kind of route fails the test that walks: whoever adds one
    (a mount, a websocket) has to decide here how it is protected.
    """
    for route in routes:
        included = getattr(route, "original_router", None)
        if included is not None:  # newer FastAPI keeps an included router as a single entry
            extra = getattr(getattr(route, "include_context", None), "prefix", "")
            yield from walk(included.routes, prefix + extra)
        elif getattr(route, "methods", None) and isinstance(getattr(route, "path", None), str):
            for method in sorted(route.methods):
                yield method, prefix + route.path, route
        else:
            raise AssertionError(f"unknown kind of route: {route!r}")


def fill(path: str) -> str:
    """A route path with every parameter replaced by a name that stands for nothing."""
    return re.sub(r"\{[^}]*\}", "x", path)


def login(client: TestClient, password: str = PASSWORD) -> Any:
    return client.post("/api/login", json={"password": password})


def session_cookie(response: Any) -> Any:
    return SimpleCookie(response.headers["set-cookie"])[auth.COOKIE]


# --- who may see what ---------------------------------------------------------------


def end_streams_at_once(s: Site, monkeypatch) -> None:
    """Safety net: a stream opened on this site gets the feed's shutdown message straight away.

    Should /api/stream ever lose its protection, the walk below then gets a
    short answer with status 200 and fails, instead of reading a stream that
    never ends.
    """
    subscribe = s.feed.subscribe

    def subscribe_and_end(loop: Any) -> Any:
        queue = subscribe(loop)
        queue.put_nowait(None)
        return queue

    monkeypatch.setattr(s.feed, "subscribe", subscribe_and_end)


def test_every_route_needs_a_session_except_the_documented_public_ones(site, monkeypatch):
    s = site()
    end_streams_at_once(s, monkeypatch)
    visitor = s.visitor()
    routes = {(method, path) for method, path, _ in walk(s.app.routes)}
    # The walk misses nothing FastAPI itself knows about, routes of included routers as well.
    listed = {(method.upper(), path) for path, item in s.app.openapi()["paths"].items() for method in item}
    assert listed <= {(method, re.sub(r":\w+\}", "}", path)) for method, path in routes}
    assert PUBLIC_ROUTES <= routes
    assert {("GET", "/"), ("GET", "/api/state"), ("GET", "/api/stream"), ("POST", "/api/notify/test")} <= routes

    for method, path in sorted(routes - PUBLIC_ROUTES):
        response = visitor.request(method, fill(path), follow_redirects=False)
        if (method, path) == ("GET", "/"):
            assert response.status_code == 303 and response.headers["location"] == "/login"
            assert "dashboard" not in response.text
        else:
            assert response.status_code == 401, f"{method} {path} answered {response.status_code} without a session"
            assert method == "HEAD" or response.json() == {"detail": "login required"}
    assert s.notifier.sent == []  # and nothing was done on the way


def test_a_session_opens_every_route_that_was_closed(site):
    s = site()
    member = s.member()
    for method, path, _ in walk(s.app.routes):
        if (method, path) in PUBLIC_ROUTES or path == "/api/stream":  # the stream is endless: tested by its parts
            continue
        response = member.request(method, fill(path), follow_redirects=False)
        assert response.status_code in (200, 404), f"{method} {path} answered {response.status_code} with a session"
        assert (response.status_code == 404) == path.startswith("/static/")  # "x" is no file


def test_the_public_routes_answer_without_a_session(site):
    visitor = site().visitor()
    assert visitor.get("/api/health").status_code == 200
    page = visitor.get("/login")
    assert page.status_code == 200 and page.text == FILES["login.html"]
    assert page.headers["content-type"] == "text/html; charset=utf-8"
    assert visitor.post("/api/logout").status_code == 200
    assert login(visitor, "wrong").json() == {"detail": "wrong password"}  # reached, and refused for the password


def test_nothing_can_be_posted_but_login_logout_and_the_test_email(site):
    # Signals only: no route sends, changes or closes an order, and none stores anything.
    reading = ("GET", "HEAD", "OPTIONS")
    changing = {(method, path) for method, path, _ in walk(site().app.routes) if method not in reading}
    assert changing == {("POST", "/api/login"), ("POST", "/api/logout"), ("POST", "/api/notify/test")}


def test_health_is_public_and_tells_nothing_but_liveness(site):
    s = site()
    expected = {
        "status": "ok",
        "version": __version__,
        "symbol": "XAUUSD",
        "feed": "not_started",
        "worker": "not_running",
        "source": "fake",
        "last_candle_age": None,
    }
    assert s.visitor().get("/api/health").json() == expected
    s.load()  # with prices in memory it still shows none of them
    assert s.visitor().get("/api/health").json() == expected


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"])
def test_the_interactive_api_docs_stay_switched_off(site, path):
    s = site()
    assert s.visitor().get(path).status_code == 404
    assert s.member().get(path).status_code == 404


def test_the_pages_send_people_where_they_belong(site):
    s = site()
    visitor, member = s.visitor(), s.member()
    away = visitor.get("/", follow_redirects=False)
    assert away.status_code == 303 and away.headers["location"] == "/login"
    dashboard = member.get("/", follow_redirects=False)
    assert dashboard.status_code == 200 and dashboard.text == FILES["index.html"]
    assert dashboard.headers["content-type"] == "text/html; charset=utf-8"
    home = member.get("/login", follow_redirects=False)
    assert home.status_code == 303 and home.headers["location"] == "/"


# --- static files ------------------------------------------------------------------------


def test_only_the_three_files_the_login_page_needs_are_public(site):
    visitor = site().visitor()
    assert main.PUBLIC_STATIC == set(PUBLIC_FILES)
    for name, media in PUBLIC_FILES.items():
        response = visitor.get(f"/static/{name}")
        assert response.status_code == 200 and response.text == FILES[f"static/{name}"]
        assert response.headers["content-type"].startswith(media)


@pytest.mark.parametrize(
    "name",
    [
        "app.js",
        "notes.txt",
        "vendor/chart.js",
        "missing.js",  # 401 like the rest: a visitor cannot find out which files exist
        "page.html",
        "",
        "styles.css/",
        "%2e/styles.css",  # another spelling of a public file is not that public file
        "vendor/%2e%2e/styles.css",
        "%2e%2e/index.html",
        "%2e%2e/secret.txt",
    ],
)
def test_every_other_static_path_needs_a_session(site, name):
    response = site().visitor().get(f"/static/{name}")
    assert response.status_code == 401 and response.json() == {"detail": "login required"}


def test_a_session_gets_the_other_static_files_with_their_media_type(site):
    member = site().member()
    for name, media in (
        ("app.js", "text/javascript; charset=utf-8"),
        ("vendor/chart.js", "text/javascript; charset=utf-8"),
        ("notes.txt", "text/plain; charset=utf-8"),
        ("styles.css", "text/css; charset=utf-8"),
        ("favicon.svg", "image/svg+xml"),
    ):
        response = member.get(f"/static/{name}")
        assert response.status_code == 200 and response.text == FILES[f"static/{name}"]
        assert response.headers["content-type"] == media


def test_static_never_serves_a_file_outside_its_folder_or_of_an_unknown_kind(site):
    s = site()
    member = s.member()
    outside = (s.frontend / "secret.txt").as_posix()  # exists, and .txt is a kind that is served
    attempts = [
        "%2e%2e/secret.txt",
        "..%2Fsecret.txt",
        "..%5Csecret.txt",
        "vendor/%2e%2e/%2e%2e/secret.txt",
        "%2e%2e/index.html",
        "%2e%2e/login.html",
        outside,  # an absolute path
        "/" + outside,
        "page.html",
        "data.json",
        ".env",
        "missing.js",
        "vendor",
        "vendor/",
        "",
    ]
    for attempt in attempts:
        response = member.get(f"/static/{attempt}")
        assert response.status_code == 404, attempt
        assert not any(text in response.text for text in NEVER_IN_A_RESPONSE), attempt
    # A path with ".." is refused as text, even one that would end up on a file inside the folder.
    assert member.get("/static/vendor/%2e%2e/app.js").status_code == 404
    assert member.get("/static/app.js").text == FILES["static/app.js"]


def test_static_refuses_windows_and_network_paths_before_any_file_call(site, monkeypatch):
    member = site().member()
    touched: list[str] = []
    resolve = main.Path.resolve

    def recording(path: Any, *args: Any, **kwargs: Any) -> Any:
        touched.append(str(path))
        return resolve(path, *args, **kwargs)

    monkeypatch.setattr(main.Path, "resolve", recording)
    attempts = [
        "%5C%5C198.51.100.7%5Cshare%5Cx.txt",  # \\host\share: Windows would open a network connection
        "//198.51.100.7/share/x.txt",
        "C:%5CWindows%5Cwin.ini",
        "C:/Windows/win.ini",
        "app.js%00",  # a NUL used to end in a 500
        "%00",
        "vendor%5Capp.js",
    ]
    for attempt in attempts:
        response = member.get(f"/static/{attempt}")
        assert response.status_code == 404, attempt
        assert "content-security-policy" in response.headers, attempt
    assert touched == []
    assert member.get("/static/app.js").status_code == 200 and touched  # the stand-in does see real file calls


# --- login ---------------------------------------------------------------------------------


def test_a_wrong_password_is_refused_and_sets_no_cookie(site):
    visitor = site().visitor()
    response = login(visitor, PASSWORD + "x")
    assert response.status_code == 401 and "set-cookie" not in response.headers
    assert visitor.get("/api/state").status_code == 401


def test_the_right_password_sets_a_cookie_that_scripts_and_other_sites_cannot_use_and_it_opens_the_api(site):
    s = site()
    visitor = s.visitor()
    response = login(visitor)
    assert response.status_code == 200 and response.json() == {"ok": True}

    cookie = session_cookie(response)
    assert cookie["httponly"] is True and cookie["samesite"].lower() == "strict"
    assert cookie["path"] == "/" and cookie["max-age"] == str(12 * 3600) and not cookie["secure"]
    assert auth.token_valid(cookie.value, SECRET, s.clock.now)
    for secret in (PASSWORD, SECRET, HASH):
        assert secret not in response.headers["set-cookie"] and secret not in response.text
    assert visitor.get("/api/state").status_code == 200
    assert visitor.get("/", follow_redirects=False).status_code == 200


def test_the_cookie_is_marked_secure_once_the_site_is_set_to_https(site):
    response = login(site(cookie_secure=True).visitor())
    assert response.status_code == 200 and session_cookie(response)["secure"] is True


def test_a_session_ends_after_session_hours(site):
    s = site(session_hours=2)
    visitor = s.visitor()
    assert session_cookie(login(visitor))["max-age"] == "7200"
    s.clock.now += 2 * 3600 - 1
    assert visitor.get("/api/state").status_code == 200
    s.clock.now += 1
    assert visitor.get("/api/state").status_code == 401
    assert visitor.get("/", follow_redirects=False).headers["location"] == "/login"


def test_forged_and_expired_tokens_are_refused(site):
    s = site()
    now = s.clock.now
    good = auth.new_token(SECRET, now, 12)
    expires, signature = good.split(".")
    assert s.with_token(good).get("/api/state").status_code == 200  # so the refusals below mean something
    forged = {
        "expired an hour ago": auth.new_token(SECRET, now - 13 * 3600, 12),
        "signed with another secret": auth.new_token("not the server's secret", now, 12),
        "later expiry with the old signature": f"{int(expires) + 86400}.{signature}",
        "no signature": expires,
        "empty signature": f"{expires}.",
        "three parts": f"{expires}.{signature}.x",
        "garbage": "garbage",
    }
    for label, token in forged.items():
        client = s.with_token(token)
        assert client.get("/api/state").status_code == 401, label
        assert client.get("/", follow_redirects=False).status_code == 303, label
        assert client.get("/static/app.js").status_code == 401, label


def test_a_cookie_with_non_ascii_bytes_is_refused_not_a_server_error(site):
    visitor = site().visitor(raise_server_exceptions=False)
    response = visitor.get("/api/state", headers={"cookie": b"tp_session=1999999999.\xe9"})
    assert response.status_code == 401


@pytest.mark.parametrize(
    "missing", [{"password_hash": ""}, {"session_secret": ""}, {"password_hash": "", "session_secret": ""}]
)
def test_without_a_password_or_a_secret_on_the_server_nobody_gets_in(site, missing):
    s = site(**missing)
    visitor = s.visitor()
    for password in (PASSWORD, ""):
        response = login(visitor, password)
        assert response.status_code == 503 and "set-cookie" not in response.headers
    assert visitor.get("/api/state").status_code == 401
    if "session_secret" in missing:
        # A token "signed" with the empty secret, as anyone could compute it, opens nothing.
        expires = int(s.clock.now + 3600)
        signature = hmac.new(b"", f"session:{expires}".encode("ascii"), hashlib.sha256).hexdigest()
        assert s.with_token(f"{expires}.{signature}").get("/api/state").status_code == 401


@pytest.mark.parametrize("body", [b"", b"not json", b"{}", b'{"password": 5}', b'{"password": null}', b'["x"]'])
def test_a_malformed_login_body_is_refused_as_such_and_never_opens_a_session(site, body):
    response = site().visitor().post("/api/login", content=body, headers={"content-type": "application/json"})
    assert response.status_code == 422 and "set-cookie" not in response.headers


@pytest.mark.parametrize(
    "body",
    [
        b'{"password": ""}',
        b'{"password": "\\ud800"}',  # a lone surrogate: text that cannot be encoded
        b'{"password": "' + b"x" * 5000 + b'"}',  # longer than a password may be
    ],
)
def test_an_odd_password_is_refused_like_any_wrong_one(site, body):
    response = site().visitor().post("/api/login", content=body, headers={"content-type": "application/json"})
    assert response.status_code == 401 and "set-cookie" not in response.headers


def test_five_wrong_passwords_start_a_pause_in_which_even_the_right_one_is_refused(site):
    s = site()
    visitor = s.visitor()
    for _ in range(5):
        assert login(visitor, "a wrong guess").status_code == 401

    blocked = login(visitor)  # the right password, during the pause
    assert blocked.status_code == 429 and blocked.headers["retry-after"] == "31"
    assert "set-cookie" not in blocked.headers and visitor.get("/api/state").status_code == 401
    s.clock.now += 29
    assert login(visitor).headers["retry-after"] == "2"
    s.clock.now += 1
    assert login(visitor).status_code == 200  # the pause is over
    assert visitor.get("/api/state").status_code == 200


def test_the_pause_doubles_while_the_guessing_goes_on(site):
    s = site()
    visitor = s.visitor()
    for _ in range(5):
        login(visitor, "a wrong guess")
    for pause in (30, 60, 120):
        refused = login(visitor, "a wrong guess")  # not even looked at during the pause, so it is not counted
        assert refused.status_code == 429 and refused.headers["retry-after"] == str(pause + 1)
        s.clock.now += pause
        assert login(visitor, "a wrong guess").status_code == 401  # the next failure doubles the pause


def test_a_successful_login_forgets_the_wrong_tries_before_it(site):
    visitor = site().visitor()
    for _ in range(2):
        for _ in range(4):
            assert login(visitor, "a wrong guess").status_code == 401
        assert login(visitor).status_code == 200


def test_a_logout_without_the_session_changes_nothing(site):
    # What a form on another site can send: the SameSite=Strict cookie stays behind, so no session comes along.
    s = site()
    member = s.member()
    for stranger in (s.visitor(), s.with_token("0.forged")):
        response = stranger.post("/api/logout", headers={"origin": "https://elsewhere.example"})
        assert response.status_code == 200 and "set-cookie" not in response.headers
    assert member.get("/api/state").status_code == 200


def test_wrong_passwords_sent_together_are_still_checked_one_by_one(site):
    s = site()

    async def burst(passwords: list[str]) -> list[int]:
        transport = httpx.ASGITransport(app=s.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            replies = await asyncio.gather(*(client.post("/api/login", json={"password": p}) for p in passwords))
        return [reply.status_code for reply in replies]

    # Twenty tries at one moment, the right password among them as the tenth: five wrong ones are
    # checked, the rest is refused unchecked, and the right one does not slip through with the crowd.
    passwords = [f"guess {number}" for number in range(20)]
    passwords[9] = PASSWORD
    codes = asyncio.run(burst(passwords))
    assert codes.count(401) <= 5 and codes.count(200) <= 1 and codes.count(429) >= 14
    assert asyncio.run(burst(["one more"])) == [429]


def test_a_refused_request_never_repeats_what_was_sent(site):
    visitor = site().visitor()
    typed = "my-real-password-123"
    bodies = [
        {"content": f"password={typed}", "headers": {"content-type": "application/x-www-form-urlencoded"}},
        {"json": {"pasword": typed}},
        {"json": [typed]},
        {"content": '{"password": NaN}', "headers": {"content-type": "application/json"}},
        {"content": '{"password": 1e400}', "headers": {"content-type": "application/json"}},
        {"content": '"\\ud800"', "headers": {"content-type": "application/json"}},
        {"content": "{", "headers": {"content-type": "application/json"}},
    ]
    for body in bodies:
        response = visitor.post("/api/login", **body)
        assert response.status_code == 422, body
        assert typed not in response.text and "input" not in response.text, body
        assert "content-security-policy" in response.headers, body
        assert all(set(item) == {"type", "loc"} for item in response.json()["detail"]), body
    member = site().member()
    assert member.get("/api/candles?limit=abc").status_code == 422
    assert "abc" not in member.get("/api/candles?limit=abc").text


def test_an_error_nobody_handled_is_a_500_that_still_carries_the_security_headers(site, caplog):
    s = site()
    s.feed.snapshot = lambda: 1 / 0  # a bug somewhere behind a route
    response = s.member().get("/api/state")
    assert response.status_code == 500 and response.text == "Internal Server Error"
    for name, value in main.SECURITY_HEADERS.items():
        assert response.headers[name] == value
    assert "ZeroDivisionError" in caplog.text  # the cause is in the server's log, not in the answer


def test_logout_clears_the_cookie(site):
    visitor = site().visitor()
    login(visitor)
    assert visitor.get("/api/state").status_code == 200

    response = visitor.post("/api/logout")
    cookie = session_cookie(response)
    assert response.status_code == 200 and cookie.value == "" and cookie["max-age"] == "0" and cookie["path"] == "/"
    assert visitor.get("/api/state").status_code == 401
    assert visitor.get("/", follow_redirects=False).headers["location"] == "/login"


def test_a_password_set_by_the_script_logs_in(site, tmp_path):
    env = tmp_path / ".env"
    assert set_password.main(["--env", str(env)], ask=lambda prompt: "set by the script") == 0
    visitor = site(settings=Settings(_env_file=env)).visitor()
    assert login(visitor, "set by the Script").status_code == 401
    assert login(visitor, "set by the script").status_code == 200
    assert visitor.get("/api/state").status_code == 200


# --- security headers ------------------------------------------------------------------------


def test_every_kind_of_response_carries_the_security_headers(site):
    s = site()
    visitor, member = s.visitor(), s.member()
    responses = {
        "api": (visitor.get("/api/health"), 200),
        "api without a session": (visitor.get("/api/state"), 401),
        "redirect": (visitor.get("/", follow_redirects=False), 303),
        "login page": (visitor.get("/login"), 200),
        "public file": (visitor.get("/static/styles.css"), 200),
        "refused file": (visitor.get("/static/app.js"), 401),
        "unknown path": (visitor.get("/nothing-here"), 404),
        "docs": (visitor.get("/docs"), 404),
        "wrong method": (visitor.delete("/api/state"), 405),
        "malformed body": (visitor.post("/api/login", json={}), 422),
        "wrong password": (login(visitor, "wrong"), 401),
        "dashboard": (member.get("/"), 200),
        "api with a session": (member.get("/api/state"), 200),
        "private file": (member.get("/static/app.js"), 200),
        "missing file": (member.get("/static/missing.js"), 404),
        "bad parameter": (member.get("/api/candles?limit=0"), 422),
        "logout": (member.post("/api/logout"), 200),
    }
    for label, (response, status) in responses.items():
        assert response.status_code == status, label
        headers = response.headers
        assert headers["x-content-type-options"] == "nosniff", label
        assert headers["x-frame-options"] == "DENY", label
        assert headers["referrer-policy"] == "no-referrer", label
        assert headers["cache-control"] == "no-store", label  # prices and sessions never come from a cache
        assert "frame-ancestors 'none'" in headers["content-security-policy"], label
        assert len(headers.get_list("content-security-policy")) == 1, label


def test_the_content_security_policy_allows_the_sites_own_files_and_no_inline_script(site):
    policy = site().visitor().get("/login").headers["content-security-policy"]
    directives = {name: sources for name, *sources in (part.split() for part in policy.split(";"))}
    assert directives["default-src"] == ["'self'"]
    assert directives["script-src"] == ["'self'"]  # no inline script, no eval, no other host
    assert directives["connect-src"] == ["'self'"]  # the page talks to this server only
    assert directives["frame-ancestors"] == ["'none'"] and directives["base-uri"] == ["'none'"]
    assert directives["form-action"] == ["'self'"]
    allowed = {"'self'", "'none'", "'unsafe-inline'", "data:"}
    assert all(set(sources) <= allowed for sources in directives.values())  # no host name anywhere
    assert [name for name, sources in directives.items() if "'unsafe-inline'" in sources] == ["style-src"]


# --- the API behind the login -------------------------------------------------------------------


def test_state_is_the_feeds_snapshot_and_the_smaller_routes_are_parts_of_it(site):
    s = site()
    s.load()
    member = s.member()

    def steady(data: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in data.items() if key not in ("now", "age")}

    state = member.get("/api/state").json()
    assert set(state) == SNAPSHOT_KEYS | {"chart", "prediction"} and state["feed"] == "ok"
    chart, prediction = state.pop("chart"), state.pop("prediction")
    assert steady(state) == steady(strict_json(s.feed.snapshot()))
    # The test source has no intervals of its own: the chart is built from the M15 candles in memory.
    assert chart == {
        "timeframes": ["M15", "M30", "M45", "H1", "H2", "H3", "H4", "D1", "W1", "MN1", "MN3", "MN6", "MN12"],
        "signal_timeframe": "M15",
        "filter_timeframe": "H1",
    }
    assert prediction == {  # no model file: nothing to draw, and the reason is said
        "available": False, "reason": "no_model", "timeframe": "M15", "horizon_candles": 4, "made_at": None,
        "base_price": None, "points": [], "up_probability": None, "direction": None, "skill": None,
        "trained_at": None,
    }  # fmt: skip
    assert state["price"] == float(UP["open"].iloc[FRIDAY - 1])
    assert state["source"] == {"name": "fake", "symbol": "XAUTEST"} and state["email"] is True
    assert 0 <= state["now"] - s.source.clock.now < 60  # the source's clock, carried on for the moments since
    assert state["card"]["kind"] == "signal" and len(state["log"]) >= 2 and state["market_closed"] is False

    quote = member.get("/api/quote").json()
    assert set(quote) == {"price", "bid", "ask", "now", "feed", "age", "source"}
    assert steady(quote) == {key: state[key] for key in ("price", "bid", "ask", "feed", "source")}
    assert member.get("/api/signal/latest").json() == state["card"]
    assert member.get("/api/signals").json() == state["log"]
    assert member.get("/api/signals?limit=1").json() == state["log"][:1]
    assert member.get("/api/signals?limit=5000").json() == state["log"]
    assert member.get("/api/signals?limit=many").status_code == 422


def test_the_app_applies_the_market_hours_rule_at_the_weekend(site):
    s = site(cursor=SATURDAY)
    s.load()
    state = s.member().get("/api/state").json()
    assert state["market_closed"] is True and state["log"] == []
    assert (state["card"]["kind"], state["card"]["code"], state["card"]["side"]) == ("blocked", "MARKET_CLOSED", "BUY")


def test_before_the_first_load_the_api_says_starting_and_shows_no_price(site):
    member = site().member()
    state = member.get("/api/state").json()
    assert state["feed"] == "starting" and state["card"] == {"kind": "starting"}
    assert (state["price"], state["bid"], state["ask"], state["log"]) == (None, None, None, [])
    assert member.get("/api/candles").json()["bars"] == [] and member.get("/api/signals").json() == []
    assert member.get("/api/signal/latest").json() == {"kind": "starting"}


def test_news_and_stats_say_plainly_that_they_do_not_exist_yet(site):
    member = site().member()
    assert member.get("/api/news").json() == {"available": False, "events": [], "digest": None}
    assert member.get("/api/stats").json() == {"available": False, "backtest": None, "live": None}


@pytest.mark.parametrize(
    "query",
    [
        "timeframe=M5",
        "timeframe=M10",
        "timeframe=MN2",
        "timeframe=m15",
        "timeframe=",
        "limit=0",
        "limit=-1",
        "limit=1501",
        "limit=abc",
        "limit=1.5",
        "limit=",
        "timeframe=H1&limit=0",
        "timeframe=M1&limit=10",
    ],
)
def test_candles_refuses_a_timeframe_or_a_limit_it_does_not_serve(site, query):
    s = site()
    assert s.member().get(f"/api/candles?{query}").status_code == 422
    assert s.source.calls == []  # and the price source is never asked because of a request


def test_candles_come_oldest_first_in_the_timeframe_and_number_asked_for(site):
    s = site()
    s.load()
    member = s.member()
    seen = s.source.seen()

    def get(query: str = "") -> list[dict[str, Any]]:
        """The bars of an answer, without their volume (checked on its own below)."""
        response = member.get(f"/api/candles{query}")
        assert response.status_code == 200
        return [{key: value for key, value in item.items() if key != "volume"} for item in response.json()["bars"]]

    assert get() == [bar(row) for row in seen.tail(500).itertuples()]  # M15, 500 by default; the last one is forming
    assert get("?timeframe=M15&limit=1") == [bar(seen.iloc[-1])] and get()[-1]["closed"] is False
    assert len(get("?limit=1500")) == HISTORY  # no more than is kept in memory
    hour = int(seen["time"].iloc[-1]) // 3600 * 3600
    assert get("?timeframe=H1&limit=3") == [hour_bar(seen, start) for start in (hour - 7200, hour - 3600, hour)]
    assert s.source.calls == [HISTORY]  # all of it answered from memory

    whole = member.get("/api/candles?timeframe=M15&limit=2").json()
    assert {key: value for key, value in whole.items() if key != "bars"} == {
        "timeframe": "M15", "source": "fake", "symbol": "XAUTEST", "built_from": None,
        "history": "full", "partial_first": False, "gaps": 0,
    }  # fmt: skip
    assert [item["volume"] for item in whole["bars"]] == [float(value) for value in seen["volume"].tail(2)]
    built = member.get("/api/candles?timeframe=M45&limit=4").json()
    assert built["built_from"] == "M15" and built["gaps"] == 0 and len(built["bars"]) == 4
    assert [item["time"] % 2700 for item in built["bars"]] == [0, 0, 0, 0]
    assert [item["closed"] for item in built["bars"]][:3] == [True, True, True]


def test_the_test_email_needs_smtp_and_says_whether_it_went_out(site):
    without = site(notifier=notify.NullNotifier())
    response = without.member().post("/api/notify/test")
    assert response.status_code == 409 and without.member().get("/api/state").json()["email"] is False

    working = site()
    assert working.visitor().post("/api/notify/test").status_code == 401 and working.notifier.sent == []
    assert working.member().post("/api/notify/test").json() == {"ok": True}
    ((subject, _),) = working.notifier.sent
    assert subject.endswith(notify.alert_text("email_test"))
    assert working.member().get("/api/state").json()["alerts"][0]["code"] == "email_test"

    failing = site(notifier=notify.MemoryNotifier(fail=True))
    response = failing.member().post("/api/notify/test")
    assert response.status_code == 200 and response.json() == {"ok": False}
    assert failing.member().get("/api/state").json()["alerts"][0]["code"] == "email_failed"


# --- the stream, part by part ----------------------------------------------------------------------


def stream_endpoint(app: Any) -> Any:
    (route,) = [route for method, path, route in walk(app.routes) if (method, path) == ("GET", "/api/stream")]
    return route.endpoint


def member_request(s: Site, hours: float = 12) -> Any:
    """What the stream route reads of a request: the cookies of a logged-in browser."""
    return SimpleNamespace(cookies={auth.COOKIE: auth.new_token(SECRET, s.clock.now, hours)})


def event(chunk: str) -> tuple[str, dict[str, Any]]:
    """One server-sent event as (name, data)."""
    assert chunk.endswith("\n\n")
    name, data = chunk[:-2].split("\n")
    assert name.startswith("event: ") and data.startswith("data: ")
    return name[len("event: ") :], json.loads(data[len("data: ") :])


def test_the_stream_sends_the_current_tick_then_one_per_cycle_and_ends_at_shutdown(site):
    s = site()
    s.load()

    async def scenario() -> list[Any]:
        response = await stream_endpoint(s.app)(member_request(s))
        events = response.body_iterator
        got = [response.media_type, await asyncio.wait_for(anext(events), timeout=5)]
        s.source.clock.now += 2
        await asyncio.to_thread(s.feed.poll)  # one feed cycle, in a thread as the real loop is
        got.append(await asyncio.wait_for(anext(events), timeout=5))
        s.feed.close()  # the server shuts down
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(events), timeout=5)
        return got

    media_type, first, second = asyncio.run(scenario())
    assert media_type == "text/event-stream"
    (first_name, first_tick), (second_name, second_tick) = event(first), event(second)
    assert first_name == second_name == "tick"
    assert set(first_tick) == set(second_tick) == TICK_KEYS
    assert first_tick["feed"] == "ok" and first_tick["price"] == second_tick["price"]
    assert second_tick["now"] > first_tick["now"]


def test_a_quiet_stream_sends_keep_alive_comments_until_the_page_leaves(site, monkeypatch):
    monkeypatch.setattr(main, "KEEP_ALIVE_SECONDS", 0)  # no waiting: "nothing came" is decided at once
    s = site()

    async def scenario() -> list[str]:
        response = await stream_endpoint(s.app)(member_request(s))
        events = response.body_iterator
        got = [await asyncio.wait_for(anext(events), timeout=5) for _ in range(3)]
        await events.aclose()  # the page went away
        return got

    first, *quiet = asyncio.run(scenario())
    assert event(first)[1]["feed"] == "starting"
    assert quiet == [": keep-alive\n\n", ": keep-alive\n\n"]


def test_an_open_stream_ends_when_its_session_runs_out(site, monkeypatch):
    monkeypatch.setattr(main, "KEEP_ALIVE_SECONDS", 0)
    s = site()
    s.load()

    async def scenario() -> list[str]:
        response = await stream_endpoint(s.app)(member_request(s, hours=1))
        events = response.body_iterator
        got = [await asyncio.wait_for(anext(events), timeout=5) for _ in range(2)]
        s.clock.now += 3600  # the session's hour is over while the stream is still open
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(events), timeout=5)
        return got

    first, quiet = asyncio.run(scenario())
    assert event(first)[0] == "tick" and quiet == ": keep-alive\n\n"
    assert s.feed._subscribers == {}  # and the feed no longer keeps a queue for it


# --- start-up and shutdown ------------------------------------------------------------------------------


def test_with_run_feed_off_the_app_starts_and_stops_without_a_feed_thread(site):
    s = site()
    with s.visitor() as client:
        assert client.get("/api/health").json()["worker"] == "not_running"
        assert not any(thread.name == "live-feed" for thread in threading.enumerate())
    assert s.source.calls == [] and s.source.quotes == 0  # the source was never asked


def test_a_stop_signal_ends_the_open_streams_before_the_servers_own_handler_runs():
    import signal

    order: list[str] = []
    feed = SimpleNamespace(end_streams=lambda: order.append("streams ended"))
    before = signal.signal(signal.SIGINT, lambda number, frame: order.append("server stops"))
    try:
        servers_own = signal.getsignal(signal.SIGINT)
        undo = main.end_streams_on_exit(feed)
        signal.raise_signal(signal.SIGINT)
        assert order == ["streams ended", "server stops"]  # no open page keeps the server from stopping
        undo()
        assert signal.getsignal(signal.SIGINT) is servers_own
    finally:
        signal.signal(signal.SIGINT, before)
