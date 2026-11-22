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
- Login password: python -m backend.scripts.set_password (writes the hash to .env)
- Web app: python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
  It runs the live feed loop itself (backend/app/live.py). API shapes: docs/WEB.md.
- Worker: python -m backend.app.worker (from Phase 6)

## Conventions
- Python 3.11+, type hints, small pure functions.
- Times are UTC Unix seconds everywhere.
- Lightweight Charts v5 API: chart.addSeries(CandlestickSeries, ...), createSeriesMarkers(...).
- The chart's timeframe (M1 to MN12, backend/app/chart.py, core/candles.resample) is a view
  only: signals stay on M15 with the H1 filter. Longer candles are built from real shorter
  ones, never over a hole. No forecast is drawn unless a real prediction output exists.
- Web UI text is Mongolian (Cyrillic). Look and layout follow docs/PLAN.md section 11,
  which the owner approved from a mock-up on 2026-11-11.
- The pages load nothing from other hosts (strict content security policy). Third-party
  browser code is kept in frontend/static/vendor with its licence. No inline scripts.
- Server strings never go into innerHTML.

## Status
- Phase 0 (repo and environment): done.
- Phase 1 (indicators): done.
- Phase 2 (scoring rule, levels, entry zone): done.
- Phase 3 (price feed): code done; waiting on the owner's 24 h collection run
  and 20 MT5 price readings, then choose the source. Do not start Phase 4 unasked.
- Web (Phase 7) was brought forward at the owner's request on 2026-11-22: login, live
  dashboard, in-memory live feed with the H1 / duplicate / market-hours filters, browser
  and email notifications. Still to come in their phases: backtest (4), news window (5),
  SQLite and the separate worker (6), AI digest (8), deployment (9).
