# SPDX-License-Identifier: Apache-2.0
"""Exact, deterministic metrics pinned by the golden-mini oracle."""

from __future__ import annotations

import math as integer_math
from fractions import Fraction
from typing import Iterable, Sequence

from core.math import RATE_SCALE, ceil_div, floor_fraction
from core.models import DistanceStatistics


class UndefinedMetricError(ValueError):
    """Raised when a required metric has no mathematically defined value."""


def bar_returns(navs_micro: Sequence[int]) -> tuple[Fraction, ...]:
    """Per-bar arithmetic returns using exact rational NAV ratios."""

    if len(navs_micro) < 2:
        raise ValueError("at least two NAV observations are required")
    returns: list[Fraction] = []
    for previous, current in zip(navs_micro, navs_micro[1:]):
        if previous <= 0:
            raise UndefinedMetricError("bar return is undefined after non-positive NAV")
        returns.append(Fraction(current - previous, previous))
    return tuple(returns)


def net_return_1e8(
    final_nav_micro: int,
    starting_nav_micro: int,
    *,
    rate_scale: int = RATE_SCALE,
) -> int:
    """Floor total return to the metrics/v1 fixed-point scale."""

    if starting_nav_micro <= 0:
        raise ValueError("starting NAV must be positive")
    return floor_fraction(
        Fraction((final_nav_micro - starting_nav_micro) * rate_scale, starting_nav_micro)
    )


def max_drawdown_1e8(
    navs_micro: Sequence[int], *, rate_scale: int = RATE_SCALE
) -> int:
    """Peak-to-close maximum drawdown, floored to fixed point."""

    if not navs_micro:
        raise ValueError("NAV series cannot be empty")
    if navs_micro[0] <= 0:
        raise ValueError("initial NAV must be positive")
    peak = navs_micro[0]
    maximum = Fraction(0)
    for nav in navs_micro:
        peak = max(peak, nav)
        if peak <= 0:
            raise UndefinedMetricError("drawdown is undefined without a positive peak")
        maximum = max(maximum, Fraction((peak - nav) * rate_scale, peak))
    return floor_fraction(maximum)


def floor_ratio_over_sqrt(numerator: Fraction, denominator: Fraction) -> int:
    """Return ``floor(numerator / sqrt(denominator))`` exactly."""

    if denominator <= 0:
        raise UndefinedMetricError("square-root denominator must be positive")
    squared = (numerator * numerator) / denominator
    root_floor = integer_math.isqrt(floor_fraction(squared))
    if numerator >= 0:
        return root_floor
    exact = (
        squared.denominator == 1
        and root_floor * root_floor == squared.numerator
    )
    return -(root_floor if exact else root_floor + 1)


def sortino_1e8(
    returns: Sequence[Fraction], *, rate_scale: int = RATE_SCALE
) -> int:
    """Golden estimator: mean return / RMS negative-part downside deviation."""

    if not returns:
        raise ValueError("return series cannot be empty")
    total = Fraction(0)
    downside_squares = Fraction(0)
    for value in returns:
        total += value
        downside = min(value, Fraction(0))
        downside_squares += downside * downside
    count = len(returns)
    mean_return = total / count
    mean_squared_downside = downside_squares / count
    if mean_squared_downside <= 0:
        raise UndefinedMetricError("Sortino is undefined when downside deviation is zero")
    return floor_ratio_over_sqrt(mean_return * rate_scale, mean_squared_downside)


def cvar_5_1e8(
    returns: Sequence[Fraction], *, rate_scale: int = RATE_SCALE
) -> int:
    """Mean of the worst ``ceil(5% * n)`` bar returns, floored."""

    if not returns:
        raise ValueError("return series cannot be empty")
    tail_count = ceil_div(len(returns), 20)
    worst = sorted(returns)[:tail_count]
    total = sum(worst, Fraction(0))
    return floor_fraction(total * rate_scale / tail_count)


def nearest_rank(values: Sequence[int], percentile_num: int, percentile_den: int) -> int:
    """Nearest-rank percentile with rank ``max(1, ceil(p*n))``."""

    if not values:
        raise ValueError("nearest-rank input cannot be empty")
    if percentile_den <= 0 or not 0 <= percentile_num <= percentile_den:
        raise ValueError("percentile must be between zero and one")
    ordered = sorted(values)
    rank = max(1, ceil_div(percentile_num * len(ordered), percentile_den))
    return ordered[rank - 1]


def distance_statistics(
    close_distances_1e8: Sequence[int],
    intrabar_distances_1e8: Sequence[int],
) -> DistanceStatistics:
    """Build the metrics/v1 distance summary from close and wick distances."""

    if not close_distances_1e8:
        if intrabar_distances_1e8:
            raise ValueError("intrabar distances cannot exist without in-position closes")
        return DistanceStatistics(None, None, None, None)
    if not intrabar_distances_1e8:
        raise ValueError("in-position closes require intra-bar distances")
    return DistanceStatistics(
        min_intrabar_1e8=min(intrabar_distances_1e8),
        p05_close_1e8=nearest_rank(close_distances_1e8, 1, 20),
        p25_close_1e8=nearest_rank(close_distances_1e8, 1, 4),
        median_close_1e8=nearest_rank(close_distances_1e8, 1, 2),
    )


def turnover_1e8(
    turnover_notional_micro: int,
    starting_nav_micro: int,
    *,
    rate_scale: int = RATE_SCALE,
) -> int:
    """Floor summed absolute fill notional divided by starting NAV."""

    if turnover_notional_micro < 0:
        raise ValueError("turnover notional must be non-negative")
    if starting_nav_micro <= 0:
        raise ValueError("starting NAV must be positive")
    return floor_fraction(
        Fraction(turnover_notional_micro * rate_scale, starting_nav_micro)
    )


def total_fill_cost_micro(
    fill_and_reference_notionals: Iterable[tuple[int, int]],
) -> int:
    """Sum absolute fill-vs-reference notional differences."""

    total = 0
    for fill_notional, reference_notional in fill_and_reference_notionals:
        if fill_notional < 0 or reference_notional < 0:
            raise ValueError("notionals must be non-negative")
        total += abs(fill_notional - reference_notional)
    return total
