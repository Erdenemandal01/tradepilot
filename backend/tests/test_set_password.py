"""The script that writes the login password's hash and a session secret into .env."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from backend.app import auth
from backend.app.config import Settings
from backend.scripts import set_password as sp

PASSWORD = "a long enough password"
HASH_KEY, SECRET_KEY = "TRADEPILOT_PASSWORD_HASH", "TRADEPILOT_SESSION_SECRET"
EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"


class Keyboard:
    """Scripted answers for the hidden prompt, in order."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.answers.pop(0)


def run(path: Path, *answers: str) -> int:
    return sp.main(["--env", str(path)], ask=Keyboard(*answers))


def lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def values(path: Path) -> dict[str, str]:
    pairs = (text.split("=", 1) for text in lines(path) if "=" in text and not text.lstrip().startswith("#"))
    return {key.strip(): value for key, value in pairs}


@pytest.fixture(autouse=True)
def no_exported_settings(monkeypatch):
    # A variable exported in the shell would win over the file when Settings reads it back.
    for name in [name for name in os.environ if name.upper().startswith("TRADEPILOT_")]:
        monkeypatch.delenv(name)


def test_a_new_file_gets_the_hash_and_a_session_secret_and_never_the_password(tmp_path, capsys):
    env = tmp_path / ".env"
    assert run(env, PASSWORD, PASSWORD) == 0

    written = values(env)
    assert set(written) == {HASH_KEY, SECRET_KEY}
    assert auth.verify_password(PASSWORD, written[HASH_KEY])
    assert not auth.verify_password(PASSWORD + "x", written[HASH_KEY])
    assert len(written[SECRET_KEY]) >= 43  # at least 256 bits of randomness
    output = capsys.readouterr()
    assert PASSWORD not in env.read_text(encoding="utf-8")
    assert PASSWORD not in output.out and PASSWORD not in output.err


def test_what_was_written_loads_through_settings_and_opens_a_session(tmp_path):
    env = tmp_path / ".env"
    run(env, PASSWORD, PASSWORD)

    settings = Settings(_env_file=env)
    stored = settings.password_hash.get_secret_value()
    secret = settings.session_secret.get_secret_value()
    assert (stored, secret) == (values(env)[HASH_KEY], values(env)[SECRET_KEY])  # nothing lost to .env quoting
    assert auth.verify_password(PASSWORD, stored)
    assert auth.token_valid(auth.new_token(secret, 1_795_000_000, 12), secret, 1_795_000_000)


def test_other_lines_of_an_existing_file_stay_and_old_values_are_replaced(tmp_path):
    env = tmp_path / ".env"
    kept = [
        "# local settings",
        "TRADEPILOT_SYMBOL=XAUUSD",
        "",
        f"# {HASH_KEY}=a commented-out line is not a setting",
        "TRADEPILOT_SMTP_PASSWORD=p=ss=word",
        "TRADEPILOT_EMAIL_TO=owner@example.com",
    ]
    env.write_text(
        "\n".join([*kept[:3], f"{HASH_KEY}=old-hash", kept[3], f"{SECRET_KEY} = old-secret", *kept[4:]]) + "\n",
        encoding="utf-8",
    )
    assert run(env, PASSWORD, PASSWORD) == 0

    after = lines(env)
    assert [text for text in after if not text.startswith((HASH_KEY, SECRET_KEY))] == kept  # same lines, same order
    assert sum(text.startswith(HASH_KEY) for text in after) == 1
    assert sum(text.startswith(SECRET_KEY) for text in after) == 1
    assert "old-hash" not in env.read_text(encoding="utf-8") and "old-secret" not in env.read_text(encoding="utf-8")
    assert auth.verify_password(PASSWORD, values(env)[HASH_KEY])
    loaded = Settings(_env_file=env)
    assert loaded.smtp_password.get_secret_value() == "p=ss=word" and loaded.email_to == "owner@example.com"


def test_a_copy_of_the_example_file_keeps_every_other_setting(tmp_path):
    env = tmp_path / ".env"
    env.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    before = [text for text in lines(env) if not text.startswith((HASH_KEY, SECRET_KEY))]
    assert run(env, PASSWORD, PASSWORD) == 0

    assert [text for text in lines(env) if not text.startswith((HASH_KEY, SECRET_KEY))] == before
    example, loaded = Settings(_env_file=EXAMPLE), Settings(_env_file=env)
    assert example.password_hash.get_secret_value() == ""  # the example ships without a password
    assert auth.verify_password(PASSWORD, loaded.password_hash.get_secret_value())
    untouched = set(Settings.model_fields) - {"password_hash", "session_secret"}
    assert {name: getattr(loaded, name) for name in untouched} == {name: getattr(example, name) for name in untouched}


@pytest.mark.parametrize("short", ["", "123456789", "есөн үсэг"])
def test_a_short_password_is_refused_before_anything_is_written(tmp_path, capsys, short):
    assert len(short) < sp.MIN_LENGTH == 10
    env = tmp_path / ".env"
    keyboard = Keyboard(short, short)
    assert sp.main(["--env", str(env)], ask=keyboard) == 1
    assert not env.exists()
    assert len(keyboard.prompts) == 1  # it does not go on to ask a second time
    assert "10" in capsys.readouterr().err

    env.write_text(f"{HASH_KEY}=old-hash\n", encoding="utf-8")
    assert run(env, short, short) == 1
    assert lines(env) == [f"{HASH_KEY}=old-hash"]


def test_two_entries_that_differ_change_nothing(tmp_path, capsys):
    env = tmp_path / ".env"
    assert run(env, PASSWORD, PASSWORD + "!") == 1
    assert not env.exists()
    assert capsys.readouterr().err.strip()

    env.write_text(f"{HASH_KEY}=old-hash\n{SECRET_KEY}=old-secret\n", encoding="utf-8")
    assert run(env, PASSWORD, PASSWORD.upper()) == 1
    assert lines(env) == [f"{HASH_KEY}=old-hash", f"{SECRET_KEY}=old-secret"]


@pytest.mark.parametrize("password", ["0123456789", "арван үсэг", "нууц үг 🔑 арван тэмдэгт"])
def test_ten_characters_are_enough_in_any_script(tmp_path, password):
    assert len(password) >= 10
    env = tmp_path / ".env"
    assert run(env, password, password) == 0
    assert auth.verify_password(password, Settings(_env_file=env).password_hash.get_secret_value())


def test_setting_the_password_again_changes_the_salt_and_logs_every_browser_out(tmp_path):
    env = tmp_path / ".env"
    run(env, PASSWORD, PASSWORD)
    first = values(env)
    token = auth.new_token(first[SECRET_KEY], 1_795_000_000, 12)
    run(env, PASSWORD, PASSWORD)
    second = values(env)

    assert second[HASH_KEY] != first[HASH_KEY] and second[SECRET_KEY] != first[SECRET_KEY]
    assert auth.verify_password(PASSWORD, second[HASH_KEY])
    assert not auth.token_valid(token, second[SECRET_KEY], 1_795_000_000)  # the old session cookie is dead


def test_a_password_the_login_would_refuse_as_too_long_is_never_written(tmp_path):
    env = tmp_path / ".env"
    env.write_text(f"{HASH_KEY}=old-hash\n", encoding="utf-8")
    too_long = "x" * (auth.MAX_PASSWORD_BYTES + 1)
    try:
        code = run(env, too_long, too_long)
    except ValueError:  # today the script stops with the hash function's error instead of a message
        code = 1
    assert code == 1
    assert lines(env) == [f"{HASH_KEY}=old-hash"]


def test_update_env_adds_missing_keys_and_creates_the_file(tmp_path):
    env = tmp_path / "nested.env"
    sp.update_env(env, {"A": "1"})
    sp.update_env(env, {"B": "two=2", "A": "3"})
    assert values(env) == {"A": "3", "B": "two=2"}
    assert env.read_text(encoding="utf-8").endswith("\n")


def test_the_file_is_replaced_in_one_step_and_no_draft_is_left_behind(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("TRADEPILOT_SYMBOL=XAUUSD\n", encoding="utf-8")
    assert run(path, PASSWORD, PASSWORD) == 0
    assert [item.name for item in tmp_path.iterdir()] == [".env"]
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600  # other accounts on the machine cannot read the secrets

    # A write that fails half-way leaves the old file exactly as it was.
    before = path.read_bytes()
    monkeypatch.setattr(sp.os, "replace", lambda *names: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        run(path, PASSWORD + " 2", PASSWORD + " 2")
    assert path.read_bytes() == before and [item.name for item in tmp_path.iterdir()] == [".env"]
