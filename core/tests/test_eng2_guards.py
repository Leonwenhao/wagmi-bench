# SPDX-License-Identifier: Apache-2.0
"""ENG-2 guard coverage for invalid money-path inputs and edge branches."""

from __future__ import annotations

from collections.abc import Callable
from fractions import Fraction
from typing import cast

import pytest

from core.math import (
    apply_cost_multiplier,
    ceil_div,
    ceil_sqrt_fraction,
    conservative_liquidation_close_price_ticks,
    distance_to_liquidation_1e8,
    fee_micro,
    fill_price_ticks,
    floor_to_step,
    funding_cash_flow_micro,
    half_spread_ticks,
    impact_ticks,
    liquidation_crossed,
    liquidation_price_ticks,
    maintenance_margin_micro,
    margin_used_micro,
    notional_micro,
    participation_capacity_base_1e8,
    participation_fill,
    raw_target_quantity_base_1e8,
    realized_pnl_micro,
    slippage_1e8,
    unrealized_pnl_micro,
)
from core.metrics import (
    UndefinedMetricError,
    bar_returns,
    cvar_5_1e8,
    distance_statistics,
    floor_ratio_over_sqrt,
    max_drawdown_1e8,
    nearest_rank,
    net_return_1e8,
    sortino_1e8,
    total_fill_cost_micro,
    turnover_1e8,
)
from core.models import ImpactModel


@pytest.mark.parametrize(
    ("operation", "message"),
    [
        (lambda: ceil_div(1, 0), "divisor"),
        (lambda: floor_to_step(1, 0), "step"),
        (lambda: ceil_sqrt_fraction(Fraction(-1)), "square root"),
        (lambda: apply_cost_multiplier(-1, 10_000), "base cost"),
        (lambda: apply_cost_multiplier(1, 0), "multiplier"),
        (
            lambda: raw_target_quantity_base_1e8(
                1, -1, 1, 1
            ),
            "equity",
        ),
        (
            lambda: raw_target_quantity_base_1e8(
                1, 1, 0, 1
            ),
            "reference price",
        ),
        (
            lambda: raw_target_quantity_base_1e8(
                1, 1, 1, 1, leverage_scale=0
            ),
            "scales",
        ),
        (
            lambda: participation_capacity_base_1e8(-1, 1, 1),
            "volume",
        ),
        (
            lambda: participation_capacity_base_1e8(
                1, 100_000_001, 1
            ),
            "participation cap",
        ),
        (lambda: participation_fill(-1, 1, 1, 1), "requested"),
        (lambda: half_spread_ticks(0, 0), "reference"),
        (lambda: half_spread_ticks(1, -1), "half spread"),
        (lambda: impact_ticks(0, 1, 1, 1, "linear"), "reference"),
        (lambda: impact_ticks(1, -1, 1, 1, "linear"), "quantity"),
        (lambda: impact_ticks(1, 1, 0, 1, "linear"), "bar volume"),
        (lambda: impact_ticks(1, 2, 1, 1, "linear"), "exceed"),
        (
            lambda: impact_ticks(
                1,
                1,
                1,
                1,
                cast(ImpactModel, "bad"),
            ),
            "unknown impact",
        ),
        (lambda: fill_price_ticks("buy", 0, 0, 0), "reference"),
        (lambda: fill_price_ticks("buy", 1, -1, 0), "spread"),
        (lambda: fill_price_ticks("sell", 1, 1, 0), "remain positive"),
        (lambda: notional_micro(1, 0, 1), "positive"),
        (lambda: fee_micro(-1, 1), "non-negative"),
        (lambda: slippage_1e8(0, 1), "positive"),
        (lambda: unrealized_pnl_micro(1, 0, 1, 1), "positive"),
        (lambda: realized_pnl_micro(0, 1, 1, 1, 1), "position sign"),
        (lambda: realized_pnl_micro(1, -1, 1, 1, 1), "closed quantity"),
        (lambda: realized_pnl_micro(1, 1, 0, 1, 1), "positive"),
        (lambda: funding_cash_flow_micro(1, 0, 1, 1), "index price"),
        (lambda: margin_used_micro(1, 0, 1, 1), "entry price"),
        (lambda: margin_used_micro(1, 1, 1, 0), "leverage"),
        (lambda: maintenance_margin_micro(1, 0, 1, 1), "mark price"),
        (lambda: maintenance_margin_micro(1, 1, 1, -1), "maintenance"),
        (lambda: liquidation_price_ticks(0, 1, 1, 1), "entry price"),
        (lambda: liquidation_price_ticks(1, 0, 1, 1), "position sign"),
        (lambda: liquidation_price_ticks(1, 1, 0, 1), "leverage"),
        (
            lambda: liquidation_price_ticks(
                1, 1, 1, 100_000_000
            ),
            "maintenance rate",
        ),
        (lambda: distance_to_liquidation_1e8(0, 1, 1), "current"),
        (lambda: distance_to_liquidation_1e8(1, 1, 0), "position sign"),
        (lambda: distance_to_liquidation_1e8(1, 0, -1), "short"),
        (lambda: liquidation_crossed(1, 1, 0, 1), "positive"),
        (lambda: liquidation_crossed(1, 1, 1, 2), "cannot exceed"),
        (
            lambda: conservative_liquidation_close_price_ticks(
                0, 2, 1
            ),
            "position sign",
        ),
        (
            lambda: conservative_liquidation_close_price_ticks(
                1, 1, 2
            ),
            "extremes",
        ),
    ],
)
def test_money_math_rejects_invalid_inputs(
    operation: Callable[[], object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        operation()


def test_money_math_zero_and_short_branches_are_explicit() -> None:
    assert impact_ticks(1, 0, 0, 1, "linear") == 0
    assert margin_used_micro(0, 0, 0, 0) == 0
    assert maintenance_margin_micro(0, 0, 0, -1) == 0
    assert liquidation_crossed(-1, 10, 10, 1)
    assert not liquidation_crossed(0, 10, 10, 1)
    assert conservative_liquidation_close_price_ticks(-1, 10, 1) == 10


@pytest.mark.parametrize(
    ("operation", "error"),
    [
        (lambda: bar_returns([1]), ValueError),
        (lambda: bar_returns([0, 1]), UndefinedMetricError),
        (lambda: net_return_1e8(1, 0), ValueError),
        (lambda: max_drawdown_1e8([]), ValueError),
        (lambda: max_drawdown_1e8([0]), ValueError),
        (lambda: sortino_1e8([]), ValueError),
        (
            lambda: floor_ratio_over_sqrt(
                Fraction(1), Fraction(0)
            ),
            UndefinedMetricError,
        ),
        (lambda: cvar_5_1e8([]), ValueError),
        (lambda: nearest_rank([1], 1, 0), ValueError),
        (lambda: distance_statistics([], [1]), ValueError),
        (lambda: distance_statistics([1], []), ValueError),
        (lambda: turnover_1e8(-1, 1), ValueError),
        (lambda: turnover_1e8(1, 0), ValueError),
        (lambda: total_fill_cost_micro([(-1, 1)]), ValueError),
    ],
)
def test_survival_metrics_reject_undefined_inputs(
    operation: Callable[[], object],
    error: type[Exception],
) -> None:
    with pytest.raises(error):
        operation()
