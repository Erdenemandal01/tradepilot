"""Login: one password, stored as a scrypt hash, and a signed session cookie.

Standard library only. The password itself is never stored or logged; the
hash and the session secret live in ``.env`` (written by
``python -m backend.scripts.set_password``). Without both, nobody can log in.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os

COOKIE = "tp_session"

_SCHEME = "scrypt"
_N, _R, _P = 2**14, 8, 1
_KEY_BYTES = 32
MAX_PASSWORD_BYTES = 1024  # refuse absurd inputs before hashing them


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str, salt: bytes | None = None) -> str:
    """``scrypt$n$r$p$salt$key``, safe to keep in ``.env``."""
    secret = password.encode("utf-8")
    if not secret or len(secret) > MAX_PASSWORD_BYTES:
        raise ValueError("the password must be 1 to 1024 bytes long")
    salt = salt if salt is not None else os.urandom(16)
    key = hashlib.scrypt(secret, salt=salt, n=_N, r=_R, p=_P, dklen=_KEY_BYTES)
    return f"{_SCHEME}${_N}${_R}${_P}${_b64(salt)}${_b64(key)}"


def verify_password(password: str, stored: str) -> bool:
    """Whether ``password`` matches the stored hash. Never raises on bad input."""
    try:
        scheme, n, r, p, salt, key = stored.split("$")
        secret = password.encode("utf-8")
        if scheme != _SCHEME or not secret or len(secret) > MAX_PASSWORD_BYTES:
            return False
        expected = _unb64(key)
        if len(expected) != _KEY_BYTES:  # a cut-off hash must match nothing
            return False
        actual = hashlib.scrypt(secret, salt=_unb64(salt), n=int(n), r=int(r), p=int(p), dklen=len(expected))
    except (ValueError, TypeError, MemoryError):
        return False
    return hmac.compare_digest(actual, expected)


def _sign(secret: str, expires: int) -> str:
    return hmac.new(secret.encode("utf-8"), f"session:{expires}".encode("ascii"), hashlib.sha256).hexdigest()


def new_token(secret: str, now: float, hours: float) -> str:
    """A session token ``<expiry>.<signature>`` valid for ``hours``."""
    if not secret:
        raise ValueError("no session secret is set")
    expires = int(now + hours * 3600)
    return f"{expires}.{_sign(secret, expires)}"


def token_valid(token: str | None, secret: str, now: float) -> bool:
    if not token or not secret:
        return False
    try:
        raw, signature = token.split(".")
        expires = int(raw)
    except ValueError:
        return False
    # Compared as bytes: a cookie may carry any characters.
    matches = hmac.compare_digest(signature.encode("utf-8"), _sign(secret, expires).encode("ascii"))
    return matches and now < expires


class Throttle:
    """Slows down password guessing: after 5 wrong tries, a growing pause.

    The site has a single account, so the count is global rather than per
    address (an address is easy to change, the password is the target).
    """

    FREE_TRIES = 5
    FIRST_PAUSE = 30.0
    MAX_PAUSE = 900.0

    def __init__(self) -> None:
        self._failures = 0
        self._locked_until = 0.0

    def wait(self, now: float) -> float:
        """Seconds until the next try is allowed (0 when allowed now)."""
        return max(0.0, self._locked_until - now)

    def failed(self, now: float) -> None:
        self._failures += 1
        over = self._failures - self.FREE_TRIES
        if over >= 0:
            self._locked_until = now + min(self.FIRST_PAUSE * 2 ** min(over, 16), self.MAX_PAUSE)

    def succeeded(self) -> None:
        self._failures = 0
        self._locked_until = 0.0
