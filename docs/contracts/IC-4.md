# IC-4 — Engine Event Stream (engine → recorder)

**Schema:** [`event/v1`](../../spec/schemas/event.v1.schema.json)
**Status:** FROZEN v1 (founder sign-off 2026-07-26, DECISIONS.md #7). Changes now follow the VERSIONING.md migration procedure with written impact assessment.
**Complete field reference:** [`field-reference.md`](field-reference.md) — generated from the schema descriptions (`spec/tools/gen_field_reference.py`), CI-gated for 100% description coverage, render currency, and reverse-drift (SCH-2). This doc is the curated narrative; the generated reference is the exhaustive one.

## Purpose

`events.jsonl`: ordered, append-only, one JCS-canonical line per event. **The event stream is the canonical primary record**; decision records (IC-5) are a materialized view over it, and on any conflict events win. Every line is individually hashed and chained (IC-5 chain). Timestamps are **real** UTC epoch-ms — bundles are trusted-side evidence; only observations are anonymized.

## Envelope

| Field | Type | Semantics |
|---|---|---|
| `schema` | const `event/v1` | Per-line self-description (chained records may be extracted standalone) |
| `seq` | int ≥ 0 | Monotonic, contiguous, recorder-assigned. THE total order; chain index; tamper errors name a seq |
| `ts` | epoch-ms | Virtual clock at emission (harness's copy for harness events) |
| `turn` | int \| null | Owning decision turn. Turn n owns: its observation, responses, gates, next-bar-open fills, and the funding/margin flows of its holding bar. Null for run-scoped events |
| `bar_index` | int \| null | Decision bar at emission |
| `source` | `engine` \| `harness` \| `recorder` | Emitting authority — who witnessed the fact |
| `type` | closed 15-member enum | Discriminates the typed payload union |
| `payload` | per-type object | See table |

Why `seq`+`ts`+`turn` all three: seq gives total order and chain addressing, ts gives economic time, turn gives the audit's unit of account; any one alone forces inference.

## Event vocabulary (V1 complete; payloads fully specified in the schema)

| `type` | Evidence role | Key payload fields |
|---|---|---|
| `ObservationEmitted` | *saw* | `observation_sha256`, `observation_ref` — binds the stored observation into the chain |
| `AgentResponded` | *said* | `attempt`, `raw_ref`, `raw_sha256`, `raw_bytes`, `latency_ms`, `http_status`, `token_usage`, `transport` — verbatim text content-addressed into `raw/` |
| `ActionParsed` | *meant* | `intent_kind` (canonical discriminator, constant `leverage_target` in v1 — the IC-3 seam; additive-tolerant enum), canonical `target_lev_1e4` map, `max_slippage_bps` (both conditionally required behind `intent_kind` via `allOf/if/then`, mirroring the wire seam — always present in v1 since `leverage_target` is the sole member; M0 audit round 3), `from_attempt` |
| `ActionRejected` | *meant (failed)* | `reason` (12-member enum), `detail`, `validator_error` (what the retry was told), `attempts` |
| `RiskCheck` | *rules* | `constraint_id`, `constraint_type`, `scope`, `observed`, `limit`, `unit`, `verdict` — one per active constraint per parsed action, pass AND block (SAFE-1). `verdict` is additive-tolerant (VERSIONING.md): a third outcome is a minor bump |
| `OrderFilled` | *happened* | full price decomposition: `ref_open_px_ticks` + `half_spread_ticks` + `impact_ticks` → `fill_px_ticks`, `notional_micro`, `fee_micro`, `slippage_1e8`, `requested_qty_base_1e8` vs `qty_base_1e8`, `cost_profile: "primary"` |
| `OrderCancelled` | *happened* | `reason` ∈ {`participation_cap`, `min_notional`, `max_slippage_exceeded`, `qty_rounding`}, requested/cancelled qty, structured `detail`. Partial cap-overage = one `OrderFilled` + one `OrderCancelled`; excess never fills (MATH-7) |
| `FundingApplied` | *cost to hold* | `settlement_ts`, `rate_1e8`, `index_px_ticks` (notional on INDEX, never mark/trade — MATH-4), position, signed `amount_micro` |
| `MarginUpdate` | *cost to hold* | mark close/high/low, position, entry, margin, maintenance, uPnL, `liq_px_ticks`, `dist_to_liq_1e8`, `min_intrabar_dist_to_liq_1e8` (the wick near-miss). Emitted per market at every bar close with a position, and after every fill/funding/liquidation |
| `NearLiquidation` | *near-death* | `market`, `trigger` (`mark_high`\|`mark_low`), `mark_extreme_px_ticks`, `liq_px_ticks`, `min_intrabar_dist_to_liq_1e8`, `threshold_1e8` (V1: 5000000 = 5%). Emitted when the holding bar's adverse mark extreme comes within the threshold of the liquidation price **without crossing**; feeds the C5.1 near-death timeline (added to IC-4 at the C0.3 reconciliation) |
| `LiquidationTriggered` | terminal | `trigger` (`mark_high`\|`mark_low`), trigger/liq/close prices (close at conservative bar side), `penalty_micro`, `loss_micro` (MATH-3, incl. gap-through) |
| `KillSwitchTriggered` | safety | drawdown vs limit, peak/current NAV, `flatten_order_seqs` (forced fills referenced by seq, not duplicated) |
| `PostKillSwitchAttempt` | discipline | attempted `target_lev_1e4`, `raw_sha256` — the violation itself is evidence (SAFE-3) |
| `EgressBlocked` | isolation | `destination`, `port`, `protocol`, coalesced `count`; `source: "harness"` (ISO-2) |
| `EpisodeEnd` | sentinel | `reason` ∈ {`completed`, `liquidated`, `aborted`}, `final_turn`, `final_nav_micro`, `metrics_sha256`. Always last; a stream without it is truncated (SCH-3) |

## Ordering rules

Within a turn, lifecycle order is normative and recorded order = execution order (`seq` is the law):

```
ObservationEmitted → AgentResponded(×1..2) → (ActionParsed | ActionRejected)
  → FundingApplied* (settlement coinciding with the fill instant — see below)
  → RiskCheck × active constraints (only after ActionParsed)
  → (OrderFilled | OrderCancelled)* → FundingApplied* (settlements inside the holding bar)
  → MarginUpdate* → NearLiquidation?
  → (LiquidationTriggered | KillSwitchTriggered)? → PostKillSwitchAttempt?
```

**Coinciding-instant rule (pinned by the golden fixture, C0.3b):** a funding settlement stamped exactly at the holding bar's open — i.e. at the fill instant — settles on the position carried *into* the instant, **before** that turn's sizing/gates/fill, and is recorded before them (funding → sizing/gates → fill). Settlements strictly inside the holding bar follow the fills as shown.

The recorder appends and fsyncs in seq order, each line together with its chain link (see IC-5 crash safety).

## Design decisions (synthesis resolutions)

1. **`AgentResponded.raw` inline → content-addressed `raw/NNNN-aK.txt` + `raw_sha256`** (trader + determinism over commerce's inline-with-selector-redaction): deleting a blob file leaves every chained record hash, link, and root valid — redaction (SCH-4) falls out of the storage design instead of requiring field-surgery verification rules. Human-legible `NNNN-aK` naming (trader) over hash-named blobs: auditors browse by turn; hashes still bind content.
2. **`slippage_1e8` replaces the sketch's `slippage_bps`** in `OrderFilled` (trader): global 1e-8 rate-unit rule; bps derivable exactly (÷10000). Agent-facing tolerance stays bps for ergonomics.
3. **`OrderCancelled.reason` extended** with `max_slippage_exceeded` (the sketch's own `max_slippage_bps` was otherwise unenforceable-in-evidence) and `qty_rounding` (determinism).
4. **`RiskCheck` payload is the flat constraint-verdict row** (commerce shape): `constraint_id` as data + closed `constraint_type` enum = CI-assertable gate coverage now, commerce gates later by minor vocab bump.
5. **Gates run only on parsed actions** (determinism/commerce majority; trader's evaluate-hold-current-on-missed-turns dropped): a gate verdict on an action nobody proposed is noise; missed turns are evidenced by `ActionRejected` + empty `rules`.
6. **Closed `type` enum per major, extension by minor bump** with dev-plan names kept verbatim (commerce's conscious non-deviation): the names are the blueprint's public vocabulary; commerce neutrality lives in payloads (`constraint_id`, `unit`) where it is cheap.
7. **Envelope (`seq`/`turn`/`bar_index`/`source`) and the full `MarginUpdate` payload** are new specification completing the dev-plan's `{...}` placeholders, including `min_intrabar_dist_to_liq_1e8` (trader) — near-death on the wick is evidence even when the close was safe.
8. **Single-profile event stream** (`cost_profile: "primary"` const): the stress path is a stored ledger re-simulation (IC-5), not a second event stream — one source of truth for what happened, two for what it would have cost.
9. **Conformance-verdict extensibility (M0 audit resolution of the IC-4 breaking-risk finding).** `RiskCheck.verdict` (and its `decision_record.rules` mirror) is now on the VERSIONING.md additive-tolerant list: a future third outcome for commerce mandate flows (`step_up` / `requires_user_authorization` / `deferred`) is a **minor** bump, not a chain-rippling major. `observed`/`limit` are deliberately numeric-only in v1 (every V1 constraint is scalar); a non-scalar conformance value (observed merchant vs allowed set) arrives as an **optional additive payload field** — also a minor bump under the policy. No speculative field shape is reserved now; the documented seam is the commitment.
10. **Golden-fixture event scope (M0 audit round 2).** The lifecycle above is normative for every engine-emitted stream, but the C0.3 golden fixture's `expected/*/events.jsonl` asserts only the **economic projection** of it: `ActionParsed`, `ActionRejected`, `RiskCheck`, `OrderFilled`, `OrderCancelled`, `FundingApplied`, `NearLiquidation`, `LiquidationTriggered`, `EpisodeEnd`, with `seq` renumbered contiguously over that subset. `ObservationEmitted`/`AgentResponded`/`MarginUpdate` are out of the fixture's scope because it stores no observation files or `raw/` blobs to hash (pinning fabricated `observation_sha256`/latency values would poison the oracle), and MarginUpdate's state is asserted per-bar by the fixture's ledger rows. Engine conformance to the fixture = filter-to-subset + renumber + byte-diff (the exact rule is normative in `fixtures/golden-mini/scenario.md` §"Event-stream scope" and CI-locked); the **full-stream** byte oracle, including the per-turn `ObservationEmitted → AgentResponded → …` lifecycle and MarginUpdate cadence, is produced and reviewed at the C2 engine golden run.
