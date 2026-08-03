# golden-mini — hand-checkable mini-episode

**Status (C0.3d, 2026-07-26): RECONCILED, re-expressed in the published v1 schemas; funding
coverage extended at M0 audit round 2.** Expected outputs were computed twice by independent
agents (dev-plan convention #3, MATH-1), reconciled field-by-field with a third independent
recomputation (C0.3b), and at the M0 audit re-emitted in the FINAL contract shapes —
`event/v1`, `ledger_row/v1`, `metrics/v1`. The **ledger and metrics files diff byte-for-byte**
against a conforming engine's output with no translation layer; the **events file is a defined
economic projection** of the IC-4 stream, diffed byte-for-byte after one mechanical projection
step (see "Event-stream scope" below). They live in
`expected/{main,variant-liquidation}/{events.jsonl,ledger.jsonl,ledger_stress_2x.jsonl,
metrics.json}` (JCS-canonical lines; every line CI-validated against its schema). Every ledger
row carries the MATH-2 delta-NAV attribution terms (`d_nav_micro` = `d_price_pnl_micro` +
`d_funding_micro` + `d_fees_micro` + `d_liq_penalty_micro`, asserted on the written bytes). The
full discrepancy table, adjudications, and pinned-rule rationale are in
`derivations/reconciliation.md` (C0.3c addendum §7 covers the re-expression + the stress_2x
arithmetic); the two raw computations are preserved as `derivations/calc-A.json` / `calc-B.json`;
the emitter is `derivations/emit_expected_v1.py` (deterministic, exact integer/`Fraction`
arithmetic, asserts equality with the reconciled C0.3b headline anchors before writing). The
semantics pinned by reconciliation are in "Reconciled semantics" below — they are freeze-READY,
not frozen (DECISIONS.md #5: founder review precedes the M0 freeze).

**Fixture run ids (placeholders):** `metrics/v1` requires a `run_id`, but a fixture has no
harness-minted one. The pinned placeholders are `run_00000000000000a1` (main) and
`run_00000000000000b1` (variant-liquidation); engines diff their metrics against the fixture
with `run_id` normalized to these values.

## Event-stream scope (economic projection — normative, M0 audit round 2)

`expected/*/events.jsonl` is **not** a full IC-4 lifecycle stream; it is the **economic
projection** of one. A conforming C2 engine MUST additionally emit, per IC-4's ordering rules,
`ObservationEmitted` and `AgentResponded` every turn and `MarginUpdate` at every bar close with
a position (and after fills/funding/liquidation). Those three types are **out of this fixture's
scope by construction**: the fixture stores no observation files and no `raw/` blobs, so any
`observation_sha256`/`raw_sha256`/latency values it pinned would be fabrications, and MarginUpdate's
per-bar margin state is already asserted field-by-field by the ledger rows
(`positions.BTC.{entry_px_ticks, mark_px_ticks, upnl_micro, margin_micro,
maintenance_margin_micro, liq_px_ticks, dist_to_liq_1e8}`).

**Asserted event types (exactly these may appear):** `ActionParsed`, `ActionRejected`,
`RiskCheck`, `OrderFilled`, `OrderCancelled`, `FundingApplied`, `NearLiquidation`,
`LiquidationTriggered`, `EpisodeEnd`.

**Projection rule (mechanical, no judgment):** filter the engine's `events.jsonl` to the
asserted types, preserve order, renumber `seq` contiguously from 0, then diff **byte-for-byte**
(JCS lines). Ledger and metrics files diff byte-for-byte with **no** projection.
`spec/tests/test_contract_instances.py::test_golden_events_validate` locks the scope in: an
omitted type appearing in the fixture is a CI failure. The full-stream byte oracle (all 15 event
types, real observation/raw hashes) is produced by the C2 engine golden run and reviewed there;
see IC-4 design decision 10 and the IC-5 note on decision-record regeneration.

| Path | What it is |
|---|---|
| `pack/` | Synthetic 14-bar 1h BTC pack, valid per IC-1 (`manifest.json` + `bars_1h`/`mark_1h`/`index_1h`/`funding` JSONL, JCS lines, real hashes, real `content_hash`) |
| `actions.jsonl` | Scripted per-turn agent outputs for turns t0..t12 (format below) |
| `episode_config.json` | Run parameters: starting NAV, caps, kill switch, and a convenience restatement of all cost/fee params (manifest is authoritative) |
| `variant-liquidation/` | Same pack except the bar-5 **mark** low crosses the long's liquidation price (wick liquidation, terminal) + the actions file truncated at t4 |
| `expected/<episode>/` | The oracle, in the published v1 shapes: `events.jsonl` (`event/v1`, primary profile), `ledger.jsonl` + `ledger_stress_2x.jsonl` (`ledger_row/v1`, both mandatory cost profiles, MATH-5), `metrics.json` (`metrics/v1`, both profiles, identical key sets) |
| `scenario.md` | This walkthrough |

## Fixed parameters (all integers, agent-adverse rounding pinned later by the fill-model spec)

- 1 tick = 0.01 USDT (`tick_size_micro` 10000) → **1,000,000 ticks = 10,000.00 USDT**
- Window: 2020-06-01 **07:00 → 21:00 UTC** (start_ts 1590994800000, end_ts 1591045200000), 14 × 1h bars, `warmup_bars` 0
- Funding: **4h interval** (synthetic venue — deliberately NOT Binance-BTCUSDT's 8h, so an engine
  must read the per-market `funding.interval_ms` from the pack, never assume a constant; changed
  at M0 audit round 2 to land settlements on held positions), settlements 00/04/08/12/16/20 UTC →
  exactly **four in-window stamps: 08:00 (rate +10000, zero position), 12:00 (rate +12500, held
  long), 16:00 (rate +300000 = the cap, held long), 20:00 (rate −12500, held short)**;
  cap ±300000 ×1e-8 (±0.30%, 2020-era Binance value)
- Starting NAV **10,000 USDT** (10,000,000,000 micro); leverage cap 3x (30000 ×1e-4) per-market and gross; drawdown kill switch 20% (20,000,000 ×1e-8)
- Taker fee 4.5 bp (45000 ×1e-8) on every fill; maker 1.5 bp recorded only
- Half-spread 5 bp (50000 ×1e-8) worst-side; impact coefficient **0** (impact disabled so fills are hand-checkable); participation cap **10%** of fill-bar trade volume, excess cancelled
- Margin: single tier, initial 33333334 ×1e-8 (= ceil(1e8/3)), maintenance 16666667 (= half initial, validator invariant); liquidation penalty 1% (1,000,000 ×1e-8)
- Bars are price-continuous (each open = previous close); trade volume is 50 BTC on normal bars, **3 BTC on bar 3** (the cap-binding bar), 80 BTC on the crash bar 5

## Turn grid

Decisions happen at bar closes; a decision at the close of bar *k* fills at the open of bar *k+1*
(next-bar-open, conservative side). Turn t*k* = close of bar *k* = `START + (k+1)×1h`. 13 turns,
t0 (08:00) … t12 (20:00). **Odd turns are scripted timeouts** (the agent never answers): a missed
decision per SAFE-4 — position carried unchanged, no gate events, recorded. This is how the fixture
"holds" between scripted moves without emitting re-balancing orders (any restated numeric target
would re-trade, since NAV drifts), and it adds missed-decision coverage for free.

### `actions.jsonl` format (fixture-local, not an IC artifact)

One JCS line per turn: `{"schema":"scripted_turn/v1","turn":k,"mode":"respond","body":"<raw HTTP
response body>"}` or `{"schema":"scripted_turn/v1","turn":k,"mode":"timeout"}`. `body` is the
verbatim string the scripted agent returns from `/decide`; on the IC-6 single retry after a parse
failure the scripted agent returns **the same bytes again**. All `respond` bodies except t4
validate against `action/v1`; t4's body is by design not JSON. `scripted_turn/v1` is defined only
by this file — it is harness-script input, not a spec schema.

## Walkthrough (what each turn exercises)

| Turn (UTC) | Scripted output | Expected mechanics exercised (qualitative — numbers come from the twice-computed ledger) |
|---|---|---|
| **t0** 08:00 | `target {"BTC":"2"}` | Open long 2.0x. Buy fills at bar-1 open 1,000,000 ticks + worst-side half-spread, taker fee charged, ≈2 BTC ≪ 5 BTC cap → full fill. **Fill instant coincides with the 08:00 funding stamp** (see ordering note below). Gates G1/G2 evaluated and recorded as passes. |
| t1 09:00 | timeout | Missed decision (SAFE-4): position unchanged, no gate events, recorded. Same at every odd turn. |
| **t2** 10:00 | `target {"BTC":"2.5"}` | Raise to 2.5x. Top-up buy (≈0.5 BTC) fills at bar-3 open; bar 3 volume is only 3 BTC so the 10% participation cap admits **0.3 BTC — partial fill, excess cancelled, never filled** (MATH-7). Realized leverage lands ≈2.3x, below target. |
| **t4** 12:00 | `go long 3x now!!` | Malformed non-JSON body → V2 `invalid_json`, IC-6 retry echoes the validator error, agent repeats the same bytes → `ActionRejected{invalid_json, attempts: 2}`. **Position unchanged; a rejection is data, not a crash** (SAFE-2). No gate events this turn. **The 12:00 funding stamp (rate +12500, a NORMAL uncapped rate) settles on the carried 2.3 BTC long** — the long pays 2,863,500 micro on index notional at 996,000 (coverage: normal-rate funding magnitude on a live position; also proves funding applies on turns with no parsed action). |
| — bar 5 12:00–13:00 | — | **Near-liquidation, no cross:** the mark series wicks to **745,000 ticks (7,450.00)** and closes back at 950,000. Design basis (below) puts the ≈2.3x long's liquidation price ≈720,400 ticks → the wick comes within ≈3.4% of liquidation **without crossing**. The trade-series low is 750,000, so distance-to-liquidation must be measured on **mark**, not trade. NAV at the close (≈11–12% drawdown) stays clear of the 20% kill switch. |
| **t6** 14:00 | `target {"BTC":"1"}` | Reduce to 1.0x: **sell-side fill** at bar-7 open minus half-spread, taker fee, realized loss booked. Reduce-only implicit in target semantics. |
| **t8** 16:00 | `target {"BTC":"-1.5"}` | Flip long→short through zero (one sell crossing the origin). **Fill instant coincides with the 16:00 funding stamp, which is AT the +cap (rate_1e8 = 300000 = +0.30%)**: under the design-basis ordering the 1.0x long carried into the stamp **pays** capped funding on index notional (index open at 16:00 is 943,000 ticks, deliberately ≠ trade open 944,000 — funding must price off **index**). Gates pass (1.5 ≤ 3). |
| **t10** 18:00 | `target {"BTC":"4"}` | Well-formed action exceeding the 3x cap: parses fine (V7 sanity bound is 999.9999), then **G1 per-market and G2 gross record blocks** → whole action blocked, never clamped, never retried; position stays short 1.5x (SAFE-1 attempted-vs-executed). |
| **t12** 20:00 | `target {"BTC":"0"}` | **Fill instant coincides with the 20:00 funding stamp (rate −12500, negative)**: the carried 1.4 BTC short **pays** 1,620,500 micro on index notional at 926,000 (negative rate = shorts pay — sign coverage in both directions) before the flat close. Then buy-to-cover fills at bar-13 open + half-spread + fee. Episode runs to the bar-13 close (21:00) and ends flat. |

Short leg safety: from t8 the path grinds down (short profits); the short's liquidation price is far
above every subsequent mark high. No kill-switch trigger anywhere in the primary episode by design
(worst close-of-bar drawdown ≈12%); kill-switch **firing** is deliberately not this fixture's job
(C2.8 reckless-agent tests own it) — the 20% parameter is still carried in `episode_config.json`.

## variant-liquidation/ (wick liquidation, terminal)

Identical bytes to the primary pack for `bars_1h.jsonl`, `index_1h.jsonl`, `funding.jsonl`; the
**only** series difference is bar 5 of `mark_1h.jsonl`: low **660,000 ticks (6,600.00)** instead of
745,000. That low crosses the long's liquidation price under every candidate margin-rule reading
(see robustness note), while the bar **close (950,000) is back above it** — a pure intra-bar wick
liquidation: a bar-close check would pass; the intra-bar mark-low check must trigger (MATH-3).
Trade low stays 750,000, so an engine wrongly checking liquidation against trade prices fails to
liquidate — diagnostic by construction. Expected behavior: terminal liquidation during bar 5
(12:00–13:00), force-close at the conservative side, 1% penalty, episode flagged. Turn t5 (13:00)
never occurs, so `variant-liquidation/actions.jsonl` is the same script truncated after **t4**
(turns 0–4: open, timeout, raise, timeout, malformed-rejection). Manifest differs only in
`pack_id` (`golden-mini-liq`), `regime_description`, the mark file's hash/bytes, and
`content_hash`.

## Design-basis notes (assumptions the expected-ledger agents must verify first)

These are **input-design assumptions**, not computed expectations. If the reconciled engine
semantics differ, the flagged inputs may need one adjustment pass; everything else stands.

1. **Isolated-margin allocation = filled notional / |target leverage|** (Hyperliquid-parity
   user-set-leverage semantics; the action space *is* target leverage). Under this rule, with entry
   ≈1,000,500 ticks (open + 5 bp) and the position re-margined at 2.5x from t2, the long's
   liquidation price ≈ (6/5) × entry × (1 − 1/2.5) ≈ **720,400 ticks** (+~1% shift if the penalty
   enters the liquidation-price formula). Near-liq wick 745,000 → ≈3.4% above (<5% ✓, no cross ✓).
   *Robustness:* if margin were instead achieved-leverage-based (≈2.3x → liq ≈678,300) the wick is
   ≈9.8% away — no cross, near-liq property softened; if margin were flat-initial-rate (1/3 → liq
   ≈800,000) the 745,000 wick **would cross** — in that (contract-contradicting) reading, raise the
   bar-5 mark low to 820,000. The variant low 660,000 crosses under **all** three readings, and its
   close 950,000 is safe under all three.
2. **Same-instant ordering: funding settles on the position carried into the stamp, before that
   instant's bar-open fill.** Three of the four stamps deliberately coincide with fill instants
   (08:00 = t0's fill: funding on a zero position — the empty-settlement edge; 16:00 = t8's flip:
   the *long* pays the +cap print before going short; 20:00 = t12's flat close: the *short* pays
   the negative print before covering); the 12:00 stamp lands on a no-fill turn (t4's rejection),
   pinning that funding applies regardless of parse outcome. This coincidence is intentional: the
   golden fixture is where the funding-vs-fill order-of-operations gets pinned. If the reconciled
   rule were fill-first, the 08:00 print would hit the fresh 2.0x long and the 16:00 cap print
   would be *received* by the new short — the fixture would still work, but scenario prose and the
   cap-hit's sign would flip; flag before computing.
3. Order sizing (target notional → qty) happens at the engine's reference price and rounds toward
   zero to `qty_step_base_1e8` (0.001 BTC); with NAV ≈10,000 and prices ≈10,000 every scripted
   order clears `min_notional_micro` (10 USDT) by orders of magnitude, and only bar 3 binds the
   participation cap (0.3 BTC = exactly 300 qty-steps, no rounding ambiguity).

## Reconciled semantics (pinned at C0.3b; normative for C2 unless the M0 founder review overrides)

Every rule below was either agreed by both independent computations or adjudicated at
reconciliation (arithmetic in `derivations/reconciliation.md`).

1. **Same-instant ordering (design-basis note 2 CONFIRMED): funding → sizing/gates → fill.** A
   funding stamp coinciding with a fill instant settles on the position carried *into* the instant.
   Consequence at 16:00: the 1.0x long pays the +cap print (26,507,730 micro on index notional at
   943,000) before the flip to short. C0.3d adds: at 12:00 the 2.3 BTC long pays the normal-rate
   print (2,863,500 micro at rate +12500 on index 996,000) with no coinciding fill; at 20:00 the
   1.4 BTC short pays the negative-rate print (1,620,500 micro at rate −12500 on index 926,000)
   before the flat-close fill.
2. **Order-sizing equity is the POST-same-instant-funding equity**, marked at the decision-bar mark
   close; reference price = next-bar trade open (equal to the decision-bar trade close by price
   continuity). `qty = floor_to_qty_step(|target_lev| × equity / ref_px)`. At t8 this gives a
   1.400 BTC short (a pre-funding equity would give 1.404 — the one material A/B divergence).
3. **Rounding is agent-adverse for money, floor for reported ratios.** Fees: `ceil(notional ×
   taker_rate / 1e8)`. Funding: `ceil` of the exact amount (pay more / receive less). Fill price:
   half-spread ticks = `ceil(open × half_spread / 1e8)`, added for buys, subtracted for sells.
   Liquidation price: `ceil` for longs, `floor` for shorts (closer to price either way). All
   reported 1e-8 ratio fields (distance-to-liq, net return, max drawdown, turnover ratio): `floor`
   of the exact rational.
4. **Liquidation price formula** (isolated, target-leverage margining per design-basis note 1):
   long `E·(L−1)/L ÷ (1−mm)`, short `E·(L+1)/L ÷ (1+mm)`, `mm = 16666667e-8`; the 1% penalty does
   NOT enter the trigger price. A long at ≤1x has no reachable liquidation: report
   `liq_px_ticks: 0` and distances `100000000` (never null while a position is open; null means
   flat).
5. **Trigger and forced close:** long liquidates when intra-bar **mark low ≤ liq px** (short: mark
   high ≥); force-close fills at the **conservative bar extreme** (the mark low/high itself — full
   gap-through, MATH-3), penalty = `ceil(1% × close-price notional)`, **no separate taker fee**,
   terminal. Variant: close at 660,000 → realized −7,831,500,000, penalty 151,800,000, final NAV
   2,003,481,325 (C0.3d: includes the 12:00 funding payment of 2,863,500 settled before the bar-5
   liquidation; the pre-C0.3d value was 2,006,344,825).
6. **Distance-to-liquidation denominator is the current price**: long `(px − liq)/px`, short
   `(liq − px)/px`; `distance_to_liq` uses the mark close, `min_intrabar_dist_to_liq` the bar's
   adverse mark extreme. `NearLiquidation` event threshold: distance < 5,000,000 ×1e-8 (5%).
7. **Missed decisions** (timeouts) are recorded as `ActionRejected{reason: timeout}` (IC-6 wording;
   IC-4 note added at reconciliation). Gate `RiskCheck` events (G1 per-market, G2 gross) are
   emitted for every *parsed* action — pass and block both — never for timeouts/invalid turns.
8. **Ledger** = one row per bar (bar 0 included), end-of-bar state; fills at turn t_k belong to the
   row of bar k+1. Fields as in `expected/*/ledger.jsonl` (`golden_ledger_row/v1`); position field
   is `position_base_1e8`; `margin_used = floor(entry-price notional / |target_lev|)`
   (informational). On the terminal liquidation bar the row shows the post-liquidation flat state
   (liq/distance fields null).
9. **Metrics** (`golden_metrics/v1`): net return and drawdowns over the close-of-bar NAV series;
   turnover = summed absolute fill notional including a forced close; `turnover_ratio_1e8` =
   `floor(turnover / starting NAV)`. A terminal liquidation preempts the kill switch
   (`kill_switch_triggered` stays false; `episode_end_reason: liquidated`).

## Coverage checklist

| # | Mechanic | Where | Status |
|---|---|---|---|
| 1 | Buy-side fill (+ half-spread + taker fee) | t0 open, t12 buy-to-cover | ✓ authored |
| 2 | Sell-side fill (+ half-spread + taker fee) | t6 reduce, t8 flip-through-zero | ✓ authored |
| 3 | Funding settlement on a **zero position** (typical rate +10000; edge case — the rate is dollar-inert, amount 0) | 08:00 stamp | ✓ authored (relabeled at M0 audit round 2: this is the empty-settlement edge, NOT a magnitude test) |
| 3b | Funding settlement at a **normal, uncapped rate on a held position** (+12500 on the 2.3 BTC long → 2,863,500 paid) — the dollar-magnitude oracle for ordinary funding | 12:00 stamp | ✓ authored (C0.3d) |
| 3c | Funding settlement at a **negative rate paid by a held short** (−12500 on the 1.4 BTC short → 1,620,500 paid) — sign coverage both directions | 20:00 stamp | ✓ authored (C0.3d) |
| 4 | Funding settlement **at the +cap** (300000 ×1e-8, held long pays 26,507,730) | 16:00 stamp, position held into it | ✓ authored |
| 5 | Participation-cap **partial cancel** (excess cancelled) | t2 on thin bar 3 | ✓ authored |
| 6 | Malformed-output **rejection** (retry, then `ActionRejected`, position unchanged) | t4 | ✓ authored |
| 7 | Risk-gate **block** (recorded, not clamped, position unchanged) | t10 target 4x vs 3x cap | ✓ authored |
| 8 | **Near-liquidation** (<5% above liq on mark low, no cross) | bar 5 mark wick 745,000 | ✓ authored (design basis note 1) |
| 9 | **Wick liquidation** (intra-bar mark low crosses, close safe, terminal + penalty) | variant bar 5, mark low 660,000 | ✓ authored |
| 10 | Flat close + clean episode end | t12 | ✓ authored |
| 11 | Missed decision (timeout → position unchanged, recorded) — bonus | all odd turns | ✓ authored |
| 12 | Mark/trade/index separation diagnostics — bonus | mark-only wick depth (bar 5); index-only 16:00 divergence (bars 8–9) | ✓ authored |
| 13 | Funding-vs-fill same-instant ordering pinned — bonus | 08:00/16:00/20:00 stamps coincide with fills; 12:00 lands on a no-fill (rejected) turn | ✓ authored (design basis note 2) |
| — | Hand-computed expected ledger | computed twice by separate agents, reconciled to `expected/` (see `derivations/reconciliation.md`; C0.3d funding deltas in §8) | ✓ reconciled |

**Coverage limit (stated honestly, M0 audit round 2):** no **runtime cap-clamp** path is
exercised anywhere — funding in this protocol is pure replay of pack-recorded rates (the pack
validator enforces `|rate_1e8| ≤ cap_1e8` at authoring; the engine never clamps at runtime).
The 16:00 print being exactly at the cap tests the cap-*valued* rate's arithmetic, not a
clamping branch, and the 12:00/20:00 normal-rate prints now ensure an engine that wrongly
clamps every rate to the cap (or mishandles cap-vs-passthrough) produces a visible diff.

## Validation performed at authoring

Manifests validate against `pack_manifest/v1`; every series row against `bar_row/v1` /
`funding_row/v1`; every non-malformed action body against `action/v1`. Series lines are JCS
(sorted keys, compact, LF-terminated); per-file `sha256`/`bytes`/`records` and both `content_hash`
values recomputed from stored bytes. OHLC invariants, price continuity, monotonic `ts`,
`available_at` = `ts`+interval (bars) / = `ts` (funding), settlement stamps exactly on declared
offsets, |rate| ≤ cap, maintenance = half initial, zero fractional JSON numbers anywhere.
Primary pack `content_hash`: `sha256:cd49b97b981cd01c1f61fe9c9f2ab4a43c128e21ad00e535d6e4216527b176e9`;
variant: `sha256:e21eb5180609351c1bf3aa6f852b3f1eeb04fffb6177cc41e7337855a375bfd5`
(both recomputed at C0.3d after the funding-interval change; pre-C0.3d values
`sha256:cc80931d…` / `sha256:ea4828fa…` identify the two-stamp pack that
`derivations/calc-A.json` / `calc-B.json` / `reconcile_check.py` were computed against).
