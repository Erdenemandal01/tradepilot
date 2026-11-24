"""A small WebSocket client (RFC 6455), standard library only, for public market streams.

What it does: the opening handshake with its ``Sec-WebSocket-Accept`` check,
masked frames from the client, text messages (also when the server sends
them in fragments), a pong for every ping, close frames, timeouts, and a cap
on the size of a message, so that a broken or hostile peer cannot fill the
memory. ``wss://`` runs over TLS with the system's certificates, and the
certificate and host name are checked.

Every failure is a ``DataSourceError``: the caller drops the connection and
opens a new one. The client never sends anything but pongs and a close.
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
import struct
import time
from typing import Callable
from urllib.parse import urlsplit

from backend.app.datasource.base import DataSourceError

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"  # RFC 6455, section 1.3
MAX_MESSAGE = 1 << 20  # bytes: a kline message is about 400; anything near this is not market data
# Bytes each fragment of a message counts against MAX_MESSAGE besides its payload: what keeping it costs
# (an object and a list entry). Without it, endless empty fragments (2 bytes each on the wire) would
# grow the list without ever reaching the cap.
FRAME_COST = 64
MAX_HEAD = 16 * 1024  # bytes of the handshake answer's status line and headers
USER_AGENT = "Mozilla/5.0 (compatible; candle-feed-client)"  # generic, as for the REST requests

TEXT, BINARY, CONTINUATION, CLOSE, PING, PONG = 0x1, 0x2, 0x0, 0x8, 0x9, 0xA

# (host, port, secure, timeout) -> a connected socket. Tests pass their own.
Opener = Callable[[str, int, bool, float], socket.socket]


def open_socket(host: str, port: int, secure: bool, timeout: float) -> socket.socket:
    raw = socket.create_connection((host, port), timeout=timeout)
    if not secure:
        return raw
    try:
        context = ssl.create_default_context()  # CERT_REQUIRED and check_hostname: the defaults, kept
        return context.wrap_socket(raw, server_hostname=host)
    except BaseException:
        raw.close()
        raise


def _mask(data: bytes, key: bytes) -> bytes:
    if not data:
        return b""
    repeated = (key * (len(data) // 4 + 1))[: len(data)]
    return (int.from_bytes(data, "big") ^ int.from_bytes(repeated, "big")).to_bytes(len(data), "big")


def accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")


class WebSocket:
    """An open connection. ``recv`` gives the next text message; ``close`` ends it.

    One thread reads. ``close`` may be called from any thread, also while
    another one waits in ``recv``: the wait then ends with DataSourceError.
    """

    def __init__(self, sock: socket.socket, buffer: bytes = b"", *, max_message: int = MAX_MESSAGE) -> None:
        self._sock = sock
        self._buffer = bytearray(buffer)
        self._max = int(max_message)
        self._parts: list[bytes] | None = None  # a fragmented message being put together
        self._parts_kind = TEXT
        self._parts_size = 0
        self._closed = False
        self.last_frame_at = time.monotonic()  # any frame, pings included: the line is alive

    # --- reading -------------------------------------------------------------------

    def recv(self, timeout: float) -> str | None:
        """The next text message, or None when none came within ``timeout`` seconds.

        Pings are answered on the way. A frame that is only partly here when
        the time is up stays buffered for the next call, and so does a
        message whose fragments are still coming. The time is checked after
        every frame: a peer that keeps sending frames without a message
        (pings, fragments) cannot hold the call past it.
        """
        if self._closed:
            raise DataSourceError("the stream connection is closed")
        deadline = time.monotonic() + max(float(timeout), 0.0)
        while True:
            frame = self._frame()
            if frame is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self._fill(remaining):
                    return None
                continue
            message = self._handle(*frame)
            if message is not None:
                return message
            if time.monotonic() >= deadline:
                return None

    def _fill(self, timeout: float) -> bool:
        """Read what the socket has. False when nothing came in time."""
        try:
            self._sock.settimeout(timeout)
            data = self._sock.recv(65536)
        except (socket.timeout, TimeoutError):
            return False
        except (OSError, ValueError) as error:  # ssl.SSLError is an OSError; ValueError: closed meanwhile
            self._drop()
            raise DataSourceError(f"the stream connection failed: {type(error).__name__}: {error}") from None
        if not data:
            middle = " in the middle of a frame" if self._buffer or self._parts is not None else ""
            self._drop()
            raise DataSourceError(f"the server closed the stream connection{middle}")
        self._buffer += data
        return True

    def _frame(self) -> tuple[bool, int, bytes] | None:
        """One whole frame from the buffer, or None while it is not all here."""
        buffer = self._buffer
        if len(buffer) < 2:
            return None
        first, second = buffer[0], buffer[1]
        fin, opcode = bool(first & 0x80), first & 0x0F
        if first & 0x70:
            self._fail("a frame with reserved bits set")
        if second & 0x80:
            self._fail("a masked frame from the server")
        if opcode not in (CONTINUATION, TEXT, BINARY, CLOSE, PING, PONG):
            self._fail(f"a frame of unknown kind {opcode}")
        length, start = second & 0x7F, 2
        if length == 126:
            if len(buffer) < 4:
                return None
            (length,) = struct.unpack("!H", buffer[2:4])
            start = 4
        elif length == 127:
            if len(buffer) < 10:
                return None
            (length,) = struct.unpack("!Q", buffer[2:10])
            start = 10
        if opcode >= CLOSE and (not fin or length > 125):
            self._fail("a control frame that is fragmented or longer than 125 bytes")
        # Judged before the payload is waited for: a peer cannot make the buffer grow past the cap.
        if length > self._max or (opcode == CONTINUATION and self._parts_size + length + FRAME_COST > self._max):
            self._fail(f"a message longer than {self._max} bytes")
        if len(buffer) < start + length:
            return None
        payload = bytes(buffer[start : start + length])
        del buffer[: start + length]
        self.last_frame_at = time.monotonic()
        return fin, opcode, payload

    def _handle(self, fin: bool, opcode: int, payload: bytes) -> str | None:
        if opcode == PING:
            self._send(PONG, payload)
            return None
        if opcode == PONG:
            return None
        if opcode == CLOSE:
            code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else 1005
            reason = payload[2:125].decode("utf-8", "replace")
            try:
                self._send(CLOSE, payload[:2])  # the closing handshake: say it back
            except DataSourceError:
                pass
            self._drop()
            raise DataSourceError(f"the server closed the stream (code {code}{': ' + reason if reason else ''})")
        if opcode == CONTINUATION:
            if self._parts is None:
                self._fail("a continuation frame without a message to continue")
            assert self._parts is not None
            self._parts.append(payload)
            self._parts_size += len(payload) + FRAME_COST
            if not fin:
                return None
            data, kind = b"".join(self._parts), self._parts_kind
            self._parts, self._parts_size = None, 0
        else:  # TEXT or BINARY
            if self._parts is not None:
                self._fail("a new message inside a fragmented one")
            if not fin:
                self._parts, self._parts_kind, self._parts_size = [payload], opcode, len(payload) + FRAME_COST
                return None
            data, kind = payload, opcode
        if kind == BINARY:
            return None  # market streams send text; anything else is not read
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            self._fail("a text message that is not UTF-8")
        return None  # not reached

    # --- writing and closing ---------------------------------------------------------

    def _send(self, opcode: int, payload: bytes) -> None:
        key = os.urandom(4)
        length = len(payload)
        if length < 126:
            head = struct.pack("!BB", 0x80 | opcode, 0x80 | length)
        elif length < 65536:
            head = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, length)
        else:
            head = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, length)
        try:
            self._sock.settimeout(10.0)
            self._sock.sendall(head + key + _mask(payload, key))
        except (OSError, ValueError) as error:
            self._drop()
            raise DataSourceError(f"the stream connection failed while sending: {type(error).__name__}") from None

    def _fail(self, what: str) -> None:
        self._drop()
        raise DataSourceError(f"the stream server sent {what}")

    def _drop(self) -> None:
        self._closed = True
        try:
            self._sock.shutdown(socket.SHUT_RDWR)  # wakes a thread that waits in recv
        except (OSError, ValueError):
            pass
        try:
            self._sock.close()
        except (OSError, ValueError):
            pass

    def close(self) -> None:
        """End the connection: a close frame when it is still open, then the socket goes."""
        if self._closed:
            return
        try:
            self._send(CLOSE, struct.pack("!H", 1000))
        except DataSourceError:
            pass
        self._drop()

    @property
    def closed(self) -> bool:
        return self._closed


def connect(
    url: str,
    *,
    timeout: float = 10.0,
    max_message: int = MAX_MESSAGE,
    opener: Opener = open_socket,
) -> WebSocket:
    """Open ``url`` (ws:// or wss://) and do the opening handshake.

    An answer other than 101 raises DataSourceError with its HTTP status and
    the ``Retry-After`` seconds when it gave them (a refusal: 418, 429).
    """
    parts = urlsplit(url)
    if parts.scheme not in ("ws", "wss") or not parts.hostname:
        raise ValueError(f"not a WebSocket address: {url!r}")
    secure = parts.scheme == "wss"
    host = parts.hostname
    port = parts.port or (443 if secure else 80)
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    where = f"{parts.scheme}://{host}{parts.path}"  # no query in any message
    try:
        sock = opener(host, port, secure, timeout)
    except (OSError, ValueError) as error:
        raise DataSourceError(f"cannot reach {where}: {type(error).__name__}: {error}") from None
    try:
        return _handshake(sock, host, port, secure, target, where, timeout, max_message)
    except BaseException:
        try:
            sock.close()
        except OSError:
            pass
        raise


def _handshake(
    sock: socket.socket, host: str, port: int, secure: bool, target: str, where: str, timeout: float, max_message: int
) -> WebSocket:
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    named = host if port == (443 if secure else 80) else f"{host}:{port}"
    request = (
        f"GET {target} HTTP/1.1\r\n"
        f"Host: {named}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        f"User-Agent: {USER_AGENT}\r\n"
        "\r\n"
    )
    deadline = time.monotonic() + timeout
    head = b""
    try:
        sock.settimeout(timeout)
        sock.sendall(request.encode("ascii"))
        while b"\r\n\r\n" not in head:
            if len(head) > MAX_HEAD:
                raise DataSourceError(f"{where}: the handshake answer is longer than {MAX_HEAD} bytes")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DataSourceError(f"{where}: no handshake answer within {timeout:.0f} s")
            sock.settimeout(remaining)
            chunk = sock.recv(4096)
            if not chunk:
                raise DataSourceError(f"{where}: the server closed the connection during the handshake")
            head += chunk
    except (socket.timeout, TimeoutError):
        raise DataSourceError(f"{where}: no handshake answer within {timeout:.0f} s") from None
    except OSError as error:
        raise DataSourceError(f"{where}: the handshake failed: {type(error).__name__}: {error}") from None
    raw, rest = head.split(b"\r\n\r\n", 1)
    lines = raw.decode("iso-8859-1").split("\r\n")
    status = lines[0].split(" ", 2)
    # Three ASCII digits: str.isdigit() alone also passes digits like '²' that int() refuses.
    digits = status[1] if len(status) >= 2 else ""
    if not status[0].startswith("HTTP/1.") or len(digits) != 3 or not (digits.isascii() and digits.isdigit()):
        raise DataSourceError(f"{where}: not an HTTP answer to the handshake")
    code = int(status[1])
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    if code != 101:
        try:
            retry_after: float | None = float(headers.get("retry-after", ""))
        except ValueError:
            retry_after = None
        raise DataSourceError(f"{where}: the handshake was refused: HTTP {code}", status=code, retry_after=retry_after)
    if headers.get("upgrade", "").lower() != "websocket" or "upgrade" not in [
        token.strip().lower() for token in headers.get("connection", "").split(",")
    ]:
        raise DataSourceError(f"{where}: the answer does not switch to the WebSocket protocol")
    if headers.get("sec-websocket-accept") != accept_key(key):
        raise DataSourceError(f"{where}: wrong Sec-WebSocket-Accept in the handshake answer")
    return WebSocket(sock, rest, max_message=max_message)
