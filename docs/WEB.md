# Web app: how it works and what the API returns

Built ahead of the plan's order at the owner's request (2026-11-22). The web
process runs its own live loop for now; storage, outcome tracking by a worker
and the news layer still come in their phases.

## Flow

```
price source --every 2 s--> LiveFeed (backend/app/live.py, one thread)
   newest 3 candles + quote, asked for in parallel
   -> laid over the 1500 M15 candles kept in memory (core/candles.merge)
   -> when a candle has closed: rule (core/signal) + filters (core/filters), once
   -> new signal: alert in the page, browser notification, email if SMTP is set
   -> every cycle: one `tick` pushed to every open page (server-sent events)
```

A cycle costs two small public requests. Indicators are recomputed only when
a candle closes; a tick only moves the price, the forming candle and the
state of the current signal. A signal reaches the page about 2 to 4 seconds
after the exchange lists the next candle.

Feed status: `starting`, `ok`, `late` (no fresh data for 10 s), `down` (60 s,
`TRADEPILOT_STALE_AFTER_SECONDS`). The age that decides it is the largest of
three: the time since anything in the answers changed, the age of the price
by its own time stamp, and how long the forming candle has been overdue. So
the feed is down when either half stands still: a live price over candles
that stopped coming, or candles that keep closing under a price that is
repeated unchanged. While down the page shows the feed-down card; a candle
that closes then is decided but stopped as `STALE_FEED`, and is not sent
later. After 5 minutes (`TRADEPILOT_ALERT_AFTER_SECONDS`) an email alert goes
out, and one more when the feed is back. If candles were missed the whole
history is reloaded, and every signal decided in it that has not been looked
at before and is still valid is announced (a signal lives for two candles, so
the one from the candle before the newest can be among them). A decision made
before the reload is never rewritten by it.

A hole in the source's own history (a candle the source never had) is said
once, as the `history_gaps` alert. It is no reason to load the history again
on every cycle: only a hole that was not there before is.

Between cycles (a slow source, a pause after the source refused requests) a
tick still goes out every poll interval. It carries no new data, only the
feed's age judged at that moment, so a page can tell a silent source from a
lost server.

The clock (`now`) is the newest time the source has reported plus the time
passed since. It keeps running when the source repeats an old answer.

Filters applied now: stale feed, market hours (a rule of thumb, see
`core/pricecheck.probably_closed`, until the broker's hours are known), H1
trend, duplicate. Not yet: the news window (Phase 5). Every signal carries
the status "experimental" and grade B (no digest).

After a restart the last 192 closed candles are judged again for the log and
the chart markers; nothing from that replay is announced or emailed.

## Login

`python -m backend.scripts.set_password` writes the scrypt hash and a session
secret to `.env`. Session: signed cookie `tp_session`, HttpOnly,
SameSite=Strict, 12 hours. Five wrong passwords start a growing pause.
Public: `GET /api/health`, `GET /login`, `POST /api/login`, `POST /api/logout`,
and `/static/styles.css`, `/static/login.js`, `/static/favicon.svg`. Everything
else answers 401 (API, static files) or redirects to `/login` (the page).

How the login behaves:

- One password is checked at a time, so tries sent together cannot pass the
  pause before they are counted.
- A refused request (422) says which field is wrong and never repeats what
  was sent. An error nothing handled is a plain 500; every answer, that one
  included, carries the security headers.
- `POST /api/logout` ends the session only for a request that carries it. A
  form on another site is sent without the cookie and changes nothing.
- An open `/api/stream` ends when its session runs out; the page then
  reconnects and gets 401. Before it leaves it says so with a notification
  and the alert sound, then goes to `/login?expired=1`, whose title and
  message say that the session ended. Nothing renews a session: an open
  dashboard stops announcing signals after `TRADEPILOT_SESSION_HOURS`.
- Static paths are checked as text first: a backslash, a colon, a NUL or a
  `..` part is a 404 before any file call.
- On Ctrl-C the open streams are ended first, so the server stops although a
  dashboard is open.

Known limits, accepted for a one-owner site:

- The pause after wrong passwords is one counter for everybody, not one per
  address: someone guessing can also keep the owner waiting.
- Logging out removes the cookie from that browser; the token itself stays
  valid until its 12 hours are over. Setting a new password (a new session
  secret) ends every session at once.
- `set_password` writes `.env` with mode 0600 on Linux and macOS. On Windows
  the folder's access list decides who can read it; other accounts on the
  same PC may be able to.

## Routes behind login

| Route | Returns |
| --- | --- |
| `GET /` | the dashboard page |
| `GET /api/state` | the snapshot below |
| `GET /api/stream` | server-sent events, event name `tick`, data = the tick below |
| `GET /api/candles?timeframe=<chart timeframe>&limit=1..1500` | the candles answer below; 422 for a timeframe the source cannot show, 503 with `Retry-After` when the source gives nothing |
| `GET /api/quote`, `/api/signal/latest`, `/api/signals?limit=` | parts of the snapshot |
| `GET /api/news`, `/api/stats` | `{"available": false, ...}` until Phases 4, 5 and 8 |
| `POST /api/notify/test` | sends a test email; 409 when SMTP is not configured |

All times are UTC Unix seconds. Prices are the feed's (Binance XAUUSDT), not the broker's.

### Tick (pushed every cycle, also the first part of the snapshot)

```
v              int    grows whenever the snapshot changed: fetch /api/state again
now            float  current time by the source's clock
feed           "starting" | "ok" | "late" | "down"
age            float  seconds since fresh data (see "Feed status" above)
price, bid, ask       float or null (the last real values while the feed is down)
market_closed  bool   rule of thumb for the broker's hours
bars           {"M15": [bar, bar], "H1": [bar, bar]}  the two newest candles of the signal
               timeframe and of H1; {} while the feed is down
               bar: {time, open, high, low, close, closed}; closed false = still forming
card           the signal card, one of:
  {kind: "starting"}
  {kind: "feed_down", last_price, last_time, age, emailed}
     last_time: the last price's own time; emailed: the feed-down email has gone out
  {kind: "signal", state: "valid" | "outside_zone", ...signal row}
  {kind: "hold" | "expired", candle_close, next_check, score, side, h1_score}
  {kind: "blocked", code, candle_close, next_check, score, side, h1_score}
     code: HTF_CONFLICT | MARKET_CLOSED | INSUFFICIENT_BARS | STALE_FEED
```

Signal row: `time` (the candle's close), `side` (BUY / SELL), `entry`,
`zone_low`, `zone_high`, `sl`, `tp`, `sl_distance`, `tp_distance` (positive
amounts), `expires_at`, `valid_seconds`, `grade` ("B"), `run` (1 = first of a
run of back-to-back signals of one side), `score`, `h1_score`.

### Snapshot (`GET /api/state`): the tick plus

```
symbol "XAUUSD", timeframe "M15", source {name: "binance", symbol: "XAUUSDT"}
reasons   null or {candle_close, score, side, blocked, conditions: [{id, value}], h1_score, rsi, atr}
          condition ids: trend_ema200, trend_ema20_50, macd_vs_signal, macd_vs_zero, rsi_zone
log       sent signals, newest first (up to 60): signal row + outcome
          outcome: {kind: "valid"} | {kind: "tp" | "sl", time} | {kind: "none"}
          (first level the feed's price reached, measured from the reference entry)
today     {total, buy, sell}   signals since midnight of the display zone
alerts    newest first: {id, time, level: "ok" | "warn" | "down", code, text}
          codes: started, feed_down, feed_back, reloaded, history_gaps, email_failed,
          email_test, signal, signal_emailed. `text` is Mongolian, ready to show.
email     bool   whether SMTP is configured
utc_offset_hours  8
alert_after_seconds  300   how long the feed is down before the email alert
poll_seconds   2.0   how often ticks come; the page calls the server lost after
               max(10 s, 3 x this) without one
rules     {status: "experimental", filters_off: ["NEWS_WINDOW"], market_hours: "approximate"}
chart     {timeframes: [...], signal_timeframe: "M15", filter_timeframe: "H1"}
          timeframes: the chart timeframes this source can show, shortest first
prediction  {available: false, timeframe: "M15", horizon_candles: 4}
          the place for a forecast of the next 4 M15 closes (60 minutes). Nothing in the
          project produces one, so it is always "not available" and the page draws nothing.
```

### Candles (`GET /api/candles`)

```
timeframe      the chart timeframe asked for
source, symbol the price source and its instrument, e.g. "binance", "XAUUSDT"
built_from     null, or the shorter candles the server combined (M45 from "M15")
history        "full" | "short"; short: the first bar is the oldest candle the source has
partial_first  true when the first bar was built from a bucket whose beginning does not exist
gaps           places where a candle is missing between two bars (nothing is drawn there)
bars           oldest first: {time, open, high, low, close, volume, closed}
               closed false marks the forming candle (only the last one can be)
```

## Chart timeframes

The chart's timeframe is a way of looking at prices and nothing else. The
rule runs on M15 with H1 as its filter whatever the chart shows; signal
markers are placed on the candle of the chosen timeframe they fall in.

Menu: M1, M2, M3, M5, M10, M15, M30, M45, H1, H2, H3, H4, D1, W1, MN1, MN3,
MN6, MN12. Boundaries are UTC: fixed lengths count from midnight, weeks from
Monday, MN3 / MN6 / MN12 are calendar quarters, half-years and years
(`core/candles.opens`).

Where a timeframe's candles come from (`backend/app/chart.py`):

- M15 (the signal timeframe): the live feed's memory, the very candles the
  rule ran on.
- A length the source has itself (Binance: M1, M3, M5, M30, H1, H2, H4, D1,
  W1, MN1): asked from the source, kept in memory, and refreshed with a
  3-candle request at most once per poll interval however many pages ask.
- Anything else is built from the longest shorter candles that fill it
  exactly: M2 from M1, M10 from M5, M45 from M15, H3 from H1, MN3 / MN6 /
  MN12 from MN1. A source without intervals of its own (Twelve Data, CSV)
  gets M15 and whatever M15 can build.

Building (`core/candles.resample`): first open, highest high, lowest low,
last close, summed volume. A candle is never built over a hole: that bucket
is left out and counted in `gaps`. The newest bucket is kept as forming; the
oldest one is kept, and flagged `partial_first`, only when the source has
nothing older.

Live updates: M15 and H1 ride on the tick. For any other timeframe on screen
the page asks for the 3 newest candles about every 2 seconds and moves the
chart with them. When the source fails, the chart request answers 503, the
source is left alone for 5 seconds (or as long as it asked after a refusal),
and no kept candle is passed off as current. None of this touches the signal
path.

In the page the chosen timeframe and the starred ones (quick buttons, M15 and
H1 by default) are kept in the browser's local storage. Choosing quickly is
safe: every load carries a number and only the newest one may fill the chart.

## The page's own safeguards

- A request that gets no answer within 15 seconds is given up and retried, so
  one stalled request cannot block the snapshots (and with them the signal
  notification) or the candle loads after it.
- The tab title's flag for a new signal ("BUY XAUUSD · ...") goes when the
  card is no longer that signal.
- A browser that grants notifications and then cannot show one (phones allow
  them from a service worker only) gets the button "cannot show
  notifications" instead of "on". Phones are not served yet.
- The login page's pause after too many wrong passwords runs to a moment in
  time, so a PC that slept is released when the pause is really over.

## The page and the chart

The chart holds the candles of `/api/candles` and moves its two newest bars
with every tick. When a tick shows that a candle on screen missed its close
(the page slept, the stream was down), the page loads the candles again and
keeps asking on every tick until one load succeeds.

The source's instrument can trade while the broker is closed (Binance
XAUUSDT trades through the weekend). Those candles are real trades of the
source and are drawn; the chart says so in a note while `market_closed` is
true, and no signal is sent in those hours.
