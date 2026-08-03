# SPDX-License-Identifier: Apache-2.0
"""Typed value objects shared by the deterministic exchange-math layer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias

Side: TypeAlias = Literal["buy", "sell"]
ImpactModel: TypeAlias = Literal["linear", "sqrt"]


@dataclass(frozen=True, slots=True)
class FillQuantity:
    """Participation-capped quantity split for one requested order."""

    requested_qty_base_1e8: int
    capacity_qty_base_1e8: int
    filled_qty_base_1e8: int
    cancelled_qty_base_1e8: int

    def __post_init__(self) -> None:
        values = (
            self.requested_qty_base_1e8,
            self.capacity_qty_base_1e8,
            self.filled_qty_base_1e8,
            self.cancelled_qty_base_1e8,
        )
        if any(value < 0 for value in values):
            raise ValueError("fill quantities must be non-negative")
        if self.filled_qty_base_1e8 > self.capacity_qty_base_1e8:
            raise ValueError("filled quantity exceeds participation capacity")
        if (
            self.filled_qty_base_1e8 + self.cancelled_qty_base_1e8
            != self.requested_qty_base_1e8
        ):
            raise ValueError("filled and cancelled quantities must equal requested quantity")


@dataclass(frozen=True, slots=True)
class FillCalculation:
    """Complete deterministic price-and-cost calculation for one fill."""

    side: Side
    quantities: FillQuantity
    ref_open_px_ticks: int
    half_spread_ticks: int
    impact_ticks: int
    fill_px_ticks: int
    notional_micro: int
    reference_notional_micro: int
    fee_micro: int
    slippage_1e8: int

    @property
    def fill_cost_micro(self) -> int:
        """Absolute modeled execution cost versus the same quantity at reference open."""

        return abs(self.notional_micro - self.reference_notional_micro)


@dataclass(frozen=True, slots=True)
class LedgerDelta:
    """The five stored delta terms that must satisfy the MATH-2 identity."""

    d_nav_micro: int
    d_price_pnl_micro: int
    d_funding_micro: int
    d_fees_micro: int
    d_liq_penalty_micro: int

    @property
    def attributed_nav_micro(self) -> int:
        return (
            self.d_price_pnl_micro
            + self.d_funding_micro
            + self.d_fees_micro
            + self.d_liq_penalty_micro
        )

    @property
    def is_balanced(self) -> bool:
        return self.d_nav_micro == self.attributed_nav_micro

    def require_balanced(self) -> None:
        if not self.is_balanced:
            raise ArithmeticError(
                "ledger invariant failed: "
                f"d_nav_micro={self.d_nav_micro}, "
                f"attributed_nav_micro={self.attributed_nav_micro}"
            )


@dataclass(frozen=True, slots=True)
class DistanceStatistics:
    """The four distance-to-liquidation metrics stored in metrics/v1."""

    min_intrabar_1e8: int | None
    p05_close_1e8: int | None
    p25_close_1e8: int | None
    median_close_1e8: int | None

