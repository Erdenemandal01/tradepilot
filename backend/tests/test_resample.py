"""Chart timeframes: boundaries of every candle length and building longer candles from shorter ones."""

from __future__ import annotations

import calendar
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from backend.app.core import candles as cd


def at(year: int, month: int = 1, day: int = 1, hour: int = 0, minute: int = 0) -> int:
    return calendar.timegm((year, month, day, hour, minute, 0))


def frame(times: list[int], *, forming: bool = True, volume: float | None = 1.0) -> pd.DataFrame:
    """Candles whose prices tell their position: candle i opens at 100 + i and closes half a point higher."""
    count = len(times)
    opens = 100.0 + np.arange(count)
    return pd.DataFrame(
        {
            "time": times,
            "open": opens,
            "high": opens + 2.0,
            "low": opens - 1.0,
            "close": opens + 0.5,
            "volume": np.nan if volume is None else volume * (np.arange(count) + 1),
            "closed": [True] * (count - 1) + [not forming] if count else [],
        }
    )


def stepped(start: int, step: int, count: int, **options) -> pd.DataFrame:
    return frame([start + step * index for index in range(count)], **options)


def months(start_year: int, start_month: int, count: int, **options) -> pd.DataFrame:
    index = start_year * 12 + start_month - 1
    return frame([at((index + offset) // 12, (index + offset) % 12 + 1) for offset in range(count)], **options)


def rows(candles: pd.DataFrame) -> list[tuple]:
    return [
        (int(row.time), row.open, row.high, row.low, row.close, row.volume, bool(row.closed))
        for row in candles.itertuples()
    ]


# --- boundaries -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("timeframe", "moment", "opens_at"),
    [
        ("M1", at(2026, 11, 20, 13, 7), at(2026, 11, 20, 13, 7)),
        ("M2", at(2026, 11, 20, 13, 7), at(2026, 11, 20, 13, 6)),
        ("M3", at(2026, 11, 20, 13, 7), at(2026, 11, 20, 13, 6)),
        ("M10", at(2026, 11, 20, 13, 57), at(2026, 11, 20, 13, 50)),
        ("M45", at(2026, 11, 20, 0, 50), at(2026, 11, 20, 0, 45)),  # 32 candles a day, from midnight UTC
        ("M45", at(2026, 11, 20, 23, 59), at(2026, 11, 20, 23, 15)),
        ("H2", at(2026, 11, 20, 13, 7), at(2026, 11, 20, 12)),
        ("H3", at(2026, 11, 20, 13, 7), at(2026, 11, 20, 12)),
        ("H4", at(2026, 11, 20, 13, 7), at(2026, 11, 20, 12)),
        ("D1", at(2026, 11, 20, 13, 7), at(2026, 11, 20)),
        ("W1", at(2026, 11, 22, 23, 59), at(2026, 11, 16)),  # a Sunday belongs to the week that opened on Monday
        ("W1", at(2026, 11, 23), at(2026, 11, 23)),
        ("MN1", at(2024, 2, 29, 23, 59), at(2024, 2)),
        ("MN3", at(2026, 11, 20), at(2026, 10)),  # calendar quarters
        ("MN3", at(2026, 3, 31, 23, 59), at(2026, 1)),
        ("MN6", at(2026, 6, 30, 23, 59), at(2026, 1)),  # calendar half-years
        ("MN6", at(2026, 7), at(2026, 7)),
        ("MN12", at(2026, 11, 20), at(2026, 1)),  # calendar years
    ],
)
def test_every_timeframe_knows_where_its_candle_opens(timeframe, moment, opens_at):
    assert datetime.fromtimestamp(at(2026, 11, 16), timezone.utc).weekday() == 0  # the test's Monday is one
    assert cd.opens(pd.Series([moment]), timeframe).tolist() == [opens_at]


@pytest.mark.parametrize(
    ("timeframe", "opened", "next_opens"),
    [
        ("M45", at(2026, 11, 20, 23, 15), at(2026, 11, 21)),
        ("W1", at(2026, 11, 16), at(2026, 11, 23)),
        ("MN1", at(2024, 2), at(2024, 3)),  # 29 days
        ("MN1", at(2026, 12), at(2027, 1)),
        ("MN3", at(2026, 10), at(2027, 1)),
        ("MN6", at(2026, 7), at(2027, 1)),
        ("MN12", at(2024, 1), at(2025, 1)),
    ],
)
def test_the_following_candle_opens_where_the_calendar_says(timeframe, opened, next_opens):
    assert cd.following(pd.Series([opened]), timeframe).tolist() == [next_opens]


def test_the_chart_timeframes_are_the_menu_in_order():
    assert cd.CHART_TIMEFRAMES == (
        "M1", "M2", "M3", "M5", "M10", "M15", "M30", "M45",
        "H1", "H2", "H3", "H4",
        "D1", "W1",
        "MN1", "MN3", "MN6", "MN12",
    )  # fmt: skip
    with pytest.raises(ValueError, match="no fixed length"):
        cd.timeframe_seconds("MN1")
    with pytest.raises(ValueError, match="unknown timeframe"):
        cd.opens(pd.Series([at(2026)]), "M7")


def test_validate_accepts_weeks_from_monday_and_months_from_the_first():
    weeks = cd.validate(stepped(at(2026, 11, 2), 7 * 86400, 3), "W1")
    assert weeks["time"].tolist() == [at(2026, 11, 2), at(2026, 11, 9), at(2026, 11, 16)]
    assert cd.validate(months(2025, 11, 4), "MN1")["time"].tolist() == [
        at(2025, 11), at(2025, 12), at(2026, 1), at(2026, 2),
    ]  # fmt: skip
    with pytest.raises(ValueError, match="aligned"):
        cd.validate(stepped(at(2026, 11, 1), 7 * 86400, 2), "W1")  # a Sunday
    with pytest.raises(ValueError, match="aligned"):
        cd.validate(frame([at(2026, 1, 2)]), "MN1")
    with pytest.raises(ValueError, match="aligned"):
        cd.validate(frame([at(2026, 2)]), "MN3")  # February opens no quarter


def test_breaks_are_counted_by_the_calendar():
    assert cd.count_breaks(stepped(at(2026, 11, 20), 2700, 40), "M45") == 0
    assert cd.count_breaks(frame([at(2026, 1), at(2026, 2), at(2026, 4), at(2026, 5), at(2026, 8)]), "MN1") == 2
    assert cd.count_breaks(months(2023, 11, 6), "MN1") == 0  # 30, 31, 31, 29 days long: no break
    assert cd.count_breaks(frame([at(2026, 11, 20, 0, 0), at(2026, 11, 20, 0, 4)]), "M2") == 1
    assert cd.count_breaks(frame([at(2026, 11, 20)]), "D1") == 0 and cd.count_breaks(frame([]), "D1") == 0


# --- building longer candles ------------------------------------------------------------------------


def test_two_one_minute_candles_make_one_two_minute_candle():
    built, partial = cd.resample(stepped(at(2026, 11, 20, 13, 0), 60, 5), "M1", "M2")
    assert partial is False
    assert rows(built) == [
        # first open, highest high, lowest low, last close, summed volume
        (at(2026, 11, 20, 13, 0), 100.0, 103.0, 99.0, 101.5, 1.0 + 2.0, True),
        (at(2026, 11, 20, 13, 2), 102.0, 105.0, 101.0, 103.5, 3.0 + 4.0, True),
        (at(2026, 11, 20, 13, 4), 104.0, 106.0, 103.0, 104.5, 5.0, False),  # one of two so far: still forming
    ]


def test_the_newest_candle_is_closed_only_when_its_last_part_is(source="M5", target="M10"):
    start = at(2026, 11, 20, 13, 0)
    assert rows(cd.resample(stepped(start, 300, 2), source, target)[0])[-1][-1] is False  # the second half still forms
    assert rows(cd.resample(stepped(start, 300, 2, forming=False), source, target)[0])[-1][-1] is True
    assert rows(cd.resample(stepped(start, 300, 3), source, target)[0])[0][-1] is True


@pytest.mark.parametrize(
    ("source", "target", "parts"),
    [("M1", "M2", 2), ("M5", "M10", 2), ("M15", "M45", 3), ("H1", "H3", 3), ("M15", "H1", 4), ("D1", "W1", 7)],
)
def test_each_longer_candle_takes_the_right_number_of_parts(source, target, parts):
    step = cd.timeframe_seconds(source)
    start = at(2026, 11, 16)  # a Monday midnight: every timeframe opens a candle here
    built, partial = cd.resample(stepped(start, step, parts * 3 + 1), source, target)
    assert partial is False and len(built) == 4
    assert built["time"].tolist() == [start + parts * step * index for index in range(4)]
    assert built["closed"].tolist() == [True, True, True, False]
    assert built["open"].tolist() == [100.0 + parts * index for index in range(4)]
    assert built["close"].tolist()[:3] == [100.5 + parts * index + parts - 1 for index in range(3)]
    assert built["volume"].iloc[0] == sum(range(1, parts + 1))


def test_a_bucket_with_a_candle_missing_is_left_out_not_guessed():
    start = at(2026, 11, 20, 0, 0)
    times = [start + 900 * index for index in range(12)]  # four 45-minute buckets
    del times[4]  # the middle part of the second bucket never came
    built, partial = cd.resample(frame(times), "M15", "M45")
    assert partial is False
    assert built["time"].tolist() == [start, start + 2 * 2700, start + 3 * 2700]
    assert cd.count_breaks(built, "M45") == 1  # the hole stays visible as a break


def test_a_history_that_starts_inside_a_bucket_drops_it_unless_nothing_older_exists():
    start = at(2026, 11, 20, 0, 15)  # the first 45-minute bucket lacks its first quarter of an hour
    source = stepped(start, 900, 6)
    window, partial = cd.resample(source, "M15", "M45")
    assert partial is False and window["time"].tolist() == [at(2026, 11, 20, 0, 45), at(2026, 11, 20, 1, 30)]

    whole, partial = cd.resample(source, "M15", "M45", history_starts=True)
    assert partial is True
    assert whole["time"].tolist() == [at(2026, 11, 20, 0, 0), at(2026, 11, 20, 0, 45), at(2026, 11, 20, 1, 30)]
    assert rows(whole)[0][1:] == (100.0, 103.0, 99.0, 101.5, 3.0, True)  # built from the two parts that exist


def test_a_hole_inside_the_oldest_or_the_newest_bucket_is_never_built():
    start = at(2026, 11, 20, 0, 0)
    oldest = frame([start, start + 1800, start + 2700, start + 3600, start + 4500])  # 00:15 is missing
    built, partial = cd.resample(oldest, "M15", "M45", history_starts=True)
    assert partial is False and built["time"].tolist() == [start + 2700]

    newest = frame([start, start + 900, start + 1800, start + 2700, start + 4500])  # 01:00 is missing
    built, _ = cd.resample(newest, "M15", "M45")
    assert built["time"].tolist() == [start]


def test_calendar_months_make_quarters_half_years_and_years():
    source = months(2024, 1, 35)  # January 2024 to November 2026, the last one forming
    quarters, partial = cd.resample(source, "MN1", "MN3")
    assert partial is False and len(quarters) == 12
    assert quarters["time"].tolist()[:2] == [at(2024, 1), at(2024, 4)] and quarters["time"].iloc[-1] == at(2026, 10)
    assert quarters["closed"].tolist() == [True] * 11 + [False]
    assert rows(quarters)[0][1:] == (100.0, 104.0, 99.0, 102.5, 1.0 + 2.0 + 3.0, True)

    halves, _ = cd.resample(source, "MN1", "MN6")
    assert halves["time"].tolist() == [at(2024, 1), at(2024, 7), at(2025, 1), at(2025, 7), at(2026, 1), at(2026, 7)]
    assert halves["closed"].tolist() == [True] * 5 + [False]

    years, _ = cd.resample(source, "MN1", "MN12")
    assert years["time"].tolist() == [at(2024, 1), at(2025, 1), at(2026, 1)]
    assert years["closed"].tolist() == [True, True, False]
    assert rows(years)[1][1:] == (112.0, 125.0, 111.0, 123.5, float(sum(range(13, 25))), True)
    # December closed: the year is complete even though nothing newer is listed.
    done, _ = cd.resample(months(2025, 1, 12, forming=False), "MN1", "MN12")
    assert done["closed"].tolist() == [True]


def test_an_instrument_younger_than_a_year_gives_one_marked_partial_candle():
    source = months(2026, 3, 9)  # listed in March 2026; November is forming
    assert cd.resample(source, "MN1", "MN12")[0]["time"].tolist() == []  # a window: no honest candle to show

    years, partial = cd.resample(source, "MN1", "MN12", history_starts=True)
    assert partial is True and years["time"].tolist() == [at(2026, 1)] and years["closed"].tolist() == [False]
    quarters, partial = cd.resample(source, "MN1", "MN3", history_starts=True)
    assert partial is True  # the first quarter holds March alone
    assert quarters["time"].tolist() == [at(2026, 1), at(2026, 4), at(2026, 7), at(2026, 10)]
    assert quarters["closed"].tolist() == [True, True, True, False]


def test_days_can_be_combined_into_calendar_months():
    source = stepped(at(2024, 1, 1), 86400, 31 + 29 + 5)  # January, a leap February, five days of March
    built, partial = cd.resample(source, "D1", "MN1")
    assert partial is False and built["time"].tolist() == [at(2024, 1), at(2024, 2), at(2024, 3)]
    assert built["closed"].tolist() == [True, True, False]
    assert built["volume"].iloc[1] == sum(range(32, 32 + 29))


def test_volume_stays_unknown_when_the_source_reports_none():
    built, _ = cd.resample(stepped(at(2026, 11, 20), 60, 4, volume=None), "M1", "M2")
    assert built["volume"].isna().all()


@pytest.mark.parametrize(
    ("source", "target"),
    [("M15", "M10"), ("M15", "M15"), ("M45", "H1"), ("W1", "MN1"), ("MN3", "MN1"), ("MN1", "D1"), ("MN3", "MN6x")],
)
def test_candles_that_do_not_fit_are_refused(source, target):
    with pytest.raises(ValueError):
        cd.resample(frame([]), source, target)


def test_an_empty_frame_gives_an_empty_frame():
    built, partial = cd.resample(frame([]), "M1", "M2")
    assert partial is False and built.empty and list(built.columns) == list(cd.CANDLE_COLUMNS)
