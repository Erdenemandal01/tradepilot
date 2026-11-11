"""How far the price feed is from the broker's price (plan section 6).

A difference is ``feed price - broker price`` for one moment. Each reading is
judged against the entry zone's half width (0.25 x ATR by default) of its own
moment, because that width changes several-fold over a day. A steady offset
can be corrected; scatter cannot, so the report counts the readings that fall
outside their zone both as measured and after removing the average offset.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

import numpy as np

MAX_DIFFERENCE_SHARE = 0.005  # a typed price further than 0.5% from the feed is probably a slip
MAX_SPREAD_SHARE = 0.001  # so is a spread wider than 0.1% of the price


@dataclass(frozen=True)
class PriceDiffReport:
    count: int
    mean: float  # average difference: a steady offset of the feed
    mean_abs: float
    max_abs: float
    std: float  # sample standard deviation; 0 with one reading
    max_abs_after_offset: float  # largest distance from the average difference
    judged: int  # readings that have a zone of their own moment
    raw_outside: int  # of those, readings with |difference| above their zone
    offset_outside: int  # of those, readings still outside after removing the offset
    worst_raw_ratio: float | None  # largest |difference| / zone
    worst_offset_ratio: float | None  # largest |difference - mean| / zone

    @property
    def raw_within_zone(self) -> bool | None:
        return None if self.judged == 0 else self.raw_outside == 0

    @property
    def offset_within_zone(self) -> bool | None:
        return None if self.judged == 0 else self.offset_outside == 0


def mid(bid: float, ask: float) -> float:
    if bid > ask:
        raise ValueError(f"bid {bid} is above ask {ask}")
    return (bid + ask) / 2


def summarize(differences: Sequence[float], zones: Sequence[float | None] | None = None) -> PriceDiffReport:
    """Summarise the differences; ``zones`` holds each reading's zone half width.

    A zone that is None, NaN or not positive means "unknown": that reading
    counts in the statistics but is not judged against a zone.
    """
    values = np.asarray(differences, dtype=float)
    if values.size == 0:
        raise ValueError("no price differences to summarize")
    if not np.isfinite(values).all():
        raise ValueError("price differences must be finite")
    if zones is None:
        widths = np.full(values.shape, np.nan)
    else:
        widths = np.asarray([np.nan if z is None else z for z in zones], dtype=float)
        if widths.shape != values.shape:
            raise ValueError("zones must have one entry per difference")
    known = np.isfinite(widths) & (widths > 0)

    mean = float(values.mean())
    raw = np.abs(values)
    shifted = np.abs(values - mean)
    judged = int(known.sum())
    return PriceDiffReport(
        count=int(values.size),
        mean=mean,
        mean_abs=float(raw.mean()),
        max_abs=float(raw.max()),
        std=float(values.std(ddof=1)) if values.size > 1 else 0.0,
        max_abs_after_offset=float(shifted.max()),
        judged=judged,
        raw_outside=int((raw[known] > widths[known]).sum()),
        offset_outside=int((shifted[known] > widths[known]).sum()),
        worst_raw_ratio=float((raw[known] / widths[known]).max()) if judged else None,
        worst_offset_ratio=float((shifted[known] / widths[known]).max()) if judged else None,
    )


def implausible(bid: float, ask: float, feed_price: float) -> str | None:
    """Why a typed broker price looks like a slip of the keyboard, or None."""
    if not all(math.isfinite(v) for v in (bid, ask, feed_price)) or min(bid, ask, feed_price) <= 0:
        return "prices must be positive, finite numbers"
    if bid > ask:
        return "bid is above ask"
    if ask - bid > MAX_SPREAD_SHARE * feed_price:
        return f"the spread {ask - bid:.2f} is unusually wide"
    difference = feed_price - (bid + ask) / 2
    if abs(difference) > MAX_DIFFERENCE_SHARE * feed_price:
        return f"the difference from the feed ({difference:+.2f}) is unusually large"
    return None


def probably_closed(utc_seconds: float) -> bool:
    """Whether a broker's spot gold market is usually closed at this moment.

    A rule of thumb for warnings, not the broker's calendar: Friday 22:00 to
    Sunday 23:00 UTC and the daily break 22:00 to 23:00 UTC (US winter time;
    both are one hour earlier in US summer time). Holidays are not known here.
    """
    days, rest = divmod(int(utc_seconds), 86400)
    weekday = (days + 3) % 7  # Monday is 0; 1970-01-01 was a Thursday
    hour = rest // 3600
    if weekday == 5:
        return True
    if weekday == 4:
        return hour >= 22
    if weekday == 6:
        return hour < 23
    return hour == 22


def distinct_hours(times: Sequence[float]) -> int:
    """How many different clock hours the readings fall in."""
    return len({int(t // 3600) for t in times})
