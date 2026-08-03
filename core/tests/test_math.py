# SPDX-License-Identifier: Apache-2.0
"""Golden and property-style checks for the pure exchange arithmetic."""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
from typing import TypedDict, cast

import pytest

from core.math import (
    apply_cost_multiplier,
    calculate_fill,
    cash_conservation_holds,
    ceil_div,
    ceil_fraction,
    clamp_funding_rate_1e8,
    conservative_liquidation_close_price_ticks,
    distance_to_liquidation_1e8,
    fee_micro,
    floor_fraction,
    floor_to_step,
    funding_cash_flow_micro,
    impact_ticks,
    ledger_delta,
    liquidation_crossed,
    liquidation_penalty_micro,
    liquidation_price_ticks,
    maintenance_margin_micro,
    margin_used_micro,
    nav_identity_holds,
    price_pnl_delta_micro,
    raw_target_quantity_base_1e8,
    realized_pnl_micro,
    target_quantity_base_1e8,
    unrealized_pnl_micro,
)
from core.models import LedgerDelta

ROOT = Path(__file__).resolve().parents[2]
STARTING_NAV_MICRO = 10_000_000_000


class PositionRow(TypedDict):
    qty_base_1e8: int
    upnl_micro: int


class LedgerRow(TypedDict):
    nav_micro: int
    cash_micro: int
    realized_pnl_micro: int
    d_nav_micro: int
    d_price_pnl_micro: int
    d_funding_micro: int
    d_fees_micro: int
    d_liq_penalty_micro: int
    positions: dict[str, PositionRow]


def _load_ledger(episode: str, filename: str) -> list[LedgerRow]:
    path = ROOT / "fixtures" / "golden-mini" / "expected" / episode / filename
    return cast(
        list[LedgerRow],
        [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()],
    )


def test_exact_rounding_including_negative_values() -> None:
    assert ceil_div(5, 2) == 3
    assert ceil_div(-5, 2) == -2
    assert floor_fraction(Fraction(-3, 2)) == -2
    assert ceil_fraction(Fraction(-3, 2)) == -1
    assert floor_to_step(1499, 1000) == 1000
    assert floor_to_step(-1499, 1000) == -1000
    assert apply_cost_multiplier(45_000, 20_000) == 90_000


def test_target_sizing_uses_post_funding_equity_and_step_floor() -> None:
    assert (
        target_quantity_base_1e8(
            20_000, 10_000_000_000, 1_000_000, 10_000, 100_000
        )
        == 200_000_000
    )
    assert (
        target_quantity_base_1e8(
            -15_000, 8_814_821_260, 944_000, 10_000, 100_000
        )
        == -140_000_000
    )


def test_primary_and_stress_fill_match_golden_oracle() -> None:
    primary = calculate_fill(
        side="buy",
        requested_qty_base_1e8=200_000_000,
        bar_volume_base_1e8=5_000_000_000,
        participation_cap_1e8=10_000_000,
        qty_step_base_1e8=100_000,
        ref_px_ticks=1_000_000,
        tick_size_micro=10_000,
        half_spread_1e8=50_000,
        impact_coeff_1e8=0,
        impact_model="linear",
        taker_fee_rate_1e8=45_000,
    )
    assert primary.half_spread_ticks == 500
    assert primary.fill_px_ticks == 1_000_500
    assert primary.notional_micro == 20_010_000_000
    assert primary.fee_micro == 9_004_500
    assert primary.slippage_1e8 == 50_000
    assert primary.fill_cost_micro == 10_000_000

    stress = calculate_fill(
        side="buy",
        requested_qty_base_1e8=200_000_000,
        bar_volume_base_1e8=5_000_000_000,
        participation_cap_1e8=10_000_000,
        qty_step_base_1e8=100_000,
        ref_px_ticks=1_000_000,
        tick_size_micro=10_000,
        half_spread_1e8=50_000,
        impact_coeff_1e8=0,
        impact_model="linear",
        taker_fee_rate_1e8=45_000,
        cost_multiplier_1e4=20_000,
    )
    assert stress.half_spread_ticks == 1_000
    assert stress.fill_px_ticks == 1_001_000
    assert stress.notional_micro == 20_020_000_000
    assert stress.fee_micro == 18_018_000


def test_participation_overage_is_cancelled_never_filled() -> None:
    fill = calculate_fill(
        side="buy",
        requested_qty_base_1e8=49_500_000,
        bar_volume_base_1e8=300_000_000,
        participation_cap_1e8=10_000_000,
        qty_step_base_1e8=100_000,
        ref_px_ticks=1_000_000,
        tick_size_micro=10_000,
        half_spread_1e8=50_000,
        impact_coeff_1e8=0,
        impact_model="linear",
        taker_fee_rate_1e8=45_000,
    )
    assert fill.quantities.capacity_qty_base_1e8 == 30_000_000
    assert fill.quantities.filled_qty_base_1e8 == 30_000_000
    assert fill.quantities.cancelled_qty_base_1e8 == 19_500_000
    assert fill.notional_micro == 3_001_500_000
    assert fill.fee_micro == 1_350_675


def test_linear_and_sqrt_impact_use_exact_adverse_ceil() -> None:
    assert impact_ticks(1_000_000, 25, 100, 1_000_000, "linear") == 2_500
    assert impact_ticks(1_000_000, 25, 100, 1_000_000, "sqrt") == 5_000
    assert impact_ticks(1_000_000, 1, 2, 1_000_000, "sqrt") == 7_072


def test_pnl_funding_margin_and_liquidation_match_golden_anchors() -> None:
    assert unrealized_pnl_micro(200_000_000, 1_000_500, 1_001_000, 10_000) == 10_000_000
    assert realized_pnl_micro(1, 230_000_000, 1_000_500, 660_000, 10_000) == -7_831_500_000
    assert funding_cash_flow_micro(230_000_000, 996_000, 10_000, 12_500) == -2_863_500
    assert funding_cash_flow_micro(-140_000_000, 926_000, 10_000, -12_500) == -1_620_500
    assert margin_used_micro(230_000_000, 1_000_500, 10_000, 25_000) == 9_204_600_000
    assert (
        maintenance_margin_micro(
            230_000_000, 998_000, 10_000, 16_666_667
        )
        == 3_825_666_744
    )

    assert liquidation_price_ticks(1_000_500, 1, 20_000, 16_666_667) == 600_301
    assert liquidation_price_ticks(1_000_500, 1, 25_000, 16_666_667) == 720_361
    assert liquidation_price_ticks(943_528, -1, 15_000, 16_666_667) == 1_347_897
    assert liquidation_price_ticks(1_000_500, 1, 10_000, 16_666_667) == 0
    assert distance_to_liquidation_1e8(998_000, 720_361, 1) == 27_819_539
    assert distance_to_liquidation_1e8(950_000, 0, 1) == 100_000_000

    assert liquidation_crossed(230_000_000, 720_361, 1_010_000, 660_000)
    assert not liquidation_crossed(230_000_000, 720_361, 1_010_000, 745_000)
    assert conservative_liquidation_close_price_ticks(1, 1_010_000, 660_000) == 660_000
    assert (
        liquidation_penalty_micro(
            230_000_000, 660_000, 10_000, 1_000_000
        )
        == 151_800_000
    )


def test_costs_round_up_and_credits_round_down() -> None:
    assert fee_micro(1, 1) == 1
    assert funding_cash_flow_micro(1, 1, 1, 1) == -1
    assert funding_cash_flow_micro(-1, 1, 1, 1) == 0


def test_target_sizing_exposes_raw_quantity_before_step_rounding() -> None:
    raw = raw_target_quantity_base_1e8(
        500,
        1_000_000,
        1_000_000,
        10_000,
    )
    assert raw == 500
    assert target_quantity_base_1e8(
        500,
        1_000_000,
        1_000_000,
        10_000,
        100_000,
    ) == 0


def test_funding_rate_uses_the_pack_era_cap_and_floor() -> None:
    assert clamp_funding_rate_1e8(-400_000, -300_000, 300_000) == -300_000
    assert clamp_funding_rate_1e8(400_000, -300_000, 300_000) == 300_000
    assert clamp_funding_rate_1e8(54_685, -300_000, 300_000) == 54_685
    with pytest.raises(ValueError, match="floor"):
        clamp_funding_rate_1e8(0, 1, -1)


@pytest.mark.parametrize("episode", ["main", "variant-liquidation"])
@pytest.mark.parametrize("filename", ["ledger.jsonl", "ledger_stress_2x.jsonl"])
def test_every_golden_ledger_row_satisfies_m2_invariants(
    episode: str, filename: str
) -> None:
    rows = _load_ledger(episode, filename)
    for row in rows:
        delta = LedgerDelta(
            d_nav_micro=row["d_nav_micro"],
            d_price_pnl_micro=row["d_price_pnl_micro"],
            d_funding_micro=row["d_funding_micro"],
            d_fees_micro=row["d_fees_micro"],
            d_liq_penalty_micro=row["d_liq_penalty_micro"],
        )
        delta.require_balanced()
        position = row["positions"]["BTC"]
        assert nav_identity_holds(
            row["nav_micro"], row["cash_micro"], position["upnl_micro"]
        )
        assert cash_conservation_holds(
            row["cash_micro"], STARTING_NAV_MICRO, row["realized_pnl_micro"]
        )


def test_delta_helpers_reject_an_unbalanced_ledger_row() -> None:
    assert price_pnl_delta_micro(10, 14, 20, 27) == 11
    balanced = ledger_delta(
        previous_nav_micro=100,
        current_nav_micro=111,
        d_price_pnl_micro=11,
        d_funding_micro=0,
        d_fees_micro=0,
        d_liq_penalty_micro=0,
    )
    assert balanced.is_balanced
    with pytest.raises(ArithmeticError, match="ledger invariant failed"):
        ledger_delta(
            previous_nav_micro=100,
            current_nav_micro=112,
            d_price_pnl_micro=11,
            d_funding_micro=0,
            d_fees_micro=0,
            d_liq_penalty_micro=0,
        )
