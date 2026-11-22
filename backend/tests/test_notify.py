"""Email wording and delivery. No test opens a connection: SMTP is a fake object."""

from __future__ import annotations

import email
import email.policy
import os
import smtplib
import socket
import ssl
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from backend.app import notify
from backend.app.config import Settings
from backend.app.core import filters as fl
from backend.app.core.signal import BUY, HOLD, SELL
from backend.tests.test_filters import M15, START, proposal

MINUS = "−"  # the minus sign the emails use, not a hyphen


def sent(side: str, *, run_after: fl.Decision | None = None, at: int = START) -> fl.Decision:
    """A sent signal at 4000.00: zone 3999 to 4001, stop 6 away, target 8 away, valid for 30 minutes."""
    result = replace(proposal(side, at, entry=4000.0), indicators={"rsi": 61.3, "atr": 4.0})
    decision = fl.decide(result, h1_score=2 if side == BUY else -2, previous=run_after)
    assert decision.sent
    return decision


def message(decision: fl.Decision, offset_hours: int = 8) -> tuple[str, list[str]]:
    subject, body = notify.signal_message(decision, "XAUUSD", "M15", "binance", offset_hours)
    return subject, body.split("\n")


def line(lines: list[str], start: str) -> str:
    found = [text for text in lines if text.startswith(start)]
    assert len(found) == 1, f"expected one line starting with {start!r}, got {found}"
    return found[0]


def local_clock(utc_seconds: float, offset_hours: int) -> str:
    """HH:MM in a fixed-offset zone, worked out with datetime instead of notify.py."""
    return datetime.fromtimestamp(utc_seconds, timezone(timedelta(hours=offset_hours))).strftime("%H:%M")


# --- signal email -----------------------------------------------------------------


def test_a_buy_email_puts_the_stop_below_and_the_target_above_the_fill_price():
    subject, lines = message(sent(BUY))
    assert "XAUUSD" in subject and "BUY" in subject and "4000.00" in subject and "SELL" not in subject
    assert f"{MINUS}6.00" in line(lines, "SL:") and "3994.00" in line(lines, "SL:")
    assert "+8.00" in line(lines, "TP:") and "4008.00" in line(lines, "TP:")


def test_a_sell_email_puts_the_stop_above_and_the_target_below_the_fill_price():
    subject, lines = message(sent(SELL))
    assert "XAUUSD" in subject and "SELL" in subject and "4000.00" in subject and "BUY" not in subject
    assert "+6.00" in line(lines, "SL:") and "4006.00" in line(lines, "SL:")
    assert f"{MINUS}8.00" in line(lines, "TP:") and "3992.00" in line(lines, "TP:")


def test_the_email_names_symbol_timeframe_side_grade_zone_and_source():
    subject, lines = message(sent(BUY))
    grade = f" {notify.GRADE_WITHOUT_DIGEST} "  # no digest yet: every signal is grade B
    assert notify.GRADE_WITHOUT_DIGEST == "B"
    assert all(part in lines[0] for part in ("XAUUSD", "M15", "BUY", grade))
    assert grade in subject
    zone = lines[1]
    assert "3999.00" in zone and "4001.00" in zone and "4000.00" in zone and "binance" in zone


def test_the_validity_is_a_clock_time_in_the_display_zone_with_its_length():
    decision = sent(BUY)
    expires = START + 2 * M15
    for offset in (8, 0, -5):
        _, lines = message(decision, offset)
        validity = [text for text in lines if f"(UTC{offset:+d})" in text]
        assert len(validity) == 1
        assert f"{local_clock(expires, offset)} " in validity[0] and "30 минут" in validity[0]
    assert local_clock(expires, 8) != local_clock(expires, 0)  # the zone really changes the text


def test_the_email_shows_both_scores_and_the_indicators():
    _, buy = message(sent(BUY))
    scores = line(buy, "Оноо:")
    assert "+4/5" in scores and "+2" in scores and "RSI 61.3" in scores and "ATR 4.00" in scores
    _, sell = message(sent(SELL))
    assert "-4/5" in line(sell, "Оноо:") and "-2" in line(sell, "Оноо:")


def test_the_run_line_appears_only_from_the_second_signal_of_a_run():
    first = sent(BUY)
    second = sent(BUY, run_after=first, at=START + 2 * M15)
    third = sent(BUY, run_after=second, at=START + 4 * M15)
    assert (first.run, second.run, third.run) == (1, 2, 3)
    _, lines = message(first)
    assert not any("Дараалсан" in text for text in lines)
    assert "Дараалсан 2 дахь BUY" in message(second)[1]
    assert "Дараалсан 3 дахь BUY" in message(third)[1]


def test_every_signal_email_says_it_is_experimental_and_not_advice():
    for side in (BUY, SELL):
        _, lines = message(sent(side))
        text = "\n".join(lines)
        assert "туршилтын" in text  # status: experimental
        assert "Санхүүгийн зөвлөгөө биш" in text  # not financial advice
        assert "Систем захиалга илгээдэггүй" in text  # the system sends no orders
        assert "MT5" in text and "зайгаар" in text  # set SL and TP by distance from the real fill


@pytest.mark.parametrize(
    "decision",
    [
        fl.decide(proposal(HOLD, START), h1_score=3, previous=None),
        fl.decide(proposal(BUY, START), h1_score=0, previous=None),  # HTF_CONFLICT
        fl.decide(proposal(SELL, START), h1_score=-2, previous=None, market_closed=True),
        fl.decide(proposal(BUY, START + M15), h1_score=2, previous=sent(BUY)),  # DUPLICATE
        fl.decide(proposal(None, START, reason="INSUFFICIENT_BARS"), h1_score=None, previous=None),
    ],
)
def test_a_decision_that_was_not_sent_has_no_email(decision):
    assert not decision.sent
    with pytest.raises(ValueError):
        notify.signal_message(decision, "XAUUSD", "M15", "binance", 8)


# --- alerts ---------------------------------------------------------------------------


def test_every_documented_alert_code_has_a_mongolian_text():
    # docs/WEB.md lists these codes; live.py formats them with exactly these values.
    values = {"feed_down": {"seconds": 60}, "signal": {"side": "BUY", "grade": "B"}}
    values["signal_emailed"] = values["signal"]
    values["history_gaps"] = {"count": 2}
    documented = {"started", "feed_down", "feed_back", "reloaded", "email_failed", "email_test"}
    documented |= {"signal", "signal_emailed", "history_gaps"}
    assert set(notify.ALERT_TEXTS) == documented
    for code in documented:
        text = notify.alert_text(code, **values.get(code, {}))
        assert any(0x0400 <= ord(letter) <= 0x04FF for letter in text), f"{code} is not in Cyrillic"
        assert "{" not in text
    assert "60" in notify.alert_text("feed_down", seconds=60)
    assert "SELL" in notify.alert_text("signal", side="SELL", grade="B")


def test_an_alert_email_carries_the_text_the_symbol_and_the_local_time():
    at = START + 5 * 3600 + 47 * 60
    text = notify.alert_text("feed_down", seconds=300)
    subject, body = notify.alert_message(text, "XAUUSD", at, 8)
    assert subject.startswith("[TradePilot]") and subject.endswith(text)
    assert text in body and "XAUUSD" in body and "TradePilot AI" in body
    assert f"{local_clock(at, 8)} (UTC+8)" in body
    assert f"{local_clock(at, -5)} (UTC-5)" in notify.alert_message(text, "XAUUSD", at, -5)[1]
    assert "(UTC+0)" in notify.alert_message(text, "XAUUSD", at, 0)[1]


DAY = 86400 * 20_000  # a UTC midnight


@pytest.mark.parametrize(
    ("utc_seconds", "offset", "expected"),
    [
        (DAY, 0, "00:00"),
        (DAY + 59, 0, "00:00"),  # seconds are cut off, not rounded
        (DAY + 23 * 3600 + 59 * 60 + 59.9, 0, "23:59"),
        (DAY + 15 * 3600 + 59 * 60, 8, "23:59"),
        (DAY + 16 * 3600, 8, "00:00"),  # midnight in Ulaanbaatar is 16:00 UTC
        (DAY + 23 * 3600 + 30 * 60, 8, "07:30"),  # already tomorrow there
        (DAY + 30 * 60, -5, "19:30"),  # still yesterday there
        (DAY + 4 * 3600 + 59 * 60, -5, "23:59"),
        (DAY + 5 * 3600, -5, "00:00"),
    ],
)
def test_clock_text_crosses_midnight_in_both_directions(utc_seconds, offset, expected):
    assert notify.clock_text(utc_seconds, offset) == expected
    assert notify.clock_text(utc_seconds, offset) == local_clock(utc_seconds, offset)


# --- choosing the notifier ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_exported_settings(monkeypatch):
    # SMTP variables exported in the shell that runs the tests must not turn email on here.
    for name in [name for name in os.environ if name.upper().startswith("TRADEPILOT_")]:
        monkeypatch.delenv(name)


def settings(**values) -> Settings:
    return Settings(_env_file=None, **values)


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"email_to": "owner@example.com"},  # no host
        {"smtp_host": "smtp.example.com"},  # nobody to send to
        {"smtp_host": "smtp.example.com", "email_to": " , ,"},
    ],
)
def test_without_a_host_or_a_recipient_nothing_is_sent_and_the_site_can_tell(values):
    notifier = notify.make_notifier(settings(**values))
    assert isinstance(notifier, notify.NullNotifier) and notifier.enabled is False
    assert notifier.send("subject", "body") is None


def test_the_memory_notifier_remembers_or_fails_on_request():
    memory = notify.MemoryNotifier()
    memory.send("a", "b")
    assert memory.enabled and memory.sent == [("a", "b")]
    failing = notify.MemoryNotifier(fail=True)
    with pytest.raises(notify.NotifyError):
        failing.send("a", "b")
    assert failing.sent == []


class RecordingSmtp:
    """Stands in for smtplib.SMTP and SMTP_SSL: no socket, every step is written down."""

    made: list["RecordingSmtp"] = []

    def __init__(self, host, port, timeout=None, context=None):
        self.opened = (host, port, timeout)
        self.context = context
        self.steps: list[str] = []
        self.credentials = None
        self.messages: list = []
        type(self).made.append(self)

    def starttls(self, context=None):
        self.steps.append("starttls")
        self.context = context

    def login(self, user, password):
        self.steps.append("login")
        self.credentials = (user, password)

    def send_message(self, message):
        self.steps.append("send")
        self.messages.append(message)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.steps.append("quit")
        return False


def forbidden(*args, **kwargs):
    raise AssertionError("this kind of connection must not be opened here")


SMTP_SETTINGS = {
    "smtp_host": "smtp.example.com",
    "smtp_user": "bot@example.com",
    "smtp_password": "app-password",
    "email_to": " owner@example.com , second@example.com ,, ",
}


def test_port_587_upgrades_to_tls_before_the_password_or_the_message_is_sent(monkeypatch):
    class Plain(RecordingSmtp):
        made = []

    monkeypatch.setattr(smtplib, "SMTP", Plain)
    monkeypatch.setattr(smtplib, "SMTP_SSL", forbidden)
    notifier = notify.make_notifier(settings(**SMTP_SETTINGS, smtp_port=587))
    assert notifier.enabled
    notifier.send("subject", "body")

    (server,) = Plain.made
    assert server.opened[:2] == ("smtp.example.com", 587) and server.opened[2] > 0  # never without a timeout
    assert server.steps == ["starttls", "login", "send", "quit"]
    assert server.context.verify_mode == ssl.CERT_REQUIRED and server.context.check_hostname
    assert server.credentials == ("bot@example.com", "app-password")
    (sent_message,) = server.messages
    assert sent_message["To"] == "owner@example.com, second@example.com"
    assert sent_message["From"] == "bot@example.com"  # no sender configured: the login address


def test_port_465_is_tls_from_the_first_byte(monkeypatch):
    class Tls(RecordingSmtp):
        made = []

    monkeypatch.setattr(smtplib, "SMTP", forbidden)
    monkeypatch.setattr(smtplib, "SMTP_SSL", Tls)
    notifier = notify.make_notifier(settings(**SMTP_SETTINGS, smtp_port=465, email_from="signals@example.com"))
    notifier.send("subject", "body")

    (server,) = Tls.made
    assert server.opened[:2] == ("smtp.example.com", 465)
    assert server.steps == ["login", "send", "quit"]
    assert server.context.verify_mode == ssl.CERT_REQUIRED and server.context.check_hostname
    assert server.messages[0]["From"] == "signals@example.com"


# --- SmtpNotifier through an injected connection -------------------------------------------


class FakeConnection:
    """What ``SmtpNotifier(connect=...)`` gets instead of a server; can fail at one step."""

    def __init__(self, fail_at: str | None = None, error: Exception | None = None):
        self.fail_at, self.error = fail_at, error
        self.steps: list[str] = []
        self.opened = None
        self.credentials = None
        self.messages: list = []

    def _step(self, name: str) -> None:
        self.steps.append(name)
        if name == self.fail_at:
            raise self.error

    def connect(self, host, port, timeout):
        self.opened = (host, port, timeout)
        self._step("connect")
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.steps.append("quit")
        return False

    def login(self, user, password):
        self.credentials = (user, password)
        self._step("login")

    def send_message(self, message):
        self._step("send")
        self.messages.append(message)


def notifier_over(connection: FakeConnection, user: str = "bot@example.com") -> notify.SmtpNotifier:
    return notify.SmtpNotifier(
        "smtp.example.com", 587, user, "app-password", "signals@example.com",
        ["owner@example.com", "second@example.com"], connect=connection.connect,
    )  # fmt: skip


def test_a_message_goes_out_with_its_headers_and_survives_the_wire_format():
    subject, body = notify.signal_message(sent(SELL), "XAUUSD", "M15", "binance", 8)
    connection = FakeConnection()
    notifier_over(connection).send(subject, body)

    assert connection.opened == ("smtp.example.com", 587, 20.0)
    assert connection.steps == ["connect", "login", "send", "quit"]
    (outgoing,) = connection.messages
    assert outgoing["From"] == "signals@example.com"
    assert outgoing["To"] == "owner@example.com, second@example.com"
    # Through bytes and back, as a mail server would see it: the Mongolian text and the minus sign survive.
    received = email.message_from_bytes(outgoing.as_bytes(), policy=email.policy.default)
    assert received["Subject"] == subject
    assert received.get_content().rstrip("\n") == body


def test_login_happens_only_when_a_user_is_set():
    with_user, without_user = FakeConnection(), FakeConnection()
    notifier_over(with_user).send("subject", "body")
    notifier_over(without_user, user="").send("subject", "body")
    assert with_user.steps == ["connect", "login", "send", "quit"]
    assert with_user.credentials == ("bot@example.com", "app-password")
    assert without_user.steps == ["connect", "send", "quit"] and without_user.credentials is None


SERVER_TEXT = "owner@example.com at mail.internal.example"  # what a server's reply may quote


@pytest.mark.parametrize(
    ("fail_at", "error"),
    [
        ("connect", ConnectionRefusedError(f"refused: {SERVER_TEXT}")),
        ("connect", TimeoutError(f"timed out: {SERVER_TEXT}")),
        ("connect", socket.gaierror(11001, f"getaddrinfo failed: {SERVER_TEXT}")),
        ("connect", ssl.SSLError(f"certificate verify failed: {SERVER_TEXT}")),
        ("login", smtplib.SMTPAuthenticationError(535, f"5.7.8 not accepted: {SERVER_TEXT}".encode())),
        ("send", smtplib.SMTPRecipientsRefused({"owner@example.com": (550, SERVER_TEXT.encode())})),
        ("send", smtplib.SMTPSenderRefused(553, SERVER_TEXT.encode(), "signals@example.com")),
        ("send", smtplib.SMTPServerDisconnected(f"connection closed: {SERVER_TEXT}")),
    ],
)
def test_any_mail_or_network_error_becomes_notify_error_without_the_servers_text(fail_at, error):
    connection = FakeConnection(fail_at, error)
    with pytest.raises(notify.NotifyError) as caught:
        notifier_over(connection).send("subject", "body")

    said = str(caught.value)
    assert type(error).__name__ in said  # the kind of failure is kept for the log
    for secret in (SERVER_TEXT, "owner@example.com", "mail.internal", "app-password"):
        assert secret not in said
    # Raised "from None": a logged traceback does not print the server's reply either.
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    assert connection.messages == []
    if fail_at != "connect":
        assert connection.steps[-1] == "quit"  # the connection is closed even after a failure
