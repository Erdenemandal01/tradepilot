"""Structural guards for the hard rules in CLAUDE.md.

These tests read the source code itself, so a rule cannot be broken quietly.
"""

from __future__ import annotations

import ast
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1] / "app"
CORE_DIR = APP_DIR / "core"

# Rule: signals only. Nothing in the application may send or check orders
# or talk to a trading terminal.
FORBIDDEN_TRADING_TOKENS = ("order_send", "order_check", "MetaTrader5")

# Rule: core/ is pure calculation. Anything outside this list (files, network,
# clocks, databases) is not allowed there.
ALLOWED_CORE_IMPORTS = {"__future__", "dataclasses", "math", "typing", "numpy", "pandas", "backend"}


def _python_files(directory: Path) -> list[Path]:
    return sorted(path for path in directory.rglob("*.py") if "__pycache__" not in path.parts)


def _imported_modules(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_application_contains_no_trading_calls():
    files = _python_files(APP_DIR)
    assert files, "no application files found"
    for path in files:
        source = path.read_text(encoding="utf-8")
        for token in FORBIDDEN_TRADING_TOKENS:
            assert token not in source, f"{path.relative_to(APP_DIR)} mentions {token}"


def test_core_imports_only_pure_calculation_modules():
    files = _python_files(CORE_DIR)
    assert files, "no core files found"
    for path in files:
        for module in _imported_modules(path):
            root = module.split(".")[0]
            assert root in ALLOWED_CORE_IMPORTS, f"core/{path.name} imports {module}"
            if root == "backend":
                assert module.startswith("backend.app.core"), f"core/{path.name} imports {module}"
