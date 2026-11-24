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

The chart has its own live path beside this one (see "The chart's live
stream" below): a WebSocket kline stream from the source. Signals never
depend on it: the feed above polls closed M15 candles over REST, and nothing
the chart stream does can create, change, delay or block a decision.

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

The clock (`now`) runs on from a time the source reported plus the time
passed since. A quote dated ahead of it moves it forward. An older quote
does not set it back: when the source repeats an old answer, or the first
quote after an outage is minutes old (a copy that lags), the clock keeps
running and that quote shows its age, so the feed reads late or down and an
expired signal never reads valid again. The clock goes back only to the
freshest quote of a whole hour in which no quote was ahead of it (a quote
once dated ahead, or a PC stopwatch that runs a little fast). That hour
starts with the first quote behind the clock since it was last set (a
quote repeated unchanged does not count twice). So one quote dated ahead,
by 90 seconds or by two hours, takes the feed down for about an hour, not
for the jump and an hour.

An answer without a single candle, and one whose candles end before the
stored ones (a copy of the source that is a candle behind), bring nothing
new: the cycle counts as failed, the stored candles and the last quote stay,
and the feed's age runs on from the last answer that was right. Answers that
alternate between a right copy and one that is behind therefore load
nothing again and raise no `reloaded`. Only after a whole minute of nothing
but answers that end before the stored candles is the source believed: the
history is loaded once as it is then, with `reloaded`.

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
  dashboard is open. A chart stream thread that is still opening its
  connection (a connect or handshake that hangs) is waited for at most half a
  second; it is a daemon thread and ends by itself.
- Logging out removes the cookie from that browser and ends that session on
  the server as well: a copy of the token gets 401, and streams opened with
  it end (a chart stream within a second, the tick stream at its next tick).

Known limits, accepted for a one-owner site:

- The pause after wrong passwords is one counter for everybody, not one per
  address: someone guessing can also keep the owner waiting.
- The sessions ended by logging out are kept in the server's memory only. A
  restart of the server forgets them: a copy of a logged-out token works
  again until its 12 hours are over. Setting a new password (a new session
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
| `GET /api/candles?timeframe=<chart timeframe>&limit=1..1500` | the candles answer below; 422 for a timeframe the source cannot show, 503 with `Retry-After` when the source gives nothing right now (an empty answer included, and the signal timeframe before the feed's first answer) |
| `GET /api/candles/stream?timeframe=<chart timeframe>` | server-sent events, event name `candles`, see "The chart's live stream"; 422 as above, 503 with `Retry-After: 10` beyond 8 open chart streams |
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
               bar: {time, open, high, low, close, volume, closed}; closed false = still forming;
               volume null: the source gave none (H1: summed over its quarters)
               The H1 entry is the rule's filter view: built by the feed from its own M15
               candles. The chart's H1 comes from the source's 1h candles (its own origin),
               so the page does not draw this entry. The page may draw the M15 entry on
               the M15 chart: those are the feed's own candles, as is M15's history.
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
chart     {timeframes: [...], signal_timeframe: "M15", filter_timeframe: "H1",
           missing: {...}, shortest: "M1", stream: {available, timeframes}}
          timeframes: the chart timeframes this source can show, shortest first
          missing: each of the 18 menu timeframes this source cannot show, with a reason code:
            "no_shorter_candles"  the source's shortest candle (`shortest`) is longer than this one
                                  (CSV and other M15-only sources: M1, M2, M3, M5, M10)
            "cannot_build"        no candle of the source fills this one exactly
          shortest: the source's shortest candle, its own or the signal timeframe
          stream.available: whether the chart has a live candle stream with this source and
            setting (Binance with TRADEPILOT_CHART_STREAM=true); false: the chart stream route
            still works, from polling ("mode": "poll")
          stream.timeframes: the chart timeframes the live stream moves (their origin streams)
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
rev            the history counter of this timeframe's origin (see "The chart's live stream")
bars           oldest first: {time, open, high, low, close, volume, closed}
               closed false marks the forming candle (the last one), and for a few
               seconds the one before it when the live stream closed it without the
               exchange's own flag (by starting the next one): its final values are not
               confirmed yet (see "The chart's live stream"). Every other bar is final.
               volume null: the source gave none (draw no volume bar); never negative
               or infinite (a source answer with such a volume is refused)
```

Three answers the page words differently: 422 (this source cannot show the
timeframe at all: see `chart.missing` for why), 503 with `Retry-After`
(nothing right now: try again after that many seconds), and `history:
"short"` (the candles are real but the source's history begins at the first
one).

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
  rule ran on. The M15 stream may add three things to them: its newer state
  of the feed's newest candle while that one forms (not when the feed's own
  state has more volume traded: then the feed's is the newer one), that
  candle's final values once the exchange closed it with its own flag, and
  the candle after it, but only once the one before is closed, in the feed
  or by that flag. The next candle never closes a candle the feed still has
  as forming, and nothing further than one candle past the feed's own is
  laid. A candle the feed has closed is never changed, and the feed's memory
  itself is never touched. The event's freshness (`mode` "stream", `at`,
  `age`) comes from the stream only while the stream's newest candle is the
  newest one shown; otherwise it is the feed's own, so a feed that is stuck
  reads late and then down on the chart too, however long the stream runs.
- A length the source has itself (Binance: M1, M3, M5, M30, H1, H2, H4, D1,
  W1, MN1): asked from the source, kept in memory, and refreshed with a
  3-candle request at most once per poll interval however many pages ask
  (months too, by calendar steps: one load of 240 months, then 3-month
  tails), unless the live stream keeps it current.
- Anything else is built from the longest shorter candles that fill it
  exactly: M2 from M1, M10 from M5, M45 from M15, H3 from H1, MN3 / MN6 /
  MN12 from MN1. A source without intervals of its own (Twelve Data, CSV)
  gets M15 and whatever M15 can build.

Building (`core/candles.resample`): first open, highest high, lowest low,
last close, summed volume. A candle is never built over a hole: that bucket
is left out and counted in `gaps`. The newest bucket is kept as forming; the
oldest one is kept, and flagged `partial_first`, only when the source has
nothing older. A built candle is closed only when its bucket is complete and
its last part is closed; a newest bucket that lacks a part is never shown,
let alone as closed.

An empty answer from the source is never data: it is not kept in place of
the candles, and the route answers 503.

When the source fails, the chart request answers 503, the source is left
alone for 5 seconds (or as long as it asked after a refusal), and no kept
candle is passed off as current. None of this touches the signal path.

## The chart's live stream (`GET /api/candles/stream?timeframe=TF`)

Signals never depend on the chart stream: the feed polls closed M15 candles
over REST, does not read any stream, and decides the same with or without one.

One timeframe, one origin, history and live alike: the live bars of a
timeframe come from the same candles as its `/api/candles` history (M2 from
M1, H1 from the source's 1h candles, M15 from the feed). The page draws
nothing from the tick for any timeframe but M15.

How it works (`backend/app/chart.py`, `backend/app/candlestream.py`,
`backend/app/datasource/ws.py`):

- One thread per origin timeframe somebody is watching, started by the first
  open chart stream that needs it and stopped when nobody has watched it for
  30 s. Binance: `wss://fstream.binance.com/market/ws/xauusdt@kline_<interval>`
  (public market data, no key; the client sends nothing but pongs). The
  exchange pushes the forming candle about every 250 ms while trades happen,
  and once more with its own `closed` flag at the end.
- Each update is laid over the kept candles (`core/candles.lay_stream`): the
  same open time replaces the newest candle, the next one is appended, an
  older one is ignored, and a jump over a missing candle is never built
  over: the candles are refilled over REST instead.
- After every (re)connect the origin is refilled over REST (tail, or the whole
  history when the tail does not reach), so candles missed while
  disconnected are there, once and in order. A dropped stream is opened again
  after 1, 2, 5, 10, then 30 s; a refusal (418, 429) is waited out. The
  pauses start again at 1 s only after a connection that delivered and stayed
  up for a minute: a stream that is cut right after its first update, again
  and again, waits longer each time (and its refills with it), like one that
  never opens. A message that cannot be read (not JSON, a number that is not
  finite such as `Infinity` or `1e400`, a negative volume) breaks the
  connection like any other failure and counts for the pauses.
- While the stream delivered within the poll interval (2 s), the origin's REST
  tail refresh is skipped. When it has been silent longer, or is down, REST
  refreshes as it did before. So the stream only makes things faster.
- A candle the exchange closed with its own flag keeps those values: a REST
  answer from a copy that lags and still lists it as the forming newest one,
  with its values from before the close, does not reopen it.
- A candle the stream closed only by starting the next one (no closed flag
  seen, for instance after a reconnect) may lack its last trades. It is sent
  with `closed: false` until a REST answer lists a newer candle (asked about
  2 s later, again while a lagging copy still lists it as the newest, at most
  5 times); then it is sent closed with REST's final values. A built candle
  that holds it is not closed before that either.
- `TRADEPILOT_CHART_STREAM=false` switches the stream off: everything works
  from polling, `mode` is always "poll".

Events: name `candles`, data:

```
timeframe    the chart timeframe asked for
bars         the newest 3 bars, oldest first, in the shape of /api/candles bars
             ([] when error is set: nothing is passed off as current)
mode         "stream": the origin's live stream is connected and delivered within 10 s
             "poll":   the bars come from the REST-refreshed candles (no stream for this
                       source or timeframe, the setting is off, or the stream is down or quiet)
at           the source's time of the newest confirmation of these candles, UTC seconds:
             the exchange's event time of the last stream update, or, from polling, the
             server's clock when the last REST answer (or the feed's last good cycle, for
             M15 and what is built from it) arrived; null before any
age          seconds since the server received that confirmation (its own stopwatch;
             no clock difference in it). Rises while nothing new arrives.
rev          history counter of the origin: moves whenever candles older than the newest
             three changed (a refill after a reconnect, a reload, a hole that healed).
             /api/candles carries the same number.
gaps         breaks in the whole kept history of this timeframe (count_breaks over up to
             1500 candles; /api/candles counts only within its limit)
error        null, or "unavailable": the source gives nothing right now
retry_after  with error: seconds before the source is asked again; else null
```

Example (real, Binance XAUUSDT, M1, 2026-11-23):

```
event: candles
data: {"timeframe": "M1", "bars": [{"time": 1795439220, "open": 3624.38, "high": 3625.64,
"low": 3623.02, "close": 3625.4, "volume": 445.315, "closed": true}, {"time": 1795439280,
"open": 3625.46, "high": 3627.88, "low": 3625.46, "close": 3626.85, "volume": 460.308,
"closed": true}, {"time": 1795439340, "open": 3626.77, "high": 3626.77, "low": 3626.48,
"close": 3626.48, "volume": 10.699, "closed": false}], "mode": "stream", "at": 1795439341.101,
"age": 0.0, "rev": 1, "gaps": 0, "error": null, "retry_after": null}
```

Timing: the first event is sent at once. Then an event whenever the bars,
`mode`, `rev`, `gaps` or `error` changed, at most one every 0.2 s per page
(what changed meanwhile is coalesced: the newest state wins, there is never
a backlog), and at least one every 5 s as a heartbeat (same bars, newer
`age`). A page can call the chart late when `age` grows past a few seconds
and the stream silent when no event came for well over 5 s. An update of
one origin wakes only the chart streams whose timeframe comes from it (each
stream also looks at its candles every 0.5 s, which is how M15 and M45 see a
new feed cycle). The events are strict JSON: a number that is not finite is
never sent (such an event is left out and logged).

What the page does with it: see "The page and the chart" below. At most 8
chart streams are open at once; the slot is taken when the request is
accepted, so streams asked for at the same moment cannot pass the limit
together, and it is given back however the response ends, also when it never
starts. The stream ends when the session ends (logging out included) and
when the server stops (Ctrl-C with a page open still stops the server at
once).

Browsers allow about six connections to one server over HTTP/1.1, which
uvicorn serves. Every open dashboard keeps its tick stream (signals are
announced from hidden tabs too), and the tab on screen also its chart
stream; a tab hidden for more than 10 s gives its chart stream back and
asks for no tail requests. So up to four dashboard tabs leave a connection free for the
other requests (before the chart stream: five); with more, requests wait in
the browser's queue until a tab is closed. HTTP/2 behind the proxy of the
deployment (Phase 9) removes this limit.

In the page the chosen timeframe and the starred ones (quick buttons, M15 and
H1 by default) are kept in the browser's local storage. Choosing quickly is
safe: every load carries a number and only the newest one may fill the chart,
and only the newest choice's chart stream is drawn.

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
  baseline's, fewer than 300 calls, a hit rate not above 0.5 or one whose
  lower 95% bound (`core/forecast.hit_rate_floor` of the calls and the hits
  they imply) is not above 0.5, or a hit rate given although `calls` is 0;
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
  not an arrow: it is not a signal). While a signal's TP and SL labels are
  at the chart's right edge, the word goes to the first place they cannot
  cover (`forecastWordPlace`, judged in pixels after every tick, zoom and
  scroll): the end of the line, then the points before it, then the line's
  start, on either side; when no place is clear (a chart zoomed far out with
  both levels near the line) it stays at the end, on the side with more
  room. At the default zoom the line is only
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
  `better_than_baseline` that its own numbers do not carry (the page asks
  for the same as the server: at least 300 calls, an error below the
  baseline's, and a hit rate whose lower 95% bound is above 0.5). Anything else is
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

(`frontend/static/app.js`, "the chart's own candle stream".) Signals never
depend on any of this: the chart is a view.

Where the candles come from. The history is `/api/candles` (1500 candles).
The newest candles come from one writer at a time:

- the chart stream (`/api/candles/stream`), one EventSource for the timeframe
  on screen, opened after the tick stream and replaced when the timeframe
  changes. While it delivers (an event without `error` in the last 8 s) it
  alone moves the chart: the tick's bars and the tail requests are not used
  for drawing then. A tab hidden for more than 10 s closes it (and makes no
  tail requests); when the tab shows again, the history is loaded again and
  the stream reopened. A shorter hide changes nothing.
- while it is silent for longer than that, or broken: on M15 the tick's M15
  bars (the feed's own candles, as is the M15 history), on any other
  timeframe a request for its newest 3 candles about every 1.5 s, as before
  the chart stream existed. The tick's H1 bars are never drawn: they are the
  filter's view, built from the feed's M15 candles, while the H1 history comes
  from the source's 1h candles. One timeframe, one origin.

A chart stream that the browser gave up (an error answer: 401, 422, 503) is
opened again after 2, 5, 10, then 30 s, each time after asking `/api/state`
(which leads to the login page when the session has ended). One that stays
open but silent for 20 s is opened again. An event of a timeframe that is not
on screen any more, or bars that are not candles of the named timeframe, are
not drawn.

Drawing. The candle on screen and the next one go through the library's
`update()`: the forming candle moves in place, a candle that closes keeps
its final values and the next one is added once. An older candle of an event
may only get final values (a closed candle confirmed after its close, drawn
with the library's historical update); it never becomes a forming one again.
An event or a tick whose candles start after the candle on screen, or jump
over a candle, means a missed close: the history is loaded again, and asked
for on every update until a load succeeds. When an event's `rev` differs from
the `rev` of the history on screen, or its `gaps` from the previous event's,
the history is loaded again too. A reload keeps the view: following the
newest candle it keeps following it, scrolled into the history it keeps the
same candles in view at the same zoom. A 503 is waited out for its
`Retry-After`.

Measured on 2026-11-23 with the real source on M1 (the newest candle's
O/H/L/C in the page's legend, 80 s): 215 changes, a median 0.31 s apart, 90%
within 0.57 s, the longest gap 1.34 s. Before the chart stream: 14 changes,
4.0 s, 8.1 s and 12.1 s.

On the chart:

- Volume bars under the candles, on a scale of their own in the lowest fifth,
  in the candle's colour and dimmer than the candles (dimmer still while the
  candle forms). A candle without volume data (`volume` null) gets no bar,
  and the legend shows a dash. A row without a `volume` field at all (a tick
  of an older server) keeps the volume the page already has for that candle.
- The legend above the chart shows O, H, L, C and V of the candle under the
  crosshair, and of the newest candle when the pointer leaves the chart.
- On the right price axis the newest candle's label shows its close and the
  time left until it closes, for example `3626.03 · 00:22`: mm:ss up to H1,
  h:mm:ss up to D1, days for W1 and the month timeframes (h:mm:ss in their
  last day), zeros once the time is up. The close time is the open time plus
  the candle's length, calendar months included; the clock is the server's,
  as the page estimates it from the ticks. One width per timeframe, so the
  axis does not change width while it counts. The label is coloured like the
  candle while the chart's candles are current and grey while they are late
  or down. It is the candle's close (the source's last trade), not Bid or Ask;
  the legend under the chart says so. On a chart narrower than 480 px (a
  phone) the label shows the close only, so the axis stays narrow, and the
  time left is written in the O H L C row ("хаагдах хүртэл 00:22").
- The chip beside the title names the real source and symbol ("Binance
  XAUUSDT") and the state of the chart's own candles, apart from the feed's
  status in the header: "шууд урсгал" (the stream delivers), "асуулгаар
  шинэчилж байна" (the server or the page polls), "хоцорч байна" (the newest
  confirmation is over 10 s old) and "тасарсан" (an error, over 60 s, or the
  server lost), with the time of the last update when it is not live.
- Dragging moves the chart, the plain mouse wheel zooms it (as on
  TradingView; the owner asked for it), a sideways wheel moves it, a pinch
  zooms it on a phone, and a vertical swipe on a phone scrolls the page.
  While the newest candle is in view a zoom keeps the right edge where it is
  (the library's `rightBarStaysOnScroll`), so zooming in never pushes the
  newest candle out of view; scrolled into the history, a zoom centres on
  the pointer.
  Live updates never change the visible range or the zoom; a user who
  scrolled into the history stays there while candles arrive. The button
  «Шууд ханш руу» shows while the newest candle is out of view; it goes back
  to the newest candle at the same zoom, and from there the chart follows new
  candles again.
- Signal markers sit on the candle that holds the signal's time, on every
  timeframe. The forecast exists on M15 only; another timeframe clears it.

The timeframe menu keeps its 18 timeframes, groups, stars and quick buttons.
A timeframe this source cannot show stays in the menu, disabled, and says why
in its row (from `chart.missing`): for an M15-only source (CSV, a test
source) "Энэ эх үүсвэрт M15-аас богино лаа байхгүй". On the chart a 503 is
worded as a temporary failure of the source, and `history: "short"` as a
short history; they are not the same message.

The signal log's footnote says that "TP-д хүрсэн" / "SL-д хүрсэн" means the
source's price touched that reference level, not the owner's trade.

The source's instrument can trade while the broker is closed (Binance
XAUUSDT trades through the weekend). Those candles are real trades of the
source and are drawn; the chart says so in a note while `market_closed` is
true, and no signal is sent in those hours.
