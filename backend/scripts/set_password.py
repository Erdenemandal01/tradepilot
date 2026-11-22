"""Set the site's login password.

    python -m backend.scripts.set_password

Asks for the password twice without showing it, then writes its scrypt hash
and a fresh session secret into ``.env`` (TRADEPILOT_PASSWORD_HASH and
TRADEPILOT_SESSION_SECRET). The password itself is stored nowhere. A new
session secret logs every open browser out. Restart the server afterwards.
"""

from __future__ import annotations

import argparse
import getpass
import os
import secrets
import sys
from pathlib import Path
from typing import Callable

from backend.app import auth

MIN_LENGTH = 10
KEYS = ("TRADEPILOT_PASSWORD_HASH", "TRADEPILOT_SESSION_SECRET")


def update_env(path: Path, values: dict[str, str]) -> None:
    """Replace or add ``KEY=value`` lines, leaving every other line as it is."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    kept = [line for line in lines if line.split("=", 1)[0].strip() not in values]
    kept += [f"{key}={value}" for key, value in values.items()]
    # Written beside the file and moved into place: a write that fails half-way
    # leaves the old .env whole. Mode 0600 keeps other accounts out where the
    # system has file modes (Linux, macOS); on Windows the folder's access list applies.
    draft = path.with_name(path.name + ".new")
    handle = os.open(draft, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "w", encoding="utf-8") as file:
        file.write("\n".join(kept) + "\n")
    if os.name == "posix":
        os.chmod(draft, 0o600)  # a draft left over from an earlier run keeps its old mode
    try:
        os.replace(draft, path)
    except OSError:
        draft.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None, ask: Callable[[str], str] = getpass.getpass) -> int:
    parser = argparse.ArgumentParser(description="Set the site's login password in .env")
    parser.add_argument("--env", default=".env", help="path of the .env file (default: .env)")
    args = parser.parse_args(argv)

    password = ask("New password (not shown): ")
    if len(password) < MIN_LENGTH:
        print(f"Too short: use at least {MIN_LENGTH} characters.", file=sys.stderr)
        return 1
    if ask("Again: ") != password:
        print("The two entries differ. Nothing was changed.", file=sys.stderr)
        return 1
    try:
        hashed = auth.hash_password(password)
    except ValueError as error:
        print(f"Not accepted: {error}. Nothing was changed.", file=sys.stderr)
        return 1
    update_env(Path(args.env), {KEYS[0]: hashed, KEYS[1]: secrets.token_urlsafe(48)})
    print(f"Password set in {args.env}. Restart the server.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
