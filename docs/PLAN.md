# TradePilot AI: plan v0.3

English working copy of the plan for coding sessions. The Mongolian original
lives in the owner's Claude Docs artifact "TradePilot AI — 24/7 сигналын вэбийн
төлөвлөгөө v0.3". If the two disagree, ask the owner.

## 1. What this is

A website that runs 24/7 on a server, reads the XAUUSD price and the news,
and tells a small private group when to enter, at what price, and where to
put the stop loss and take profit, with the reasons. Signals go out by email
and are shown on a password-protected dashboard.

The owner trades by hand on MT5. The system never places orders and has no
broker connection.

Signal quality is not promised. It is measured, by backtest and by the live
signal log.

## 2. Scope of v1

In:
- Collect the price 24/7 and build M15 and H1 candles.
- Scoring rule, H1 confirmation, news window.
- Signals with entry zone, SL, TP and a validity period.
- Economic calendar and an hourly AI digest of headlines.
- Email notifications, password-protected dashboard.
- Backtest report and a live signal log with outcomes.

Out:
- Sending orders, automated trading, any MT5 or broker connection.
- Position sizing or money-management advice.
- Analysing every headline within seconds.
- A public signal service.
- Machine-learning price prediction, multiple symbols.

## 3. Budget: start at $0

Every external source sits behind an interface. A free source is replaced
only when measurement shows it is the bottleneck.

| Part | Free option | Trade-off | Paid fallback |
| --- | --- | --- | --- |
| Price | Binance XAUUSDT perpetual, public market data | Not spot XAUUSD, so it differs from the broker's price. Keyless candle access and reachability from the server region must be tested in Phase 3. | none |
| Price, backup | Twelve Data Basic: 8 credits/min, 800 requests/day | Unclear whether XAU/USD is on the free plan; "internal non-display usage only". | Grow, $29/month |
| Server | Oracle Cloud Always Free (Arm, 2 OCPU / 12 GB) | Free terms were halved in June 2026 and can change again. | small VPS |
| Server, backup | Owner's own computer, always on | Signals stop when the PC or its internet is down. | small VPS |
| Calendar | An economic-calendar API with a free tier | Rate limits; pick the provider in Phase 5. | none |
| AI digest | Gemini API free tier | Limits vary by model and project and are not guaranteed, so one digest per hour, not one per headline. | pay per token |
| Email | Gmail SMTP with an app password | 500 emails per 24 hours (plenty). Needs 2-Step Verification on the sending account. Email can be delayed or land in spam. | transactional email service |

Sources checked on 2026-11-11: twelvedata.com/pricing.md, the InfoQ report on
Oracle's free-tier limits, ai.google.dev/gemini-api/docs/rate-limits,
Cointelegraph's report on Binance gold perpetuals, the Nylas guide to Gmail
SMTP settings.

## 4. Architecture

One worker process computes a signal each time a candle closes, stores it and
sends it. The web app only shows stored results.

```
price sources (Binance XAUUSDT, Twelve Data, CSV)     news sources (calendar, RSS)
                |                                                |
                v                                                v
        +---------------------- worker, 24/7 ----------------------+
        |  core/: indicators, score, filters,   <--  news/: calendar window,  |
        |         entry zone, SL, TP, backtest       hourly AI digest (grades only) |
        +-------------------------------------------------------------+
                                   |
                                   v
              signal: side, entry zone, SL, TP, grade, reasons
                   |                                   |
                   v                                   v
        SQLite (candles, signals, outcomes) ---> web dashboard      email
                                                  (login)             |
                                                     |                |
                                                     v                v
                                   the owner decides and trades by hand on MT5
```

| Part | Technology | Role |
| --- | --- | --- |
| Price sources | `binance_source.py`, `twelvedata_source.py`, `csv_source.py` | One `DataSource` interface. Tests and backtests run on CSV. |
| Worker | Python 3.11+, asyncio | Collect candles, compute signals, track outcomes, notify |
| Core | pandas, NumPy | Indicators, score, filters, backtest. No external connections. |
| News | calendar API, RSS, Gemini API | Event window, hourly digest |
| Storage | SQLite | Candles, signals, outcomes, digests |
| Web | FastAPI, HTML / JavaScript, Lightweight Charts v5 | Dashboard, login |
| Notifications | SMTP email | Signals and system alerts |
| Deployment | Docker Compose, HTTPS reverse proxy | Everything on one server, restarts by itself |

Target layout:

```
tradepilot/
├─ CLAUDE.md
├─ docs/PLAN.md
├─ requirements.txt
├─ .env.example
├─ docker-compose.yml          # Phase 9
├─ backend/
│  ├─ app/
│  │  ├─ main.py               # FastAPI: API, login, static files
│  │  ├─ worker.py             # 24/7 loop: candle -> signal -> notification
│  │  ├─ config.py             # .env settings
│  │  ├─ store.py              # SQLite
│  │  ├─ notify.py             # email (SMTP)
│  │  ├─ core/
│  │  │  ├─ indicators.py      # EMA, RSI, MACD, ATR
│  │  │  ├─ signal.py          # score, levels, entry zone
│  │  │  ├─ filters.py         # H1 confirmation, news window, grade
│  │  │  └─ backtest.py        # engine, metrics
│  │  ├─ datasource/
│  │  │  ├─ base.py            # DataSource interface
│  │  │  ├─ binance_source.py
│  │  │  ├─ twelvedata_source.py
│  │  │  └─ csv_source.py
│  │  └─ news/
│  │     ├─ calendar.py        # economic calendar
│  │     ├─ headlines.py       # RSS headlines
│  │     └─ digest.py          # AI digest
│  ├─ scripts/                 # history download, backtest runner
│  └─ tests/
├─ frontend/
└─ data/                       # SQLite, CSV; not in git
```

## 5. Signal logic

Four steps: the M15 scoring rule proposes a side, filters decide whether it
is blocked, levels are computed, and the news digest sets a grade. Only fully
closed candles are used.

### 5.1 Scoring rule (M15)

Five conditions, each +1, 0 or -1; the sum runs from -5 to +5.
Sum >= +3 proposes BUY, sum <= -3 proposes SELL, otherwise HOLD.

| # | Condition | +1 (up) | -1 (down) |
| --- | --- | --- | --- |
| 1 | Long-term trend | Close > EMA200 | Close < EMA200 |
| 2 | Short-term trend | EMA20 > EMA50 | EMA20 < EMA50 |
| 3 | MACD vs signal line | MACD > signal | MACD < signal |
| 4 | MACD vs zero line | MACD > 0 | MACD < 0 |
| 5 | RSI(14) zone | 50 < RSI < 70 | 30 < RSI < 50 |

Equal sides score 0. RSI >= 70 or RSI <= 30 also scores 0.
Signal strength = abs(sum) / 5.

Indicator formulas: EMA is recursive, seeded with the first value. RSI and
ATR use Wilder's smoothing with an SMA seed. The MACD signal line is the
EMA(9) of the MACD line. A platform's own indicators may smooth differently.

### 5.2 Filters

If any of these holds, no signal is sent and the reason code is logged.

| Filter | Blocks when | Code |
| --- | --- | --- |
| H1 trend | BUY while the H1 score is below +1, SELL while it is above -1 | `HTF_CONFLICT` |
| News window | From 30 minutes before to 15 minutes after a high-impact USD event | `NEWS_WINDOW` |
| Market hours | XAUUSD is closed at the broker (weekends, for example). Needed because the Binance contract trades 24/7. | `MARKET_CLOSED` |
| Candle count | Fewer than 300 closed candles | `INSUFFICIENT_BARS` |
| Price age | Latest price older than 60 seconds | `STALE_FEED` |
| Duplicate | A signal in the same direction is still valid | `DUPLICATE` |

### 5.3 Levels

- Entry: close of the signal candle, a reference price.
- Entry zone: Entry +- 0.25 x ATR(14). If the price has left the zone, do not chase.
- SL: Entry - 1.5 x ATR for BUY, Entry + 1.5 x ATR for SELL.
- TP: Entry + 2.0 x ATR for BUY, Entry - 2.0 x ATR for SELL.
- Validity: 2 candles, 30 minutes on M15.

SL and TP are written both as prices and as distances from Entry. The feed
can differ from the broker's price, so the distances are applied to the real
fill price. At this SL / TP ratio the break-even win rate before spread is
1.5 / (1.5 + 2.0) = 42.9%.

### 5.4 News digest and grade

Once an hour the AI reads headlines and calendar results and returns a
structured view for gold: `bullish`, `bearish` or `neutral`, the main
reasons, and nearby risks.

| Grade | When |
| --- | --- |
| A | News direction agrees with the signal |
| B | News is neutral, or there is no digest |
| C | News direction is against the signal |

The digest never creates or flips a signal. It only grades. Its weight is
raised only after the log shows that grades relate to outcomes.

Every number above (thresholds, 0.25, 30 and 15 minutes, 2 candles) is a
proposed starting value, not a validated strategy. Change them only with a
backtest, and bump `rules_version` each time.

### 5.5 Email template (numbers are made up)

```
Subject: [TradePilot] XAUUSD BUY · grade A · 4050.00

XAUUSD · M15 · BUY · grade A
Entry zone: 4048.00 - 4052.00 (reference 4050.00)
SL: 4038.00 (-12.00)    TP: 4066.00 (+16.00)
Valid until 09:30 UTC (30 minutes)
Score: +4/5 · H1 score: +2 · RSI 71.2 · ATR 8.00
News: bullish, <one-sentence reason>
Next event: US CPI, in 3 h 10 min
Status: experimental (backtest gate not passed)
Not financial advice.
```

## 6. Backtesting and measurement

The scoring rule and the H1 filter are tested on historical candles. The
news layer can only be measured on the live log, because the free path has
no archive of past news.

Historical test rules:
1. Download M15 history from the chosen source into `data/` as CSV.
2. Evaluate on every closed candle. On a signal, enter at the next candle's
   open and compute SL and TP from that price.
3. One position at a time. If one candle touches both SL and TP, count SL first.
4. Costs: spread is a parameter, set from the broker's real spread.
5. Tune on the first 70% of the data, evaluate only on the last 30%.

Metrics: number of trades, win rate, expectancy (R), profit factor, max
drawdown (R), longest losing streak. 1R is the distance to the SL.

Gate: at least 100 trades on the last 30% and positive expectancy after
costs marks signals "backtest passed". Otherwise signals carry the status
"experimental" and the rules go back to Phase 2. 100 and 70 / 30 are
proposed starting thresholds.

Live log: the worker follows the price after each signal and records
whether TP was hit, SL was hit, or the signal expired. Grades A, B and C
are reported separately.

Price difference: in Phase 3 record the source price next to the broker's
MT5 price 20 times and report the mean and maximum difference. If the
difference is larger than the entry zone, change the source.

Past results do not prove future profit.

## 7. API and notifications

Everything except `/api/health` requires login. All times are UTC Unix seconds.

| Method | Endpoint | Purpose |
| --- | --- | --- |
| POST | `/api/login` | Log in with the password, receive a session cookie |
| GET | `/api/health` | Worker, source, age of the last candle |
| GET | `/api/quote` | Latest price and the source name |
| GET | `/api/candles?timeframe=M15&limit=500` | Candles |
| GET | `/api/signal/latest` | The valid signal, or the reason code for none |
| GET | `/api/signals?limit=100` | Signal log with outcomes |
| GET | `/api/news` | Upcoming events, latest AI digest |
| GET | `/api/stats` | Backtest and live metrics, by grade |

Two kinds of email are sent: signals and system alerts. An alert goes out
when no price has arrived for 5 minutes or the worker has restarted. A signal
is valid for only 30 minutes, so the email states the expiry time. No signal
is ever produced from a made-up price.

## 8. Phases

| Phase | Build | Done when | External dependency |
| --- | --- | --- | --- |
| 0. Repo and environment | GitHub repo, layout, `CLAUDE.md`, test setup, empty FastAPI app | `pytest` passes. `GET /api/health` answers. | no |
| 1. Indicators | `core/indicators.py`: EMA, RSI, MACD, ATR | Unit tests on hand-computed data pass. | no |
| 2. Scoring rule | `core/signal.py`: 5 conditions, thresholds, levels, entry zone | Tests for +1 / 0 / -1 of each condition, the thresholds at exactly +-3, and fewer than 300 candles pass. | no |
| 3. Price source | `DataSource` interface, Binance and Twelve Data adapters, CSV, M15 to H1 aggregation | Candles collect for 24 hours without a gap. A report compares 20 readings with the broker's price, and a source is chosen. | price API |
| 4. Backtest | History download script, `core/backtest.py`, report | Engine tests pass: SL first when a candle touches both; no future candles used. The M15 report exists. | history download |
| 5. Filters | `core/filters.py`: H1 confirmation, calendar window, duplicates | Each filter's tests pass. A backtest with and without the H1 filter is compared. | calendar API |
| 6. Worker and email | 24/7 loop, SQLite, notifications, outcome tracking | A test signal arrives by email within 1 minute. Cutting the source produces an alert. | SMTP account |
| 7. Web | API, login, dark dashboard | A request without login gets 401. Chart, signal, log and metrics are visible. | no |
| 8. AI digest | RSS headlines, Gemini, grades | A structured digest appears every hour. When the AI fails, signals still go out with grade B. | Gemini API |
| 9. Deployment | Docker Compose, HTTPS, data backup | After a server restart everything comes back by itself. 72 hours without interruption. | server |
| 10. Observation | Log signals for 2 to 4 weeks | A report compares live results with the backtest, and a decision on next steps is made. | no |

Stop after Phase 4 and review the backtest gate together with the owner.
Even without passing the gate, signals may flow from Phase 6 with the status
"experimental" while the rules are improved.

Status: Phases 0, 1 and 2 are done.

## 9. Safety and limits

The system cannot trade by construction: no order functions, no broker
account, no trading API keys.

- Login: every page and API route is password-protected. Passwords are
  stored hashed, traffic goes over HTTPS.
- Secrets: API keys and the SMTP password live only in the server's `.env`.
- No fabricated prices: when the source is down, signals stop and an alert is sent.
- Unvalidated signals are labelled "experimental".
- The AI makes no decisions: the digest only grades, so a misread or
  invented headline cannot change a signal's side or levels.
- Data terms: check each source's terms of use, in particular the right to
  display prices to others, in Phase 3.
- No public distribution: offering trading signals to the public is
  regulated in some countries. Get legal advice before widening the audience.
- Signals are not financial advice. Spread, slippage and the price
  difference change real results. The system gives no lot-size advice.

## 10. Open questions

- ~~Which broker's MT5 does the owner trade on?~~ Answered 2026-11-11: MOT Capital,
  MT5 demo account, leverage 1:500, web terminal at mt5-demo.motcapital.com/terminal.
  Phase 3 compares the feed with this terminal's gold price.
  Gold symbol at MOT Capital: `XAUUSD` ("Contract for Spot Gold"), 2 decimals.
  One spread reading: bid 4062.08 / ask 4062.21 = 0.13 USD, at 2026-11-11
  07:44 UTC (Asian session, no news). Spreads widen around rollover and news,
  so take more readings in Phase 3 and set the backtest spread from them.
- Is M15 still the main timeframe? (Phase 4)
- Which email addresses receive signals, and which Gmail account sends them? (Phase 6)
- Server: Oracle's free server, or the owner's own computer running 24/7?
  Oracle sign-up may require a card. (Phase 9)
- Buy a domain name for the site? Needed for HTTPS. (Phase 9)
