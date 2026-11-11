# Phase 3 code review, 2026-11-11

Six independent reviewers read the Phase 3 price-source code, each with a different lens; their findings
were merged into the 25 below. A second pass was meant to try to refute each finding by reproducing it,
but it was cut short: only findings 8 and 10 went through it. Treat every other finding as a reviewer's
claim that still has to be checked before it is acted on. Line numbers refer to commit cae3581.

## 1. Binance: `closed` is decided by the PC clock, so a forming or not-yet-final candle is saved as closed (seen live), and a slow clock fails every fetch after a close

- Severity claimed: high
- Where: `backend/app/datasource/binance_source.py` line 75
- Verified: no
- Status: fixed 2026-11-11 (closed only when a newer candle is listed)

`closed` is `int(row[6]) < now_ms` (line 75): the candle's nominal close time against the PC clock, which is read after the response has arrived (line 61), with no margin. Nothing checks what the exchange has actually published. signal.closed_candles and the H1 candles built by aggregate() inherit the flag, so this breaks the hard rule 'signals use closed candles only' inside the adapter the worker will reuse. Observed live on 2026-11-11 with the real adapter and the real clock, the rule is wrong in three ways.

1. Snapshot plus clock offset. A response is a snapshot taken before it is received, and this PC's clock is about 0.2 s (0.17-0.25 s) ahead of Binance (/fapi/v1/time, 74 ms round trip, measured 20 to 34 minutes after a Windows time sync; w32tm: stratum 5, root dispersion 7.97 s). An answer produced before the close is therefore stamped after it. A clock X s ahead flags the forming candle closed for the last X s of every candle; a clock one hour ahead flags it from its first second. At the 10:45:00 UTC boundary three fetches sent -30 ms, +53 ms and +132 ms from the boundary each received the 10:30 candle as the last row and marked it closed=True with volume 2390.957 / 2390.957 / 2390.866 and close 4056.16 / 4056.16 / 4056.17; the final candle has volume 2391.525 and close 4056.16.

2. Binance's REST klines trail the close. For several seconds after a close the response can still end with the just-closed candle in a pre-close state, with no newer candle listed, and the adapter flags that row closed. After the 10:45:00 UTC close, responses received 1.6 s and 5.3 s later ended with the 10:30 M15 candle at close 4056.17, volume 2391.421, 8784 trades, closed=True; the final candle is 4056.16, 2391.525, 8786. On a 1-minute candle, 4.1 s after its close: close and low 4053.95, 760 trades, closed=True; final close and low 4053.86, 810 trades. In all, 5 of 7 responses received within 8.3 s after a close were non-final and flagged closed; the 2 that listed a newer candle were final. Two API nodes disagreed (volume 162.562 at 0.6 s, then 162.258 at 2.6 s). In every observation reported, a row that had a later row after it was final.

3. PC clock behind the exchange. A response that already lists the new candle makes two rows look forming: the just-closed one (its close time is still ahead of the PC clock) and the new one. validate() then raises 'only the last candle may still be forming' (candles.py lines 103 to 104), the adapter turns that into DataSourceError, and no candles are returned at all.

The collector saves a row the first time it sees closed=True and never revisits it (collect_candles.py:91 keeps only time > last_time), and check_candles cannot see it. The tests cannot see it either: the Binance closed-flag tests (test_datasources.py:52 and :66) use a frozen fake clock. twelvedata_source.py lines 88 and 104 use the same clock rule (listed as its own entry). State at review time: the four rows saved by the running collector (10:00 to 10:45) equal Binance's final candles.

Evidence: offline repros <scratch>/review_time_closed_r3/repro_closed_flag.py, <scratch>/candles-lens-r3q7/repro_closed_flag.py (measurements in <scratch>/candles-lens-r3q7/evidence_after_close.txt) and <scratch>/rev-rules-tests-docs-7c41/repro_closed_flag.py (live output <scratch>/rev-rules-tests-docs-7c41/live_race_out.txt); live boundary trial <scratch>/rev24h-lens-7c41e9/boundary_trial.py; arithmetic model <scratch>/rev24h-lens-7c41e9/sim_clock_fast.py.

**Failure scenario.** A collector poll lands in the last fraction of a second before an M15 close or in the first seconds after it. The cycle is about 30.4 s, so polls drift about 12 s per candle against the close (saves logged at 10:30:07, 10:45:19, 11:00:30) and this happens every few candles. Binance answers with the forming or just-closed candle in a pre-close state, the adapter says closed=True, the collector appends it and moves on; every later poll skips it. Replaying the recorded 10:45 responses through collect_candles.main saved close 4056.17 / volume 2390.866 for the 10:30 candle (final 4056.16 / 2391.525). Live M1 trials with the real fetch_candles: an answer produced about 0.1 s before the boundary (Date 10:38:59) and received 0.136 s after it came back closed=True with close 4053.76, volume 165.212 (six seconds later the same candle was 4053.77 / 166.957); an answer produced about 0.08 s after the boundary (Date 10:40:00) came back closed=True with 4054.52 / 98.876 (final 4054.59 / 98.884).

Simulated 24 h with the real collector code and the current rule: perfect clock with a REST view 0 to 8 s old: 9 to 23 of 97 rows non-final; PC clock 45 s ahead: 96 of 97; PC clock 1 h ahead: 97 of 97. check_candles prints PASS every time. (Estimates for the clock and travel-time effect alone: 1.1 such rows per 25 h run and at least one in 73% of runs at the measured +0.2 s, 0.44 rows even with a perfect clock, 17 rows at +5 s, every row once the clock is 31 s or more ahead.)

With the clock behind instead (5 s; w32tm on this PC reports a root dispersion of 7.97 s): a poll 3 s after 10:45:00 gets 10:30 (final) and 10:45 (new); PC time is 10:44:58, so both are flagged forming and fetch_candles raises DataSourceError('Binance klines failed validation: only the last candle may still be forming'). The collector logs FETCH FAILED and saves nothing until the PC clock passes the boundary. Simulated 24 h: 14 failed polls at 5 s behind, 141 at 45 s behind, all 2843 polls at 15 min or more behind (1 min for M1), with no file written and a message that blames the response.

From Phase 6 the worker evaluates right at each close, so it asks in exactly this window on almost every candle: signals (entry = that candle's close) would be computed from a candle that is not closed, the stored candle would differ from the one the signal used, and with a slow clock it would look like a feed outage after every close.

**Suggested fix.** Decide from the response, not the clock: in BinanceSource.fetch_candles loop with `for index, row in enumerate(rows)` and set `closed` to `index < len(rows) - 1`. A candle is closed only when the same response lists a newer one; the last row is always forming. Drop now_ms. The just-closed candle is then picked up on the next poll, a few seconds late, never early. In simulation this rule gives 0 non-final rows and 0 failed fetches for PC clock errors from -1000 s to +3600 s (<scratch>/review_time_closed_r3/sim_fix.py), and it matched all 7 live responses.

Variants other reviewers proposed for the last row only: let it count as closed once a clock reading taken BEFORE the request is a long margin past its close time, so the last candle of a halted market still closes (`closed = i < len(rows) - 1 or int(row[6]) + 60_000 < now_ms`); or use the exchange's own clock (GET /fapi/v1/time) and log the local-versus-exchange offset when the collector starts. Do not fix this with a time margin alone: `closed = closeTime + margin < clock` makes the just-closed candle look forming while the new one is listed, so validation fails after every close even with a perfect clock (if a margin is added for non-last rows, drop the rows after the first not-yet-closed one, because validate() allows only the last row to be forming).

Rewrite test_binance_candle_is_closed_after_its_last_millisecond (backend/tests/test_datasources.py:66), which pins the old rule: a lone row is forming. On a patched scratch copy it is the only test that fails (139 pass). Add a test where the last row's closeTime is already in the past, and one whose fake getter moves the fake clock across the boundary during the call and expects closed == [False].

The collector process started at 10:28 UTC holds the old adapter in memory, so restart it after the fix (it resumes after the last saved candle). After the current run, diff every saved row against a fresh klines download: <scratch>/rev24h-lens-7c41e9/compare_live.py does this on a scratch copy of the CSV (raise its limit from 12 to at least 101); <scratch>/candles-lens-r3q7/compare_csv_to_final.py is a read-only alternative.

## 2. A saved row is never re-checked: the collector cannot correct a non-final candle and check_candles still prints PASS (the run in progress uses the old rule)

- Severity claimed: high
- Where: `backend/scripts/collect_candles.py` line 91
- Verified: no
- Status: fixed 2026-11-11 (check_candles compares content; collector restarted)

The collector appends each candle the first time the adapter calls it closed and advances last_time (lines 91 to 114). After that the row is never compared with anything, although each of the next 39 responses contains the final version of that candle. After a restart, last_saved_time() reads only the time column.

check_candles.py looks at times only (line 45: gaps and span), so a file with non-final rows passes.

The collector process started at 10:28 UTC holds the old adapter in memory for its whole 25 h. Changing the file on disk does not change that process.

**Failure scenario.** A poll lands 1 s after a close. The adapter returns the pre-close candle as closed (the Binance closed-flag entry). The row is written with, for example, close 4056.17 instead of 4056.16 and a high or low that misses the last seconds. Every later poll skips it because its time is not above last_time.

At the end check_candles reports 'PASS (24 h without a gap)', and the file becomes the Phase 3 evidence and possibly backtest input.

State at review time: the four rows saved by 11:00 UTC (10:00 to 10:45) equal Binance's final candles. Rows the old process saves after that are unverified.

**Suggested fix.** After fixing the adapter, restart the collector; it resumes after the last saved candle.

Add a content check to check_candles: fetch the latest candles from the same source (limit 1500 covers 15 days of M15), compare open, high, low, close and volume for every overlapping closed candle, and FAIL on any difference.

Optionally, in the collector, compare the response's row for last_time with what was saved and log CHANGED.

## 3. Opening the CSV in Excel kills the collector at the next candle (uncaught PermissionError); the loop survives only DataSourceError

- Severity claimed: high
- Where: `backend/scripts/collect_candles.py` line 97
- Verified: no
- Status: fixed 2026-11-11 (write errors are logged and retried)

The CSV append at line 97 (`out.open('a')`) and the log append at line 68 are unguarded. The loop catches only DataSourceError (line 82) and KeyboardInterrupt (line 117), so any OSError from the file system ends the process with a traceback on the terminal. Nothing reaches the .log, there is no 'done' line, and the candle is not written. On this PC .csv is associated with Excel (assoc .csv = Excel.CSV, Office16 is installed), and Excel holds an opened CSV with a share mode that refuses writers for as long as the workbook stays open.

More generally the polling loop survives only DataSourceError from fetch_candles; any other exception ends the 24-hour run. Besides PermissionError / OSError when opening the CSV or the log for append, the author's review names ValueError / TypeError from the unwrapped int(row[0]) or int(row[6]) conversions in binance_source.py on a malformed kline.

**Failure scenario.** The owner double-clicks data/collect_binance_M15.csv to look at the candles and leaves Excel open. At the next quarter-hour out.open('a') raises PermissionError [Errno 13] Permission denied and the run is over. Unattended, the file simply ends there; collection stops until someone notices and restarts it, and after about 10 hours the missed candles can no longer be backfilled (FETCH_LIMIT = 40), so the acceptance run has to start again. Confirmed twice. (1) Real Excel on this PC, driven on a scratch copy: reading still works, opening for append is refused while the workbook is open and works again once it is closed (<scratch>/rev24h-lens-7c41e9/excel_test/excel_lock.ps1). (2) The real main() with the file held by a reader that denies writers: the collector died at +3.03 h of 24 h with the traceback at collect_candles.py:97, and the last .log line was an ordinary 'saved' line (<scratch>/rev24h-lens-7c41e9/repro_excel_lock.py). Other reviewers reproduced the same crash with simulated share locks (<scratch>/candles-lens-r3q7/exp5.py and <scratch>/rev-rules-tests-docs-7c41/repro_lock.py).

**Suggested fix.** Wrap the open-and-write in try/except OSError: log 'WRITE FAILED, n candle(s) kept for the next try (is the file open in Excel?)', leave last_time unchanged, sleep(poll) and continue. The next poll derives the same pending rows again from the fetch, so nothing is lost once the file is released (up to 39 candles). Protect log() the same way: try/except OSError around print and around the file append. Checked on a patched scratch copy (<scratch>/rev24h-lens-7c41e9/validate_fixes.py): a 40-minute lock produced WRITE FAILED lines, then 'saved 3 candle(s)', and the 24 h check passed. Also wrap the malformed-field conversions in binance_source.py as DataSourceError, and add a last-resort handler in the loop that logs the exception type and message and keeps polling. Until this is fixed, the owner should not open the live CSV in Excel: copy it and open the copy.

## 4. Transport errors that are not OSError (http.client.IncompleteRead, BadStatusLine) escape get_json and end the run without a trace

- Severity claimed: high
- Where: `backend/app/datasource/http.py` line 32
- Verified: no
- Status: fixed 2026-11-11 (HTTPException becomes DataSourceError)

get_json converts HTTPError, URLError, TimeoutError and OSError around urlopen() and response.read() (line 32), but http.client's own exceptions (IncompleteRead, BadStatusLine, LineTooLong) derive from http.client.HTTPException, not OSError, and the read of an error body at line 30 (`exc.read()`) runs inside the 'except HTTPError' block, where nothing is converted. They leave get_json and BinanceSource.fetch_candles as raw exceptions, against the promise in base.py lines 3-6. The collector catches only DataSourceError (collect_candles.py:82), so each of these ends the process with a traceback on the terminal and no line in the .log, although the script's docstring says it logs every failure. The same client will sit under the 24/7 worker. The usual Wi-Fi failures are handled: DNS failure, connect and read timeouts, resets, RemoteDisconnected, TLS errors, readable HTTP errors, non-JSON bodies. No API key appears in these tracebacks (checked).

Reproduced by four reviewers: with this venv (Python 3.11.6, OpenSSL 3.0.11) against a local HTTPS server that closes the connection after part of the body, with and without a TLS close_notify; over plain HTTP with a garbage status line; and with a stubbed urlopen (<scratch>/candles-lens-r3q7/exp4.py, <scratch>/rev-rules-tests-docs-7c41/repro_http.py).

The test that should cover this cannot fail: in test_binance_network_errors_propagate_as_datasource_errors (test_datasources.py:108) the fake itself raises DataSourceError, so no conversion is exercised. (One reviewer paired this with the unprotected CSV append at collect_candles.py:97; that part is covered by the Excel entry.)

**Failure scenario.** Through the real get_json and BinanceSource against a local server, over HTTP and HTTPS (<scratch>/rev24h-lens-7c41e9/repro_exceptions.py): (a) the connection ends in mid-body: http.client.IncompleteRead from line 28. Binance answers with Content-Length (5361 bytes seen), and Python's ssl turns a ragged TLS end into a plain end of data, so any connection that a router, proxy, antivirus or the CDN closes mid-body ends this way. (b) A non-HTTP reply: http.client.BadStatusLine from line 27. (c) An error status whose body is cut short: IncompleteRead from line 30. (d) An error status whose body stalls: TimeoutError from line 30. End to end with the real main() (<scratch>/rev24h-lens-7c41e9/repro_collector_net.py, part A; also <scratch>/rev-rules-tests-docs-7c41/repro_collector_crash.py, which prints 'COLLECTOR DIED: http.client.IncompleteRead: IncompleteRead(2180 bytes read, 2180 more expected)'): one truncated answer 5 h into a 24 h run killed the collector; the .log ended with an ordinary 'saved' line, with no FETCH FAILED and no 'done'. Rare per request, but there are 2,880 requests a day and the result is fatal: candles stop until someone restarts the collector, and after about 10 hours (FETCH_LIMIT 40) the gap can no longer be filled.

**Suggested fix.** In http.py import HTTPException from http.client, catch (URLError, TimeoutError, OSError, HTTPException) at line 32, and put the exc.read() of line 30 in its own try/except (OSError, HTTPException) with a placeholder detail, for example `try: detail = exc.read()[:200].decode('utf-8', 'replace')` / `except (OSError, HTTPException): detail = ''`. In the collector add 'except Exception as exc:' after the DataSourceError handler that logs the type and message as a failure, sleeps and continues, so that only Ctrl+C ends an unattended run (at the least, log any unexpected exception before exiting so a death always leaves a line in the log). Checked on the patched scratch copy: the same truncated answer became one FETCH FAILED line and the run finished and passed. Replace the vacuous test with get_json tests that use a fake urlopen raising IncompleteRead (also from read()), TimeoutError and ConnectionResetError and expect DataSourceError.

## 5. check_candles prints PASS for runs with hours of down time: backfilled candles, restarts and fetch failures are invisible to it

- Severity claimed: high
- Where: `backend/scripts/check_candles.py` line 45
- Verified: no
- Status: partly addressed 2026-11-11 (failed polls and catch-ups are reported from the log)

check_candles reads only candle open times from the CSV; the verdict (line 45) tests gaps and span. After any outage of up to 39 candles (9.75 h) the collector fills the hole from the exchange's history on its next successful poll (FETCH_LIMIT = 40, collect_candles.py lines 30 and 91) and writes those rows exactly like live ones, with no GAP line. A frozen process cannot log FETCH FAILED either, so the 'done' line says fetch_failures=0. Restarts and FETCH FAILED counts exist only in the .log file, which check_candles never opens. So 'PASS (24 h without a gap)' shows that Binance's history has no hole and that it was reached at least once every 10 hours. It does not show that candles were collected for 24 hours, which is what PLAN section 8 and README step 1 (line 56: 'the computer must not sleep, the internet must not drop') ask for, and what the collector's docstring and the comment at collect_candles.py:89-90 say the check measures ('live collection rather than downloaded history').

On this laptop idle sleep is off, but the lid-close action is Sleep on AC and on battery (powercfg) and the system uses S0 modern standby, which suspends desktop processes. A Windows restart or a closed terminal followed by a restart has the same effect.

The span (line 33) also starts at the open of the first saved candle, which closed before the collector started: in the run going on now the collector started at 10:28:35 UTC and the first row is the 10:00 candle, so PASS will first appear after about 23.5 hours of running. Neither script has a test.

**Failure scenario.** Reproduced with the real main() of both scripts by three reviewers (<scratch>/rev24h-lens-7c41e9/repro_verdict.py parts 1 and 2; <scratch>/rev-rules-tests-docs-7c41/sim_collector.py; <scratch>/rev_reports_a7c3/sim_restart.sh):
- Process frozen from +3 h to +12.5 h of a 24 h run: the log shows only 'saved 38 candle(s)', zero GAP or FETCH FAILED lines, 'done: saved=97 fetch_failures=0', and the check prints '97 of 97 expected, gaps: 0, PASS (24 h without a gap)'.
- PC suspended for 8 h: one log line 'saved 32 candle(s)', no GAP line, fetch_failures=0; '97 of 97 expected', 'gaps: 0', 'PASS', exit 0.
- Network down for 9.5 h: 1140 FETCH FAILED lines, then 'saved 38 candle(s)'; PASS, exit 0.
- Four 2-minute sessions 9.6 h apart, 8 minutes of running in total: '98 of 98 expected, PASS'. Four runs of about one second each, 9.75 h apart: 'candles: 118 of 118 expected / span: 29.50 h / gaps: 0 / result: PASS (24 h without a gap)', exit code 0.

The likely real case tonight: the owner closes the lid at midnight and opens it at 8, or the internet is down from 02:00 to 05:00, or Windows restarts. On recovery the collector fetches the missed candles from Binance history, the log shows 'saved 24 candle(s)', and check_candles prints PASS with exit code 0. That output is presented as the Phase 3 acceptance evidence, although a worker would have had no price for those hours and every signal in that time (valid for 30 minutes) would have been missed.

**Suggested fix.** Let the check also judge when each candle was collected. For the file being collected now (7-column header, no collection-time column), read the collector's log beside the CSV (file.with_suffix('.log')): every 'saved N candle(s), last T' line carries its write time, so count a candle as collected live only when it was written within about 5 minutes of its close (the alert threshold of PLAN section 7; another reviewer suggests about two poll intervals), the first candle of a fresh file excepted. Print the number of restarts ('start:' lines), FETCH FAILED lines and backfilled candles with the longest outage or delay, and give PASS only when the live, gap-free stretch from the first 'start:' time to the last candle's close reaches --hours.

For later runs a saved_at column (Unix seconds at write time) written by the collector is cleaner: count the candles after the first whose saved_at is more than 300 s after their close, print the count and the longest delay, and fail when there are any; write the new column only to files created with it, or keep the columns of the existing header. If the format must stay, at least log 'BACKFILL: n candle(s) from history after m min without a poll', count it in the 'done' line, and state in the check's output that it cannot see outages shorter than 9.75 h.

Measured from the real start, a clean `--hours 24` run ends at 23.75 to 24.0 h, so raise the collector default and the README to 24.5 h (the current run already uses 25). Test: drive collect main() and check main() with a fake clock through an outage and expect a result other than PASS. For the current run: before trusting PASS, look in the .log for 'saved N candle(s)' with N above 1 and for jumps of more than 15 minutes between lines.

## 6. Weekend candles from the 24/7 Binance contract reach the indicators as if XAUUSD had traded

- Severity claimed: high
- Where: `backend/app/datasource/binance_source.py` line 9
- Verified: no
- Status: open

The docstring (lines 7-10) and plan 5.2 say the Phase 5 market-hours filter handles the fact that the contract trades while the broker's XAUUSD is closed. That filter only stops a signal from being sent during those hours. The candles stay in every indicator window, and nothing in core/ can remove them: candles.py has no notion of market hours, and find_gaps would report a removed weekend as 196 missing candles.

Measured on 1,499 real M15 candles (26 Oct to 11 Nov 2026, fetched once from /fapi/v1/klines):
- Each weekend (Fri 22:00 to Sun 23:00 UTC) adds 196 M15 candles, 49 hours in the H1 frame from aggregate.
- Weekend median range is 0.89 against 6.28 on weekdays; volume is 11 times lower.
- At the Sunday 23:00 UTC reopen, 196 of the 200 bars behind EMA200 are weekend bars.
- ATR14 at the two reopens was 2.68 and 5.36; with the weekend candles left out it was 7.93 and 7.21.

Evidence: <scratch>/candles-lens-r3q7/evidence_weekend.txt (scripts weekend.py, weekend2.py, weekend3.py in the same folder).

**Failure scenario.** Monday 2 Nov 2026, 00:00 UTC, one hour after the broker reopened: signal.evaluate on the Binance M15 frame gives BUY +4 with ATR 3.62, so an SL distance of 5.43. The same candles without the weekend give HOLD -1 with ATR 7.52. In the first 12 hours after the two reopens in the sample, 12 of 26 hourly evaluations gave a different BUY / SELL / HOLD.

For the Phase 4 backtest: 392 of the 1,200 evaluated candles were weekend candles, and 65% of those were BUY or SELL proposals with a median SL distance of 1.68 (weekday 11.52). The owner cannot take those trades, yet they would count towards the 100-trade gate and the expectancy.

**Suggested fix.** This needs the owner's decision before Phase 4: remove candles outside the broker's XAUUSD hours before they reach signal.evaluate, the H1 frame and the backtest, not only block the outgoing signal.

Smallest code: a pure helper in core/candles.py that drops candles outside a given weekly schedule (Sunday 18:00 to Friday 17:00 New York time with the daily 17:00-18:00 break, passed in as parameters and checked against the MT5 symbol specification), with tests. Let find_gaps and check_candles skip the same hours so a removed weekend is not reported as a gap. Note that aggregate drops any hour a session edge cuts. Correct the docstring at binance_source.py:7-10 and plan 5.2.

## 7. compare_prices --report: the verdict depends on when the report is run (one ATR zone measured 'now' is applied to readings taken at other times)

- Severity claimed: high
- Where: `backend/scripts/compare_prices.py` line 146
- Verified: no
- Status: open

report() takes a single threshold, 0.25 x ATR14 of the feed's M15 candles at the moment --report is run (zone_half_width(), lines 123-134), and pricecheck.summarize (pricecheck.py lines 56-57) holds every stored difference against it. The readings were taken hours or days earlier, each under its own zone. On the real XAUUSDT M15 klines of the last 15.6 days this half width ran from 0.11 to 3.46 USD: about 0.8 to 3.5 between Monday and Friday (1.28 to 3.46 on Thursday 5 Nov alone), median about 1.8 on weekdays and 0.26 at weekends, when Binance keeps trading on roughly a tenth of the volume. So the verdict that decides whether the feed is kept changes with the clock, not with the readings. Context that makes this threshold decisive: the one MT5 reading in PLAN section 10 (4062.08 / 4062.21 at 07:44 UTC) is about 6.6 USD below Binance's 07:44 one-minute bar (4068.57 to 4069.09). If that timestamp is right, the raw check will fail and everything hangs on the 'within zone after offset' line.

**Failure scenario.** Replayed with the real report() and the real klines (<scratch>/rev_reports_a7c3/sim_report.py): one unchanged file of 20 readings, offset +6.65, largest deviation from it 0.95. Run on Thu 5 Nov 16:05 UTC (zone 3.306), Mon 22:20 (2.005) or Wed 11 Nov 10:35 (1.288) it says 'usable with a fixed offset correction'. Run on Sat 7 Nov 12:05 (zone 0.264) or Sun 04:05 (0.253) it says 'too far from MT5: try another source'. A second file with no offset and |difference| up to 1.2 reads 'feed is close enough' on Thursday and Wednesday and 'too far' on Saturday. An owner who finishes the readings on Friday and prints the report at the weekend is told to drop a feed that the same data accepts on Monday. In the other direction, a reading that was 2.5 away while its own zone was 1.3 passes when the report is printed at a busy hour (zone 3.4).

**Suggested fix.** Judge each reading against the zone of its own moment. Smallest change: in report() fetch the M15 history once (limit=1500, 15.6 days on Binance), compute the ATR series on closed candles, and give each reading the ATR of the last candle that closed at or before its `time`. Then raw_within_zone = all(|d_i| <= zone_i) and offset_within_zone = all(|d_i - mean| <= zone_i); print how many readings are outside and the worst |d|/zone ratio. Report readings older than the fetched history as 'zone unknown' instead of judging them. A prototype (<scratch>/rev_reports_a7c3/stats_and_fix.py) gives the same result whenever it is run. Alternative: store the zone in each row at record time.

## 8. Twelve Data: closed is decided by the PC clock in the same second the candle ends, with the clock read after the response

- Severity claimed: medium; after verification: low
- Where: `backend/app/datasource/twelvedata_source.py` line 104
- Verified: CONFIRMED
- Status: fixed 2026-11-11 (same closed rule as Binance)

`closed = start + step <= now`, with `now` read after the HTTP call returns (line 88). Consequences:
- A response built before the close and handled after it gets its forming candle flagged closed.
- At now == close time exactly, the candle already counts as closed.
- A PC clock X s ahead closes the forming candle X s early.
- A PC clock that is behind makes the just-closed and the new candle both look forming, so validation fails and the whole fetch is lost (reproduced offline with the clock 20 s behind, as reported with the Binance clock-behind finding).

Whether Twelve Data's last bar is final in the first seconds after a close could not be checked without a key. Binance's is not (see the Binance closed-flag entry, whose reviewers all noted that this adapter uses the same clock rule). The Twelve Data closed-flag test (test_datasources.py:135) uses a frozen fake clock, so the tests cannot see it.

**Failure scenario.** Offline repro with the real adapter: the response is generated 300 ms before the close and the clock is read 100 ms after it. The forming candle comes back closed=True. The collector (120 s poll) appends it and never corrects it.

**Suggested fix.** Read the clock before the request. Mark every row older than the newest row closed. For the newest row keep the clock with a margin, for example `start + step + 15 <= now` (another reviewer suggests a 60 s grace period). The clock is still needed there because XAU/USD stops at weekends and the last candle must still close. Add tests for both cases, and confirm the behaviour once the API has been seen live with a key.

**Verifier's evidence.** I could not refute it. I reproduced every listed consequence offline against the real adapter and the real collector loop, using a simulated Twelve Data server and clock. The one thing I could not check is Twelve Data itself (no key, and calling it was off limits).

**Code path**
- `backend/app/datasource/twelvedata_source.py`: line 75 makes the HTTP call, line 88 reads the clock afterwards, line 104 sets `closed = start + step <= now` with no margin.
- `backend/app/core/candles.py:103` only rejects a forming row that is not the last one, so a frame where every row is "closed" passes.
- `backend/scripts/collect_candles.py:91` appends rows with `time > last_time` once and never rewrites them.
- Nothing documents this as intended; `docs/PLAN.md` section 5 says "Only fully closed candles are used".

**Offline results** (candle 21:00-21:15, final close 4003.30 on a synthetic price path with a late move)
1. **Race, PC clock exact:** response built at 21:14:59.700, clock read at 21:15:00.100. The newest row came back `C 4001.80 closed=True`. The same exchange one second earlier gave `closed=False`.
2. **Exact boundary:** a frozen clock exactly at the close with a pre-close snapshot gave `[True, True]`; 1 ms earlier gave `[True, False]`.
3. **PC clock 5 s ahead:** the whole exchange finished 4.8 s before the real close, and the forming candle was still `closed=True`.
4. **PC clock 20 s behind (and 2 s behind):** a fetch 5 s (1 s) after the close raised `DataSourceError: Twelve Data candles failed validation: only the last candle may still be forming`.
5. **Real `collect_candles.main`, output in scratch:**
   - Race run: saved `21:00:00 ... H 4001.80 C 4001.80` against a final `H 4003.30 C 4003.30`; 11 later polls never corrected it.
   - Clock +5 s run: saved a snapshot taken 3.4 s before the close.
   - Clock -20 s run: logged one `FETCH FAILED ... only the last candle may still be forming`, then saved the correct candle at the next poll.

**Live analogue on Binance** (6 public calls; that adapter uses the same rule)
- This PC's clock is +109 ms (+-37 ms) ahead of the exchange.
- For the 1-minute candle ending 11:29:00 UTC, responses answered 0.002 s and 0.675 s after the close still had that candle as the newest row, `C 4060.08 V 120.868`. The project's rule calls it closed.
- 10 s after the close the same candle read `C 4060.07 V 120.870`. So a real feed's last bar is not final in the first second.

**Not verified**
- The defect needs Twelve Data's newest row to be the forming candle. That is what the adapter's own test (`test_datasources.py:135`) and the `DataSource` docstring assume, but I have not seen it live.
- How often it bites live, and how long Twelve Data's last bar keeps changing after a close, are unknown.

**Two corrections to the claim**
- "At now == close the candle counts as closed" is the intended rule (the Binance test asserts it); it only hurts together with the read order, clock error or non-final data.
- With the clock behind, only that one poll is lost; the candle is saved correctly at the next poll.

**Impact: low today, not medium**
- This adapter is not in use: it needs a key the owner does not have, and there is no `.env`. The 24-hour run and the default price comparison use Binance.
- With the 120 s collector, I estimate well under 1% of candles would be saved a fraction of a second early. That is cents on gold against an entry zone of about 2 USD, but silent and permanent.
- It creates no gaps, so `check_candles` still passes.
- It becomes systematic for a Phase 6 worker that fetches right at each close. Fix it before then, in the same change as the Binance closed flag.

Scripts are in `<scratch>/verify_td_closed_clock_k7q2` (`repro_adapter.py`, `repro_collector.py`, `fix_check.py`, `fix_limit.py`, `proposed_tests.py`, `net_clock_and_boundary.py`). The repository was not touched.

**Verifier's fix.** The reviewer's fix is right in shape, and all three parts are needed. In `backend/app/datasource/twelvedata_source.py`:

```python
CLOSE_GRACE_SECONDS = 60.0  # the newest candle counts as closed only this long after its end

    step = cd.timeframe_seconds(timeframe)
    now = self._clock()          # read BEFORE the request
    data = self._call("/time_series", {...})
    ...
        records.append({..., "closed": True})
    frame = pd.DataFrame(records, columns=list(cd.CANDLE_COLUMNS))
    frame = frame.sort_values("time", kind="stable").reset_index(drop=True)
    if len(frame):               # only the newest row can still be forming
        newest = int(frame["time"].iloc[-1])
        frame.loc[frame.index[-1], "closed"] = bool(newest + step + CLOSE_GRACE_SECONDS <= now)
    # then the existing cd.validate(...) in its try/except
```

I ran exactly this as a subclass in scratch. The race, the exact boundary, a clock 1 or 5 s ahead, a clock 2 or 20 s behind, and a market halt (the last candle closes after the grace period) all came out right. The collector saved 3 of 3 final candles with 0 failed fetches in the race, +5 s and -20 s scenarios.

Additions to the reviewer's version:
- **Use 60 s, not 15 s.** The margin applies only to the newest row. In normal trading it costs nothing, because a candle closes as soon as the server opens the next one; it only delays the last candle before a weekend or daily break. With 15 s, a PC clock 20 s ahead still closed the forming candle in my run; 60 s held.
- **Reading the clock before the request is required even with a margin.** `http.get_json` allows 10 s per socket operation, so a slow answer can outlast a 15 s margin.
- **Remaining limits, worth a line in the docstring:**
  - A PC clock ahead by more than the margin still closes early; Twelve Data sends no server time to check against.
  - It is unknown whether Twelve Data's previous bar is final the moment the next one opens. Once there is a key, fetch the same candle 1 s, 30 s and 120 s after a close and compare.
- **Share the rule.** Put it in one helper used by the Binance adapter too, so the two cannot drift apart.

## 9. HTTP 429 and 418 are retried every 30 s; Retry-After is discarded

- Severity claimed: medium
- Where: `backend/scripts/collect_candles.py` line 85
- Verified: no
- Status: open

get_json turns every HTTP error into the same DataSourceError and drops the response headers (http.py:29-31); the collector then sleeps 'poll' and tries again whatever the status. Binance's rate-limit rules say a client must back off after a 429, that continuing to send requests leads to an IP ban answered with 418, that bans last from 2 minutes to 3 days and grow for repeat offenders, and its 429 and 418 answers carry Retry-After with the seconds to wait (not triggered live on fapi). The limit is per IP (2400 weight a minute; a live answer showed x-mbx-used-weight-1m: 3), so the collector's own 2 calls a minute cannot cause a 429. Another program or device behind the same public address, or carrier NAT, can, and then this loop adds a violation every 30 s.

**Failure scenario.** The real main() against a local server (<scratch>/rev24h-lens-7c41e9/repro_collector_net.py, parts B and C): a 429 with Retry-After: 120 is followed by 4 requests inside those 120 s (at +0, +30, +61 and +91 s); a 418 with Retry-After: 7200 is followed by 238 requests during the two-hour ban, one every 30.3 s. On Binance that is the behaviour its rules name as the cause of a ban and of longer bans: a temporary 429 (shared IP, or other software on the same network using Binance) is answered by continued polling, Binance escalates to a 418 ban, collection stops for the ban period and a gap appears. A ban of up to 3 days on the only keyless source would also stop the Phase 4 history download and the later worker.

**Suggested fix.** Keep the header: in http.py read exc.headers.get('Retry-After') and attach it to the error, for example a 'retry_after: float | None = None' attribute on DataSourceError. In the collector sleep max(poll, exc.retry_after or 0); when a 429 or 418 has no usable header, wait at least 60 s for a 429 and 120 s for a 418, or back off exponentially up to a few minutes. Checked on the patched scratch copy: 1 request instead of 4 inside the 120 s window.

## 10. Two collectors on the same file write every candle twice, and check_candles then ends in a traceback

- Severity claimed: medium; after verification: low
- Where: `backend/scripts/collect_candles.py` line 71
- Verified: CONFIRMED
- Status: open

The collector reads the last saved time once at start (line 71) and afterwards trusts its own memory (lines 91 and 114). Nothing stops a second process from using the same default path; each process then appends every new candle. check_candles gives no verdict for such a file: cd.validate raises 'ValueError: candle times must be strictly increasing (oldest first)' at check_candles.py:28 and the script ends with a traceback (exit code 1).

Run with two real processes on one scratch file (fake M1 source, started 4 s apart, <scratch>/rev-rules-tests-docs-7c41/fake_live_collector.py): every candle after the first was appended by both, giving 7 rows for 4 candles. A scratch file with one duplicated row reproduces the check_candles traceback (<scratch>/rev24h-lens-7c41e9/repro_verdict.py, part 3).

**Failure scenario.** A collector started at 10:28:35 UTC with --hours 25 is running in the background right now. The owner follows README step 1 and starts '.venv/Scripts/python -m backend.scripts.collect_candles --hours 24' in a terminal, or starts the collector again in a second terminal tab to pick up a fix without stopping the first. From the next candle on, both processes append the same rows to data/collect_binance_M15.csv. The acceptance check then crashes instead of printing a result, and the file has to be cleaned by hand or the 24 hours repeated.

**Suggested fix.** Hold an exclusive lock for the run (for example create '<out>.lock' with O_CREAT|O_EXCL, refuse to start while it exists, remove it on exit, and tell the user to delete it after a crash), or at least re-read the file's last saved time just before each append and drop rows that are not newer (the file is a few kilobytes). In check_candles catch the ValueError from validate and print 'result: FAIL: the file is damaged: ...' with the reason instead of a traceback, counting duplicate times explicitly. Test: two collector loops on one file leave each time exactly once.

**Verifier's evidence.** Both halves of the claim reproduce with the real, unmodified scripts. I could not refute it; I rate it lower than claimed.

CODE PATH
- backend/scripts/collect_candles.py: line 71 reads the file's last time once; lines 91 and 114 then use only the in-memory last_time; line 59 gives every default start the same path; the append at line 97 takes no lock and does not re-read.
- backend/scripts/check_candles.py line 28 calls cd.validate with no try; backend/app/core/candles.py lines 87-88 raise ValueError on a repeated time.
- Nothing in the code, README or plan says only one collector may run; the docstring only promises stop-then-restart. So it is not documented, intended behaviour.

REPRODUCTION (all files in <scratch>/verify-dupcollect-k9x3)
1. Two real Windows processes running collect_candles.main, fake real-clock M1 source, started 5 s apart on one scratch file (fake_live_collector.py, two_M1.csv): 9 rows for 5 candles. 11:22 is saved once; 11:23, 11:24, 11:25 and 11:26 twice each. Both processes logged "saved 1 candle(s)" for the same candle one second apart.
2. The claim's own settings on a simulated clock (sim_two_collectors.py: M15, poll 30 s, A --hours 25 from 10:28:35 UTC, B --hours 24 started 62 min later): "rows=197 distinct_times=102 duplicated_rows=95". One collector alone: 101 rows, check PASS.
3. Real "python -m backend.scripts.check_candles" on both duplicated files: traceback through check_candles.py line 28 and candles.py line 88, ending "ValueError: candle times must be strictly increasing (oldest first)", exit code 1, no "result:" line.

PREMISE CHECKED
The live collector is PID 8916 (venv launcher) with child 10196, "-m backend.scripts.collect_candles --hours 25"; its log starts "2026-11-11 10:28:35 UTC start: ... out=data/collect_binance_M15.csv". At 11:30 UTC it had one start line, 6 candles and no repeated time, so the defect has not happened in the current run.

WHY LOW, AND WHY NOT FIX_NOW
- It needs an operator slip: a second start on the same file while the first still runs.
- It fails loudly. Exit code 1 is also the FAIL code, and I found no route to a false PASS.
- Nothing is lost. The repeated rows are identical copies, so dropping them repairs the file. The claim's "or the 24 hours repeated" is overstated.
- The file feeds only the Phase 3 gap check, not signals or the backtest.
- A code fix cannot protect the current run: the running process has the old code and would hold no lock. Until it ends (about 2026-11-12 11:28:35 UTC), the only protection is not starting a second collector on data/collect_binance_M15.csv. A double start is visible afterwards as a second "start:" line with "resume_after=" in the log.
- Killing only the venv launcher also ends the real interpreter (tested on a sleeping script), so a restart does not leave an orphan collector that way.

**Verifier's fix.** Two small changes. Both were tried on patched copies in the scratch folder (collect_fix_lock.py, check_fix.py), not in the repository.

1. collect_candles.py: refuse a second instance with a lock the operating system holds for the life of the process.

import os

def hold_lock(path: Path) -> int | None:
    fd = os.open(path, os.O_RDWR | os.O_CREAT)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd

In main(), right after out.parent.mkdir(...):

    lock = hold_lock(out.with_suffix(".lock"))
    if lock is None:
        print(f"another collector is already writing to {out}: stop it first, or pass a different --out")
        return 2

Close the fd in a finally when main() returns, so main() can be called twice in one process (tests). The leftover data/*.lock file is already git-ignored.

Tested with real processes (lock_real_processes.py): the second collector exits with code 2 after 0.5 s while the first keeps running; after "taskkill /F" on the holder, the next start works although the .lock file is still on disk. In the 25-hour simulation the result is 101 rows, no duplicates, PASS.

2. check_candles.py: replace line 28 with a verdict instead of a traceback.

    try:
        raw = pd.read_csv(args.file)
        repeats = int(raw["time"].duplicated().sum()) if "time" in raw.columns else 0
        if repeats:
            raise ValueError(f"{repeats} candle time(s) are saved more than once (two collectors on one file?)")
        frame = cd.validate(raw.drop(columns=["utc"], errors="ignore"), args.timeframe)
    except (OSError, ValueError) as exc:
        print(f"file:      {args.file}")
        print(f"result:    FAIL (the file cannot be checked: {exc})")
        return 1

Tested: duplicated, 0-byte, missing and double-header files all print "result: FAIL (...)" with exit 1; a clean file still prints PASS with exit 0.

Where the reviewer's suggestion falls short:
- The O_CREAT|O_EXCL lock file goes stale after any hard stop. Demonstrated in excl_lock_stale.py: after the holder is killed the file remains and the next start gets FileExistsError. The background collector can only be stopped by a kill, and a reboot or power cut does the same, so restarts would regularly need a file deleted by hand. Use the OS-held lock above.
- Re-reading the last saved time before each append does give a clean file (simulation: 102 rows, no duplicates, PASS), but it is not sufficient alone. Both collectors keep polling (5880 fetches against 3000 in 25 h; for Twelve Data at 120 s that is 1440 calls a day against the 800 free), and the check-then-append is not atomic. It is fine as an optional extra.
- Catching only ValueError in check_candles misses a missing or locked file; catch OSError too.

Neither change protects the run in progress, because the old process holds no lock.

## 11. Twelve Data quote is stamped with the PC's fetch time, so a frozen price never looks stale

- Severity claimed: medium
- Where: `backend/app/datasource/twelvedata_source.py` line 119
- Verified: no
- Status: open

`/price` returns only `{"price": ...}`, and fetch_quote() sets Quote.time to the PC clock at fetch (`self._clock()`). `Quote.time` is the only freshness information a consumer gets (base.py:29), so for this source the price's age is always about zero by construction, whatever its real age: offline, a price fetched 30 hours after the last candle comes back with age 0. As far as the reviewer knows from Twelve Data's documentation (it cannot be called here without a key; fairly sure, not certain), `/price` keeps answering with the last price while the market is closed or their feed is stalled. compare_prices writes this time into the reading's time and utc columns.

The planned STALE_FEED rule (price older than TRADEPILOT_STALE_AFTER_SECONDS = 60), the 'no price for 5 minutes' alert and the dashboard's price age can therefore never fire for Twelve Data. That defeats the hard rule 'feed stale or down: no signal, send a system alert'. test_twelvedata_quote_is_timed_at_fetch (test_datasources.py:162) pins the current behaviour.

The Binance quote is fine: it uses the exchange's own time, which was 83 ms old in a live call (its fallback to the PC clock at binance_source.py:92 applies only when the answer has no 'time').

Separately, line 117 (`if price <= 0`) lets non-finite values through: `{"price": "NaN"}` returns `Quote(price=nan)` (checked).

**Failure scenario.** Twelve Data is chosen as the source (it is the plan's backup). At the weekend, in the daily break when XAU/USD is closed, or when the vendor's feed stalls, /price keeps returning the last price (Friday's on a Saturday) and Quote.time says 'now'. Every freshness check passes: the STALE_FEED filter and the 'no price for 5 minutes' alert can never fire for this source, the price is shown and used as live, and a signal could go out on an hours-old price. Already in Phase 3, `compare_prices --source twelvedata` would record such a frozen price next to a live MT5 reading, stamped as current, with nothing in the row to tell it apart.

**Suggested fix.** Do not present a local fetch time as the price's time. Smallest change: add a field to `Quote` (for example `time_from_source: bool = True`, or allow time to be None), set it False here and in the Binance fallback on binance_source.py:92, and have the stale rule treat such a quote as of unknown age; or take price and time from the newest 1min candle of /time_series, whose datetime is the vendor's own. Better once a key exists: read the quote from an endpoint that carries a source timestamp. `/quote` documents `timestamp`, `last_quote_at` and `is_market_open` for 1 credit; confirm the field names live. Also reject non-finite prices: `if not math.isfinite(price) or price <= 0`.

## 12. compare_prices can record an old price as a live reading: the csv source gets in through the default when TRADEPILOT_PRICE_SOURCE=csv, and the quote's age is never checked

- Severity claimed: medium
- Where: `backend/scripts/compare_prices.py` line 177
- Verified: no
- Status: open

(a) `--source` excludes csv through `choices`, but its default is `settings.price_source`, and argparse does not check a default against `choices` (line 177). With TRADEPILOT_PRICE_SOURCE=csv and no `--source`, `make_source` builds CsvSource and `record()` treats CsvSource.fetch_quote(), the last close in the file, as the feed's current price. Typing `--source csv` explicitly is refused, so the guard exists but the default walks around it.

(b) record() never looks at quote.time (line 78), so an old quote from any source is saved as if read at that moment. settings.stale_after_seconds exists but is not used here.

(c) The same reviewer also listed the Twelve Data quote being stamped with the PC clock (twelvedata_source.py:119) and the Binance fallback at binance_source.py:92; that part is covered by the Twelve Data quote-time entry.

collect_candles.py:50 has the same default pattern: a misspelt value such as `Binance` ends in a raw ValueError traceback from `make_source`.

**Failure scenario.** From Phase 4 the owner sets TRADEPILOT_PRICE_SOURCE=csv in .env for backtests (an option .env.example lists) while still taking the 20 readings at different hours, and runs `compare_prices` exactly as the README shows. Each reading compares MT5's live price with a candle close that is hours or months old and is saved as a genuine reading with source `csv`; --report then gives a verdict about a file. Reproduced with the collector's file as the CSV and a scratch output file: typing `4062.08 4062.21` printed `saved: csv 4057.22 vs MT5 mid 4062.145 -> difference -4.93` and appended the row. On other scratch files a five-day-old close was saved as a reading ('saved: csv 3995.00 vs MT5 mid 4062.145 -> difference -67.14') and --report then summarised 'source csv'.

**Suggested fix.** Validate after parsing in compare_prices.main: `if args.source not in allowed: parser.error(...)` (in particular when args.source == 'csv'), or use `binance` as the default when `settings.price_source` is not one of the allowed live sources. The same check in collect_candles turns the misspelt-source traceback into a clean argparse error. In record(), do not save a reading whose quote is older than settings.stale_after_seconds; print its age instead. Add a test for each.

## 13. The feed quote can be much older than the MT5 numbers it is paired with

- Severity claimed: medium
- Where: `backend/scripts/compare_prices.py` line 78
- Verified: no
- Status: open

The feed is read once, at the first Enter (line 78). The MT5 numbers are whatever is typed afterwards: nothing measures the time in between and the feed is not read again. Each rejected entry (decimal comma, one number, empty line) asks again inside the inner loop (lines 82-90) and keeps the old quote. An extra Enter after typing the numbers is taken by the next '[n/20] ... press Enter' prompt, which fetches the next quote at once.

**Failure scenario.** Reproduced with scripted keyboard input (<scratch>/rev_reports_a7c3/sim_input.py and <scratch>/rev_reports_a7c3/sim_gate.py): Enter, then 'x', '4062,08 4062,21', an empty line and finally '4062.08 4062.21' costs one quote, and the row is saved against it. After a stray Enter the quote for the next reading is fetched immediately and the numbers typed later are saved against it. On the last 25 hours of 1-minute XAUUSDT bars the price moved by a median of 0.63 USD within one minute (90th percentile 1.86, 99th 4.36) and 0.89 / 2.66 within two. That is the size of the zone half width (1.3 to 2.5) the scatter is judged against, and because the verdict takes the largest of 20 readings, one pair typed a minute apart can turn 'usable with a fixed offset correction' into 'too far'. The smoke-test row in <scratch>/smoke_compare.csv shows the effect: the 07:44 MT5 reading typed at 07:58:12 was saved as difference 4.9, while Binance at 07:44 was 6.4 to 6.9 above it.

**Suggested fix.** Time and bracket each reading: take time.monotonic() after fetch_quote() and fetch a second quote once the numbers parse. If more than about 20 seconds passed, or the two feed prices differ by more than about 0.5 USD, discard the reading and start again from the Enter prompt; do the same after a parse error instead of asking again. Otherwise save the mean of the two feed prices and store the elapsed seconds in the row.

## 14. A mistyped MT5 price is saved at once: no check against the feed and no way to take it back

- Severity claimed: medium
- Where: `backend/scripts/compare_prices.py` line 58
- Verified: no
- Status: open

parse_broker accepts any two numbers with 0 < bid <= ask (line 58). The typed prices are never compared with the feed price fetched a moment earlier, the row is appended straight away (lines 107-113), and there is no undo. The verdict rests on the largest |difference| and on the mean, so one slip decides it. The slip also lands in 'MT5 spread, avg / max', the figure PLAN section 10 wants for the backtest spread.

**Failure scenario.** Run with scripted keyboard input (<scratch>/rev_reports_a7c3/sim_input.py): '406208 406221' (dots forgotten) is saved with difference -402145.79; '4062.08 4602.21' (two digits swapped) is saved with difference -263.44 and spread 540.13; '4062.08 4065.21' is saved with spread 3.13; '4062.08 inf' is saved, and --report then stops with 'price differences must be finite'. Nineteen good readings (offset +6.6) plus one row that is 135 USD off give 'average difference -0.060 ... largest |difference| 128.400 ... verdict: too far from MT5: try another source'. The only repair is editing data/price_compare.csv by hand. If the editor leaves no final newline, the next reading is glued onto the last line (reproduced in <scratch>/rev_reports_a7c3/sim_broken_csv.py: the count stays at 20 and --report raises ParserError 'Expected 12 fields in line 21, saw 23').

**Suggested fix.** Before saving, hold the typed prices against the feed: if |difference| is above 0.5% of the feed price (about 20 USD), or ask - bid is above 0.1% (about 4 USD), or a value is not finite, show the numbers and ask to retype, with 'y' to keep. Add an undo ('u' at the Enter prompt removes the last saved row). When appending, write a newline first if the file does not end with one.

## 15. Readings taken while the broker's gold market is closed are accepted and counted

- Severity claimed: medium
- Where: `backend/scripts/compare_prices.py` line 92
- Verified: no
- Status: open

Binance XAUUSDT trades around the clock; the broker's XAUUSD does not. It is shut at the weekend and, judging by the collapse in Binance's own volume, for a daily break at 22:00-23:00 UTC (06:00-07:00 in Ulaanbaatar). While it is closed MT5 keeps showing its last bid and ask. record() checks neither the day and hour nor whether the MT5 numbers moved since the previous reading, and report() uses every row. PLAN section 5.2 already names this hazard for signals (MARKET_CLOSED, 'needed because the Binance contract trades 24/7'); the comparison has no such guard.

**Failure scenario.** The owner takes some of the 'different hours' readings on a Saturday. Measured on the klines (<scratch>/rev_reports_a7c3/weekend_study.py): after the close on Friday 6 Nov, Binance moved as far as 34 USD below its level at the close before the weekend was over, and up to 15 USD above it the weekend before. Each such reading is saved as a 'difference' of that size on top of the real one, so the report shows a largest |difference| of 20 to 30 and 'too far from MT5: try another source'. With Twelve Data the error would go the other way if its price also stands still at the weekend (not verified, no API key): Saturday readings would agree perfectly and pass.

**Suggested fix.** In record(), before saving: when the UTC time falls in the usual closed windows (Friday 22:00 to Sunday 23:00, and 22:00-23:00 daily; confirm against the symbol's sessions in MT5), or the typed bid and ask equal those of the previous reading taken more than a minute earlier, warn that MT5 is probably showing a frozen price and save only on an explicit 'y'. In report(), leave rows from those windows out and say how many were left out.

## 16. test_guards.py does not enforce what its docstring claims: order code and impure core code pass all three guard tests

- Severity claimed: medium
- Where: `backend/tests/test_guards.py` line 16
- Verified: no
- Status: open

Verified on a scratch copy of backend/ (<scratch>/rev-rules-tests-docs-7c41/guardcopy). Each of these additions still gives 'test_guards.py: 3 passed':

1. backend/scripts/mt5_reader.py with 'import MetaTrader5' and mt5.order_send(...). The scan covers backend/app only (APP_DIR, line 11), and scripts/ is where a 'read MT5 automatically' shortcut for compare_prices would be written.
2. backend/app/worker.py and backend/app/datasource/exec_source.py placing orders through an exchange library (binance.um_futures.UMFutures(...).new_order(...), ccxt ... create_order(...)). The trading tokens (line 16) are MT5 names only, and the endpoint tokens (lines 60-69) match raw REST paths only.
3. backend/app/notify.py with a signed 'POST /fapi/v1/order' and an X-MBX-APIKEY header. The endpoint tokens are applied to datasource/ only.
4. backend/app/core/backtest.py with pd.read_csv('data/...'), pd.read_json('https://...'), pd.Timestamp.now(), builtin open(), np.save, __import__('urllib.request') and 'from .. import datasource'. The core guard checks import names only (line 20); pandas and NumPy do file, network and clock work themselves, builtins need no import, and relative imports are skipped because node.module is None (line 32).

False alarms, also verified: 'from collections import deque', 'from enum import Enum' or 'from datetime import timezone' in core/ fails the core guard; the word 'MetaTrader5' in any docstring under app/, and '/order' or 'POST' in a datasource comment, fail the token guards.

**Failure scenario.** Phase 4 adds core/backtest.py that loads history with pd.read_csv, or Phase 5 writes the price-age filter in core/filters.py with pd.Timestamp.now(): 'core is pure' is broken and pytest stays green. Or a later session automates the 20 MT5 readings with the MetaTrader5 package inside backend/scripts/: a broker connection enters the repository and the 'signals only' guard does not fire.

**Suggested fix.** Scan backend/app and backend/scripts (everything except tests) in the trading guards and apply the endpoint tokens there too. Add an import allow-list for those folders (today: fastapi, pydantic, pydantic_settings, pandas, numpy and the standard-library modules in use), so any new third-party library needs a deliberate edit; check requirements.txt the same way. In the core guard reject relative imports (node.level > 0) and walk the AST for the names open, print, input, eval, exec, __import__ and the attributes read_csv, read_json, to_csv, now, utcnow, today, save, load and the like. That list has no hit in today's core/ (<scratch>/rev-rules-tests-docs-7c41/proposed_guard_check.py). Tone the docstring down to 'tripwires'.

## 17. Non-ASCII error text can crash the logger when stdout is not UTF-8

- Severity claimed: medium
- Where: `backend/scripts/collect_candles.py` line 62
- Verified: no
- Status: fixed 2026-11-11 (stdout errors='replace')

log() prints exception text with print(). Windows socket and OS error messages are localized and can contain non-ASCII characters; when stdout is not a UTF-8 console (output piped or redirected, legacy code page) print() raises UnicodeEncodeError inside the except handler.

**Failure scenario.** The collector is started with its output redirected to a file or a pipe on a non-English Windows. The first network error message contains characters outside the ANSI code page; print() raises UnicodeEncodeError and the run dies exactly when an outage occurs.

**Suggested fix.** Call sys.stdout.reconfigure(errors='replace') (and the same for stderr) at start, or encode defensively inside log().

## 18. check_candles: one old gap (for example an old CSV at the default path) fails the file for good, even after 24 clean hours

- Severity claimed: low
- Where: `backend/scripts/check_candles.py` line 45
- Verified: no
- Status: open

`passed = not gaps and span_hours >= args.hours` (check_candles.py:45) asks for the whole file to be free of gaps. The collector resumes any existing file at the default path, however old (collect_candles.py:71; its docstring: 'Safe to stop (Ctrl+C) and restart'). If the file's last candle is more than 39 candles back (an outage longer than about 10 hours, or an earlier try-out run), the new run starts with 'GAP: n candle(s) missing' and that hole stays, so the file can never pass, however long the collector runs afterwards. If the old file is less than 9.75 h old, the time since then is downloaded from history and counted towards the 24 h span. The docstring and the README say only that restarting is safe; neither says the file must be deleted before a new measurement.

**Failure scenario.** A 20-minute trial two days earlier, then a flawless 24 h run on the default path (<scratch>/rev24h-lens-7c41e9/repro_verdict.py, part 5): the log starts with 'GAP: 151 candle(s) missing after 2026-11-09 10:30:00 UTC', then 'saved 39 candle(s)' (9.75 h of history in spite of the fresh-start rule), and check_candles prints '138 of 289 expected, gaps: 1, FAIL'. Also built in <scratch>/rev_reports_a7c3/sim_check.py: 5 candles, a hole of 55, then 30 hours of consecutive candles give 'gaps: 1 ... result: FAIL (24 h without a gap)', exit code 1. This is what a second attempt meets after any failed first attempt (a short try-out run the day before, or a first attempt cut off by a day without the PC): the check says FAIL, and the owner has to work out that the file must be deleted and then wait another 24 hours.

**Suggested fix.** In check_candles judge the last gap-free stretch: split the series at the gaps, compute each stretch's span (start the last one at gaps[-1].before), still list the gaps, print that stretch's start and end beside them, and pass when the latest stretch covers --hours. Or, in the collector, when the existing file cannot be continued without a gap, rename it (for example add its last candle time to the name) and start a fresh file under the fresh-start rule. At the least, say in the GAP line and in the README that the file must be deleted to start a new measurement.

## 19. check_candles ends in a traceback instead of a result on duplicate rows, a cut last row or an empty file

- Severity claimed: low
- Where: `backend/scripts/check_candles.py` line 28
- Verified: no
- Status: fixed 2026-11-11 (broken files give a message and exit code 1)

cd.validate() raises on the first irregular row and pd.read_csv raises on an empty file; check_candles (line 28) lets both through. The exit code is 1, so this is never a false PASS, but there is no report and no hint at the cause.

**Failure scenario.** Reproduced in <scratch>/rev_reports_a7c3/sim_check.py: every candle present twice gives 'ValueError: candle times must be strictly increasing (oldest first)'; a last row cut short gives 'ValueError: prices must be positive and finite'; a zero-byte file gives EmptyDataError. The duplicate case is close at hand: a collector is already running in the background on data/collect_binance_M15.csv, and README step 1 tells the owner to start one. A second process resumes from the same last row, and from then on both append every new candle. The 24-hour file then cannot be checked at all, although no candle is missing.

**Suggested fix.** In check_candles, drop rows that exactly repeat an earlier row and print the count ('duplicate rows: N, were two collectors running?'). Set aside an unparsable final row as 'last row incomplete' and check the rest. Catch ValueError and EmptyDataError and print 'result: FAIL (<reason>)' with exit code 1.

## 20. validate() accepts millisecond or nanosecond timestamps (and a coarser timeframe) as valid candle times in seconds

- Severity claimed: low
- Where: `backend/app/core/candles.py` line 85
- Verified: no
- Status: open

The only checks on `time` are whole numbers, `time % step == 0` (line 85) and strictly increasing (core/candles.py:81-88). A millisecond timestamp of an aligned candle is also a multiple of the step (multiples of 900,000 are multiples of 900), so a frame whose times are in ms passes unchanged. A datetime64 column passes as nanosecond integers (pd.to_numeric converts it). Nothing bounds the value. An H1 file read with timeframe M15 passes too, because every H1 time is M15-aligned. validate is the one place meant to stop a source's own format from reaching the signal engine, and CsvSource sends a numeric `time` column straight to it (csv_source.py:51).

Tried in <scratch>/candles-lens-r3q7/exp3.py. (One reviewer's related note about unparseable time text at csv_source.py:43 is covered by the CSV text-times entry.)

**Failure scenario.** A Phase 4 history CSV whose `time` column is Binance's raw openTime in milliseconds (a download that forgets `// 1000`, or Binance's public dump files), or a frame with pandas datetimes. Checked with 1,499 real Binance M15 candles written that way; CsvSource accepts the file without a word:
- validate() returns the ms times unchanged.
- find_gaps reports 999 missing candles between every pair (1,498 gaps).
- aggregate / fetch_candles('H1') turns 1,499 M15 candles into one forming candle and no closed ones.
- signal.evaluate runs normally and returns candle_time 1794392100000 and signal_time 1794392100900 (it adds 900 to a ms value for signal_time and expires_at).
- fetch_quote().time is 1794392100900.0.
- On an 8-candle ms file check_candles prints '8 of 7001 expected', then crashes in utc() with OSError [Errno 22] on Windows.

The M15 backtest runs without complaint because scoring is index-based, while everything time-based is silently wrong: the H1 confirmation has no closed H1 candles, and a market-hours or news-window filter would test times in the year 58,000. An H1 file read with the default TRADEPILOT_CSV_TIMEFRAME=M15 is also accepted and evaluated as M15.

**Suggested fix.** In validate(), reject a datetime-typed `time` column and values outside a plausible range of seconds, for example `if (frame['time'] >= 10_000_000_000).any(): raise ValueError('candle times look like milliseconds; expected UTC Unix seconds')` (other reviewers suggest the bound 1e11, or 4_102_444_800, which is 2100-01-01). Optionally also reject a frame of 3 or more rows in which no two neighbours are exactly one step apart; that catches milliseconds, nanoseconds and a coarser timeframe in one check. Add the cases to test_validate_rejects_bad_frames.

## 21. CSV text times: unparseable text raises a raw exception; day-first dates and time-zone names are silently misread as UTC

- Severity claimed: low
- Where: `backend/app/datasource/csv_source.py` line 43
- Verified: no
- Status: open

`pd.to_datetime(raw['time'], utc=True)` runs outside the try blocks and uses pandas' guessing parser. Run on pandas 2.3.3:
- Text it cannot parse, or two formats in one file, raises a raw ValueError or DateParseError instead of DataSourceError.
- '03.11.2026 07:00:00' (3 November, day first) is read as 11 March with no warning.
- A zone name is dropped with only a FutureWarning: '2026-11-11 09:00:00 EET' becomes 09:00 UTC, 2 hours off.

Read correctly: ISO text, offsets such as +02:00 (including across a DST change), date-only daily candles, and MT5-style '2026.11.11 07:00'.

**Failure scenario.** The plan allows history 'from another source' for Phase 4. A broker or spreadsheet export with day-first dates and only days 1 to 12 loads as the wrong months, so candles sit months away from the news and market-hours windows. With a day above 12 the load crashes with a traceback that callers catching DataSourceError do not handle.

**Suggested fix.** Parse strictly and wrap the error:
`try: stamps = pd.to_datetime(raw['time'], utc=True, format='ISO8601')`
`except (ValueError, TypeError) as exc: raise DataSourceError(f'{self.path}: bad time column: {exc}') from None`

Checked on pandas 2.3.3: this still accepts the forms listed as read correctly and rejects day-first text, zone names and garbage.

The same raw-exception gap is in twelvedata_source.py line 95: `int(stamp.timestamp())` is outside the try, so a null or empty datetime gives NaT and a raw ValueError that would stop the collector.

## 22. An H1 frame from aggregate gets M15 timing in signal.evaluate unless the caller remembers timeframe_seconds

- Severity claimed: low
- Where: `backend/app/core/signal.py` line 74
- Verified: no
- Status: open

aggregate itself is correct: its H1 candles from 1,500 real M15 candles equal Binance's own H1 candles in all 375 rows (open, high, low, close and the closed flag), and evaluate scores them the same.

The gap is the timing. evaluate takes the candle length from SignalParams.timeframe_seconds (default 900, line 74; used at line 232). The frame carries no timeframe, Settings.signal_timeframe is not connected to it, and test_aggregate_output_feeds_the_signal_engine_shape (backend/tests/test_candles.py:158) only compares column names and never calls the engine. No Phase 3 code calls evaluate on an H1 frame yet, so this is a trap for Phase 5 and 6 rather than a live error.

**Failure scenario.** `signal.evaluate(cd.aggregate(m15, 'M15', 'H1'))` on today's Binance data returns SELL -3 with candle_time 1794387600 (09:00 UTC), signal_time 1794388500 (09:15, where the candle closes at 10:00 = 1794391200) and expires_at 1794390300 (09:45). The signal expires 15 minutes before its own candle closes.

This shows up as soon as the H1 result is stored or displayed (the Phase 5 H1 score) or TRADEPILOT_SIGNAL_TIMEFRAME is set to H1 (an open question in the plan).

**Suggested fix.** Build the params from the timeframe in one place, for example a small helper that returns `dataclasses.replace(params, timeframe_seconds=cd.timeframe_seconds(timeframe))`, and use it wherever a frame of another timeframe is evaluated. Replace the columns-only test with one that evaluates an aggregated H1 frame and asserts signal_time == candle_time + 3600.

## 23. The '20 readings' gate counts the wrong thing: every source in the prompt counter, and no spread in time for the verdict

- Severity claimed: low
- Where: `backend/scripts/compare_prices.py` line 71
- Verified: no
- Status: open

Two separate things. First, record() numbers its prompt from count_rows(out) (lines 63-67 and 71), which counts the rows of every source in the file, while report() counts only the chosen source. Second, report() releases the verdict on the row count alone (lines 163-170). record() asks for the next reading immediately, and nothing checks that the readings are spread over hours, although the module's own docstring says that spread is what makes the picture fair.

**Failure scenario.** Both reproduced (<scratch>/rev_reports_a7c3/sim_report.py and <scratch>/rev_reports_a7c3/sim_gate.py). First: after 12 Binance readings, a Twelve Data session into the same default file starts at [13/20] and after its 8th reading prints '20 readings recorded. Run with --report for the summary.', although Binance has 12 and Twelve Data 8; the reports then say 'not enough readings yet'. Second: 20 readings typed 18 seconds apart (period 10:00:00 .. 10:05:42 UTC) give 'verdict: usable with a fixed offset correction'. That is the perpetual's premium over spot during six minutes, presented as the acceptance result.

**Suggested fix.** Count only rows of the current source in count_rows (pass source.name). In report(), hold the verdict back until the readings cover, for example, at least 6 different UTC hours, and say so ('not spread over enough hours yet: 20 readings within 0.1 h').

## 24. 12 of 20 deliberate bugs in the Phase 3 code pass all 140 tests; the three scripts have no test at all

- Severity claimed: low
- Where: `backend/tests/test_datasources.py` line 231
- Verified: no
- Status: open

Mutation check on a scratch copy (<scratch>/rev-rules-tests-docs-7c41/mutate.py; the repository was not touched). The suite still reports '140 passed' after each of these changes:
- http.get_json sends no query string at all, so no symbol, interval, limit or apikey. The two http tests (line 231) only raise from a fake urlopen and never look at a successful request.
- get_json no longer turns a non-JSON body, a timeout or a connection reset into DataSourceError.
- make_source gives Twelve Data the masked text '**********' instead of the key, ignores TRADEPILOT_BINANCE_SYMBOL, and ignores TRADEPILOT_CSV_PATH and CSV_TIMEFRAME. test_make_source_builds_each_source (line 217) checks only .name; it also fails when TRADEPILOT_PRICE_SOURCE is exported in the shell (verified with =csv).
- Twelve Data drops volume, and accepts a zero or negative price.
- CsvSource treats an unreadable 'closed' text as forming, and no longer rejects a file with no closed candle.
- validate accepts fractional times; pricecheck gives a verdict for a NaN zone.

Tests that do not test what their name says:
- test_binance_network_errors_propagate_as_datasource_errors (line 108): the fake raises the expected exception itself.
- test_quote_spread_is_none_without_bid_and_ask (line 252): the last line only asserts that the price is not NaN.
- test_aggregate_rejects_impossible_timeframes (test_candles.py:163): the ('M30', 'M45') case passes because M45 is unknown; no pair of known timeframes reaches the 'not a multiple' rule.

Scripts: the logic that most needs a test is the collector's loop body (collect_candles.py:88-115: take only closed candles newer than the last saved one, append once, resume, log gaps) and check_candles' verdict (lines 28-47); the three script bugs the same reviewer reported (false PASS after an outage, the collector crash, two collectors on one file) live there. Next come compare_prices' source default, parse_broker and the verdict ladder (lines 163-170). Replacing cc.make_source and cc.time with a fake source and a fake clock runs the real main() through a simulated 24 hours in about a second (<scratch>/rev-rules-tests-docs-7c41/sim_collector.py).

**Failure scenario.** A later edit to get_json's URL building (for example while adding the HTTPException fix) drops or mangles the query string. pytest stays green, and the first sign is the live collector logging 'FETCH FAILED: HTTP 400' on every poll, or Twelve Data answering 401.

**Suggested fix.** Add:
1. A get_json test with a fake urlopen that returns a body and captures the request: expect full_url == '...?symbol=XAU%2FUSD&apikey=k', the decoded JSON back, and DataSourceError for an HTML body.
2. make_source tests that check symbol, key (through a captured request parameter), CSV path and timeframe, built from Settings(_env_file=None, price_source='binance', ...).
3. Twelve Data rows with volume and a price of '0'; a CSV with closed='maybe' and one with only forming rows; a fractional time for validate; summarize(zone_half_width=nan).
4. Script tests with the fake source and clock: a fresh start saves one closed candle, a resume appends only newer ones, a forming candle is never written, an outage is reported; check_candles exit codes for clean, gap, short and duplicate files; parse_broker cases.

## 25. Documentation that does not match the code: 'py -m' usage text fails, Twelve Data instructions are incomplete, a mid-run check reads FAIL

- Severity claimed: low
- Where: `backend/scripts/collect_candles.py` line 9
- Verified: no
- Status: open

1. The usage text of all three scripts, which --help prints, says 'py -m backend.scripts...' (collect_candles.py:9-10, check_candles.py:3, compare_prices.py:5 and :14). On this PC 'py' is the global Python 3.11, and 'py -m backend.scripts.collect_candles --help' ends with 'ModuleNotFoundError: No module named pydantic'. It works only in a shell where the venv is activated. README and CLAUDE.md correctly use '.venv/Scripts/python -m ...'; those commands work with --help.
2. README lines 53-54 say that using Twelve Data needs only the key in .env. The scripts still default to Binance. It also needs '--source twelvedata' on both scripts (or TRADEPILOT_PRICE_SOURCE=twelvedata), and the gap-check file is then data/collect_twelvedata_M15.csv.
3. README line 62 invites running check_candles while the collection is still going. On a copy of the healthy live file it prints 'gaps: 0' and then 'result: FAIL (24 h without a gap)', exit 1, because the span is still short.
4. docs/PLAN.md lines 302-303 say the Twelve Data adapter is 'tested against recorded response shapes'. No response was ever recorded (there is no key); the shapes are assumed.
5. collect_candles.py:30 says 40 candles cover a 10-hour outage. One of the 40 is the forming candle, and a simulated outage of exactly 10 hours left a 1-candle gap.
6. The health() docstring in backend/app/main.py says the feed reports a real state from Phase 3; /api/health still returns 'not_configured', as README line 48 shows.

**Failure scenario.** The owner reads the usage text from --help and types 'py -m backend.scripts.compare_prices' in a fresh PowerShell window: traceback. Or the owner gets a Twelve Data key, writes it into .env as README says, repeats the two README steps and compares Binance a second time, believing it was Twelve Data. Or the owner checks the running collection as README suggests and reads 'FAIL' for a healthy run.

**Suggested fix.** Change the usage text to '.venv/Scripts/python -m ...'. Add the '--source twelvedata' form and its file name to README. While the span is short, print 'collecting: X of 24 h so far, no gap' instead of FAIL. Say 'assumed response shapes' in PLAN.md. Correct the FETCH_LIMIT comment to about 9.5 hours. Point the health() docstring to Phase 6.

