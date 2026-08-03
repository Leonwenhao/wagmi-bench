# SPDX-License-Identifier: Apache-2.0
"""Exact exchange arithmetic for WAGMI Bench.

All persisted money, price, quantity, leverage, and rate values remain integers.
``Fraction`` is used only as an exact intermediate; binary floating point never
enters an engine calculation.
"""

from __future__ import annotations

import math as integer_math
from fractions import Fraction

from core.models import FillCalculation, FillQuantity, ImpactModel, LedgerDelta, Side

RATE_SCALE = 100_000_000
QTY_SCALE = 100_000_000
LEVERAGE_SCALE = 10_000
COST_MULTIPLIER_SCALE = 10_000


def ceil_div(dividend: int, divisor: int) -> int:
    """Return mathematical ceil(dividend / divisor) for a positive divisor."""

    if divisor <= 0:
        raise ValueError("divisor must be positive")
    return -((-dividend) // divisor)


def floor_fraction(value: Fraction) -> int:
    """Floor an exact rational, including negative values."""

    return value.numerator // value.denominator


def ceil_fraction(value: Fraction) -> int:
    """Ceil an exact rational, including negative values."""

    return -((-value.numerator) // value.denominator)


def floor_to_step(value: int, step: int) -> int:
    """Round toward zero to an integer step."""

    if step <= 0:
        raise ValueError("step must be positive")
    magnitude = (abs(value) // step) * step
    return magnitude if value >= 0 else -magnitude


def ceil_sqrt_fraction(value: Fraction) -> int:
    """Return ``ceil(sqrt(value))`` exactly for a non-negative rational."""

    if value < 0:
        raise ValueError("square root input must be non-negative")
    floor_root = integer_math.isqrt(value.numerator // value.denominator)
    if floor_root * floor_root * value.denominator == value.numerator:
        return floor_root
    return floor_root + 1


def apply_cost_multiplier(
    base_rate: int,
    multiplier_1e4: int,
    *,
    multiplier_scale: int = COST_MULTIPLIER_SCALE,
) -> int:
    """Scale a non-negative cost rate, rounding a fractional cost upward."""

    if base_rate < 0:
        raise ValueError("base cost rate must be non-negative")
    if multiplier_1e4 <= 0 or multiplier_scale <= 0:
        raise ValueError("cost multiplier and scale must be positive")
    return ceil_fraction(Fraction(base_rate * multiplier_1e4, multiplier_scale))


def target_quantity_base_1e8(
    target_lev_1e4: int,
    equity_micro: int,
    ref_px_ticks: int,
    tick_size_micro: int,
    qty_step_base_1e8: int,
    *,
    leverage_scale: int = LEVERAGE_SCALE,
    qty_scale: int = QTY_SCALE,
) -> int:
    """Size a signed leverage target at the next-bar reference price.

    The exact raw quantity is floored, then rounded toward zero to the venue
    quantity step, matching the golden oracle.
    """

    raw = raw_target_quantity_base_1e8(
        target_lev_1e4,
        equity_micro,
        ref_px_ticks,
        tick_size_micro,
        leverage_scale=leverage_scale,
        qty_scale=qty_scale,
    )
    return floor_to_step(raw, qty_step_base_1e8)


def raw_target_quantity_base_1e8(
    target_lev_1e4: int,
    equity_micro: int,
    ref_px_ticks: int,
    tick_size_micro: int,
    *,
    leverage_scale: int = LEVERAGE_SCALE,
    qty_scale: int = QTY_SCALE,
) -> int:
    """Size a target before applying the venue quantity step."""

    if equity_micro < 0:
        raise ValueError("equity must be non-negative")
    if ref_px_ticks <= 0 or tick_size_micro <= 0:
        raise ValueError("reference price and tick size must be positive")
    if leverage_scale <= 0 or qty_scale <= 0:
        raise ValueError("scales must be positive")
    raw_abs = floor_fraction(
        Fraction(
            abs(target_lev_1e4) * equity_micro * qty_scale,
            leverage_scale * ref_px_ticks * tick_size_micro,
        )
    )
    return raw_abs if target_lev_1e4 >= 0 else -raw_abs


def participation_capacity_base_1e8(
    bar_volume_base_1e8: int,
    participation_cap_1e8: int,
    qty_step_base_1e8: int,
    *,
    rate_scale: int = RATE_SCALE,
) -> int:
    """Return the maximum step-rounded quantity available in the fill bar."""

    if bar_volume_base_1e8 < 0:
        raise ValueError("bar volume must be non-negative")
    if not 0 <= participation_cap_1e8 <= rate_scale:
        raise ValueError("participation cap must be between zero and rate scale")
    raw_capacity = floor_fraction(
        Fraction(bar_volume_base_1e8 * participation_cap_1e8, rate_scale)
    )
    return floor_to_step(raw_capacity, qty_step_base_1e8)


def participation_fill(
    requested_qty_base_1e8: int,
    bar_volume_base_1e8: int,
    participation_cap_1e8: int,
    qty_step_base_1e8: int,
    *,
    rate_scale: int = RATE_SCALE,
) -> FillQuantity:
    """Split a non-negative request into filled and permanently cancelled quantity."""

    if requested_qty_base_1e8 < 0:
        raise ValueError("requested quantity must be non-negative")
    capacity = participation_capacity_base_1e8(
        bar_volume_base_1e8,
        participation_cap_1e8,
        qty_step_base_1e8,
        rate_scale=rate_scale,
    )
    filled = floor_to_step(min(requested_qty_base_1e8, capacity), qty_step_base_1e8)
    return FillQuantity(
        requested_qty_base_1e8=requested_qty_base_1e8,
        capacity_qty_base_1e8=capacity,
        filled_qty_base_1e8=filled,
        cancelled_qty_base_1e8=requested_qty_base_1e8 - filled,
    )


def half_spread_ticks(
    ref_px_ticks: int,
    half_spread_1e8: int,
    *,
    rate_scale: int = RATE_SCALE,
) -> int:
    """Compute worst-side half spread with agent-adverse ceil rounding."""

    if ref_px_ticks <= 0:
        raise ValueError("reference price must be positive")
    if half_spread_1e8 < 0:
        raise ValueError("half spread must be non-negative")
    return ceil_fraction(Fraction(ref_px_ticks * half_spread_1e8, rate_scale))


def impact_ticks(
    ref_px_ticks: int,
    filled_qty_base_1e8: int,
    bar_volume_base_1e8: int,
    impact_coeff_1e8: int,
    impact_model: ImpactModel,
    *,
    rate_scale: int = RATE_SCALE,
) -> int:
    """Compute non-negative impact ticks with one final adverse ceil.

    ``linear`` means ``ref * coeff * participation``. ``sqrt`` means
    ``ref * coeff * sqrt(participation)``. The latter is evaluated as an exact
    rational square-root inequality, without decimal or binary-float rounding.
    """

    if ref_px_ticks <= 0:
        raise ValueError("reference price must be positive")
    if filled_qty_base_1e8 < 0 or impact_coeff_1e8 < 0:
        raise ValueError("quantity and impact coefficient must be non-negative")
    if filled_qty_base_1e8 == 0 or impact_coeff_1e8 == 0:
        return 0
    if bar_volume_base_1e8 <= 0:
        raise ValueError("positive fill requires positive bar volume")
    if filled_qty_base_1e8 > bar_volume_base_1e8:
        raise ValueError("filled quantity cannot exceed bar volume")
    if impact_model == "linear":
        return ceil_fraction(
            Fraction(
                ref_px_ticks * impact_coeff_1e8 * filled_qty_base_1e8,
                rate_scale * bar_volume_base_1e8,
            )
        )
    if impact_model == "sqrt":
        squared_ticks = Fraction(
            ref_px_ticks
            * ref_px_ticks
            * impact_coeff_1e8
            * impact_coeff_1e8
            * filled_qty_base_1e8,
            rate_scale * rate_scale * bar_volume_base_1e8,
        )
        return ceil_sqrt_fraction(squared_ticks)
    raise ValueError(f"unknown impact model: {impact_model}")


def fill_price_ticks(
    side: Side,
    ref_px_ticks: int,
    spread_ticks: int,
    modeled_impact_ticks: int,
) -> int:
    """Apply spread and impact against the taker."""

    if ref_px_ticks <= 0:
        raise ValueError("reference price must be positive")
    if spread_ticks < 0 or modeled_impact_ticks < 0:
        raise ValueError("spread and impact must be non-negative")
    adjustment = spread_ticks + modeled_impact_ticks
    price = ref_px_ticks + adjustment if side == "buy" else ref_px_ticks - adjustment
    if price <= 0:
        raise ValueError("modeled sell fill price must remain positive")
    return price


def notional_micro(
    qty_base_1e8: int,
    px_ticks: int,
    tick_size_micro: int,
    *,
    qty_scale: int = QTY_SCALE,
) -> int:
    """Value an absolute quantity at a tick price, flooring to micro-quote."""

    if px_ticks <= 0 or tick_size_micro <= 0 or qty_scale <= 0:
        raise ValueError("price, tick size, and quantity scale must be positive")
    return abs(qty_base_1e8) * px_ticks * tick_size_micro // qty_scale


def fee_micro(
    fill_notional_micro: int,
    fee_rate_1e8: int,
    *,
    rate_scale: int = RATE_SCALE,
) -> int:
    """Charge a fee with agent-adverse ceil rounding."""

    if fill_notional_micro < 0 or fee_rate_1e8 < 0:
        raise ValueError("notional and fee rate must be non-negative")
    return ceil_fraction(Fraction(fill_notional_micro * fee_rate_1e8, rate_scale))


def slippage_1e8(
    fill_px_ticks: int,
    ref_px_ticks: int,
    *,
    rate_scale: int = RATE_SCALE,
) -> int:
    """Return signed fill-vs-reference slippage, flooring the exact ratio."""

    if fill_px_ticks <= 0 or ref_px_ticks <= 0:
        raise ValueError("fill and reference prices must be positive")
    return floor_fraction(
        Fraction((fill_px_ticks - ref_px_ticks) * rate_scale, ref_px_ticks)
    )


def calculate_fill(
    *,
    side: Side,
    requested_qty_base_1e8: int,
    bar_volume_base_1e8: int,
    participation_cap_1e8: int,
    qty_step_base_1e8: int,
    ref_px_ticks: int,
    tick_size_micro: int,
    half_spread_1e8: int,
    impact_coeff_1e8: int,
    impact_model: ImpactModel,
    taker_fee_rate_1e8: int,
    cost_multiplier_1e4: int = COST_MULTIPLIER_SCALE,
    rate_scale: int = RATE_SCALE,
) -> FillCalculation:
    """Calculate a participation-capped fill under one cost profile."""

    quantities = participation_fill(
        requested_qty_base_1e8,
        bar_volume_base_1e8,
        participation_cap_1e8,
        qty_step_base_1e8,
        rate_scale=rate_scale,
    )
    scaled_spread = apply_cost_multiplier(half_spread_1e8, cost_multiplier_1e4)
    scaled_impact = apply_cost_multiplier(impact_coeff_1e8, cost_multiplier_1e4)
    scaled_fee = apply_cost_multiplier(taker_fee_rate_1e8, cost_multiplier_1e4)
    spread_ticks = half_spread_ticks(
        ref_px_ticks, scaled_spread, rate_scale=rate_scale
    )
    modeled_impact_ticks = impact_ticks(
        ref_px_ticks,
        quantities.filled_qty_base_1e8,
        bar_volume_base_1e8,
        scaled_impact,
        impact_model,
        rate_scale=rate_scale,
    )
    fill_px = fill_price_ticks(side, ref_px_ticks, spread_ticks, modeled_impact_ticks)
    fill_notional = notional_micro(
        quantities.filled_qty_base_1e8, fill_px, tick_size_micro
    )
    reference_notional = notional_micro(
        quantities.filled_qty_base_1e8, ref_px_ticks, tick_size_micro
    )
    return FillCalculation(
        side=side,
        quantities=quantities,
        ref_open_px_ticks=ref_px_ticks,
        half_spread_ticks=spread_ticks,
        impact_ticks=modeled_impact_ticks,
        fill_px_ticks=fill_px,
        notional_micro=fill_notional,
        reference_notional_micro=reference_notional,
        fee_micro=fee_micro(fill_notional, scaled_fee, rate_scale=rate_scale),
        slippage_1e8=slippage_1e8(
            fill_px, ref_px_ticks, rate_scale=rate_scale
        ),
    )


def unrealized_pnl_micro(
    position_qty_base_1e8: int,
    entry_px_ticks: int,
    mark_px_ticks: int,
    tick_size_micro: int,
    *,
    qty_scale: int = QTY_SCALE,
) -> int:
    """Mark an open signed position, flooring to micro-quote."""

    if entry_px_ticks <= 0 or mark_px_ticks <= 0 or tick_size_micro <= 0:
        raise ValueError("entry, mark, and tick size must be positive")
    return (
        (mark_px_ticks - entry_px_ticks)
        * position_qty_base_1e8
        * tick_size_micro
        // qty_scale
    )


def realized_pnl_micro(
    position_sign: int,
    closed_qty_base_1e8: int,
    entry_px_ticks: int,
    close_px_ticks: int,
    tick_size_micro: int,
    *,
    qty_scale: int = QTY_SCALE,
) -> int:
    """Realize PnL for an absolute closed quantity from a long or short."""

    if position_sign not in (-1, 1):
        raise ValueError("position sign must be -1 or 1")
    if closed_qty_base_1e8 < 0:
        raise ValueError("closed quantity must be non-negative")
    if entry_px_ticks <= 0 or close_px_ticks <= 0 or tick_size_micro <= 0:
        raise ValueError("entry, close, and tick size must be positive")
    signed_closed = position_sign * closed_qty_base_1e8
    return (
        (close_px_ticks - entry_px_ticks)
        * signed_closed
        * tick_size_micro
        // qty_scale
    )


def funding_cash_flow_micro(
    position_qty_base_1e8: int,
    index_px_ticks: int,
    tick_size_micro: int,
    funding_rate_1e8: int,
    *,
    qty_scale: int = QTY_SCALE,
    rate_scale: int = RATE_SCALE,
) -> int:
    """Return signed account funding flow (negative means the agent paid).

    The venue amount is ceiled before sign inversion: charges round up and
    credits round down in magnitude, including negative position/rate cases.
    """

    if index_px_ticks <= 0 or tick_size_micro <= 0:
        raise ValueError("index price and tick size must be positive")
    venue_amount = ceil_fraction(
        Fraction(
            position_qty_base_1e8
            * index_px_ticks
            * tick_size_micro
            * funding_rate_1e8,
            qty_scale * rate_scale,
        )
    )
    return -venue_amount


def clamp_funding_rate_1e8(
    observed_rate_1e8: int,
    floor_1e8: int,
    cap_1e8: int,
) -> int:
    """Apply the pack's venue-era funding floor and cap exactly."""

    if floor_1e8 > cap_1e8:
        raise ValueError("funding floor must not exceed cap")
    return min(max(observed_rate_1e8, floor_1e8), cap_1e8)


def margin_used_micro(
    position_qty_base_1e8: int,
    entry_px_ticks: int,
    tick_size_micro: int,
    target_leverage_1e4: int,
    *,
    qty_scale: int = QTY_SCALE,
    leverage_scale: int = LEVERAGE_SCALE,
) -> int:
    """Informational isolated margin: floor(entry notional / target leverage)."""

    if position_qty_base_1e8 == 0:
        return 0
    if entry_px_ticks <= 0 or tick_size_micro <= 0:
        raise ValueError("entry price and tick size must be positive")
    if target_leverage_1e4 <= 0:
        raise ValueError("target leverage magnitude must be positive")
    return floor_fraction(
        Fraction(
            abs(position_qty_base_1e8)
            * entry_px_ticks
            * tick_size_micro
            * leverage_scale,
            qty_scale * target_leverage_1e4,
        )
    )


def maintenance_margin_micro(
    position_qty_base_1e8: int,
    mark_px_ticks: int,
    tick_size_micro: int,
    maintenance_rate_1e8: int,
    *,
    qty_scale: int = QTY_SCALE,
    rate_scale: int = RATE_SCALE,
) -> int:
    """Required maintenance at mark notional, rounded upward."""

    if position_qty_base_1e8 == 0:
        return 0
    if mark_px_ticks <= 0 or tick_size_micro <= 0:
        raise ValueError("mark price and tick size must be positive")
    if maintenance_rate_1e8 < 0:
        raise ValueError("maintenance rate must be non-negative")
    return ceil_fraction(
        Fraction(
            abs(position_qty_base_1e8)
            * mark_px_ticks
            * tick_size_micro
            * maintenance_rate_1e8,
            qty_scale * rate_scale,
        )
    )


def liquidation_price_ticks(
    entry_px_ticks: int,
    position_sign: int,
    target_leverage_1e4: int,
    maintenance_rate_1e8: int,
    *,
    leverage_scale: int = LEVERAGE_SCALE,
    rate_scale: int = RATE_SCALE,
) -> int:
    """Pinned isolated-margin maintenance crossing price.

    Long triggers round upward; short triggers round downward. A long at or
    below 1x has no reachable positive trigger and returns zero.
    """

    if entry_px_ticks <= 0:
        raise ValueError("entry price must be positive")
    if position_sign not in (-1, 1):
        raise ValueError("position sign must be -1 or 1")
    if target_leverage_1e4 <= 0:
        raise ValueError("target leverage magnitude must be positive")
    if not 0 <= maintenance_rate_1e8 < rate_scale:
        raise ValueError("maintenance rate must be in [0, rate_scale)")
    if position_sign > 0:
        numerator = Fraction(entry_px_ticks) * Fraction(
            target_leverage_1e4 - leverage_scale, target_leverage_1e4
        )
        if numerator <= 0:
            return 0
        return ceil_fraction(
            numerator / Fraction(rate_scale - maintenance_rate_1e8, rate_scale)
        )
    numerator = Fraction(entry_px_ticks) * Fraction(
        target_leverage_1e4 + leverage_scale, target_leverage_1e4
    )
    return floor_fraction(
        numerator / Fraction(rate_scale + maintenance_rate_1e8, rate_scale)
    )


def distance_to_liquidation_1e8(
    current_px_ticks: int,
    liquidation_px_ticks: int,
    position_sign: int,
    *,
    rate_scale: int = RATE_SCALE,
) -> int:
    """Distance from current price to liquidation, divided by current price."""

    if current_px_ticks <= 0:
        raise ValueError("current price must be positive")
    if position_sign not in (-1, 1):
        raise ValueError("position sign must be -1 or 1")
    if position_sign > 0:
        if liquidation_px_ticks <= 0:
            return rate_scale
        numerator = current_px_ticks - liquidation_px_ticks
    else:
        if liquidation_px_ticks <= 0:
            raise ValueError("short liquidation price must be positive")
        numerator = liquidation_px_ticks - current_px_ticks
    return floor_fraction(Fraction(numerator * rate_scale, current_px_ticks))


def liquidation_crossed(
    position_qty_base_1e8: int,
    liquidation_px_ticks: int,
    mark_high_ticks: int,
    mark_low_ticks: int,
) -> bool:
    """Check the adverse intra-bar mark extreme, including gap-through."""

    if mark_high_ticks <= 0 or mark_low_ticks <= 0:
        raise ValueError("mark high and low must be positive")
    if mark_low_ticks > mark_high_ticks:
        raise ValueError("mark low cannot exceed mark high")
    if position_qty_base_1e8 > 0:
        return liquidation_px_ticks > 0 and mark_low_ticks <= liquidation_px_ticks
    if position_qty_base_1e8 < 0:
        return mark_high_ticks >= liquidation_px_ticks
    return False


def conservative_liquidation_close_price_ticks(
    position_sign: int, mark_high_ticks: int, mark_low_ticks: int
) -> int:
    """Use the adverse mark extreme as the terminal forced-close price."""

    if position_sign not in (-1, 1):
        raise ValueError("position sign must be -1 or 1")
    if mark_low_ticks <= 0 or mark_high_ticks < mark_low_ticks:
        raise ValueError("invalid mark extremes")
    return mark_low_ticks if position_sign > 0 else mark_high_ticks


def liquidation_penalty_micro(
    position_qty_base_1e8: int,
    close_px_ticks: int,
    tick_size_micro: int,
    penalty_rate_1e8: int,
    *,
    qty_scale: int = QTY_SCALE,
    rate_scale: int = RATE_SCALE,
) -> int:
    """Charge the liquidation penalty on conservative close-price notional."""

    close_notional = notional_micro(
        position_qty_base_1e8,
        close_px_ticks,
        tick_size_micro,
        qty_scale=qty_scale,
    )
    return fee_micro(close_notional, penalty_rate_1e8, rate_scale=rate_scale)


def price_pnl_delta_micro(
    previous_upnl_micro: int,
    current_upnl_micro: int,
    previous_realized_price_pnl_micro: int,
    current_realized_price_pnl_micro: int,
) -> int:
    """Derive the ledger price-attribution term from cumulative state."""

    return (current_upnl_micro - previous_upnl_micro) + (
        current_realized_price_pnl_micro - previous_realized_price_pnl_micro
    )


def ledger_delta(
    *,
    previous_nav_micro: int,
    current_nav_micro: int,
    d_price_pnl_micro: int,
    d_funding_micro: int,
    d_fees_micro: int,
    d_liq_penalty_micro: int,
) -> LedgerDelta:
    """Build and verify one MATH-2 ledger delta."""

    result = LedgerDelta(
        d_nav_micro=current_nav_micro - previous_nav_micro,
        d_price_pnl_micro=d_price_pnl_micro,
        d_funding_micro=d_funding_micro,
        d_fees_micro=d_fees_micro,
        d_liq_penalty_micro=d_liq_penalty_micro,
    )
    result.require_balanced()
    return result


def nav_identity_holds(nav_micro: int, cash_micro: int, upnl_micro: int) -> bool:
    return nav_micro == cash_micro + upnl_micro


def cash_conservation_holds(
    cash_micro: int, starting_cash_micro: int, cumulative_realized_pnl_micro: int
) -> bool:
    return cash_micro - starting_cash_micro == cumulative_realized_pnl_micro
