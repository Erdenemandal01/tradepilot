"""Login building blocks: the password hash, the session token and the guessing brake."""

from __future__ import annotations

import base64
import hashlib
import hmac

import pytest

from backend.app import auth

PASSWORD = "correct horse battery"
SALT = bytes(range(16))
HASH = auth.hash_password(PASSWORD, SALT)  # hashed once: scrypt is slow on purpose
SECRET = "a-session-secret-for-tests"
NOW = 1_795_000_000.0


def stored(**changes: str) -> str:
    """HASH with some of its six parts replaced."""
    parts = dict(zip(("scheme", "n", "r", "p", "salt", "key"), HASH.split("$")))
    parts.update(changes)
    return "$".join(parts.values())


# --- password hash -------------------------------------------------------------


def test_the_right_password_verifies_against_its_hash():
    assert auth.verify_password(PASSWORD, HASH) is True


def test_the_hash_is_scrypt_with_its_cost_and_never_holds_the_password():
    scheme, n, r, p, salt, key = HASH.split("$")
    assert (scheme, n, r, p) == ("scrypt", "16384", "8", "1")
    assert PASSWORD not in HASH and "=" not in HASH
    # Computed here without auth.py: the key really is scrypt over password and salt.
    expected = hashlib.scrypt(PASSWORD.encode("utf-8"), salt=SALT, n=2**14, r=8, p=1, dklen=32)
    assert base64.urlsafe_b64decode(key + "=") == expected
    assert base64.urlsafe_b64decode(salt + "==") == SALT


def test_every_hash_gets_its_own_salt():
    first, second = auth.hash_password(PASSWORD), auth.hash_password(PASSWORD)
    assert first != second
    assert auth.verify_password(PASSWORD, first) and auth.verify_password(PASSWORD, second)


@pytest.mark.parametrize(
    "wrong",
    [
        "correct horse batterY",
        "correct horse battery ",
        " correct horse battery",
        "CORRECT HORSE BATTERY",
        "correct horse",
        "x",
    ],
)
def test_a_wrong_password_is_refused(wrong):
    assert auth.verify_password(wrong, HASH) is False


def test_a_unicode_password_round_trips_and_is_compared_exactly():
    password = "нууц үг 🔑 арван"
    kept = auth.hash_password(password)
    assert auth.verify_password(password, kept)
    assert not auth.verify_password("нууц уг 🔑 арван", kept)  # one letter differs: ү and у
    assert not auth.verify_password(password, HASH)


def test_empty_and_oversized_passwords_are_refused_without_hashing(monkeypatch):
    def no_hashing(*args, **kwargs):
        raise AssertionError("scrypt must not run for a password that is refused outright")

    monkeypatch.setattr(auth.hashlib, "scrypt", no_hashing)
    for password in ("", "x" * 1025, "ү" * 513):  # the limit is 1024 bytes, not characters
        with pytest.raises(ValueError):
            auth.hash_password(password)
        assert auth.verify_password(password, HASH) is False


def test_the_longest_allowed_password_still_works():
    password = "x" * auth.MAX_PASSWORD_BYTES
    assert auth.verify_password(password, auth.hash_password(password))


MALFORMED = [
    "",
    "plain text",
    PASSWORD,  # the password itself pasted into .env instead of its hash must not log anyone in
    "scrypt$1$2",
    HASH + "$extra",
    stored(scheme="bcrypt"),
    stored(scheme="SCRYPT"),
    stored(n="x"),
    stored(n="16384.0"),
    stored(n="0"),
    stored(n="15"),  # not a power of two
    stored(n="-16384"),
    stored(n=str(2**30)),  # more memory than scrypt may take
    stored(n=str(2**70)),
    stored(r="0"),
    stored(p="0"),
    stored(p="99999999999"),
    stored(salt="!!!", key="???"),
    stored(salt="давс", key="түлхүүр"),
    stored(key=""),
    stored(key="a"),
    stored(salt=""),
    stored(key=HASH.split("$")[5][::-1]),  # well formed, simply another key
]


@pytest.mark.parametrize("value", MALFORMED)
def test_a_malformed_stored_hash_matches_nothing_and_never_raises(value):
    assert auth.verify_password(PASSWORD, value) is False


def test_a_password_that_cannot_be_encoded_is_refused_not_an_error():
    assert auth.verify_password("\ud800", HASH) is False  # a lone surrogate, as broken JSON can carry


def test_a_cut_off_stored_hash_matches_nothing():
    key = base64.urlsafe_b64decode(HASH.split("$")[5] + "=")
    for kept_bytes in (31, 8, 1):
        cut = stored(key=base64.urlsafe_b64encode(key[:kept_bytes]).decode("ascii").rstrip("="))
        assert auth.verify_password(PASSWORD, cut) is False, f"a key cut to {kept_bytes} byte(s) still verifies"


# --- session token ---------------------------------------------------------------


def test_a_token_is_valid_until_its_expiry_and_not_a_second_longer():
    token = auth.new_token(SECRET, NOW, 12)
    assert auth.token_valid(token, SECRET, NOW)
    assert auth.token_valid(token, SECRET, NOW + 12 * 3600 - 1)
    assert not auth.token_valid(token, SECRET, NOW + 12 * 3600)
    assert not auth.token_valid(token, SECRET, NOW + 13 * 3600)


def test_a_token_never_holds_the_secret():
    assert SECRET not in auth.new_token(SECRET, NOW, 12)


def test_a_token_with_a_later_expiry_and_the_old_signature_is_refused():
    expires, signature = auth.new_token(SECRET, NOW, 1).split(".")
    assert auth.token_valid(f"{expires}.{signature}", SECRET, NOW)
    assert not auth.token_valid(f"{int(expires) + 86400}.{signature}", SECRET, NOW)
    assert not auth.token_valid(f"{int(expires) + 1}.{signature}", SECRET, NOW)


def test_a_token_with_a_changed_signature_is_refused():
    expires, signature = auth.new_token(SECRET, NOW, 1).split(".")
    flipped = ("0" if signature[0] != "0" else "1") + signature[1:]
    assert not auth.token_valid(f"{expires}.{flipped}", SECRET, NOW)
    assert not auth.token_valid(f"{expires}.{signature[:-1]}", SECRET, NOW)
    assert not auth.token_valid(f"{expires}.{signature}0", SECRET, NOW)


def test_a_token_signed_with_another_secret_is_refused():
    token = auth.new_token("somebody else's secret", NOW, 12)
    assert not auth.token_valid(token, SECRET, NOW)


def test_without_a_secret_no_token_is_made_and_none_is_accepted():
    with pytest.raises(ValueError):
        auth.new_token("", NOW, 12)
    assert not auth.token_valid(auth.new_token(SECRET, NOW, 12), "", NOW)
    # A token "signed" with the empty key, as anyone could compute it, opens nothing either.
    expires = int(NOW + 3600)
    signature = hmac.new(b"", f"session:{expires}".encode("ascii"), hashlib.sha256).hexdigest()
    assert not auth.token_valid(f"{expires}.{signature}", "", NOW)


@pytest.mark.parametrize(
    "token",
    [
        None,
        "",
        "garbage",
        ".",
        "1.",
        ".abc",
        "a.b.c",
        "1.2.3",
        "soon.abcdef",
        "1e12.abcdef",
        "9" * 40 + ".abcdef",
        str(int(NOW) + 3600),
        f"{int(NOW) + 3600}.",
    ],
)
def test_a_malformed_token_is_refused_without_an_error(token):
    assert auth.token_valid(token, SECRET, NOW) is False


def test_a_token_with_non_ascii_characters_is_refused_without_an_error():
    assert auth.token_valid("1999999999.é", SECRET, NOW) is False


# --- throttle ------------------------------------------------------------------------


def fail(throttle: auth.Throttle, times: int, now: float = NOW) -> None:
    for _ in range(times):
        throttle.failed(now)


def test_four_wrong_tries_cost_nothing_and_the_fifth_starts_a_thirty_second_pause():
    throttle = auth.Throttle()
    assert throttle.wait(NOW) == 0
    fail(throttle, 4)
    assert throttle.wait(NOW) == 0
    fail(throttle, 1)
    assert throttle.wait(NOW) == 30


def test_the_pause_doubles_with_every_further_failure_up_to_fifteen_minutes():
    throttle = auth.Throttle()
    fail(throttle, 4)
    pauses = []
    for _ in range(8):
        throttle.failed(NOW)
        pauses.append(throttle.wait(NOW))
    assert pauses == [30, 60, 120, 240, 480, 900, 900, 900]


def test_the_pause_runs_down_with_the_clock_and_never_goes_negative():
    throttle = auth.Throttle()
    fail(throttle, 5)
    assert throttle.wait(NOW + 10) == 20
    assert throttle.wait(NOW + 29.5) == 0.5
    assert throttle.wait(NOW + 30) == 0
    assert throttle.wait(NOW + 86400) == 0


def test_the_pause_counts_from_the_latest_failure():
    throttle = auth.Throttle()
    fail(throttle, 5)
    throttle.failed(NOW + 30)  # the sixth, right after the first pause ran out
    assert throttle.wait(NOW + 30) == 60
    assert throttle.wait(NOW + 89) == 1


def test_a_success_clears_the_count_and_the_pause():
    throttle = auth.Throttle()
    fail(throttle, 7)
    throttle.succeeded()
    assert throttle.wait(NOW) == 0
    fail(throttle, 4)
    assert throttle.wait(NOW) == 0  # counting started again from nothing
    fail(throttle, 1)
    assert throttle.wait(NOW) == 30


def test_the_pause_stays_at_its_ceiling_however_long_the_guessing_goes_on():
    throttle = auth.Throttle()
    fail(throttle, 1100)
    assert throttle.wait(NOW) == auth.Throttle.MAX_PAUSE
