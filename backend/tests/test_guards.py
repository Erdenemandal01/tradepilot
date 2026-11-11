"""Tripwires for the hard rules in CLAUDE.md.

These tests read the source code itself. They catch the obvious ways of
breaking a rule (a trading library, an order endpoint, file or network access
or a clock in core/). They are tripwires, not proof that a rule holds.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
APP_DIR = BACKEND_DIR / "app"
CORE_DIR = APP_DIR / "core"
DATASOURCE_DIR = APP_DIR / "datasource"
SHIPPED_DIRS = (APP_DIR, BACKEND_DIR / "scripts")  # everything except the tests

# Rule: signals only. No code that sends, changes or checks orders, and no
# trading terminal or exchange trading library.
FORBIDDEN_TRADING_TOKENS = (
    "order_send",
    "order_check",
    "MetaTrader5",
    "new_order",
    "create_order",
    "place_order",
    "cancel_order",
    "/fapi/v1/order",
    "/api/v3/order",
    "X-MBX-APIKEY",
)

# Third-party libraries the shipped code may import. A new one (an exchange
# client, for instance) needs a deliberate edit here and in REQUIREMENTS.
ALLOWED_THIRD_PARTY = {"fastapi", "pydantic", "pydantic_settings", "pandas", "numpy", "backend"}
REQUIREMENTS = {"fastapi", "uvicorn", "pandas", "numpy", "pydantic-settings", "pytest", "httpx"}

# Rule: core/ is pure calculation: no files, no network, no clocks, no databases.
ALLOWED_CORE_IMPORTS = {"__future__", "dataclasses", "math", "typing", "numpy", "pandas", "backend"}
FORBIDDEN_CORE_NAMES = {"open", "print", "input", "eval", "exec", "compile", "__import__"}
FORBIDDEN_CORE_ATTRIBUTES = {
    "read_csv", "read_json", "read_excel", "read_parquet", "read_pickle", "read_sql", "read_html", "read_table",
    "to_csv", "to_json", "to_excel", "to_parquet", "to_pickle", "to_sql",
    "now", "utcnow", "today", "save", "savetxt", "load", "loadtxt", "fromfile", "tofile", "urlopen",
}  # fmt: skip

# Rule: price sources read public market data only. No account or order
# endpoints, no signed requests, no exchange API-key headers.
FORBIDDEN_DATASOURCE_TOKENS = (
    "/order",
    "/account",
    "/positionRisk",
    "/leverage",
    "/listenKey",
    "signature",
    "X-MBX-APIKEY",
    "POST",
)


def python_files(*directories: Path) -> list[Path]:
    return sorted(p for d in directories for p in d.rglob("*.py") if "__pycache__" not in p.parts)


def imports(source: str) -> list[tuple[str, int]]:
    """(module, relative level) for every import in the source."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [(alias.name, 0) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            found.append((node.module or "", node.level))
    return found


def token_violations(source: str, tokens: tuple[str, ...]) -> list[str]:
    return [token for token in tokens if token in source]


def third_party_violations(source: str) -> list[str]:
    roots = {module.split(".")[0] for module, level in imports(source) if level == 0}
    return sorted(roots - sys.stdlib_module_names - ALLOWED_THIRD_PARTY)


def core_violations(source: str) -> list[str]:
    found = []
    for module, level in imports(source):
        root = module.split(".")[0]
        if level:
            found.append(f"relative import of {module or '.'}")
        elif root not in ALLOWED_CORE_IMPORTS:
            found.append(f"imports {module}")
        elif root == "backend" and not module.startswith("backend.app.core"):
            found.append(f"imports {module}")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_CORE_NAMES:
            found.append(f"uses {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_CORE_ATTRIBUTES:
            found.append(f"uses .{node.attr}")
    return found


def test_shipped_code_contains_no_trading_calls():
    files = python_files(*SHIPPED_DIRS)
    assert len(files) > 10, "shipped code not found"
    for path in files:
        found = token_violations(path.read_text(encoding="utf-8"), FORBIDDEN_TRADING_TOKENS)
        assert not found, f"{path.relative_to(BACKEND_DIR)} mentions {found}"


def test_shipped_code_imports_only_known_libraries():
    for path in python_files(*SHIPPED_DIRS):
        found = third_party_violations(path.read_text(encoding="utf-8"))
        assert not found, f"{path.relative_to(BACKEND_DIR)} imports {found}: add it to the allow-list on purpose"


def test_requirements_hold_only_known_libraries():
    lines = (BACKEND_DIR.parent / "requirements.txt").read_text(encoding="utf-8").splitlines()
    names = set()
    for line in lines:
        line = line.split("#")[0].strip()
        if line:
            name = line
            for mark in "<>=!~[; ":
                name = name.split(mark)[0]
            names.add(name.lower())
    assert names <= REQUIREMENTS, f"unexpected requirements: {sorted(names - REQUIREMENTS)}"


def test_core_is_pure_calculation():
    files = python_files(CORE_DIR)
    assert files, "no core files found"
    for path in files:
        found = core_violations(path.read_text(encoding="utf-8"))
        assert not found, f"core/{path.name}: {found}"


def test_price_sources_use_public_market_data_only():
    files = python_files(DATASOURCE_DIR)
    assert files, "no datasource files found"
    for path in files:
        found = token_violations(path.read_text(encoding="utf-8"), FORBIDDEN_DATASOURCE_TOKENS)
        assert not found, f"datasource/{path.name} mentions {found}"


def test_the_tripwires_do_trip():
    # Each sample is a way a rule was shown to slip past the first version of these guards.
    assert token_violations("client.new_order(symbol='XAUUSDT')", FORBIDDEN_TRADING_TOKENS) == ["new_order"]
    assert token_violations("import MetaTrader5 as mt5\nmt5.order_send(r)", FORBIDDEN_TRADING_TOKENS)
    assert third_party_violations("import ccxt\nfrom binance.um_futures import UMFutures") == ["binance", "ccxt"]
    assert third_party_violations("import json, time\nimport pandas as pd\nfrom backend.app import config") == []
    impure = (
        "import pandas as pd\n"
        "from .. import datasource\n"
        "frame = pd.read_csv('data/x.csv')\n"
        "stamp = pd.Timestamp.now()\n"
        "handle = open('x')\n"
        "module = __import__('urllib.request')\n"
    )
    found = core_violations(impure)
    for expected in ("relative import", "uses .read_csv", "uses .now", "uses open", "uses __import__"):
        assert any(expected in item for item in found), expected
    assert core_violations("import os") == ["imports os"]
    assert core_violations("from backend.app.datasource import http") == ["imports backend.app.datasource"]
