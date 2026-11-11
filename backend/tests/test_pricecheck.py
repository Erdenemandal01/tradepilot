"""Feed-versus-broker price difference report."""

from __future__ import annotations

import math

import pytest

from backend.app.core import pricecheck as pc


def test_mid():
    assert pc.mid(4062.08, 4062.21) == pytest.approx(4062.145)
    with pytest.raises(ValueError):
        pc.mid(4062.21, 4062.08)


def test_summarize_basic_statistics():
    report = pc.summarize([1.0, -1.0, 3.0, 1.0], zone_half_width=2.0)
    assert report.count == 4
    assert report.mean == pytest.approx(1.0)
    assert report.mean_abs == pytest.approx(1.5)
    assert report.max_abs == pytest.approx(3.0)
    assert report.std == pytest.approx(math.sqrt(8 / 3))
    # After removing the average offset of +1: deviations 0, -2, 2, 0.
    assert report.max_abs_after_offset == pytest.approx(2.0)
    assert report.raw_within_zone is False
    assert report.offset_within_zone is True


def test_summarize_without_zone():
    report = pc.summarize([0.5])
    assert report.std == 0.0
    assert report.zone_half_width is None
    assert report.raw_within_zone is None
    assert report.offset_within_zone is None


def test_summarize_needs_data():
    with pytest.raises(ValueError):
        pc.summarize([])
    with pytest.raises(ValueError):
        pc.summarize([1.0, math.nan])
