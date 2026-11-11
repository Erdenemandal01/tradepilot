# TradePilot AI

24/7 XAUUSD signal service: price feed -> signal engine -> email + web dashboard.
Signals only. Full plan: docs/PLAN.md.

## Hard rules
- Never place, modify or close orders. No broker accounts, no trading API keys.
- core/ is pure Python (pandas, NumPy): no I/O, no network.
- Every external service sits behind an interface (datasource/, news/, notify.py)
  and has a fake for tests.
- Never fabricate prices. If the feed is stale or down: no signal, send a system alert.
- Signals use closed candles only.
- The AI digest never creates or flips a signal; it only grades it.
- Secrets live in .env on the server. Nothing secret in code or git.
- All pages and API routes except /api/health require login.

## Workflow
- One phase from docs/PLAN.md at a time. Do not start the next phase unasked.
- Tests first for core/. Run `pytest -q` before reporting done.
- End each phase by showing the acceptance checks and their output.
- Stop after Phase 4 and report the backtest results.

## Commands
- Windows dev PC: `python` is the Store stub; use `py -m venv .venv`, then `.venv\Scripts\python`.
- Install: pip install -r requirements.txt
- Test: pytest -q
- Collect candles: python -m backend.scripts.collect_candles --hours 24
- Gap check: python -m backend.scripts.check_candles data/collect_binance_M15.csv
- Price comparison: python -m backend.scripts.compare_prices (then --report)
- API: uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
- Worker: python -m backend.app.worker (from Phase 6)

## Conventions
- Python 3.11+, type hints, small pure functions.
- Times are UTC Unix seconds everywhere.
- Lightweight Charts v5 API: chart.addSeries(CandlestickSeries, ...), createSeriesMarkers(...).

## Status
- Phase 0 (repo and environment): done.
- Phase 1 (indicators): done.
- Phase 2 (scoring rule, levels, entry zone): done.
- Phase 3 (price feed): code done; waiting on the owner's 24 h collection run
  and 20 MT5 price readings, then choose the source. Do not start Phase 4 unasked.
