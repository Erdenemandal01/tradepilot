"""Notifications by email (SMTP), behind one interface, and their wording.

``SmtpNotifier`` sends real email, ``NullNotifier`` is used when SMTP is not
configured, ``MemoryNotifier`` is the fake for tests. The texts are Mongolian
like the site. SL and TP lead as distances from the fill price because the
feed's price can differ from the broker's by several USD.
"""

from __future__ import annotations

import smtplib
import ssl
from abc import ABC, abstractmethod
from email.message import EmailMessage
from typing import Callable

from backend.app.config import Settings
from backend.app.core.filters import Decision

GRADE_WITHOUT_DIGEST = "B"  # plan 5.4: no digest means grade B

ALERT_TEXTS = {
    "started": "Систем аслаа. Ханш ирж байна.",
    "feed_down": "Ханш {seconds} секунд ирээгүй. Сигнал түр зогслоо.",
    "feed_back": "Ханш ирж эхэллээ. Сигнал дахин ажиллаж байна.",
    "reloaded": "Ханшийн түүх тасарсан тул бүхэлд нь дахин ачааллаа.",
    "history_gaps": "Эх үүсвэрийн лааны түүхэнд {count} газар лаа дутуу байна. Тооцоо тэр хэсгийг алгасна.",
    "email_failed": "И-мэйл илгээж чадсангүй. SMTP тохиргоог шалгана уу.",
    "email_test": "Туршилтын и-мэйл илгээгдлээ.",
    "signal": "Шинэ сигнал: {side}, {grade} зэрэглэл",
    "signal_emailed": "Шинэ сигнал и-мэйлээр илгээгдлээ: {side}, {grade} зэрэглэл",
}


class NotifyError(RuntimeError):
    """The message could not be handed to the mail server."""


class Notifier(ABC):
    enabled: bool = True

    @abstractmethod
    def send(self, subject: str, body: str) -> None:
        """Deliver one message or raise ``NotifyError``."""


class NullNotifier(Notifier):
    """Email is not configured: nothing is sent, and the site says so."""

    enabled = False

    def send(self, subject: str, body: str) -> None:
        return None


class MemoryNotifier(Notifier):
    """Test fake: remembers what would have been sent."""

    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail = fail

    def send(self, subject: str, body: str) -> None:
        if self.fail:
            raise NotifyError("mail server refused the message")
        self.sent.append((subject, body))


class SmtpNotifier(Notifier):
    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        sender: str,
        recipients: list[str],
        timeout: float = 20.0,
        connect: Callable[..., smtplib.SMTP] | None = None,
    ) -> None:
        self._host, self._port, self._user, self._password = host, port, user, password
        self._sender, self._recipients, self._timeout = sender, recipients, timeout
        self._connect = connect

    def _open(self) -> smtplib.SMTP:
        if self._connect is not None:
            return self._connect(self._host, self._port, timeout=self._timeout)
        context = ssl.create_default_context()
        if self._port == 465:
            return smtplib.SMTP_SSL(self._host, self._port, timeout=self._timeout, context=context)
        server = smtplib.SMTP(self._host, self._port, timeout=self._timeout)
        server.starttls(context=context)  # never send the password or a signal in clear text
        return server

    def send(self, subject: str, body: str) -> None:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self._sender
        message["To"] = ", ".join(self._recipients)
        message.set_content(body)
        try:
            with self._open() as server:
                if self._user:
                    server.login(self._user, self._password)
                server.send_message(message)
        except Exception as exc:
            # Any failure, also one raised before the server is reached (a password
            # that cannot be encoded). The server's reply can quote addresses; keep
            # only the kind of failure.
            raise NotifyError(f"email not sent: {type(exc).__name__}") from None


def make_notifier(settings: Settings) -> Notifier:
    recipients = [address.strip() for address in settings.email_to.split(",") if address.strip()]
    if not settings.smtp_host or not recipients:
        return NullNotifier()
    return SmtpNotifier(
        host=settings.smtp_host,
        port=settings.smtp_port,
        user=settings.smtp_user,
        password=settings.smtp_password.get_secret_value(),
        sender=settings.email_from or settings.smtp_user,
        recipients=recipients,
    )


def clock_text(utc_seconds: float, offset_hours: int) -> str:
    """``HH:MM`` in the display zone."""
    minutes = int(utc_seconds + offset_hours * 3600) % 86400 // 60
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def signed(value: float) -> str:
    return f"{'+' if value >= 0 else '−'}{abs(value):.2f}"


def alert_text(code: str, **values: object) -> str:
    return ALERT_TEXTS[code].format(**values)


def alert_message(text: str, symbol: str, at: float, offset_hours: int) -> tuple[str, str]:
    """Subject and body of a system alert email."""
    body = f"{text}\n\n{symbol} · {clock_text(at, offset_hours)} (UTC{offset_hours:+d})\nTradePilot AI"
    return f"[TradePilot] Системийн мэдэгдэл: {text}", body


def signal_message(
    decision: Decision, symbol: str, timeframe: str, source: str, offset_hours: int
) -> tuple[str, str]:
    """Subject and body of a signal email (plan section 5.5, in Mongolian)."""
    result = decision.result
    levels = result.levels
    if not decision.sent or levels is None or result.expires_at is None or result.signal_time is None:
        raise ValueError("only a sent BUY or SELL signal has an email")
    side = result.signal
    direction = 1 if side == "BUY" else -1
    minutes = (result.expires_at - result.signal_time) // 60
    grade = GRADE_WITHOUT_DIGEST
    lines = [
        f"{symbol} · {timeframe} · {side} · {grade} зэрэглэл",
        f"Орох бүс ({source} ханшаар): {levels.zone_low:.2f} – {levels.zone_high:.2f} (жишиг {levels.entry:.2f})",
        f"SL: орсон үнээс {signed(-direction * levels.sl_distance)} ({source} {levels.stop_loss:.2f})",
        f"TP: орсон үнээс {signed(direction * levels.tp_distance)} ({source} {levels.take_profit:.2f})",
        f"Хүчинтэй: {clock_text(result.expires_at, offset_hours)} хүртэл (UTC{offset_hours:+d}), {minutes} минут",
        f"Оноо: {result.score:+d}/5 · H1 оноо: {decision.h1_score:+d} · "
        f"RSI {result.indicators.get('rsi', float('nan')):.1f} · ATR {result.indicators.get('atr', float('nan')):.2f}",
    ]
    if decision.run > 1:
        lines.append(f"Дараалсан {decision.run} дахь {side}")
    lines += [
        "Төлөв: туршилтын (backtest хийгдээгүй). Мэдээний шүүлтүүр идэвхгүй, AI нэгтгэл байхгүй.",
        f"Түвшнүүд {source}-ийн ханшаар. SL, TP-г MT5 дээр орсон үнээсээ зайгаар нь тавина.",
        "Санхүүгийн зөвлөгөө биш. Систем захиалга илгээдэггүй.",
    ]
    return f"[TradePilot] {symbol} {side} · {grade} зэрэглэл · {levels.entry:.2f}", "\n".join(lines)
