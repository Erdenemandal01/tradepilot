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

Feed status: `starting`, `ok`, `late`, `down` (no fresh data for 60 s,
`TRADEPILOT_STALE_AFTER_SECONDS`). `late` starts after 10 s, or after 2.5
poll intervals when that is longer (fresh data comes once per poll), but
never later than three quarters of the stale limit: with the usual 60 s that
is 45 s at the latest, so a feed always reads late before it reads down,
however slow the poll. The age that decides it is the largest of
three: the time since anything in the answers changed, the age of the price
by its own time stamp, and how long the forming candle has been overdue. So
the feed is down when either half stands still: a live price over candles
that stopped coming, or candles that keep closing under a price that is
repeated unchanged. While down the page shows the feed-down card; a candle
that closes then is decided but stopped as `STALE_FEED`, and is not sent
later. After 5 minutes (`TRADEPILOT_ALERT_AFTER_SECONDS`) an email alert goes
out, and one more when the feed is back.

A candle is decided once. If candles were missed the whole history is
reloaded, but only the candles that were not decided before are decided
then, with the last sent signal carried over as on any other cycle. A
decision made before the reload is never rewritten by it: not when one
answer lacks a candle, not when a copy of the source is behind and catches
up, not when the feed is down in between. The log, the outcomes and the
card stay those of the decisions as they were made. Of the newly decided
candles a signal is announced when it can still be valid: it comes from one
of the newest two closed candles (a signal lives for two candles) and has
not expired by the clock.

After a start nothing that closed before the first answer is announced. The
line is drawn by the clock as well as by the candles, so a first answer from
a copy that is behind does not let an older candle through. Such a candle is
decided and shown, like the rest of the replayed history.

Holes in the stored history are known by their place (the two candles around
each). One that was not there before is said, as the `history_gaps` alert
with the number of holes now stored; one that heals is forgotten. A hole the
source already had is no reason to load the history again: only a hole that
was not there before is. What is left of a hole after the source filled a
part of it is not a new hole: no candle the feed had is gone, so it is
neither said again nor a reason to reload. For the same hole the history is
reloaded, and `reloaded` raised, at most once a minute; a hole that comes
and goes with the source's answers is said once.

While the stored history has a hole younger than a day, each time a candle
closes one longer request is made, from the candle before the oldest such
hole on (at most 99 candles on M15), so a candle the source lists late is
picked up without a reload. What that request brings is used only when it
fits the stored candles and opens no new hole. It fills the history the
rule runs on; a candle that turns up this way is not decided afterwards.
When the longer request fails, the cycle goes on with what the usual one
gave and tries again at the next closed candle. When the source refuses it
(418, 429), the cycle's own answers are still used and the next cycle waits
as after any refusal. The chart's own timeframes do the same for a hole
among their newest 100 candles; there a refusal costs that chart request.

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
poll_seconds   2.0   how often ticks come; the page calls the server lost after
               max(10 s, 3 x this) without one. It is in every tick, so a page whose
               /api/state fails still has it.
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
rules     {status: "experimental", filters_off: ["NEWS_WINDOW"], market_hours: "approximate"}
chart     {timeframes: [...], signal_timeframe: "M15", filter_timeframe: "H1"}
          timeframes: the chart timeframes this source can show, shortest first
prediction  the forecast of the next 4 M15 closes (60 minutes); see "The forecast" below
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

## The forecast (`prediction` in `GET /api/state`)

A forecast of the next four M15 closes, shown beside the signals. It never
creates, changes, blocks or grades a signal, and it is not a signal itself.

```
available        bool   false: no model file, too few candles, or the feed is down
reason           null | "no_model" | "not_enough_candles" | "feed_down"
                 "no_model" also for a file that is broken, whose record cannot be true, or
                 that was fitted on another source or instrument than the feed's
                 null with available false: the forecast code itself failed, or the forecast
                 was out of range or contradicted itself (see the server log)
timeframe        "M15"  always: the forecast exists on the signal timeframe only
horizon_candles  4
made_at          close time of the candle the forecast was made on, or null
base_price       that candle's close, or null
points           one per future candle, oldest first; [] when not available
                 {time, close, low, high}
                 time: OPEN time of the future candle (the first one is the candle now forming)
                 close: the expected close
                 low, high: the band, the model's 10% and 90% quantile of that close
up_probability   the model's probability that the 4th close is above base_price, or null
direction        "up" | "down" | "flat" | null; "flat": the probability is within 0.03 of 0.5,
                 too close to a coin toss to call, or the line's 4th close is on the other
                 side of base_price than the probability says. So "up" always comes with a
                 line that ends above base_price, "down" with one that ends below.
skill            null without a model, else the measured record (also sent while not available):
  scope          "open_market": only forecasts made while the broker's market is probably
                 open (core/pricecheck.probably_closed) are in this record
  train_rows     the smallest number of rows a scored forecast's model was fitted on; never
                 less than half of what the shown model is fitted on
  samples        forecasts scored (> 0)
  from, to       the evaluated period (times the first and last scored forecast were made)
  hit_rate       share of non-flat calls whose direction was right at the 4th close; 0 when
                 there were no calls
  calls          how many non-flat calls that is (can be 0: then nothing was measured about
                 the direction word, see "When the record holds no call" below)
  calls_up, calls_down   the calls per side; they add up to calls
  hit_rate_baseline      what the best constant call would have scored on the same calls;
                 0 when there were no calls
  hit_rate_baseline_call "up" | "down": which constant call that is ("always up" / "always down");
                 means nothing when there were no calls
  mae            mean absolute error of the 4th close, price units
  baseline_mae   the same for "the price stays where it is"
  coverage       share of 4th closes inside the band (built to be near 0.8)
  better_than_baseline   bool, decided by the rule in core/forecast.judge
trained_at       when the model was fitted, or null
```

The tick does not carry the forecast. The feed's version `v` goes up when a
new forecast is made, and the page then reads it from the snapshot. On any
chart timeframe other than M15 the forecast is not converted and not drawn.

What the model is (`backend/app/core/forecast.py`, pure, numpy only):

- 28 features from closed M15 candles: the rule's score and its five
  conditions, the H1 score the filter uses, RSI, MACD histogram, distance of
  the close from EMA20 / 50 / 200 in ATR units, ATR as a share of price,
  returns over 1, 4, 16 and 96 candles, the candle's body and wicks, volume
  against its own last day, the H4 and D1 trend built from the same candles,
  hour of day and day of week, and whether the broker's market is probably
  closed. There is no news data in the project, so none is used.
- Ridge regression on standardised features for the move to each of the four
  closes, in ATR units. A logistic model on the same features for
  `up_probability`. The band is the 10% and 90% quantile of the errors on
  rows held back from the fit. All of it is stored as plain numbers.
- The ridge strength is chosen from 0.01, 0.1, 1, 10, 100, 1000 by the error
  on the held-back rows.
- `direction` comes from `up_probability`; the line comes from the
  regression. The two are fitted separately and can disagree. When they do,
  no call is made: the word is "flat" (`core/forecast.call`). The record
  counts calls the same way.

What it is not: advice, a signal, or a promise. The record is measured
walk-forward: fit on the past, forecast the next 1000 candles, fit again,
with a gap of four candles so that no training target reaches into what is
scored.

The record that is shown is as near to the record of the shown model as a
test on unseen candles can be, in the hours the owner can trade. Two kinds
of forecasts are left out of `skill`:

- those made while the broker's market is probably closed. The source trades
  through the weekend, when moves are small; counted in, they made the error
  look about a fifth smaller than it is in trading hours.
- those made by an early fit that saw less than half of the rows the final
  model is fitted on (`COMPARABLE_TRAIN`). Such a fit behaves unlike the
  final model (it called a direction far more often), so its hit rate is not
  the shown model's. One half was fixed before the run: the scored fits are
  within a factor of two of the shown one, and the newer half of the history
  is left to score them on.

Both left-out parts are still measured and written to the file's
`evaluation` (`closed_market`: the same fits in closed hours;
`all_forecasts`: every fit, every hour, as the record was counted before).
They are for the record and are not sent to the page.

It is still the record of other fits, not of the shown weights. Every fit
chooses its own ridge strength, the final one too. Two fits of a comparable
size with different strengths can differ in how often they leave the flat
zone. So the record cannot say how often the shown model says "up" or
"down". The training script counts that on the final model itself, on the
candles it was fitted on (`core/forecast.own_calls`), and prints it. That
is a count, not a record: the model has seen those candles.

When the record holds no call (`calls` 0): the scored fits never said "up"
or "down" in the scored hours. There is then no hit rate (`hit_rate` and
`hit_rate_baseline` are 0 and mean nothing), and `better_than_baseline` is
false. If the shown model still says "up" or "down" on some candle, that
word has no measured record at all, good or bad.

`better_than_baseline` is judged on the shown record and is true only when
all of this holds: at least
300 calls, an error at the 4th close below the baseline's with the lower 95%
bound of the gain above zero, and a hit rate whose lower 95% bound is above
0.5. Overlapping forecasts are counted as a quarter as many tries. When it is
false the page must say in plain words that the forecast has no measured edge.

Training (`python -m backend.scripts.train_forecast`): downloads the
source's whole M15 history in pages (public `GET /fapi/v1/klines`, 0.5 s
between pages; a 418 or 429 stops it and nothing is written), runs the
evaluation, fits on everything and writes `data/forecast_model.json`
(`TRADEPILOT_FORECAST_MODEL_PATH`). The web app reads the file at start-up
only: restart it after training. The model does not retrain itself; train
again from time to time.

A file is not used ("no_model", with a warning in the server log, and the
app starts all the same) when:

- it cannot be read as UTF-8 JSON, or is broken in any other way;
- it is of another model version, has other features, has a band whose low
  side lies above its high side, has no real time of training, or lacks its
  `skill` record or any key of it (a file written before the keys above were
  added must be trained again);
- its record cannot be true: a share outside 0..1, `calls` above `samples`
  or not the sum of the two sides, no samples, a negative error, `from`
  after `to` or outside real time, another `scope`; or it contradicts
  itself: `better_than_baseline` true with an error not below the
  baseline's, fewer than 300 calls or a hit rate not above 0.5, or a hit
  rate given although `calls` is 0;
- its record cannot be this model's: the model does not say how many rows
  it was fitted on (`model.rows`), more forecasts were scored than that, or
  `train_rows` is under half of it or above it;
- it does not name the source and instrument it was fitted on, or they are
  not the feed's (`source.name`, `source.symbol`): after a change of
  `TRADEPILOT_PRICE_SOURCE` the model must be trained again.

A forecast itself is dropped (nothing is drawn for that candle) when a
number is not finite, the probability is outside 0..1, a point is outside
its own band or more than 20 ATR from the base price, the points are not on
the next four candles, or the direction contradicts the line.

Result of the run on 2026-11-22 (Binance XAUUSDT, 33,255 candles from
2025-12-11, no holes). Shown record: 10,212 open-market forecasts from
2026-06-18 to 2026-11-20. 16 of the 29 fits saw at least 16,996 rows; the
newest of them had only weekend candles left to forecast, so 15 made the
scored forecasts. Error at 60 minutes 8.055 against 8.051 for the baseline
(lower bound of the gain -0.009), band coverage 80.6%, and not one
direction call. `better_than_baseline` is false: the expected close is no
closer to the real one than "the price stays where it is", and there is no
hit rate at all.

Why no call, checked on 2026-11-23 by a second count that does not use the
scoring code (fit by fit, word by word with `core/forecast.call`): in these
10,212 forecasts the probability stayed between 0.494 and 0.517. It never
left the flat zone (0.47 to 0.53), so the count is right and it is not the
rule "flat when the word and the line disagree" that removed the calls: no
forecast got that far. The cause is the ridge strength. With the wider grid
15 of the 16 fits chose 10, 100 or 1000, and such a strength shrinks the
probability model to nearly one constant number. The 16th chose 1.0 and
made the 3 calls of the closed hours below.

The shown model chose otherwise: its strength came out as 1.0. The choice
jumps between 1 and 1000 from fit to fit because it decides almost nothing:
in each of these fits, and in the final one, the held-back error of the
strengths from 1 to 1000 differs by less than 0.3%, and the best of them is
within 0.1% of the error of forecasting no move at all. With 1.0 the shown
model does say "up" or "down" now and then: on 545 of the 32,052 candles it
was fitted on (131 up, 414 down; 304 of them in open-market hours), and on
16 of the newest 1,500. No such call was scored, because the fits that were
scored made none. So the direction word of the shown model has no measured
record, and the page must not let it look tested.

For the record: the same fits in closed hours, 4,840 forecasts, error 2.108
against 2.106, 3 calls ("down", all three wrong); all 29 fits in all hours,
28,052 forecasts, error 7.464 against 7.453, 52.2% right on 3,556 calls
(3,549 of them "up"), where "always up" on the same calls scores 52.2% too.
The final model is fitted on 32,052 rows with strength 1.0.

Limits: the features are computed on the whole history in the evaluation and
on the feed's 1500 candles live (measured difference at the 4th close: 0.001
on average); the prices are the source's, not the broker's; the open-market
hours are the rule of thumb of `probably_closed`, not the broker's calendar;
the band is one width for all hours; a model needs
about 1210 closed candles before it can forecast; weekend candles of the
source are forecast like any other.

On the page (`frontend/static/app.js`, `syncForecast`):

- The line: line series of the chart library. A pink dashed line from the
  last closed candle's close through the four expected closes, each marked
  with a dot, and two thin faint lines through `low` and `high`. At the end
  of the line the word "Таамаг" is written on the chart (a dot and a word,
  not an arrow: it is not a signal). At the default zoom the line is only
  four candles long; the dots and the word are what make it findable. The
  series carry no price label, no price line and no crosshair dot, and they
  are not part of the O/H/L/C legend.
- The forecast takes no part in the price scale (`autoscaleInfoProvider`
  answers null for every forecast series). The candles alone decide what
  price range is in view, so no forecast can squeeze them. A band that
  reaches beyond the visible range is cut off there.
- The time axis does not move with the forecast. On the M15 chart one more
  series, never visible, always holds the four candles a forecast made now
  would be about: the forming candle and the three after it
  (`syncForecastSlots`). So the axis reaches that far with a forecast or
  without one, and drawing or clearing the line adds no time and removes
  none. When a candle closes the axis grows by that one candle, as it does
  without any forecast. On other timeframes there is no such series.
- Drawn only when all of this holds: the chart shows M15, `available` is
  true, the block has its `skill` record and the contract's shape, the feed is
  not down, the server is not lost, no candle on screen has missed its close,
  and `made_at` is the close of the newest closed candle on screen. One
  function decides this on every tick, every snapshot, every candle load and
  every change of timeframe; when the answer is no, the line is removed at
  once. So when a candle closes the old line goes with that tick and the new
  one comes with the next snapshot. "A candle missed its close" stays true
  until a reload of the candles has succeeded, not merely started, so the
  line does not come and go while a reload is slow or failing.
- The contract's shape, checked by the page as well as by the server:
  `horizon_candles` 4 and exactly four points on the next four candles; each
  band around its close; no point further from `base_price` than 20% of it;
  `up_probability` between 0 and 1; "up" only with a probability above 0.5
  and a line that ends above `base_price`, "down" the other way round. The
  record: counts that are whole and not negative, `calls` not above
  `samples` and the sum of its two sides, shares between 0 and 1, errors not
  negative, `from` <= `to` in real time, `scope` "open_market", and no
  `better_than_baseline` that its own numbers do not carry. Anything else is
  "could not be computed": no line, and nothing of that record is printed.
- The words are under the chart and in the chart's legend entry: direction
  (өсөх / буурах / тодорхойгүй), the probability, the expected close in 60
  minutes with its band, then the measured record. The record says which
  hours it was measured in (the hours the broker's market is taken to be
  open, a rule of thumb), how many calls there were per side, the hit rate,
  and beside it what the best constant call ("always up" or "always down")
  would have scored on the same calls. That, not 50%, is what the hit rate
  is to be compared with. Then the error against "the price stays where it
  is", shown with enough decimals to tell the two apart, and the band
  coverage. When the record holds no call the page says that nothing was
  measured about the direction word; and when the shown model then says
  "up" or "down" all the same, a warning says that this word was never
  tested. When `better_than_baseline` is false the line is drawn dimmer and
  a warning note says that in testing the forecast was no better than
  assuming the price stays where it is. When nothing is drawn the reason is
  said in words; on another timeframe: "Таамаг зөвхөн M15 дээр харагдана";
  while the feed is down: the forecast comes back when the feed does (the
  server brings the same forecast back if the candle has not closed).
  Always: that it is an experimental estimate, not a signal and not advice.
- Nothing of the forecast goes into the signal card, the browser
  notification, the sound or the tab title.

## The page's own safeguards

- A request that gets no answer within 15 seconds is given up and retried, so
  one stalled request cannot block the snapshots (and with them the signal
  notification) or the candle loads after it.
- A failed `/api/state` is asked again 5 seconds later by a timer, whether or
  not a tick comes. After three failures in a row, while ticks still arrive,
  a note at the signal card says that the full data cannot be had (the log,
  the reasons and the alerts may be old); it goes with the first snapshot
  that works. While snapshots fail, a new signal is announced from the
  tick's own card (notification, sound, tab title), once; the snapshot that
  comes later does not announce it again. Nothing is announced that way
  before the page has had its first snapshot.
- The tab title's flags have an end. A new signal's flag ("BUY XAUUSD · ...")
  is shown while the card is that signal and is dropped when the signal has
  expired by the clock, which runs without ticks too: a lost server cannot
  leave it there. While the card is something else for a moment (feed down)
  it is hidden, and it is back when the same signal is on the card again. A
  feed alert never takes its place. "Зогссон" (feed down) stays while the
  ticks say the feed is down; "Хэвийн" (feed back) goes after one minute.
  Looking at the page clears them all.
- A browser that grants notifications and then cannot show one (phones allow
  them from a service worker only) gets the button "cannot show
  notifications" instead of "on". The browser remembers this
  (`tradepilot.notifyBroken` in local storage), so the button does not say
  "on" again at the next visit; it is forgotten as soon as a notification
  can be made. Phones are not served yet.
- The login page's pause after too many wrong passwords runs to a moment in
  time, so a PC that slept is released when the pause is really over. Two
  clocks measure it, the time of day and the browser's running clock: the
  pause ends when either says so and never lasts or shows more than the
  seconds first asked for, also when the time of day is set back. When it
  ends, the "try again in N seconds" message is replaced.
- When the session has ended the page leaves for the login page after 1.5
  seconds. "Log out" pressed in that time cancels this: one navigation.

## The page and the chart

The chart holds the candles of `/api/candles` and moves its two newest bars
with every tick. When a tick shows that a candle on screen missed its close
(the page slept, the stream was down), the page loads the candles again and
keeps asking on every tick until one load succeeds.

The source's instrument can trade while the broker is closed (Binance
XAUUSDT trades through the weekend). Those candles are real trades of the
source and are drawn; the chart says so in a note while `market_closed` is
true, and no signal is sent in those hours.
