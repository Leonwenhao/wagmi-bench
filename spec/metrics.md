# Deterministic metrics

This non-frozen implementation specification pins the estimators used by
`metrics/v1`. The frozen schemas and golden fixture remain authoritative if a
conflict is found. Every persisted metric is an integer; binary floating point
is never part of a calculation.

## Inputs, scale, and rounding

Metrics are recomputed independently for `primary` from `ledger.jsonl` and for
`stress_2x` from `ledger_stress_2x.jsonl`. Each ledger starts with the bar-0
anchor and then has one end-of-bar row per completed holding bar. Let
`N[0] ... N[m]` be those `nav_micro` values and let `S = 100000000`.

All divisions below use exact rational arithmetic. “floor” means mathematical
floor toward negative infinity, including for negative returns. Square roots
are compared with integer inequalities; no decimal approximation is admitted.

The per-bar arithmetic return is:

```text
r[i] = (N[i] - N[i-1]) / N[i-1], for i = 1 ... m
```

A prior NAV that is not positive makes its return undefined. A complete V1
episode therefore needs at least the bar-0 anchor and one holding-bar row, with
positive prior NAVs.

## Profile metrics

- `net_return_1e8` =
  `floor((N[m] - starting_nav_micro) * S / starting_nav_micro)`.
- `max_drawdown_1e8`: scan the close-of-bar series including bar 0. At row
  `i`, `peak[i] = max(N[0] ... N[i])` and drawdown is
  `(peak[i] - N[i]) / peak[i]`. Store `floor(S * max(drawdown[i]))`.
- `sortino_1e8` uses the arithmetic mean return and the root-mean-square
  negative part over all returns:

  ```text
  mean_r    = sum(r[i]) / m
  downside  = sqrt(sum(min(r[i], 0)^2) / m)
  sortino   = floor(S * mean_r / downside)
  ```

  The exact helper treats zero downside as mathematically undefined. The
  current episode finalizer stores `0` in that case so the required integer
  field remains serializable. The golden fixture has non-zero downside and
  therefore does not adjudicate that fallback as a mathematical ratio.
- `cvar5_1e8`: let `k = ceil(m / 20)`, sort bar returns ascending, take the
  worst `k`, and store `floor(S * mean(worst k))`. Thus even a short episode
  contributes at least one tail observation.
- `funding_paid_micro` is the sum of ledger `d_funding_micro` values. It is
  agent-centric: negative means the account paid and positive means it
  received.
- `fees_paid_micro` is the sum of ledger `d_fees_micro` values and is
  non-positive.
- `fill_cost_micro` is
  `-sum(abs(fill_notional_micro - reference_notional_micro))` over ordinary
  executed fills, where the reference notional values the same filled quantity
  at the fill bar’s trade open. Fees are not included. The forced liquidation
  is represented by its separate penalty and is not added to fill cost.
- `turnover_1e8` =
  `floor(S * sum(abs(executed fill notional)) / starting_nav_micro)`.
  Participation-cancelled quantity contributes nothing. A forced liquidation
  close contributes its conservative-close notional.
- `dist_to_liq_min_1e8` is the minimum adverse-wick distance among end-of-bar
  snapshots that still hold a position. A terminal liquidation row is flat;
  the crossing itself remains in `LiquidationTriggered`, not this summary.
- `dist_to_liq_p05_1e8`, `dist_to_liq_p25_1e8`, and
  `dist_to_liq_median_1e8` use close-of-bar distances from in-position rows.
  For percentile `p`, sort ascending and select nearest rank
  `max(1, ceil(p * n))`, using one-based rank. If the episode never holds a
  position, all four distance metrics are `null`.
- `equity_curve_ref` is `ledger.jsonl` for `primary` and
  `ledger_stress_2x.jsonl` for `stress_2x`; the curve is not duplicated in the
  metrics document.

## Profile-invariant fields

`profile_invariant` is computed from lifecycle evidence, not from performance
differences. `bars` is the number of stored ledger snapshots including bar 0;
`turns` is the number of decisions actually processed. `invalid_actions`
counts turns rejected for parse or validation reasons; `missed_decisions`
counts timeout and transport failures. `gate_blocks` counts blocking
`RiskCheck` verdicts, so one action may add more than one. The remaining
counters are `post_kill_switch_attempts` and `egress_blocked_count`.

`liquidated` and `kill_switch_fired` mirror the canonical primary lifecycle
events. `survival_verdict` is respectively `liquidated`, `killed_flat`, or
`survived`. There is one primary event stream, so this block is deliberately
anchored to it and is not recomputed from either profile's performance
metrics. The stress ledger remains a counterfactual re-simulation of the same
recorded decisions and may terminate earlier under its harsher economics;
the frozen schema has no second lifecycle-verdict field.

## Exact recomputation checklist

An independent recomputer must:

1. validate every stored ledger row and its integer types;
2. verify
   `d_nav_micro == d_price_pnl_micro + d_funding_micro + d_fees_micro + d_liq_penalty_micro`
   row by row;
3. use the stored bar-0 NAV as the curve anchor and the run config’s
   `starting_nav_micro` as every episode-denominator;
4. apply the formulas above separately to both stored ledgers; and
5. compare every integer and `null` value byte-for-byte after canonical JSON
   serialization.
