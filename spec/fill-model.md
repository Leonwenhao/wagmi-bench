# Deterministic fill model

This non-frozen implementation specification records the integer arithmetic
used by C2.4. The frozen contracts and reconciled golden fixture control if a
conflict is found. Pack parameters are authoritative; the agent never supplies
fees, spread, impact, participation, margin, or liquidation parameters.

## Scales and primitive rounding

The fixed scales are:

```text
quantity       Q = 100000000 base units per base asset
rate/ratio     R = 100000000
leverage       Ls = 10000
cost multiplier Ms = 10000
```

`floor(x)` and `ceil(x)` are mathematical floor and ceiling. For a signed
integer quantity, `step0(x, s) = sign(x) * floor(abs(x) / s) * s`; this rounds
toward zero to the venue quantity step. Every intermediate is an integer or an
exact rational.

Money divisions are agent-adverse unless a rule below explicitly says floor:
costs round up and credits round down in magnitude. Reported ratio fields use
floor.

## Turn ordering and target sizing

At an instant shared by a funding settlement and a fill, the engine applies
funding to the position carried into the instant first. It then marks equity at
the decision bar’s mark close, runs sizing and risk gates, and finally executes
against the next bar’s trade open.

For signed target leverage `target_lev_1e4`, post-funding equity `E`,
next-trade-open price ticks `P`, `tick_size_micro = T`, and venue step `s`:

```text
raw_abs_qty = floor(abs(target_lev_1e4) * E * Q / (Ls * P * T))
target_qty  = sign(target_lev_1e4) * step0(raw_abs_qty, s)
request_qty = target_qty - current_position_qty
```

Sizing uses the reference open, not the eventual spread-adjusted fill. A
blocked risk check leaves the position unchanged; the engine never clamps a
target to a gate limit.

The target path retains both the raw and step-rounded targets. If the rounded
target already equals the current position but the raw target differs, the
engine emits `OrderCancelled{reason:"qty_rounding"}` for that raw delta. If
both raw and rounded targets equal the current position, there is no order to
record.

## Participation and candidate quantity

For fill-bar trade volume `V`, participation cap `c`, and step `s`:

```text
raw_capacity = floor(V * c / R)
capacity     = step0(raw_capacity, s)
candidate    = step0(min(abs(request_qty), capacity), s)
```

Capacity and quantity are non-negative in this calculation. If capacity is
zero, the current engine cancels the whole request as `participation_cap`.
Otherwise, an accepted candidate executes once and any
`abs(request_qty) - candidate` overage is emitted as a
`participation_cap` cancellation. Cancelled excess never carries forward.

## Cost profiles, price, and fee

For each profile multiplier `m`, each non-negative base cost rate is scaled
independently:

```text
scaled_rate = ceil(base_rate * m / Ms)
```

V1 requires `primary` and `stress_2x`. The pack convention is `10000` and
`20000`, respectively. The multiplier applies to taker fee, half spread, and
impact coefficient; it does not alter volume, participation cap, quantity
step, or minimum notional.

For reference-open ticks `P`, the `linear` and `sqrt` pack models are:

```text
half_spread_ticks = ceil(P * scaled_half_spread_1e8 / R)
participation     = candidate / V

linear impact ticks =
    ceil(P * scaled_impact_coeff_1e8 * candidate / (R * V))

sqrt impact ticks =
    ceil(P * scaled_impact_coeff_1e8 / R * sqrt(candidate / V))
```

The square-root expression is evaluated by exact integer inequalities, with
one final ceiling. Zero quantity or a zero coefficient produces zero impact.

The taker receives the adverse side:

```text
buy_fill_ticks  = P + half_spread_ticks + impact_ticks
sell_fill_ticks = P - half_spread_ticks - impact_ticks
```

A modeled sell price must remain positive. Executed notional and fee are:

```text
notional_micro = floor(candidate * fill_ticks * T / Q)
fee_micro      = ceil(notional_micro * scaled_taker_rate_1e8 / R)
```

Signed slippage stored in the event is
`floor((fill_ticks - P) * R / P)`. When the action supplies
`max_slippage_bps`, the candidate is rejected if
`abs(slippage_1e8) > max_slippage_bps * 10000`; equality passes.

## Cancellation order

The current runner evaluates a non-blocked, non-zero request in this order:

1. zero participation candidate → cancel the whole request as
   `participation_cap`;
2. candidate notional below `min_notional_micro` → cancel the whole request as
   `min_notional`;
3. modeled absolute slippage above the agent limit → cancel the whole request
   as `max_slippage_exceeded`;
4. otherwise execute the candidate and separately cancel only a participation
   overage.

A wholly cancelled request charges no fee and changes no position. The frozen
`qty_rounding` reason is described in the sizing coverage note above.

## Position accounting

Fees are charged to cash at execution. A reduction realizes price PnL on the
closed quantity at the modeled fill. A flip realizes the entire old side and
sets the remaining opposite-side entry to this fill. An increase on the same
side uses quantity-weighted entry ticks and floors the exact quotient. That
weighted-entry rounding is deterministic current behavior but is not
independently exercised by the zero-impact golden fixture.

`fill_cost_micro` accumulates the absolute difference between executed
notional and same-quantity reference-open notional. It is spread-plus-impact
cost only; fees are reported separately.

## Margin and forced liquidation

Margin and liquidation use the mark series, never the trade or index series.
The maintenance tier is the first tier whose notional cap is at least the
position’s mark notional.

For entry ticks `Epx`, target-leverage magnitude `a`, and maintenance rate
`mm`:

```text
long_liq  = ceil(Epx * (a - Ls) / a / (1 - mm / R))
short_liq = floor(Epx * (a + Ls) / a / (1 + mm / R))
```

A long at or below 1x has no reachable positive trigger and records liquidation
ticks `0` with distance `R`. Isolated margin is
`floor(entry_notional * Ls / a)`. Maintenance margin is
`ceil(mark_notional * mm / R)`.

A long liquidates when mark low is less than or equal to its trigger; a short
liquidates when mark high is greater than or equal to its trigger. The close is
the adverse mark extreme itself, preserving a full gap-through. The liquidation
penalty is `ceil(close_notional * penalty_rate / R)`, the trigger formula
excludes that penalty, and no separate taker fee is charged. The close notional
counts toward turnover but not ordinary fill cost. Liquidation is terminal and
preempts the drawdown kill switch.

For a surviving position, distance uses current price as denominator and floor
rounding. The close uses mark close; the intrabar minimum uses mark low for a
long and mark high for a short. `NearLiquidation` is emitted only when that
intrabar distance is strictly below `5000000` and the trigger was not crossed.
