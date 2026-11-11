"""The collector's save step and the candle-file check. No test touches the network."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.app.core import candles as cd
from backend.app.datasource import DataSourceError
from backend.scripts import check_candles as check
from backend.scripts import collect_candles as cc

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
