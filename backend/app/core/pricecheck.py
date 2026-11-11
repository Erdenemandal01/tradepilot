"""How far the price feed is from the broker's price (plan section 6).

A difference is ``feed price - broker price`` for one moment. The report says
how large the differences are against the entry zone's half width
(0.25 x ATR by default). A steady offset can be corrected; scatter cannot,
so the report shows both the raw maximum and the maximum after removing the
average offset.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

import numpy as np


@dataclass(frozen=True)
class PriceDiffReport:
    count: int
    mean: float  # average difference: a steady offset of the feed
    mean_abs: float
    max_abs: float
    std: float  # sample standard deviation; 0 with one reading
    max_abs_after_offset: float  # largest distance from the average difference
    zone_half_width: float | None
    raw_within_zone: bool | None  # max_abs <= zone half width
    offset_within_zone: bool | None  # max_abs_after_offset <= zone half width


def mid(bid: float, ask: float) -> float:
    if bid > ask:
        raise ValueError(f"bid {bid} is above ask {ask}")
    return (bid + ask) / 2


def summarize(differences: Sequence[float], zone_half_width: float | None = None) -> PriceDiffReport:
    values = np.asarray(differences, dtype=float)
    if values.size == 0:
        raise ValueError("no price differences to summarize")
    if not np.isfinite(values).all():
        raise ValueError("price differences must be finite")
    mean = float(values.mean())
    max_abs = float(np.abs(values).max())
    after_offset = float(np.abs(values - mean).max())
    has_zone = zone_half_width is not None and math.isfinite(zone_half_width)
    return PriceDiffReport(
        count=int(values.size),
        mean=mean,
        mean_abs=float(np.abs(values).mean()),
        max_abs=max_abs,
        std=float(values.std(ddof=1)) if values.size > 1 else 0.0,
        max_abs_after_offset=after_offset,
        zone_half_width=zone_half_width if has_zone else None,
        raw_within_zone=bool(max_abs <= zone_half_width) if has_zone else None,
        offset_within_zone=bool(after_offset <= zone_half_width) if has_zone else None,
    )
