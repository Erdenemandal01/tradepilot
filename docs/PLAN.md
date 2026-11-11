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
| Price | Binance XAUUSDT perpetual, public market data | Not spot XAUUSD, so it differs from the broker's price. Keyless access confirmed from the owner's network on 2026-11-11; reachability from the server region is checked in Phase 9. Listed 2025-12-11, so history is short. | none |
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
│  │  │  ├─ candles.py         # candle schema, gaps, M15 -> H1 aggregation
│  │  │  ├─ pricecheck.py      # feed vs broker price report
│  │  │  ├─ signal.py          # score, levels, entry zone
│  │  │  ├─ filters.py         # H1 confirmation, news window, grade
│  │  │  └─ backtest.py        # engine, metrics
│  │  ├─ datasource/
│  │  │  ├─ base.py            # DataSource interface
│  │  │  ├─ http.py            # stdlib JSON client
│  │  │  ├─ binance_source.py
│  │  │  ├─ twelvedata_source.py
│  │  │  └─ csv_source.py
│  │  └─ news/
│  │     ├─ calendar.py        # economic calendar
│  │     ├─ headlines.py       # RSS headlines
│  │     └─ digest.py          # AI digest
│  ├─ scripts/                 # candle collector, gap check, price comparison; history download, backtest runner
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
| 7. Web | API, login, dark dashboard in Mongolian (section 11) | A request without login gets 401. Chart, signal, log and metrics are visible. | no |
| 8. AI digest | RSS headlines, Gemini, grades | A structured digest appears every hour. When the AI fails, signals still go out with grade B. | Gemini API |
| 9. Deployment | Docker Compose, HTTPS, data backup | After a server restart everything comes back by itself. 72 hours without interruption. | server |
| 10. Observation | Log signals for 2 to 4 weeks | A report compares live results with the backtest, and a decision on next steps is made. | no |

Stop after Phase 4 and review the backtest gate together with the owner.
Even without passing the gate, signals may flow from Phase 6 with the status
"experimental" while the rules are improved.

Status: Phases 0, 1 and 2 are done. Phase 3 code is done (2026-11-11); its
acceptance checks wait on the owner: a 24-hour collection run
(`collect_candles` then `check_candles`) and 20 MT5 readings
(`compare_prices`, then `--report`). Then a source is chosen.

Phase 3 findings so far (2026-11-11):
- Binance XAUUSDT answers keyless from the owner's network (Mongolia):
  klines and bookTicker both work. Binance spread was 0.01.
- The contract (`TRADIFI_PERPETUAL`) was listed on 2025-12-11, so Binance has
  only about 11 months of history. That limits Phase 4: check that the last
  30% still holds 100 trades, or take history from another source.
- Twelve Data needs the owner's free API key; the adapter is tested against
  assumed response shapes only (nothing was ever recorded from the real
  service). Whether XAU/USD is on the free plan is still unknown.
- Feed versus broker, one data point so far: the MT5 reading of 07:44 UTC on
  2026-11-11 (4062.08 / 4062.21) was about 6.6 USD below Binance's
  one-minute bar of that minute (4068.57 to 4069.09), while the entry zone's
  half width that day ran from about 1.3 to 3.5 USD. If the 20 readings
  confirm a steady offset of that size, the feed needs a fixed offset
  correction or a spot source; `compare_prices --report` says which.
- Data terms: Binance and Twelve Data display rights are not yet checked.
- Dev PC: Windows Smart App Control blocks pandas 3.x DLLs, so
  requirements pin pandas below 3.
- Code review of 2026-11-11 (docs/reviews/2026-11-11-phase3-review.md): 25
  findings; each was then checked by hand and 23 are fixed. The main ones: a
  candle counts as closed only when the source lists a newer one (Binance
  can serve a just-closed candle in a pre-close state for a few seconds, and
  the PC clock can be wrong); transport errors, a locked CSV and HTTP 429 no
  longer stop or endanger the collector; `check_candles` compares every
  saved candle with the source; `compare_prices` judges each reading against
  the entry zone of its own moment, drops slow or doubtful readings and
  needs 20 readings in at least 6 different hours for a verdict.
- Still open from that review, for the owner to decide before Phase 4:
  weekend candles of the 24/7 Binance contract reach the indicators as if
  XAUUSD had traded (about 196 quiet M15 candles per weekend). Blocking
  signals during closed hours (Phase 5) does not remove them from the EMA,
  RSI and ATR windows or from the backtest.

Early observation for Phases 4 and 5 (2026-11-11; an ad hoc script on real
Binance candles made while preparing the UI mock-up, not a backtest): with
the H1 filter and the duplicate rule as written, a trend produces a
same-direction signal every 30 minutes (21 signals on 2026-11-11). In that
55-hour window 13 of 60 signals reached TP before SL, against a 42.9%
break-even. The sample is far too small and too correlated to conclude
from, and it ignores spread and the news filter; Phase 4 measures this
properly. The duplicate rule probably needs to be stricter, for example no
new signal while an earlier one in the same direction is still open.

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
- What are MOT Capital's XAUUSD session hours (weekly open and close, the
  daily break)? Read them from the symbol's specification in MT5. Needed for
  the market-hours filter and for the price comparison. (Phases 3 and 5)
- Should candles from hours when the broker's XAUUSD is closed be removed
  before the indicators and the backtest see them? (Phase 4)
- Should the signal emails be in Mongolian too, like the site? (Phase 6)
- Show times in Ulaanbaatar time (UTC+8) on the site and in emails? (Phases 6 and 7)

## 11. Web UI direction (Phase 7)

The owner's requests of 2026-11-11. Not yet in the Mongolian original of this plan.

1. Language: the whole site is in Mongolian (Cyrillic). Market terms that
   traders already use stay as they are: XAUUSD, BUY, SELL, SL, TP, M15, H1,
   ATR, RSI.
2. Easy to use: the current signal and what to do with it must be readable
   in a few seconds, without scrolling or clicking, on a laptop and on a phone.
3. Look: a dark "control room" dashboard built from cards, like the reference
   image the owner shared (a stock "space habitat operations dashboard"
   vector): deep navy background, rounded cards with thin borders, a slim top
   bar with a status pill, a row of small KPI tiles, chart cards, list cards
   with coloured status pills and thin progress bars, an alerts card, small
   uppercase card labels. The owner prefers this to a pure TradingView look.
4. Chart: take the chart's conventions from TradingView (tradingview.com):
   candles on a dark grid, the price scale on the right with a last-price
   tag, a legend with O H L C on the top-left, a timeframe switch above it.

Inspiration, not copies: no logos, artwork, icons or page assets from either
source, and our own name on the site.

Layout. The owner approved this direction on 2026-11-11 from a mock-up (the
design canvas "TradePilot самбарын загвар" in the owner's Claude account: the
desktop page, the phone layout and the other states of the signal card):

- Top bar: site name, a status pill for feed and worker with the price age,
  the date and time, log out.
- Left rail: icon links to the sections. On a phone it becomes a row.
- Signal card, first in reading order and first on a phone: side, a
  valid-for countdown with a bar, grade and status chips, the entry zone
  with the reference price and whether the current price is inside it, SL
  and TP with their distances from the fill price, a three-step "if you
  enter" note, the not-advice line.
- KPI tiles: price and its source, signals today, the next high-impact
  event, the rule status ("experimental" until the backtest gate is passed).
- Chart card (M15 / H1 switch): candles, BUY / SELL markers, the entry zone
  as a band over the validity window, SL and TP as dashed lines with price
  tags.
- Reasons strip under the chart: the score, the five conditions with
  +1 / 0 / -1, the H1 score, RSI and ATR.
- Lower cards: signal log with outcomes; economic calendar; AI digest;
  statistics (backtest and live); system alerts.
- Footer: a status legend and the signals-only disclaimer.
- The signal card without a signal: HOLD with the score; blocked, with the
  reason (news window and the other reason codes); feed down.
- Phone: the same page in one column.

Constraints:

- Signals only: no order, buy or sell buttons anywhere (hard rule).
- Prices come only from our stored feed. Do not embed TradingView's own
  widgets: they show TradingView's prices, not the ones a signal was
  computed from.
- The chart library is TradingView's open-source Lightweight Charts v5
  (Apache 2.0). Its licence asks for attribution: keep the chart's
  attribution logo on (`layout.attributionLogo`, on by default) or show the
  notice and a link to tradingview.com on the page.
- Fonts must cover Mongolian Cyrillic (Ө, Ү). Pages are UTF-8 with
  `lang="mn"`. Prices keep the broker's format (4062.08).
- Times stay UTC Unix seconds in storage and in the API. What the site
  displays (Ulaanbaatar time is proposed) is an open question above.
- Colour is never the only carrier of meaning: BUY / SELL and every status
  also carry a word or an icon, not just green or red.
- Every page behind login (hard rule).

Before building, look at tradingview.com again: details change.
