"""The standard-library WebSocket client, against a hand-written WebSocket server on the loopback address.

Each test scripts the server's side: what it answers to the handshake, which
frames it sends, and what it reads back from the client. The client reaches
it through an injected connect function, never through the network.
"""

from __future__ import annotations

import base64
import hashlib
import socket
import ssl
import struct
import threading
import time
from typing import Any, Callable

import pytest

from backend.app.datasource import ws
from backend.app.datasource.base import DataSourceError

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def frame(opcode: int, payload: bytes, *, fin: bool = True, length: int | None = None, rsv: int = 0) -> bytes:
    """A server frame (not masked). ``length`` lies about the payload's length when given."""
    size = len(payload) if length is None else length
    first = (0x80 if fin else 0) | rsv | opcode
    if size < 126:
        head = struct.pack("!BB", first, size)
    elif size < 65536:
        head = struct.pack("!BBH", first, 126, size)
    else:
        head = struct.pack("!BBQ", first, 127, size)
    return head + payload


class Peer:
    """The server's end of one connection, for the script to use."""

    def __init__(self, conn: socket.socket) -> None:
        self.conn = conn
        self.buffer = b""

    def take(self, count: int) -> bytes:
        while len(self.buffer) < count:
            chunk = self.conn.recv(4096)
            if not chunk:
                raise EOFError
            self.buffer += chunk
        out, self.buffer = self.buffer[:count], self.buffer[count:]
        return out

    def handshake(self, *, accept: str | None = None, status: str = "101 Switching Protocols", extra: str = "") -> str:
        head = b""
        while b"\r\n\r\n" not in head:
            head += self.conn.recv(4096)
        request = head.decode()
        key = [line.split(":", 1)[1].strip() for line in request.split("\r\n") if line.lower().startswith("sec-websocket-key")][0]
        good = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        self.conn.sendall(
            (
                f"HTTP/1.1 {status}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {good if accept is None else accept}\r\n{extra}\r\n"
            ).encode()
        )
        return request

    def read_frame(self) -> tuple[int, bool, bytes]:
        """One client frame: (opcode, masked, unmasked payload)."""
        first, second = self.take(2)
        length = second & 0x7F
        if length == 126:
            (length,) = struct.unpack("!H", self.take(2))
        elif length == 127:
            (length,) = struct.unpack("!Q", self.take(8))
        masked = bool(second & 0x80)
        key = self.take(4) if masked else b"\0\0\0\0"
        data = bytes(byte ^ key[i % 4] for i, byte in enumerate(self.take(length)))
        return first & 0x0F, masked, data


class Server:
    """A WebSocket server on 127.0.0.1 that runs one script per connection on its own thread."""

    def __init__(self, script: Callable[[Peer], Any]) -> None:
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.script = script
        self.results: list[Any] = []
        self.errors: list[BaseException] = []
        self.opened: list[tuple[str, int, bool, float]] = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        conn, _ = self.listener.accept()
        conn.settimeout(5)
        try:
            self.results.append(self.script(Peer(conn)))
        except BaseException as error:  # noqa: BLE001 - reported by the test
            self.errors.append(error)
        finally:
            conn.close()

    def opener(self, host: str, port: int, secure: bool, timeout: float) -> socket.socket:
        self.opened.append((host, port, secure, timeout))
        return socket.create_connection(("127.0.0.1", self.port), timeout=timeout)

    def connect(self, **options: Any) -> ws.WebSocket:
        return ws.connect("wss://stream.example/market/ws/xauusdt@kline_1m", opener=self.opener, timeout=5, **options)

    def finish(self) -> Any:
        self.thread.join(timeout=5)
        self.listener.close()
        assert not self.thread.is_alive(), "the server script never ended"
        if self.errors:
            raise self.errors[0]
        return self.results[0] if self.results else None


def test_a_handshake_then_text_messages_a_ping_answered_with_a_pong_and_a_close():
    def script(peer: Peer) -> Any:
        request = peer.handshake()
        peer.conn.sendall(frame(ws.TEXT, b'{"e":"kline"}') + frame(ws.PING, b"are you there"))
        pong = peer.read_frame()
        peer.conn.sendall(frame(ws.TEXT, "ünïcode".encode()) + frame(ws.CLOSE, struct.pack("!H", 1001) + b"going away"))
        return request, pong, peer.read_frame()

    server = Server(script)
    sock = server.connect()
    assert sock.recv(5) == '{"e":"kline"}'
    assert sock.recv(5) == "ünïcode"  # the ping in between was answered, not returned
    with pytest.raises(DataSourceError, match="code 1001"):
        sock.recv(5)
    request, pong, close = server.finish()
    assert request.startswith("GET /market/ws/xauusdt@kline_1m HTTP/1.1\r\nHost: stream.example\r\n")
    assert "Sec-WebSocket-Version: 13" in request and "Upgrade: websocket" in request
    assert pong == (ws.PONG, True, b"are you there")  # masked, with the ping's own payload
    assert close == (ws.CLOSE, True, struct.pack("!H", 1001))  # the closing handshake said back
    assert server.opened == [("stream.example", 443, True, 5)]  # wss: TLS asked for
    assert sock.closed


def test_a_wrong_accept_key_is_refused():
    server = Server(lambda peer: peer.handshake(accept="bm90IHRoZSByaWdodCBrZXk="))
    with pytest.raises(DataSourceError, match="Sec-WebSocket-Accept"):
        server.connect()
    server.finish()


def test_a_refusal_carries_its_status_and_wait():
    server = Server(lambda peer: peer.handshake(status="429 Too Many Requests", extra="Retry-After: 30\r\n"))
    with pytest.raises(DataSourceError) as refused:
        server.connect()
    assert refused.value.status == 429 and refused.value.retry_after == 30.0
    server.finish()


def test_an_answer_that_does_not_switch_protocols_is_refused():
    def script(peer: Peer) -> None:
        while b"\r\n\r\n" not in peer.buffer:
            peer.buffer += peer.conn.recv(4096)
        peer.conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: h2c\r\nConnection: Upgrade\r\n\r\n")

    server = Server(script)
    with pytest.raises(DataSourceError, match="does not switch"):
        server.connect()
    server.finish()


def test_a_server_that_closes_during_the_handshake():
    def script(peer: Peer) -> None:
        peer.conn.recv(4096)

    server = Server(script)
    with pytest.raises(DataSourceError, match="during the handshake"):
        server.connect()
    server.finish()


def test_an_oversized_frame_is_refused_before_its_payload_is_read():
    def script(peer: Peer) -> bytes:
        peer.handshake()
        peer.conn.sendall(frame(ws.TEXT, b"", length=10_000_000))  # the header only: the payload never comes
        time.sleep(0.3)
        return b""

    server = Server(script)
    sock = server.connect(max_message=4096)
    started = time.monotonic()
    with pytest.raises(DataSourceError, match="longer than 4096"):
        sock.recv(5)
    assert time.monotonic() - started < 2  # judged on the header: not after waiting for 10 MB
    assert sock.closed
    server.finish()


def test_a_fragmented_message_that_grows_past_the_cap_is_refused():
    def script(peer: Peer) -> None:
        peer.handshake()
        peer.conn.sendall(frame(ws.TEXT, b"a" * 3000, fin=False) + frame(ws.CONTINUATION, b"b" * 3000))

    server = Server(script)
    sock = server.connect(max_message=4096)
    with pytest.raises(DataSourceError, match="longer than 4096"):
        sock.recv(5)
    server.finish()


def test_endless_empty_fragments_are_counted_against_the_cap():
    # Each empty continuation frame is 2 bytes on the wire but a list entry in memory: they count too.
    def script(peer: Peer) -> None:
        peer.handshake()
        try:
            peer.conn.sendall(frame(ws.TEXT, b"{", fin=False) + frame(ws.CONTINUATION, b"", fin=False) * 200_000)
        except OSError:
            pass  # the client gave up and closed the connection: as it should

    server = Server(script)
    sock = server.connect(max_message=64 * 1024)
    started = time.monotonic()
    with pytest.raises(DataSourceError, match="longer than"):
        sock.recv(5)
    assert time.monotonic() - started < 2
    assert len(sock._parts or []) <= 64 * 1024 // ws.FRAME_COST  # never more fragments kept than the cap allows
    server.finish()


def test_one_recv_ends_in_its_time_while_frames_without_a_message_keep_coming():
    stop = threading.Event()

    def script(peer: Peer) -> None:
        peer.handshake()

        def drain() -> None:  # the pongs: read them, so the client's answers never block
            try:
                while not stop.is_set() and peer.conn.recv(65536):
                    pass
            except OSError:
                pass

        threading.Thread(target=drain, daemon=True).start()
        pings = frame(ws.PING, b"") * 20_000
        try:
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not stop.is_set():
                peer.conn.sendall(pings)
        except OSError:
            pass

    server = Server(script)
    sock = server.connect()
    started = time.monotonic()
    assert sock.recv(0.3) is None  # no message came: back after its time, not when the pings stop
    took = time.monotonic() - started
    stop.set()
    sock.close()
    server.finish()
    assert took < 1.2


def test_a_status_code_that_is_not_three_ascii_digits_is_not_an_answer():
    # '²' (latin-1 0xB2) passes str.isdigit() but not int(): a DataSourceError, never a ValueError.
    def script(peer: Peer) -> None:
        while b"\r\n\r\n" not in peer.buffer:
            peer.buffer += peer.conn.recv(4096)
        peer.conn.sendall(b"HTTP/1.1 \xb201 Weird\r\nUpgrade: websocket\r\n\r\n")

    server = Server(script)
    with pytest.raises(DataSourceError, match="not an HTTP answer"):
        server.connect()
    server.finish()


def test_a_close_in_the_middle_of_a_frame_is_a_failure_not_a_message():
    def script(peer: Peer) -> None:
        peer.handshake()
        whole = frame(ws.TEXT, b'{"e":"kline","k":{"c":"4100.5"}}')
        peer.conn.sendall(whole[:10])  # then the connection is gone

    server = Server(script)
    sock = server.connect()
    with pytest.raises(DataSourceError, match="in the middle of a frame"):
        sock.recv(5)
    server.finish()


def test_fragments_with_a_ping_between_them_make_one_message():
    def script(peer: Peer) -> Any:
        peer.handshake()
        peer.conn.sendall(
            frame(ws.TEXT, b'{"part":', fin=False)
            + frame(ws.PING, b"1")
            + frame(ws.CONTINUATION, b' "two",', fin=False)
            + frame(ws.CONTINUATION, b' "end": 3}')
        )
        return peer.read_frame()

    server = Server(script)
    sock = server.connect()
    assert sock.recv(5) == '{"part": "two", "end": 3}'
    assert server.finish() == (ws.PONG, True, b"1")


def test_a_timeout_returns_none_and_a_frame_split_across_the_wait_is_kept_whole():
    release = threading.Event()

    def script(peer: Peer) -> None:
        peer.handshake()
        whole = frame(ws.TEXT, b"x" * 300)  # a 126-length header: split inside it
        peer.conn.sendall(whole[:3])
        release.wait(5)
        peer.conn.sendall(whole[3:])
        time.sleep(0.2)

    server = Server(script)
    sock = server.connect()
    assert sock.recv(0.2) is None  # nothing whole yet
    release.set()
    assert sock.recv(5) == "x" * 300
    server.finish()


@pytest.mark.parametrize(
    ("data", "complaint"),
    [
        (struct.pack("!BBI", 0x81, 0x80 | 2, 0) + b"hi", "masked frame"),
        (frame(0x3, b"?"), "unknown kind"),
        (frame(ws.TEXT, b"\xff\xfe"), "not UTF-8"),
        (frame(ws.CONTINUATION, b"lost"), "without a message"),
        (frame(ws.TEXT, b"a", fin=False) + frame(ws.TEXT, b"b"), "inside a fragmented"),
        (frame(ws.PING, b"p" * 126), "control frame"),
        (frame(ws.TEXT, b"x", rsv=0x40), "reserved bits"),
    ],
)
def test_a_broken_frame_is_a_failure(data, complaint):
    def script(peer: Peer) -> None:
        peer.handshake()
        peer.conn.sendall(data)
        time.sleep(0.2)

    server = Server(script)
    sock = server.connect()
    with pytest.raises(DataSourceError, match=complaint):
        sock.recv(5)
    server.finish()


def test_close_from_another_thread_ends_a_waiting_recv():
    def script(peer: Peer) -> Any:
        peer.handshake()
        try:
            return peer.read_frame()
        except EOFError:
            return None

    server = Server(script)
    sock = server.connect()
    threading.Timer(0.2, sock.close).start()
    started = time.monotonic()
    with pytest.raises(DataSourceError):
        sock.recv(5)
    assert time.monotonic() - started < 3
    assert server.finish() == (ws.CLOSE, True, struct.pack("!H", 1000))


def test_the_default_opener_checks_the_certificate_and_the_host_name(monkeypatch):
    made: dict[str, Any] = {}
    real_context = ssl.create_default_context

    class Context:
        def wrap_socket(self, raw: Any, server_hostname: str) -> Any:
            made["server_hostname"] = server_hostname
            return raw

    def context() -> Any:
        real = real_context()
        made["verify_mode"], made["check_hostname"] = real.verify_mode, real.check_hostname
        return Context()

    monkeypatch.setattr(ws.ssl, "create_default_context", context)
    monkeypatch.setattr(ws.socket, "create_connection", lambda address, timeout: ("socket to", address, timeout))
    assert ws.open_socket("fstream.binance.com", 443, True, 7.0) == ("socket to", ("fstream.binance.com", 443), 7.0)
    assert made == {"verify_mode": ssl.CERT_REQUIRED, "check_hostname": True, "server_hostname": "fstream.binance.com"}


def test_an_address_that_cannot_be_reached_is_a_data_source_error():
    def refuse(host: str, port: int, secure: bool, timeout: float) -> socket.socket:
        raise ConnectionRefusedError("refused")

    with pytest.raises(DataSourceError, match="cannot reach wss://stream.example/x"):
        ws.connect("wss://stream.example/x?secret=never-in-a-message", opener=refuse)
    with pytest.raises(ValueError):
        ws.connect("https://stream.example/x")
