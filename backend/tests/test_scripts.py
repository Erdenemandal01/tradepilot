"""The collector's save step and the candle-file check. No test touches the network."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.app.config import get_settings
from backend.app.core import candles as cd
from backend.app.datasource import DataSourceError, Quote
from backend.scripts import check_candles as check
from backend.scripts import collect_candles as cc
from backend.scripts import compare_prices as cp

T0 = 1_793_998_800  # an H1 boundary
M15 = 900


def frame(n: int, start: int = T0, *, forming_last: bool = True) -> pd.DataFrame:
    """n consecutive M15 candles; like a source, the newest one is still forming."""
    i = np.arange(n, dtype=float)
    data = pd.DataFrame(
        {
            "time": start + M15 * np.arange(n),
            "open": 100 + i,
            "high": 101 + i,
            "low": 99 + i,
            "close": 100.5 + i,
            "volume": 2.0,
            "closed": True,
        }
    )
    if forming_last and n:
        data.loc[n - 1, "closed"] = False
    return cd.validate(data, "M15")


class Log:
    def __init__(self):
        self.lines: list[str] = []

    def __call__(self, message: str) -> None:
        self.lines.append(message)


def saved_times(path) -> list[int]:
    return pd.read_csv(path)["time"].tolist()


# --- collector: save_new_candles ---------------------------------------------


def test_fresh_file_starts_at_the_latest_closed_candle(tmp_path):
    out, log = tmp_path / "c.csv", Log()
    last, count = cc.save_new_candles(frame(6), out, None, M15, log)
    assert (last, count) == (T0 + 4 * M15, 1)
    assert saved_times(out) == [T0 + 4 * M15]
    assert cc.last_saved_time(out) == T0 + 4 * M15


def test_forming_candle_is_never_saved(tmp_path):
    out, log = tmp_path / "c.csv", Log()
    # One candle only: it is the forming one, so there is nothing to save yet.
    assert cc.save_new_candles(frame(1), out, None, M15, log) == (None, 0)
    assert not out.exists()
    # Later the same candle has a successor and is saved; the new last row is not.
    last, count = cc.save_new_candles(frame(2), out, None, M15, log)
    assert (last, count) == (T0, 1)
    assert cc.save_new_candles(frame(2), out, last, M15, log) == (T0, 0)
    assert saved_times(out) == [T0]


def test_resume_appends_only_newer_closed_candles_once(tmp_path):
    out, log = tmp_path / "c.csv", Log()
    last, _ = cc.save_new_candles(frame(3), out, None, M15, log)
    last, count = cc.save_new_candles(frame(6), out, last, M15, log)
    assert (last, count) == (T0 + 4 * M15, 3)
    assert saved_times(out) == [T0 + i * M15 for i in (1, 2, 3, 4)]
    assert not any("GAP" in line for line in log.lines)
    text = out.read_text(encoding="utf-8")
    assert text.count("time,utc,open") == 1  # one header only


def test_gap_longer_than_the_fetch_window_is_logged(tmp_path):
    out, log = tmp_path / "c.csv", Log()
    last, _ = cc.save_new_candles(frame(3), out, None, M15, log)  # saved: T0 + 1
    later = frame(4, start=T0 + 10 * M15)
    last, count = cc.save_new_candles(later, out, last, M15, log)
    assert count == 3
    assert any(line.startswith("GAP: 8 candle(s) missing") for line in log.lines)


def test_write_failure_keeps_the_position_so_the_candles_are_retried(tmp_path):
    log = Log()
    locked = tmp_path / "locked.csv"
    locked.mkdir()  # opening a directory for append fails like a file locked by Excel
    assert cc.save_new_candles(frame(4), locked, T0, M15, log) == (T0, 0)
    assert any(line.startswith("WRITE FAILED") for line in log.lines)

    out = tmp_path / "c.csv"
    last, count = cc.save_new_candles(frame(4), out, T0, M15, log)  # the retry, file free again
    assert (last, count) == (T0 + 2 * M15, 2)


def test_volume_missing_at_the_source_stays_empty(tmp_path):
    out, log = tmp_path / "c.csv", Log()
    data = frame(3).assign(volume=np.nan)
    cc.save_new_candles(data, out, T0 - M15, M15, log)
    assert pd.read_csv(out)["volume"].isna().all()


# --- check_candles -----------------------------------------------------------


class FakeSource:
    name = "fake"

    def __init__(self, reference=None, error=None):
        self.reference, self.error = reference, error

    def fetch_candles(self, timeframe, limit=500):
        if self.error:
            raise self.error
        return self.reference


def write_file(tmp_path, data: pd.DataFrame):
    path = tmp_path / "collect.csv"
    cc.save_new_candles(data, path, int(data["time"].iloc[0]) - M15, M15, Log())
    return path


def run_check(monkeypatch, path, source, *extra):
    monkeypatch.setattr(check, "make_source", lambda settings, name=None: source)
    return check.main([str(path), "--hours", "1", "--source", "binance", *extra])


def test_check_passes_when_file_is_complete_and_equals_the_source(tmp_path, monkeypatch, capsys):
    path = write_file(tmp_path, frame(6))  # five closed candles: 1.25 hours
    assert run_check(monkeypatch, path, FakeSource(frame(8))) == 0
    out = capsys.readouterr().out
    assert "5 of 5 candles compared with binance, 0 differ" in out
    assert "PASS" in out


def test_check_fails_when_a_saved_candle_differs_from_the_source(tmp_path, monkeypatch, capsys):
    path = write_file(tmp_path, frame(6))
    reference = frame(8)
    reference.loc[2, "close"] += 0.01  # the source's final value moved after the save
    assert run_check(monkeypatch, path, FakeSource(reference)) == 1
    out = capsys.readouterr().out
    assert "1 differ" in out and "close: saved 102.5 source 102.51" in out and "FAIL" in out


def test_check_fails_when_the_source_cannot_confirm(tmp_path, monkeypatch, capsys):
    path = write_file(tmp_path, frame(6))
    assert run_check(monkeypatch, path, FakeSource(error=DataSourceError("cannot reach"))) == 1
    assert "NOT VERIFIED" in capsys.readouterr().out
    # Candles older than what the source returns cannot be confirmed either.
    assert run_check(monkeypatch, path, FakeSource(frame(4, start=T0 + 3 * M15))) == 1
    assert "were not compared" in capsys.readouterr().out


def test_check_can_skip_the_comparison_on_request(tmp_path, monkeypatch, capsys):
    path = write_file(tmp_path, frame(6))
    assert run_check(monkeypatch, path, FakeSource(error=AssertionError("must not be called")), "--no-verify") == 0
    assert "--no-verify" in capsys.readouterr().out


def test_check_fails_on_a_gap_and_on_a_short_span(tmp_path, monkeypatch, capsys):
    gappy = frame(8).drop(index=[3]).reset_index(drop=True)
    path = write_file(tmp_path, gappy)
    assert run_check(monkeypatch, path, FakeSource(frame(9))) == 1
    assert "1 missing between" in capsys.readouterr().out

    short = tmp_path / "short"
    short.mkdir()
    path = write_file(short, frame(3))  # two closed candles: half an hour
    assert run_check(monkeypatch, path, FakeSource(frame(5))) == 1


@pytest.mark.parametrize(
    "text",
    [
        "",  # empty file
        "time,utc,open,high,low,close,volume\n",  # header only
        "time,utc,open,high,low,close,volume\n100,x,1,2,0.5,1.5,1\n",  # not on an M15 boundary
        f"time,utc,open,high,low,close,volume\n{T0},x,1,2,0.5,1.5,1\n{T0},x,1,2,0.5,1.5,1\n",  # duplicate row
        f"time,utc,open,high,low,close,volume\n{T0},x,1,2,0.5,1.5,1\n{T0 + M15},x,1,2",  # cut last row
    ],
)
def test_check_reports_a_broken_file_without_a_traceback(tmp_path, monkeypatch, capsys, text):
    path = tmp_path / "broken.csv"
    path.write_text(text, encoding="utf-8")
    assert run_check(monkeypatch, path, FakeSource(frame(4)), "--no-verify") == 1
    out = capsys.readouterr().out
    assert "cannot check" in out or "no candles" in out


def test_outage_summary_reads_the_collector_log(tmp_path):
    log = tmp_path / "collect.log"
    log.write_text(
        "2026-11-11 10:28:36 UTC  saved 1 candle(s), last x close 1.00\n"
        "2026-11-11 11:00:00 UTC  FETCH FAILED (1 so far): cannot reach\n"
        "2026-11-11 11:30:00 UTC  UNEXPECTED ERROR (2 so far): ValueError: x\n"
        "2026-11-11 12:00:00 UTC  saved 4 candle(s), last x close 1.00\n",
        encoding="utf-8",
    )
    assert check.outage_summary(log) == "2 failed poll(s), largest catch-up 4 candle(s)"
    assert check.outage_summary(tmp_path / "missing.log") is None


def test_check_judges_the_latest_gap_free_run(tmp_path, monkeypatch, capsys):
    data = frame(13).drop(index=[2]).reset_index(drop=True)  # an old gap, then nine clean candles
    path = write_file(tmp_path, data)
    assert run_check(monkeypatch, path, FakeSource(frame(14))) == 0
    out = capsys.readouterr().out
    assert "1 missing between" in out and "last run:  2.25 h" in out and "PASS" in out


def test_check_says_not_yet_while_the_run_is_short(tmp_path, monkeypatch, capsys):
    path = write_file(tmp_path, frame(3))
    assert run_check(monkeypatch, path, FakeSource(frame(5))) == 1
    assert "NOT YET (0.50 of 1 h" in capsys.readouterr().out


# --- collector: refusals and the lock ----------------------------------------


def test_wait_after_refusal_honours_retry_after_and_backs_off():
    hinted = DataSourceError("HTTP 429", status=429, retry_after=120.0)
    assert cc.wait_after_refusal(hinted, 30.0, 1) == 120.0
    short = DataSourceError("HTTP 429", status=429, retry_after=5.0)
    assert cc.wait_after_refusal(short, 30.0, 1) == 30.0  # never faster than the normal poll
    banned = DataSourceError("HTTP 418", status=418, retry_after=99999.0)
    assert cc.wait_after_refusal(banned, 30.0, 1) == 3600.0
    plain = DataSourceError("HTTP 429", status=429)
    waits = [cc.wait_after_refusal(plain, 30.0, streak) for streak in (1, 2, 3, 4, 5, 9)]
    assert waits == [60.0, 120.0, 240.0, 480.0, 900.0, 900.0]


def test_only_one_collector_can_hold_the_lock(tmp_path):
    path = tmp_path / "collect.lock"
    first = cc.hold_lock(path)
    assert first is not None
    assert cc.hold_lock(path) is None
    first.close()
    again = cc.hold_lock(path)
    assert again is not None
    again.close()


# --- compare_prices ----------------------------------------------------------

WED_NOON = 1_794_398_400  # Wednesday 2026-11-11 12:00 UTC: gold is trading
SATURDAY = WED_NOON + 3 * 86400


class Feed:
    name, symbol = "binance", "XAUUSDT"

    def __init__(self, prices, now=WED_NOON, timed=True, age=0.0):
        self.prices, self.now, self.timed, self.age = list(prices), now, timed, age
        self.calls = 0

    def fetch_quote(self):
        price = self.prices[min(self.calls, len(self.prices) - 1)]
        self.calls += 1
        return Quote(self.name, self.symbol, self.now - self.age, price, price - 0.005, price + 0.005, self.timed)

    def fetch_candles(self, timeframe, limit=500):
        return frame(40)  # ATR14 is 2.0 on these candles, so the zone half width is 0.5


class Console:
    """Scripted keyboard: answers in order, everything shown is kept."""

    def __init__(self, *answers):
        self.answers, self.lines = list(answers), []

    def read(self, prompt):
        self.lines.append(prompt)
        return self.answers.pop(0)

    def say(self, text=""):
        self.lines.append(str(text))

    @property
    def text(self):
        return "\n".join(self.lines)


def take(tmp_path, console, feed, ticks=(0.0, 5.0)):
    """One recording session with the feed's fixed wall clock and a scripted stopwatch."""
    ticks = list(ticks)

    def timer():
        return ticks.pop(0) if len(ticks) > 1 else ticks[0]

    path = tmp_path / "compare.csv"
    cp.record(feed, path, 60.0, read=console.read, say=console.say, clock=lambda: feed.now, timer=timer)
    return cp.read_rows(path)


def test_a_reading_is_saved_with_its_difference_time_and_zone(tmp_path):
    rows = take(tmp_path, Console("", "4062.08 4062.21", "q"), Feed([4068.70, 4068.90]))
    assert len(rows) == 1
    row = rows[0]
    assert float(row["source_price"]) == pytest.approx(4068.80)  # the mean of the two feed readings
    assert float(row["difference"]) == pytest.approx(4068.80 - 4062.145)
    assert (float(row["seconds"]), float(row["zone_half_width"]), row["flag"]) == (5.0, 0.5, "")
    assert (float(row["time"]), row["source"], row["utc"]) == (WED_NOON, "binance", "2026-11-11 12:00:00 UTC")


@pytest.mark.parametrize(
    "answers, prices, ticks, note",
    [
        (["", "4062.08", "q"], [4068.7], (0.0, 5.0), "type two numbers"),
        (["", "4062.08 4062.21", "q"], [4068.7], (0.0, 31.0), "that took 31 s"),
        (["", "4062.08 4062.21", "q"], [4068.7, 4069.4], (0.0, 5.0), "the feed moved +0.70"),
        (["4062.08 4062.21", "q"], [4068.7], (0.0, 5.0), "press Enter first"),
    ],
)
def test_a_doubtful_reading_is_dropped_not_saved(tmp_path, answers, prices, ticks, note):
    console = Console(*answers)
    assert take(tmp_path, console, Feed(prices), ticks=ticks) == []
    assert note in console.text


def test_after_a_dropped_reading_the_feed_is_read_afresh(tmp_path):
    feed = Feed([4068.70, 4068.80, 4068.90])
    rows = take(tmp_path, Console("", "oops", "", "4062.08 4062.21", "q"), feed)
    assert len(rows) == 1 and feed.calls == 3  # one quote for the dropped try, two for the saved one
    assert float(rows[0]["source_price"]) == pytest.approx(4068.85)


def test_a_slip_of_the_keyboard_needs_a_yes(tmp_path):
    dropped = Console("", "406208 406221", "n", "q")
    assert take(tmp_path, dropped, Feed([4068.70])) == []
    assert "unusually" in dropped.text
    rows = take(tmp_path, Console("", "4062.08 4067.21", "y", "q"), Feed([4068.70]))
    assert [row["flag"] for row in rows] == [cp.CONFIRMED]


def test_closed_market_readings_need_a_yes_and_stay_out_of_the_report(tmp_path):
    console = Console("", "4062.08 4062.21", "n", "", "4062.08 4062.21", "y", "q")
    rows = take(tmp_path, console, Feed([4068.70], now=SATURDAY))
    assert [row["flag"] for row in rows] == [cp.MARKET_CLOSED]
    assert "usually closed" in console.text
    said = []
    assert cp.report("binance", tmp_path / "compare.csv", say=said.append) == 1
    assert "probably closed" in said[0]


def test_unchanged_mt5_numbers_later_on_are_treated_as_frozen(tmp_path):
    take(tmp_path, Console("", "4062.08 4062.21", "q"), Feed([4068.70]))
    later = Console("", "4062.08 4062.21", "n", "q")
    rows = take(tmp_path, later, Feed([4068.70], now=WED_NOON + 600))
    assert len(rows) == 1 and "same numbers as last time" in later.text


def test_an_old_feed_price_is_refused_but_an_untimed_one_is_not_called_old(tmp_path):
    stale = Console("", "q")
    assert take(tmp_path, stale, Feed([4068.70], age=120.0)) == []
    assert "120 s old" in stale.text
    assert len(take(tmp_path, Console("", "4062.08 4062.21", "q"), Feed([4068.70], timed=False))) == 1


def test_undo_removes_the_last_reading_of_this_source_only(tmp_path):
    path = tmp_path / "compare.csv"
    cp.append_row(path, {"time": 1, "utc": "a", "source": "twelvedata", "difference": 1})
    take(tmp_path, Console("", "4062.08 4062.21", "q"), Feed([4068.70]))
    cp.append_row(path, {"time": 3, "utc": "c", "source": "twelvedata", "difference": 3})
    console = Console("u", "u", "q")
    take(tmp_path, console, Feed([4068.70]))
    assert [row["source"] for row in cp.read_rows(path)] == ["twelvedata", "twelvedata"]
    assert "removed the reading" in console.text and "nothing to remove" in console.text


def reading(i, difference, zone=0.5, source="binance", flag="", step=3600):
    moment = WED_NOON + i * step
    row = {
        "time": moment,
        "utc": cc.utc(moment),
        "broker_bid": 4062.08,
        "broker_ask": 4062.21,
        "broker_mid": 4062.145,
        "broker_spread": 0.13,
        "source": source,
        "source_symbol": "X",
        "source_price": 4062.145 + difference,
        "difference": difference,
        "seconds": 5,
        "zone_half_width": zone,
        "flag": flag,
    }
    return {key: str(value) for key, value in row.items()}


def run_report(tmp_path, rows, source="binance"):
    path = tmp_path / "compare.csv"
    cp.write_rows(path, rows)
    said = []
    code = cp.report(source, path, say=said.append)
    return code, "\n".join(said)


def test_report_needs_twenty_readings_spread_over_six_hours(tmp_path):
    assert "not enough readings yet (19/20)" in run_report(tmp_path, [reading(i, 0.1) for i in range(19)])[1]
    bunched = [reading(i, 0.1, step=60) for i in range(20)]  # twenty readings in twenty minutes
    assert "not spread over enough hours yet (1 different hour(s), 6 needed)" in run_report(tmp_path, bunched)[1]
    assert "feed is close enough to MT5" in run_report(tmp_path, [reading(i, 0.1) for i in range(20)])[1]


def test_report_judges_each_reading_against_its_own_zone(tmp_path):
    # A steady offset of +6.6 with little scatter: outside every zone as
    # measured, inside once the offset is removed.
    rows = [reading(i, 6.6 + (0.2 if i % 2 else -0.2), zone=1.4) for i in range(20)]
    code, text = run_report(tmp_path, rows)
    assert code == 0
    assert "outside their zone:       20 of 20" in text
    assert "outside after offset:     0 of 20" in text
    assert "usable with a fixed offset correction of +6.60" in text
    # One reading taken while the zone was narrow decides it, whenever the report is run.
    rows[7] = reading(7, 7.5, zone=0.3)
    assert "too far from MT5" in run_report(tmp_path, rows)[1]


def test_report_counts_only_its_source_and_leaves_closed_market_rows_out(tmp_path):
    rows = [reading(i, 0.1) for i in range(20)]
    rows += [reading(30 + i, 50.0, source="twelvedata") for i in range(5)]
    rows += [reading(40 + i, 30.0, flag=cp.MARKET_CLOSED) for i in range(3)]
    code, text = run_report(tmp_path, rows)
    assert code == 0
    assert "readings:                 20 (target 20), source binance" in text
    assert "left out:                 3 taken while the market was probably closed" in text
    assert "largest |difference|:     0.100" in text
    assert run_report(tmp_path, rows, source="csv")[0] == 1


@pytest.mark.parametrize(
    "text",
    ["4062.08 4062.21", "  4062,08   4062,21 ", "4062.08,4062.21", "4062.08/4062.21", "4062.08; 4062.21"],
)
def test_parse_broker_accepts_the_usual_ways_of_typing_two_prices(text):
    assert cp.parse_broker(text) == (4062.08, 4062.21)


@pytest.mark.parametrize(
    "text", ["", "4062.08", "4062,08", "1 2 3", "4062.21 4062.08", "abc def", "4062.08 inf", "nan 4062.21", "-1 2", "0 1"]
)
def test_parse_broker_refuses_everything_else(text):
    with pytest.raises(ValueError):
        cp.parse_broker(text)


def test_compare_prices_refuses_a_file_source_even_as_the_default(monkeypatch, capsys):
    monkeypatch.setenv("TRADEPILOT_PRICE_SOURCE", "csv")
    get_settings.cache_clear()
    try:
        with pytest.raises(SystemExit) as stop:
            cp.main(["--report"])
        assert stop.value.code == 2 and "live source" in capsys.readouterr().err
    finally:
        get_settings.cache_clear()
