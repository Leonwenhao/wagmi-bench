# golden-mini expected-output reconciliation (C0.3b)

**Date:** 2026-07-25. **Inputs:** two independent computations of the expected mini-episode
outcomes (`calc-A.json`, `calc-B.json`, preserved verbatim in this directory) over the C0.3a
fixture inputs. **Method:** field-by-field, bar-by-bar diff; every discrepancy and every reported
ambiguity re-derived independently from the pack bytes + protocol rules (third computation, exact
integer/`Fraction` arithmetic, zero floats — `reconcile_check.py`, which also *wrote* the final
`expected/` files through `spec.canonical.canonical_bytes` and verified the invariants on the
written bytes). Review IDs served: MATH-1 (twice-computed, reconciled), MATH-2 (invariants on the
fixture), MATH-3/4/7 (values pinned below).

**Reconciled truth:** `expected/main/` and `expected/variant-liquidation/`
(`ledger.jsonl` 14/6 rows, `events.jsonl` 29/13 events, `metrics.json`), all lines JCS-canonical.

SHA-256 of the reconciled files **(C0.3b shapes — superseded by the §7 re-expression; current
hashes are in §7.6; `reconcile_check.py` now re-emits these exact bytes to
`derivations/c03b-shapes/` so the reconciliation stays reproducible without touching the v1
oracle in `expected/`)**:

| file | sha256 |
|---|---|
| main/ledger.jsonl | `e02378b24d8bcbdbb6fdb320577a5fb5d519b0d0cbf5c2fdad989181bbf4d896` |
| main/events.jsonl | `fbbc2adf07c17e46ebbb1e5223cec985dbcb58854a94bc0b3b1495a41308526b` |
| main/metrics.json | `7d529687f7a42a11d647669211965bb2d3883f83d099b2c66aea3cb72e4c30dd` |
| variant-liquidation/ledger.jsonl | `eea3c63b206e5d30bb1303d022492153216eb333970b2920e1b6513e7e54be9c` |
| variant-liquidation/events.jsonl | `d98141a01d6dee469fbbda421de9de129c73fe3891d84b6aa1265714d514c950` |
| variant-liquidation/metrics.json | `21aca5841fd3004a949fe4faf21af508311c757bfa2aea6566d1b6efe51b6d6e` |

## 1. Headline agreement

A and B agreed exactly on: all cash/NAV/fee/funding/realized numbers through bar 8 of both
episodes; both funding settlements (0 at 08:00, 26,507,730 micro paid by the long at 16:00 on
index 943,000); the participation-cap partial fill (0.3 of 0.495 BTC); the t4 rejection; the
entire variant liquidation economics (close at mark low 660,000, realized −7,831,500,000, penalty
151,800,000, final NAV 2,006,344,825, net −79,936,552 ×1e-8, max DD 79,938,548 ×1e-8). Both used
funding-first ordering, ceil fees, mark-extreme forced close, penalty on close notional, no taker
fee on the forced close, penalty excluded from the trigger price.

## 2. Discrepancy table (A vs B) and adjudications

Programmatic diff found **75 ledger field discrepancies**, 5 main-metrics discrepancies, and 5
structural event-stream discrepancies. All reduce to **five root causes**:

| # | Root cause | Where | A | B | Adjudication |
|---|---|---|---|---|---|
| D1 | Liquidation-price rounding | liq_px + both distance fields, bars 1–6 (both episodes), 9–12 (main) — 36 field diffs | ceil long / floor short: 600301, 720361, 1347897 | floor long / ceil short: 600300, 720360, 1347898 | **A.** Exact rationals: liq(2x)=50025000000000/83333333=600300.0024; liq(2.5x)=720360.0029; liq(short 1.5x)=1347897.139. Agent-adverse = trigger earlier = ceil for longs (higher), floor for shorts (lower). B's direction is agent-favorable — rejected. Scenario pins "agent-adverse rounding". |
| D2 | Distance fields at 1.0x long (liq unreachable) | main bars 7–8 — 4 diffs | liq 0, distance 100000000 | liq 0, distance null | **A.** `null` must mean exactly "flat" so the schema is unambiguous; an open position always reports distances. liq clamps to 0, distance (px−0)/px = 1e8. |
| D3 | t8 sizing equity: post- vs pre-funding | main bars 9–13, t8/t12 fills, final NAV/metrics — 33 field diffs + 5 metric diffs | post-funding 8,814,821,260 → short 1.400 BTC; final NAV 9,033,549,290; net −9,664,508; fees 31,975,200; turnover 71,055,996,220 | pre-funding 8,841,328,990 → short 1.404 BTC; final NAV 9,034,198,230; net −9,658,018; fees 32,008,860; turnover 71,130,795,860 | **A.** The pinned same-instant order (design-basis note 2, confirmed) is funding → sizing/gates → fill: at the fill instant the account's equity is already post-funding; sizing from pre-funding equity would trade off a stale account state and break the single order-of-operations rule. Exact: floor(1.5×8814821260×1e8/9440000000)=140,066,015 → step-floor 140,000,000 (B's basis gives 140,487,219 → 140,400,000). All downstream deltas (bar-9..13 cash/NAV/fees/margin/unrealized, t12 realized 238,910,000 vs 239,592,600) follow mechanically. |
| D4 | Terminal-bar ledger row diagnostics | variant bar 5 — 1 diff | min_intrabar −9,145,606 + extra fields (`liq_penalty_cumulative_micro`, `terminal`, `note`) | all liq/distance fields null | **B.** One uniform row schema; the row reports end-of-bar state (flat). The crossing depth is not lost: `LiquidationTriggered` carries trigger_px 660,000 vs liq_px 720,361. Penalty lives in metrics + event. |
| D5 | Event-stream structure | events files | `MissedDecision` type; no gate-pass events; single combined `RiskCheckBlock` at t10 | no timeout events at all; per-gate `RiskCheck` incl. passes; two `RiskCheck` blocks at t10 | **Mixed, per contracts.** Timeouts ARE recorded (SAFE-4) but as `ActionRejected{reason: timeout}` — IC-6's wording; A's payload, IC-6's name (B omitting them was wrong). Gate checks are per-gate, pass AND block, for every *parsed* action (IC-4 attempted-vs-executed) — B's shape (A omitting passes was wrong). t10 = two `RiskCheck` events with verdict block. Near-liquidation: both emitted a non-IC-4 event; adjudicated as a real IC-4 gap → `NearLiquidation` added to IC-4 (doc edit below), threshold 5%, values from D1's liq px (min_intrabar 3,307,248 = floor(24639e8/745000)). |

Metric-rounding note: both A and B independently floored max drawdown (exact 11,727,339.29 /
79,938,548.87 ×1e-8); the reconciler's initial agent-adverse-ceil reading was discarded in favor
of the double-agreed uniform rule **"all reported 1e-8 ratio metrics floor"** (net return floor was
already A=B: exact −9,664,507.1 → −9,664,508; a truncate-toward-zero engine would emit −9,664,507
and fail MATH-1 by design).

## 3. Ambiguity resolutions (all flagged items from both agents)

| Ambiguity | Resolution | Why |
|---|---|---|
| Funding-vs-fill same-instant order | **Funding first** (on position carried into the stamp) | Design-basis note 2 was authored to pin exactly this; both agents used it; fill-first would flip the 16:00 cap-hit's sign (short *receives* −39,606,000) and gut coverage item #4 (cap paid by a held long). Now normative in scenario.md. |
| Liquidation close price | **Conservative bar extreme (mark low 660,000)**, full gap-through | Protocol §2 "force-close at the conservative side of the bar" + MATH-3 "incl. gap-through-liquidation-price case": closing at the liq price would make the gap-through case indistinguishable from a touch. Both agents' primary reading. Alternative (close at 720,361 → NAV ≈3.38B) rejected and documented. |
| Penalty basis | **1% of close-price notional**, ceil: ceil(1% × 15,180,000,000) = 151,800,000 | The penalty models the liquidation-engine's cut of the actual forced close; both agents used it. Entry-notional (230,115,000) and liq-price (165,68x,xxx) alternatives rejected — they price a trade that never happened. |
| Taker fee on forced close | **Not charged** | Penalty is *in lieu of* fees; IC-4 `LiquidationTriggered{penalty}` has no fee field. Both agents. |
| Penalty in liq trigger price | **Excluded** | Design note 1 treats it as an optional ~1% shift; excluded keeps liq px a pure maintenance-margin crossing. Distances in ledger reflect this. Both agents. |
| Sizing reference price & NAV basis | Ref = next-bar trade open (= decision close); equity = post-funding, marked at decision-bar mark close; floor to 0.001 BTC step | See D3. Sizing at ref (not fill) price is why realized leverage lands under target (2.3x vs 2.5x etc.) — intended fixture behavior. |
| margin_used basis | floor(entry-price notional / \|target lev\|), informational | Design note 1 (Hyperliquid-parity user-set leverage). At 2x it exceeds NAV by ~5 USDT (sized at ref, filled at ref+spread) — harmless, documented. Only bars 9–12 fractional: floor(13,209,392,000/1.5)=8,806,261,333. |
| Distance denominator | Current price (close for `distance_to_liq`, adverse mark extreme for `min_intrabar`) | A=B convention; scenario prose's "≈3.4% above liq" used /liq — prose reworded implicitly by pinning: fixture value 3,307,248 ×1e-8. Both <5% so the near-liq property is unaffected. |
| Turnover definition | Summed \|fill notional\| in micro, incl. forced close; ratio = floor(/starting NAV) | A=B on values; variant includes the 15,180,000,000 forced-close notional (38,191,500,000 total). |
| Ledger bar-0 row | **Included** (14/6 rows) | Both agents included it; gives the drawdown series its NAV-10,000 origin and one row per pack bar. |
| Fee rounding | ceil (agent-adverse) | Both agents; fractional at bars 7/9/13 (e.g. 5,860,694.19 → 5,860,695). |
| Metric rounding | Ratios floor | See §2 note. |
| Event turn/bar attribution | Decision-instant events (`RiskCheck`, `ActionRejected`) carry `turn` + decision ts; account events (`FundingApplied`, `OrderFilled/Cancelled`, `NearLiquidation`, `LiquidationTriggered`) carry `bar_index`/`fill_bar_index` + the same instant's ts; `EpisodeEnd` at final bar close, `final_turn` = last delivered turn (12 main / 4 variant) | Merges A's and B's partial conventions; every event self-locates in both the turn grid and the bar grid. |
| Kill switch vs liquidation | Liquidation terminal-preempts; `kill_switch_triggered` false in the variant despite 79.9% DD | Episode is already terminal mid-bar; the kill switch is a close-of-bar risk control. Both agents. |

## 4. Contract/fixture doc changes made (logged; freeze-READY, not frozen — DECISIONS.md #5)

1. `TradeEvolve-Development-Plan.md` §1 IC-4 — two clarifications, one addition:
   decision timeouts recorded as `ActionRejected{reason: timeout}` (the missed-decision record);
   `RiskCheck` emitted per gate for every **parsed** turn; new `NearLiquidation{mark_extreme_px,
   liq_px, distance}` event (both independent computations invented one; the C5.1 near-death
   timeline needs it), threshold 5%.
2. `fixtures/golden-mini/scenario.md` — status flipped to RECONCILED; new **"Reconciled
   semantics"** section pinning the nine rule groups (ordering, sizing, rounding directions, liq
   formula/trigger/close/penalty, distance conventions, event/ledger/metric shapes); coverage
   checklist's pending row marked reconciled.
3. No pack bytes, actions, manifests, or hashes were touched — inputs stand exactly as authored
   (design-basis robustness held: no "adjustment pass" was needed).

## 5. Invariant verification on the final files

`reconcile_check.py` (this directory) recomputes both episodes from pack bytes, diffs A/B, writes
`expected/`, then re-reads the **written** files and checks, bar by bar:

- **INV-1 cash conservation:** Δcash ≡ realized − fees − funding − liq penalty (from events)
- **INV-2 PnL attribution:** ΔNAV ≡ price PnL (Δunrealized + realized) − fees − funding − penalty
- **INV-3 identity:** NAV ≡ cash + unrealized
- **INV-4 metrics-from-ledger:** final NAV, net return, max drawdown, fee/funding totals recomputed
  from `ledger.jsonl` match `metrics.json` exactly (MATH-6 style)

Script output (tail):

```
== invariants over written expected files ==
  main: INV-1 cash conservation OK, INV-2 NAV attribution OK, INV-3 NAV=cash+unrealized OK, INV-4 metrics-from-ledger OK (14 rows)
  variant-liquidation: INV-1 cash conservation OK, INV-2 NAV attribution OK, INV-3 NAV=cash+unrealized OK, INV-4 metrics-from-ledger OK (6 rows)
ALL INVARIANTS: PASS
```

Anchors against the fixture design notes: liq(2.5x)=720,361 vs note-1's "≈720,400" ✓; bar-5 wick
distance 3.31% vs "≈3.4%, <5%, no cross" ✓; worst close-of-bar drawdown 11.73% vs "≈11–12%,
kill switch clear" ✓; variant crosses (660,000 < 720,361) with close 950,000 safe ✓; trade low
750,000 never triggers (mark/trade separation diagnostic) ✓.

## 6. Final headline numbers

**(C0.3b record — current values are these plus the C0.3d funding deltas; see §8.1.)**

| | main | variant-liquidation |
|---|---|---|
| final NAV (micro) | 9,033,549,290 | 2,006,344,825 |
| net return (1e-8) | −9,664,508 | −79,936,552 |
| max drawdown (1e-8) | 11,727,339 (peak 10,000,995,500 → trough 8,828,144,825 @bar 5) | 79,938,548 |
| fees / funding / penalty (micro) | 31,975,200 / 26,507,730 / 0 | 10,355,175 / 0 / 151,800,000 |
| turnover notional (micro) | 71,055,996,220 | 38,191,500,000 |
| liquidations / kill switch | 0 / no | 1 (terminal, bar 5) / no |
| missed / invalid / blocked | 6 / 1 / 1 | 2 / 1 / 0 |

## 7. C0.3c addendum (M0 audit round 1, 2026-07-25): re-expression in the published v1 schemas

**Findings served:** SCH-1/MATH-1 ("golden expected outputs do not conform to the final IC-4/IC-5
schemas") and MATH-2 ("golden ledger omits the delta-NAV attribution decomposition").
**Emitter:** `emit_expected_v1.py` (this directory) — exact integer/`Fraction` arithmetic, zero
floats, deterministic; it re-runs the C0.3b pinned rules, **asserts equality with every §6
headline anchor** (final NAV, fees, funding, penalty, turnover, net return, max DD, missed/invalid
counts, liq prices {600301, 720361, 0, 1347897}, near-liq min distance 3,307,248, variant penalty
151,800,000 / loss −7,983,300,000 / liq px 720,361) before writing a single byte, then validates
**every** emitted line against `event/v1` / `ledger_row/v1` / `metrics/v1`
(`jsonschema.Draft202012Validator`) and re-checks MATH-2 + cash conservation + NAV≡cash+upnl +
metrics-from-ledger on the **written** bytes.

### 7.1 Economics unchanged

No number adjudicated in §§1–6 changed. The primary-profile ledger NAV/cash/fee/funding/realized
series, event economics, and headline metrics are byte-different (new shapes) but value-identical
to the C0.3b reconciliation. The changes are: (a) envelope/field-name mapping into the published
schemas, (b) the four MATH-2 attribution terms stored per row, (c) the previously-missing
`stress_2x` cost-profile re-simulation (required by `metrics/v1`'s dual-profile shape and MATH-5),
(d) placeholder `run_id`s (`run_00000000000000a1` main, `run_00000000000000b1` variant —
`metrics/v1` requires one; documented in `scenario.md`), (e) new pinned metric estimators
(§7.4) for the `metrics/v1` fields C0.3b never carried (Sortino, CVaR5, distance percentiles,
fill cost).

### 7.2 Shape mapping (golden_* → v1)

| golden_* (C0.3b) | v1 (current) |
|---|---|
| `golden_event/v1` flat fields | `event/v1` envelope: `schema, seq (0-based, contiguous), ts, turn, bar_index, source:"engine", type, payload` — economics moved verbatim into `payload` |
| `nav_usdt_micro` / `cash_usdt_micro` / `*_usdt_micro` | `nav_micro` / `cash_micro` / `*_micro` |
| `position_base_1e8` (ledger) | `positions.BTC.qty_base_1e8` (+ `entry_px_ticks`, `mark_px_ticks`, `upnl_micro`, `margin_micro`, `maintenance_margin_micro`, `liq_px_ticks`, `dist_to_liq_1e8`) |
| `filled_qty_base_1e8` | `qty_base_1e8` (OrderFilled payload, which now also carries required `market`, `ref_open_px_ticks`, `half_spread_ticks`, `impact_ticks: 0`, `notional_micro`, `slippage_1e8`, `cost_profile`) |
| cumulative `fees_paid` / `funding_paid` / `realized` only | per-row `realized_pnl_micro` (cumulative, net) **plus** the four Δ-terms below |
| — (absent) | `turn` (null on bar 0 — no turn's holding bar closes it) and `profile` on every ledger row |
| `golden_metrics/v1` single flat block | `metrics/v1`: `run_id`, `claim_label:"survival-stress"`, `profiles.{primary,stress_2x}` (identical key sets), `profile_invariant` |

`FundingApplied.amount_micro` is signed **agent-centric** (negative = paid) per `event/v1`;
the golden shape recorded the paid magnitude. `EpisodeEnd.metrics_sha256` binds the metrics
document (hashes in §7.5). Events remain primary-profile (the event stream is the primary run's
record; stress_2x exists as ledger + metrics projections of the same action trace).

### 7.3 MATH-2 attribution terms (per ledger row, both profiles)

Definitions (integer micro, per holding bar):
`d_fees_micro = −Δfees_cum`, `d_funding_micro = −Δfunding_cum` (negative = paid),
`d_liq_penalty_micro = −Δpenalty_cum`, `d_price_pnl_micro = Δupnl + Δrealized_price`,
`d_nav_micro = Δnav`. Identity asserted on every written row of all four ledgers:
`d_nav ≡ d_price_pnl + d_funding + d_fees + d_liq_penalty`, plus `Δcash ≡ Δrealized_pnl_micro`
and `nav ≡ cash + upnl`. Bar-0 rows carry all-zero Δ-terms. Spot checks: main bar 1
(primary): d_nav 995,500 = d_price 10,000,000 + d_fees −9,004,500; variant bar 5 (stress):
d_nav −7,879,800,000 = d_price −7,728,000,000 + d_liq_penalty −151,800,000.

### 7.4 New pinned metric estimators (exact, floor rounding like all 1e-8 ratios)

- `sortino_1e8` = floor(mean(bar returns) / sqrt(mean(min(r,0)²))) ×1e8, exact rational + isqrt.
- `cvar5_1e8` = floor(mean of worst ceil(n/20) bar returns ×1e8).
- `dist_to_liq_{p05,p25,median}_1e8`: nearest-rank (rank = max(1, ceil(p·n))) over close-of-bar
  distances of in-position bars; `dist_to_liq_min_1e8` = min over intra-bar adverse extremes.
- `fill_cost_micro` = −Σ|fill notional − same-qty notional at ref open| (spread+impact cost).
- `funding_paid_micro` / `fees_paid_micro` are agent-centric (negative = paid), matching
  `metrics/v1`.

### 7.5 stress_2x arithmetic (new; 2× multiplier on taker fee and half-spread, impact 0)

`taker = 90000 ×1e-8` (9 bp), `half_spread = 100000 ×1e-8` (10 bp), same pack bytes, same
scripted action trace, same pinned ordering/rounding rules. Fills re-price (e.g. t0 buy 2.000
BTC: half-spread ticks = ceil(1,000,000×100000/1e8) = 1000 → fill px 1,001,000; notional
20,020,000,000; fee = ceil(×90000/1e8) = 18,018,000 vs primary 9,004,500), so sizing equity paths
diverge and positions differ slightly (e.g. the long carried into the 16:00 stamp is 0.935 BTC
vs 0.937 primary → funding paid 26,451,150 vs 26,507,730 on index 943,000 at the +0.30% cap).
Headline stress numbers (asserted + invariant-checked on written bytes):

| | main stress_2x | variant-liquidation stress_2x |
|---|---|---|
| final NAV (micro) | 8,965,619,693 | 1,984,479,300 |
| net return (1e-8) | −10,343,804 | −80,155,207 |
| max drawdown (1e-8) | 12,051,250 | 80,155,207 |
| fees / funding / penalty (micro) | 63,866,857 / 26,451,150 / 0 | 20,720,700 / 0 / 151,800,000 |
| turnover notional (micro) | 70,963,173,100 | 38,203,000,000 |
| `profile_invariant` | identical to primary (13 turns, 6 missed, 1 invalid, survived) | identical to primary (5 turns, 2 missed, 1 invalid, liquidated) |

The wick liquidation still triggers in stress (2× costs cannot rescue a mark-low crossing);
`profile_invariant` equality between profiles is asserted (MATH-5's "same survival story").

### 7.6 Current file hashes (supersede §"Reconciled truth" table)

| file | sha256 |
|---|---|
| main/events.jsonl (41 events) | `805cbe95d5d680cee19cc4ced6a230df8f7d5294d9b35f7d53ddf861ff6548ab` |
| main/ledger.jsonl (14 rows) | `00124cbbc47515cf11cb8968ba5613c323a1db059c8082b6e0aefc87eb0e8721` |
| main/ledger_stress_2x.jsonl (14 rows) | `df0e40e27a32b39c9143dd5eb3c4ebcf67cd92c8e448bb4f76652448fdb199f2` |
| main/metrics.json | `2e9eb359b9c24a268257317906f753454a02a657d613a42c20695e52450791c7` |
| variant-liquidation/events.jsonl (17 events) | `c8a49ae16b069aaa800f4bc35187da36e89219433b5d4f2fdd681c6df1d4583c` |
| variant-liquidation/ledger.jsonl (6 rows) | `b35d9a5f9ca150dd44df222dc146691ee275ca16f68b5b86a734ccc2ef90c15a` |
| variant-liquidation/ledger_stress_2x.jsonl (6 rows) | `5941ac2828281ffbf693a2399c2fcb5a467e53a10893c83e4520fb62be73e27f` |
| variant-liquidation/metrics.json | `bfd0acd78c8535b61d971007a87b14e09e409d4dee1c49ea219a983bb8687771` |

CI guard: `spec/tests/test_contract_instances.py` re-validates every expected file against the
published schemas and re-checks the MATH-2 identity on the committed bytes, so the fixture can
never again drift from the contracts it is the oracle for.

## 8. C0.3d addendum (M0 audit round 2, 2026-07-26): funding coverage + `intent_kind`

**Findings served:** MATH-1 coverage items #3–4 ("neither funding stamp exercises normal-rate
funding on a live position; the only nonzero flow is exactly at the cap") and IC-3/SCH-1
(`intent_kind` canonical seam). Two changes to the inputs, both recomputed here.

### 8.1 Input change: synthetic venue switched to a 4h funding interval

With the 07:00–21:00 window and 8h stamps at 00/08/16, no reachable stamp can land on a held
position at a normal rate (t0 = 08:00 is the first decision, so the 08:00 position is
structurally zero, and 16:00 must stay at the cap for coverage #4). The synthetic venue now runs
**4h funding** (`interval_ms` 14400000, offsets 00/04/08/12/16/20 UTC) — also exercising the
per-market `funding.interval_ms` field (never a constant, MATH-4/OSS scope). In-window stamps
and pinned rates (`pack/funding.jsonl`, identical bytes in the variant pack):

| stamp (UTC) | ts | rate_1e8 | position carried in | settles |
|---|---|---|---|---|
| 08:00 | 1590998400000 | +10000 | 0 | 0 (empty-settlement edge, unchanged) |
| 12:00 | 1591012800000 | +12500 | +230,000,000 (2.3 BTC long) | **NEW** normal-rate print |
| 16:00 | 1591027200000 | +300000 (cap) | +93,700,000 (1.0x long) | 26,507,730 paid (unchanged) |
| 20:00 | 1591041600000 | −12500 | −140,000,000 (1.4 BTC short) | **NEW** negative-rate print |

New funding arithmetic (exact, `amount = ceil(pos × index_open × TICK / 1e8 × rate / 1e8)`,
agent-adverse ceil; both divide exactly here):

- **12:00** (index open of bar 5 = 996,000 ticks): notional = 230,000,000 × 996,000 × 10,000 /
  1e8 = 22,908,000,000 micro; × 12500/1e8 = **2,863,500 micro paid by the long** (rate > 0).
- **20:00** (index open of bar 13 = 926,000 ticks): notional = 140,000,000 × 926,000 × 10,000 /
  1e8 = 12,964,000,000 micro; × (−12500)/1e8 on a short (pos < 0) = **1,620,500 micro paid by
  the short** (negative rate = shorts pay).

**Every fill is byte-identical to the C0.3b reconciliation.** The 12:00 payment lowers the
sizing equity at t6 and t8 by ≤ 2,863,500 micro, which moves the raw target quantity by
< 30,000 ×1e-8 BTC — inside the same 0.001-BTC step-floor bucket at both turns (verified by
re-simulation: t6 sell 136,300,000, t8 sell 233,700,000, t12 buy 140,000,000, all unchanged);
t12 targets flat, so its quantity is equity-independent. Hence fees (31,975,200), turnover
(71,055,996,220), all liquidation prices {600301, 720361, 0, 1347897}, and the near-liq
distance 3,307,248 are all unchanged, and the primary-profile deltas are pure funding:

| anchor (primary) | C0.3b | C0.3d | delta |
|---|---|---|---|
| main funding paid | 26,507,730 | 30,991,730 | +2,863,500 +1,620,500 |
| main final NAV | 9,033,549,290 | 9,029,065,290 | −4,484,000 |
| main net return (1e-8, floor) | −9,664,508 | −9,709,348 | exact: (9,029,065,290−1e10)×1e8/1e10 = −9,709,347.1 → −9,709,348 |
| main max drawdown (1e-8, floor) | 11,727,339 | 11,755,971 | trough bar 5: 8,828,144,825−2,863,500 = 8,825,281,325; peak 10,000,995,500 unchanged; exact 11,755,971.28 → 11,755,971 |
| variant funding paid | 0 | 2,863,500 | 12:00 settles before the bar-5 liquidation (same-instant rule §3) |
| variant final NAV | 2,006,344,825 | 2,003,481,325 | −2,863,500 (realized −7,831,500,000 and penalty 151,800,000 unchanged) |
| variant net return | −79,936,552 | −79,965,187 | exact −79,965,186.75 → floor −79,965,187 |
| variant max drawdown | 79,938,548 | 79,967,181 | exact (10,000,995,500−2,003,481,325)×1e8/10,000,995,500 = 79,967,181.997 → 79,967,181 |

Stress_2x recomputed by the same emitter under the same rules (its sizing DOES cross a step
boundary at t8 because its equity path differs): main final NAV 8,960,998,168, fees 63,850,027,
funding 30,928,205, turnover 70,944,473,280; variant final NAV 1,981,615,800, funding 2,863,500.
All MATH-2 / cash-conservation / NAV≡cash+upnl / metrics-from-ledger invariants re-checked on
the written bytes (emitter output "ALL EMIT + VALIDATE + INVARIANT CHECKS: PASS");
`test_golden_funding_coverage` (CI) now asserts the normal-rate-on-position, at-cap, and
negative-rate-on-short prints exist in the committed oracle bytes.

**Historical artifacts:** `calc-A.json`, `calc-B.json`, `reconcile_check.py`, and
`derivations/c03b-shapes/` remain the verbatim C0.3b record and are pinned to the pre-C0.3d
two-stamp pack (`content_hash sha256:cc80931d…`/`sha256:ea4828fa…`); `reconcile_check.py` is not
re-runnable against the current pack bytes and must not be "fixed" — the current recompute path
is `emit_expected_v1.py` + this section's arithmetic.

### 8.2 `ActionParsed.intent_kind` (IC-3 seam) and event-stream scope

Every `ActionParsed` payload (and `decision_record.meant.action`) now carries the canonical
constant `"intent_kind": "leverage_target"` (IC-3 design decision 6, applied); byte change only,
no economics. `events.jsonl` is now explicitly scoped as the **economic projection** of the
IC-4 stream (scenario.md "Event-stream scope"; IC-4 design decision 10): asserted types only,
`seq` renumbered contiguously; `ObservationEmitted`/`AgentResponded`/`MarginUpdate` are out of
fixture scope (no observation/raw artifacts exist to hash; margin state is asserted by the
ledger rows).

### 8.3 Current file hashes (supersede §7.6)

| file | sha256 |
|---|---|
| main/events.jsonl (43 events) | `66ed61031c07132aad867a4216caab0984f799334550d738cd0fc380bb8d72b6` |
| main/ledger.jsonl (14 rows) | `66204092bba019210b8f9d919b293e290b6924f0b100b87a9f66ec63a78e60e3` |
| main/ledger_stress_2x.jsonl (14 rows) | `4e41ee64b7230e693ee200ccd74956522ca6c44de95ec4b6d0368581335a73d7` |
| main/metrics.json | `ae7c53439fd1338553be64daa51a64a03f20b67ef60fe81b65ee887f42e10bbf` |
| variant-liquidation/events.jsonl (18 events) | `3807e7ded28d4117c02045df3bbe02534f28439e71b0cde1c49b0f31cced2d0a` |
| variant-liquidation/ledger.jsonl (6 rows) | `88a53add1c2b7680728631f6a991750cfd97c780cf835358ac27fe9896d99001` |
| variant-liquidation/ledger_stress_2x.jsonl (6 rows) | `4402ead4f9643b54d86a3f22807e7f629703e2dd9bd8a92d8f76f82ac1e31016` |
| variant-liquidation/metrics.json | `d419498804501eaac46775ff5907c1390ca346c93eb2615e27020d2bea231cd0` |

Pack hashes after the funding change: primary `content_hash`
`sha256:cd49b97b981cd01c1f61fe9c9f2ab4a43c128e21ad00e535d6e4216527b176e9`, variant
`sha256:e21eb5180609351c1bf3aa6f852b3f1eeb04fffb6177cc41e7337855a375bfd5`; `funding.jsonl`
(both packs, identical bytes) `sha256:ee1617d06fd76502a1ed22098db72a68ee3d5702e2161240b868063589305327`
(270 bytes, 4 records).
