---
name: perps-leverage-discipline
description: Position sizing and survival discipline for BTC perpetual futures under leverage, funding, and liquidation constraints.
---

## Objective

You decide one leverage target per turn for a BTC-USDT perpetual futures
account. Survival outranks profit: an account that is liquidated or breaches
the drawdown kill switch has failed regardless of prior gains.

## Sizing rules

- Never exceed 1.0x absolute leverage unless the last four bars all moved in
  your direction; never exceed 2.0x under any circumstances.
- When realized volatility over the visible bars is high (average absolute
  bar-to-bar close change above 2%), halve your target leverage.
- When your unrealized drawdown from the episode's peak NAV exceeds 10%,
  target at most 0.5x until NAV recovers.
- Prefer flat (0) over a low-conviction position: funding and churn are real
  costs, but only enter flat when the trend is genuinely unclear — sitting
  out every turn is not a strategy.

## Regime reading

- A sequence of lower closes with rising volume is a downtrend: prefer short
  targets. Higher closes with rising volume: prefer long targets.
- After a bar whose high-low range exceeds 5%, treat the market as
  dislocated: reduce exposure for the next two turns.
- Do not reverse direction on a single bar; require two consecutive closes
  against your position before flipping sign.

## Output contract

- Emit exactly one action/v1 JSON object and nothing else.
- Leverage targets are signed decimal strings with at most 4 decimal places
  (for example "0.5", "-1", "0").
- Keep the comment field under 80 characters.
