"""Feed-versus-broker price difference report."""

from __future__ import annotations

import math

import pytest

from backend.app.core import pricecheck as pc

# 2026-11-11 was a Wednesday; 12:00 UTC that day.
WED_NOON = 1_794_398_400
HOUR, DAY = 3600, 86400


def test_mid():
    assert pc.mid(4062.08, 4062.21) == pytest.approx(4062.145)
    with pytest.raises(ValueError):
        pc.mid(4062.21, 4062.08)


def test_summarize_basic_statistics():
    report = pc.summarize([1.0, -1.0, 3.0, 1.0])
    assert report.count == 4
    assert report.mean == pytest.approx(1.0)
    assert report.mean_abs == pytest.approx(1.5)
    assert report.max_abs == pytest.approx(3.0)
    assert report.std == pytest.approx(math.sqrt(8 / 3))
    # After removing the average offset of +1: deviations 0, -2, 2, 0.
    assert report.max_abs_after_offset == pytest.approx(2.0)


def test_summarize_judges_each_reading_against_its_own_zone():
    # Same differences, but the zone was narrow for the third reading only.
    report = pc.summarize([1.0, -1.0, 3.0, 1.0], zones=[2.0, 2.0, 2.5, 2.0])
    assert (report.judged, report.raw_outside, report.offset_outside) == (4, 1, 0)
    assert report.raw_within_zone is False and report.offset_within_zone is True
    assert report.worst_raw_ratio == pytest.approx(3.0 / 2.5)
    assert report.worst_offset_ratio == pytest.approx(1.0)

    tight = pc.summarize([1.0, -1.0, 3.0, 1.0], zones=[2.0, 0.5, 2.5, 2.0])
    assert (tight.raw_outside, tight.offset_outside) == (2, 1)  # -1 against 0.5: outside either way
    assert tight.offset_within_zone is False


def test_summarize_does_not_judge_readings_without_a_zone():
    for zones in (None, [None, math.nan, 0.0]):
        report = pc.summarize([0.5, 9.0, -9.0], zones=zones)
        assert report.judged == 0
        assert report.raw_within_zone is None and report.offset_within_zone is None
        assert report.worst_raw_ratio is None
    partly = pc.summarize([0.5, 9.0], zones=[1.0, None])
    assert (partly.judged, partly.raw_outside) == (1, 0)  # the 9.0 has no zone to be held against


def test_summarize_single_reading_and_bad_input():
    assert pc.summarize([0.5]).std == 0.0
    with pytest.raises(ValueError):
        pc.summarize([])
    with pytest.raises(ValueError):
        pc.summarize([1.0, math.nan])
    with pytest.raises(ValueError):
        pc.summarize([1.0, 2.0], zones=[1.0])


@pytest.mark.parametrize(
    "bid, ask, expected",
    [
        (4062.08, 4062.21, None),  # about 6.6 below the feed: a real offset, not a slip
        (406208, 406221, "spread"),  # decimal points forgotten: the spread check fires first
        (4062.08, 4602.21, "spread"),  # two digits swapped
        (4062.08, 4068.21, "spread"),  # 6.13 wide
        (4062.21, 4062.08, "bid is above ask"),
        (4062.08, math.inf, "finite"),
        (0.0, 4062.21, "finite"),
        (3962.08, 3962.21, "difference"),  # 100 off
    ],
)
def test_implausible_catches_slips_but_not_a_real_offset(bid, ask, expected):
    reason = pc.implausible(bid, ask, 4068.80)
    assert (reason is None) if expected is None else (expected in reason)


@pytest.mark.parametrize(
    "moment, closed",
    [
        (WED_NOON, False),
        (WED_NOON + 10 * HOUR, True),  # Wednesday 22:00, the daily break
        (WED_NOON + 11 * HOUR, False),  # Wednesday 23:00
        (WED_NOON + 2 * DAY + 9 * HOUR, False),  # Friday 21:00
        (WED_NOON + 2 * DAY + 10 * HOUR, True),  # Friday 22:00
        (WED_NOON + 3 * DAY, True),  # Saturday noon
        (WED_NOON + 4 * DAY + 10 * HOUR, True),  # Sunday 22:00
        (WED_NOON + 4 * DAY + 11 * HOUR, False),  # Sunday 23:00
        (WED_NOON + 5 * DAY, False),  # Monday noon
    ],
)
def test_probably_closed_follows_the_usual_gold_week(moment, closed):
    assert pc.probably_closed(moment) is closed


def test_distinct_hours():
    assert pc.distinct_hours([]) == 0
    assert pc.distinct_hours([WED_NOON, WED_NOON + 59, WED_NOON + 3599]) == 1
    assert pc.distinct_hours([WED_NOON, WED_NOON + HOUR, WED_NOON + DAY]) == 3
