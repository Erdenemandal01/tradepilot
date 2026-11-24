"""core/candles: laying stream candles over stored ones, REST tails, month tails, built candles.

Written before the code (CLAUDE.md: tests first for core/).
"""

from __future__ import annotations

import calendar

import numpy as np
import pandas as pd
import pytest

from backend.app.core import candles as cd


def at(year: int, month: int = 1, day: int = 1, hour: int = 0, minute: int = 0) -> int:
    return calendar.timegm((year, month, day, hour, minute, 0))


T0 = at(2026, 11, 23, 10, 0)


def frame(times: list[int], timeframe: str, *, forming: bool = True) -> pd.DataFrame:
    """Candle i opens at 100 + i, closes half a point higher, volume 1 + i."""
    count = len(times)
    opens = 100.0 + np.arange(count)
    data = pd.DataFrame(
        {
            "time": times,
            "open": opens,
            "high": opens + 2.0,
            "low": opens - 1.0,
            "close": opens + 0.5,
            "volume": 1.0 + np.arange(count),
            "closed": [True] * (count - 1) + [not forming] if count else [],
        }
    )
    return cd.validate(data, timeframe)


def minutes(count: int, start: int = T0, **options) -> pd.DataFrame:
    return frame([start + 60 * index for index in range(count)], "M1", **options)


def months(start_year: int, start_month: int, count: int, **options) -> pd.DataFrame:
    index = start_year * 12 + start_month - 1
    return frame([at((index + offset) // 12, (index + offset) % 12 + 1) for offset in range(count)], "MN1", **options)


def bar(time: int, close: float = 105.0, *, closed: bool = False, volume: float = 7.0, **values) -> dict:
    out = {"time": time, "open": 104.0, "high": max(close, 104.0) + 1.0, "low": min(close, 104.0) - 1.0,
           "close": close, "volume": volume, "closed": closed}  # fmt: skip
    out.update(values)
    return out


def rows(candles: pd.DataFrame) -> list[tuple]:
    return [
        (int(r.time), float(r.open), float(r.high), float(r.low), float(r.close), float(r.volume), bool(r.closed))
        for r in candles.itertuples()
    ]


# --- lay_stream: one stream candle over the stored ones ------------------------------------------------------


def test_the_same_open_time_replaces_the_forming_row_and_leaves_the_input_alone():
    stored = minutes(5)
    before = rows(stored)
    laid, outcome = cd.lay_stream(stored, bar(T0 + 240, 110.0), "M1")
    assert outcome == cd.STREAM_SAME
    assert rows(laid)[:4] == before[:4] and len(laid) == 5
    assert rows(laid)[-1] == (T0 + 240, 104.0, 111.0, 103.0, 110.0, 7.0, False)
    assert rows(stored) == before  # the stored frame is shared: it is never changed


def test_the_exchanges_closed_flag_is_kept_on_the_row():
    laid, outcome = cd.lay_stream(minutes(5), bar(T0 + 240, 110.0, closed=True), "M1")
    assert outcome == cd.STREAM_SAME and bool(laid["closed"].iloc[-1]) is True


def test_the_next_open_time_appends_and_the_row_before_becomes_closed():
    stored = minutes(5)
    laid, outcome = cd.lay_stream(stored, bar(T0 + 300, 106.0), "M1")
    assert outcome == cd.STREAM_NEXT
    assert [int(t) for t in laid["time"]] == [T0 + 60 * i for i in range(6)]
    assert laid["closed"].tolist() == [True] * 5 + [False]
    assert rows(laid)[4][:6] == rows(stored)[4][:6]  # the earlier row keeps its values; only its flag changes
    cd.validate(laid, "M1")


def test_an_older_open_time_is_ignored():
    stored = minutes(5)
    for time in (T0 + 180, T0, T0 - 60):
        laid, outcome = cd.lay_stream(stored, bar(time, 120.0, closed=True), "M1")
        assert outcome == cd.STREAM_OLD and laid is stored


def test_a_jump_over_a_hole_is_not_built_over_it_asks_for_a_refill():
    stored = minutes(5)
    laid, outcome = cd.lay_stream(stored, bar(T0 + 360, 106.0), "M1")  # T0 + 300 never came
    assert outcome == cd.STREAM_HOLE and laid is stored
    laid, outcome = cd.lay_stream(stored.iloc[:0], bar(T0, 106.0), "M1")  # nothing stored: nothing to lay over
    assert outcome == cd.STREAM_HOLE and laid.empty


def test_a_row_the_exchange_closed_is_not_reopened_by_a_late_forming_update():
    closed_last = minutes(5, forming=False)
    laid, outcome = cd.lay_stream(closed_last, bar(T0 + 240, 99.0, closed=False), "M1")
    assert outcome == cd.STREAM_OLD and laid is closed_last
    laid, outcome = cd.lay_stream(closed_last, bar(T0 + 240, 99.5, closed=True), "M1")
    assert outcome == cd.STREAM_SAME and float(laid["close"].iloc[-1]) == 99.5


def test_keep_closed_never_touches_a_closed_row_but_still_lays_the_forming_one():
    closed_last = minutes(5, forming=False)
    laid, outcome = cd.lay_stream(closed_last, bar(T0 + 240, 99.5, closed=True), "M1", keep_closed=True)
    assert outcome == cd.STREAM_OLD and laid is closed_last
    laid, outcome = cd.lay_stream(minutes(5), bar(T0 + 240, 99.5, closed=True), "M1", keep_closed=True)
    assert outcome == cd.STREAM_SAME and float(laid["close"].iloc[-1]) == 99.5


@pytest.mark.parametrize("volume", [-5.0, float("inf"), float("-inf")])
def test_a_volume_that_cannot_be_true_is_refused(volume):
    data = minutes(3)
    data.loc[1, "volume"] = volume
    with pytest.raises(ValueError, match="volume"):
        cd.validate(data, "M1")
    with pytest.raises(ValueError, match="volume"):
        cd.lay_stream(minutes(3), bar(T0 + 120, volume=volume), "M1")


def test_no_volume_and_a_volume_of_zero_are_both_allowed():
    data = minutes(3)
    data.loc[0, "volume"] = float("nan")
    data.loc[1, "volume"] = 0.0
    checked = cd.validate(data, "M1")
    assert np.isnan(checked["volume"].iloc[0]) and checked["volume"].iloc[1] == 0.0


def test_a_bar_without_volume_and_a_broken_bar():
    laid, outcome = cd.lay_stream(minutes(3), bar(T0 + 120, volume=float("nan")), "M1")
    assert outcome == cd.STREAM_SAME and np.isnan(laid["volume"].iloc[-1])
    with pytest.raises(ValueError):
        cd.lay_stream(minutes(3), bar(T0 + 120, high=50.0), "M1")  # high below the close
    with pytest.raises(ValueError):
        cd.lay_stream(minutes(3), bar(T0 + 150), "M1")  # not on a minute boundary


def test_months_follow_the_calendar():
    stored = months(2026, 9, 3)  # Sep, Oct, Nov (forming)
    laid, outcome = cd.lay_stream(stored, bar(at(2026, 12)), "MN1")
    assert outcome == cd.STREAM_NEXT and int(laid["time"].iloc[-1]) == at(2026, 12)
    assert cd.lay_stream(stored, bar(at(2027, 1)), "MN1")[1] == cd.STREAM_HOLE


def test_a_sequence_of_updates_gives_the_frame_rest_would_give():
    stored = minutes(3)
    updates = [bar(T0 + 120, 104.5), bar(T0 + 120, 106.0, closed=True), bar(T0 + 180, 104.2), bar(T0 + 120, 2.0),
               bar(T0 + 180, 104.9)]  # fmt: skip
    for item in updates:
        stored, _ = cd.lay_stream(stored, item, "M1")
    assert [int(t) for t in stored["time"]] == [T0, T0 + 60, T0 + 120, T0 + 180]
    assert rows(stored)[2] == (T0 + 120, 104.0, 107.0, 103.0, 106.0, 7.0, True)
    assert rows(stored)[3] == (T0 + 180, 104.0, 105.9, 103.0, 104.9, 7.0, False)


# --- older_changed: did anything change apart from the newest three? -----------------------------------------


def test_older_changed_ignores_the_newest_three_and_the_window_moving():
    old = minutes(10)
    assert cd.older_changed(None, old) is True
    assert cd.older_changed(old, old) is False
    moved, _ = cd.lay_stream(old, bar(T0 + 600), "M1")
    assert cd.older_changed(old, moved) is False  # one candle more: the page gets it in the event
    assert cd.older_changed(old, moved.iloc[1:].reset_index(drop=True)) is False  # the oldest fell off
    newest = old.copy()
    newest.loc[newest.index[-2], "close"] = 100.9
    assert cd.older_changed(old, newest) is False


def test_older_changed_sees_a_change_or_a_new_candle_further_back():
    old = minutes(10)
    changed = old.copy()
    changed.loc[changed.index[-5], "volume"] = 99.0
    assert cd.older_changed(old, changed) is True
    holed = old.drop(index=[4]).reset_index(drop=True)
    assert cd.older_changed(holed, old) is True  # the hole healed
    assert cd.older_changed(old, holed) is True
    later = minutes(13)  # three candles came at once: the old forming one is now final further back
    later.loc[later.index[9], "close"] = 109.9
    assert cd.older_changed(old, later) is True


def test_older_changed_counts_a_missing_volume_as_equal_to_itself():
    old = minutes(6)
    old.loc[:, "volume"] = np.nan
    assert cd.older_changed(old, old.copy()) is False


# --- lay_rest: a REST tail over stored candles that the stream may have moved ahead -----------------------


def test_a_rest_tail_that_is_level_or_ahead_is_laid_as_merge_does():
    stored = minutes(10)
    tail = minutes(12).iloc[-3:].reset_index(drop=True)
    assert rows(cd.lay_rest(stored, tail, "M1")) == rows(cd.merge(stored, tail, "M1"))
    assert cd.lay_rest(stored, minutes(16).iloc[-3:].reset_index(drop=True), "M1") is None  # does not connect


def test_a_rest_tail_behind_the_stream_corrects_closed_rows_and_keeps_the_newer_ones():
    stored = minutes(10)  # the stream has the candle of T0 + 540 forming
    tail = minutes(9).iloc[-3:].reset_index(drop=True)  # REST still lists T0 + 480 as forming
    tail.loc[0, ["high", "close"]] = [120.0, 119.0]  # its closed T0 + 360 has other (final) values
    laid = cd.lay_rest(stored, tail, "M1")
    assert len(laid) == 10 and int(laid["time"].iloc[-1]) == T0 + 540 and bool(laid["closed"].iloc[-1]) is False
    assert float(laid["close"].iloc[6]) == 119.0  # the closed REST row is taken
    assert rows(laid)[8] == rows(stored)[8]  # REST's forming row is not: the stream's is newer
    cd.validate(laid, "M1")


def test_a_rest_tail_with_a_new_hole_is_refused():
    stored = minutes(10)
    tail = minutes(12).drop(index=[10]).iloc[-2:].reset_index(drop=True)
    assert cd.lay_rest(stored, tail, "M1") is None


def test_a_rest_tail_level_with_the_stream_never_reopens_a_candle_the_exchange_closed():
    # The stream closed T0 + 540 with the exchange's own flag; a REST copy that lags still lists it as
    # the newest, forming, with its values from before the close.
    stored = minutes(10, forming=False)
    tail = minutes(10).iloc[-3:].reset_index(drop=True)
    tail.loc[2, ["high", "close", "volume"]] = [109.2, 109.1, 5.0]  # pre-close values
    tail.loc[0, ["high", "close"]] = [120.0, 119.0]  # its closed T0 + 420 has other (final) values
    laid = cd.lay_rest(stored, tail, "M1")
    assert rows(laid)[-1] == rows(stored)[-1]  # the exchange's final values stand, still closed
    assert float(laid["close"].iloc[7]) == 119.0  # a closed REST row is still taken
    assert len(laid) == 10 and laid["closed"].all()
    cd.validate(laid, "M1")
    # A forming stored candle is still replaced by REST's newest as before (merge).
    forming = cd.lay_rest(minutes(10), tail, "M1")
    assert rows(forming)[-1] == rows(tail)[-1]


# --- months: tails and holes ---------------------------------------------------------------------------------


def test_month_frames_have_holes_and_tails_like_any_other():
    whole = months(2010, 1, 200)
    holed = whole.drop(index=[150]).reset_index(drop=True)
    assert cd.find_gaps(holed, "MN1") == [cd.Gap(after=int(whole["time"][149]), before=int(whole["time"][151]), missing=1)]
    assert cd.hole_reach(holed, "MN1", 100) == 51  # from the month before the hole to the newest
    tail = months(2026, 9, 3)
    tail.loc[2, "close"] = 101.0
    merged = cd.merge(months(2010, 1, 203), tail, "MN1")
    assert merged is not None and len(merged) == 203 and float(merged["close"].iloc[-1]) == 101.0
    newer = months(2026, 10, 3)  # December opened
    merged = cd.merge(months(2010, 1, 203), newer, "MN1", keep=240)
    assert int(merged["time"].iloc[-1]) == at(2026, 12) and merged["closed"].tolist()[-2:] == [True, False]
    assert cd.merge(months(2010, 1, 203), months(2027, 2, 3), "MN1") is None  # a gap of months: reload
    assert cd.count_breaks(holed, "MN1") == 1


# --- built candles: closed only when complete ----------------------------------------------------------------


def m1(times: list[int], closes: list[float], *, forming: bool) -> pd.DataFrame:
    closes = [100.0 + c for c in closes]
    data = pd.DataFrame({"time": times, "open": [c - 0.5 for c in closes], "close": closes})
    data["high"] = data[["open", "close"]].max(axis=1) + 1.0
    data["low"] = data[["open", "close"]].min(axis=1) - 1.0
    data["volume"] = [1.0 + i for i in range(len(times))]
    data["closed"] = [True] * (len(times) - 1) + [not forming]
    return cd.validate(data, "M1")


def test_a_built_candle_is_closed_only_when_its_bucket_is_complete_and_its_last_part_closed():
    base = at(2026, 11, 23, 10, 0)
    # M2 buckets 10:00, 10:02. The 10:02 bucket has its first minute only, forming.
    built, _ = cd.resample(m1([base, base + 60, base + 120], [1, 2, 3], forming=True), "M1", "M2")
    assert built["closed"].tolist() == [True, False]
    # Both minutes of the newest bucket are there, the second still forming: not closed.
    built, _ = cd.resample(m1([base, base + 60, base + 120, base + 180], [1, 2, 3, 4], forming=True), "M1", "M2")
    assert built["closed"].tolist() == [True, False]
    # The exchange closed the second minute: the bucket is complete and closed.
    built, _ = cd.resample(m1([base, base + 60, base + 120, base + 180], [1, 2, 3, 4], forming=False), "M1", "M2")
    assert built["closed"].tolist() == [True, True]


def test_a_bucket_with_a_missing_part_is_never_shown_as_a_full_closed_candle():
    base = at(2026, 11, 23, 10, 0)
    # 10:01 is missing: the 10:00 bucket is left out entirely, never drawn as a closed M2 candle.
    built, _ = cd.resample(m1([base, base + 120, base + 180], [1, 3, 4], forming=False), "M1", "M2")
    assert [int(t) for t in built["time"]] == [base + 120] and built["closed"].tolist() == [True]
    # The newest bucket lacks its first minute: not shown (a forming candle without its open is no candle).
    built, _ = cd.resample(m1([base, base + 60, base + 180], [1, 2, 4], forming=True), "M1", "M2")
    assert [int(t) for t in built["time"]] == [base]
    # M10 from M5 with the second five minutes missing in the newest bucket and the exchange's flag closed.
    five = cd.validate(
        pd.DataFrame({"time": [base, base + 300, base + 600], "open": [1.0, 2, 3], "high": [2.0, 3, 4],
                      "low": [0.5, 1.5, 2.5], "close": [1.5, 2.5, 3.5], "volume": [1.0, 1, 1],
                      "closed": [True, True, True]}),  # fmt: skip
        "M5",
    )
    built, _ = cd.resample(five, "M5", "M10")
    assert [(int(t), bool(c)) for t, c in zip(built["time"], built["closed"])] == [(base, True), (base + 600, False)]


@pytest.mark.parametrize(("source", "target", "step", "parts"), [("M1", "M2", 60, 2), ("M5", "M10", 300, 2)])
def test_built_candles_take_first_open_highest_high_lowest_low_last_close_summed_volume_also_while_forming(
    source, target, step, parts
):
    base = at(2026, 11, 23, 10, 0)
    times = [base + step * i for i in range(parts * 2 - 1)]  # the last bucket has its first part, forming
    data = cd.validate(
        pd.DataFrame({"time": times, "open": [10.0, 11.0, 12.0], "high": [15.0, 13.0, 12.5],
                      "low": [9.0, 8.0, 11.5], "close": [11.0, 12.0, 12.2], "volume": [1.0, 2.0, 4.0],
                      "closed": [True, True, False]}),  # fmt: skip
        source,
    )
    built, _ = cd.resample(data, source, target)
    assert rows(built) == [(base, 10.0, 15.0, 8.0, 12.0, 3.0, True), (base + step * parts, 12.0, 12.5, 11.5, 12.2, 4.0, False)]
