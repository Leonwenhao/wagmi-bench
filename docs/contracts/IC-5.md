# IC-5 — Evidence Bundle (recorder → report / replay / world)

**Schemas:** [`bundle_manifest/v1`](../../spec/schemas/bundle_manifest.v1.schema.json) · [`agent_manifest/v1`](../../spec/schemas/agent_manifest.v1.schema.json) · [`decision_record/v1`](../../spec/schemas/decision_record.v1.schema.json) · [`ledger_row/v1`](../../spec/schemas/ledger_row.v1.schema.json) · [`metrics/v1`](../../spec/schemas/metrics.v1.schema.json) · [`chain/v1`](../../spec/schemas/chain.v1.schema.json) · [`redaction/v1`](../../spec/schemas/redaction.v1.schema.json)
**Status:** FROZEN v1 (founder sign-off 2026-07-26, DECISIONS.md #7). Changes now follow the VERSIONING.md migration procedure with written impact assessment.
**Complete field reference:** [`field-reference.md`](field-reference.md) — generated from the schema descriptions (`spec/tools/gen_field_reference.py`), CI-gated for 100% description coverage, render currency, and reverse-drift (SCH-2). This doc is the curated narrative; the generated reference is the exhaustive one.

## Directory layout

```
bundle-<run_id>/
  manifest.json            bundle_manifest/v1   written ONCE at run start, never mutated
  agent_manifest.json      agent_manifest/v1    who decided, frozen config, egress allowlist
  observations/NNNN.json   observation/v1       exactly as served (rebased time), one per turn
  raw/NNNN-aK.txt          verbatim response bytes, attempt K (the redactable store)
  events.jsonl             event/v1             canonical primary stream (IC-4)
  chain.jsonl              chain_link/v1        one link per chained record, fsynced with it
  decisions/NNNN.json      decision_record/v1   THE PRODUCT (derived view, chained)
  ledger.jsonl             ledger_row/v1        primary cost profile, one line per bar
  ledger_stress_2x.jsonl   ledger_row/v1        2x-cost re-simulation of the same action trace
  metrics.json             metrics/v1           both profiles, identical key sets
  chain.json               chain/v1             seal, written ONCE at clean finalize
  redaction.json           redaction/v1         present ONLY in share-profile sub-bundles
```

`NNNN` = zero-padded 4-digit turn index. **Every turn gets an observation file and a decision record, including timeout/missed turns** — a missing file is never ambiguous between "no turn" and "lost evidence".

Additions vs the dev-plan sketch (all three drafts converged): `observations/`, `raw/`, `chain.jsonl`, `ledger_stress_2x.jsonl`, and run config **inlined into `manifest.json`** (see decisions).

## `bundle_manifest/v1` — run identity

Pins: pack (`pack_id`, `content_hash`, `manifest_sha256`), agent (`agent_manifest_sha256`), engine (`engine_version`, exact semver — replay refuses any mismatch, DET-6), schema versions (`spec_versions` map), `episode_id`, `time_rebase_offset_ms` (observation-time mapping, auditor-side), `created_at_ms` (**the single permitted wall-clock read — recorded, never consumed**), `host` (informational, explicitly outside the determinism domain), and the inlined `run_config` (`lookback_bars`, `funding_prints`, `response_deadline_ms`, `seed`, `cost_profiles`, `starting_nav_micro`, `leverage_cap_gross_lev_1e4`, `drawdown_kill_switch_1e8`, `turnover_cap_1e8`).

**Determinism domain:** replay is the pure function `f(pack bytes, manifest.run_config, recorded action/timing events) → (ledger bytes, metrics bytes)`. The economic seed is **complete inside `run_config`** (M0 audit fix): `starting_nav_micro` seeds ledger bar-0 NAV and the `net_return_1e8` denominator, and every gate limit an observation can show (`leverage_cap_gross_lev_1e4`, `drawdown_kill_switch_1e8`, `turnover_cap_1e8` — per-market caps live in the pack descriptor) is recorded per run; replay has no hidden inputs. Wall time, latency, token usage, and raw text are evidence, not inputs.

## `agent_manifest/v1` — who decided

Model id, adapter, exact `endpoint_domains` allowlist (drives egress lockdown; everything else refused AND recorded as `EgressBlocked`), scalar-only `inference_params` (decimals as strings — this file is hashed), `prompt_sha256` (commitment without disclosure), pinned `image_sha256`. **Secret-free by construction (SEC-1): no field in any bundle schema can legally hold a credential.**

## `decision_record/v1` — the product

The per-turn audit spine, deliberately denormalized so an auditor opens one file:

| Block | Question answered | Source events |
|---|---|---|
| `saw` | what it saw | `ObservationEmitted` (hash + ref; bytes stored in-bundle) |
| `said` | what it said | `AgentResponded` per attempt (raw content-addressed; empty array = silence) |
| `meant` | what it meant | `ActionParsed` XOR `ActionRejected` (status-discriminated) |
| `rules` | which rules ran | every `RiskCheck`, pass AND block |
| `happened` | what executed | `OrderFilled`/`OrderCancelled`/`LiquidationTriggered`/`PostKillSwitchAttempt` mirrors |
| `cost_to_hold` | what holding cost | `FundingApplied` mirrors + closing `MarginUpdate` (incl. wick near-miss) |
| `account_after` | why NAV moved | nav/cash/realized + the four ΔNAV attribution terms |
| `event_seq_range` | audit-back link | contiguous seq range consumed |

**Consistency rule (normative, CI-enforced):** decision records are generated by a pure function of the event stream; the replay verifier regenerates each record from events and compares byte-for-byte. Events are canonical; on conflict events win. Both streams are chained, so tampering localizes to a named record either way. *Scope note (M0 audit round 2):* this regeneration check requires a **full** IC-4 lifecycle stream (`saw` ← ObservationEmitted, `said` ← AgentResponded, `cost_to_hold.margin_after` ← MarginUpdate, `event_seq_range.first_seq` = the turn's ObservationEmitted); it runs against real engine bundles from C2 on. The C0.3 golden fixture's events file is an economic projection without those event types (IC-4 design decision 10) and is deliberately **not** an input to this check.

## `ledger_row/v1` — the equity curve

One line per decision bar per profile file. **MATH-2 invariant, checkable from stored bytes by a from-scratch script:**
`d_nav_micro == d_price_pnl_micro + d_funding_micro + d_fees_micro + d_liq_penalty_micro` — exactly, in integer micro-units, every bar; cash conservation to the unit. The Δ-terms are stored, not derived. The bar-0 opening row (NAV = `run_config.starting_nav_micro`, all four Δ-terms zero, `turn: null` — no turn's holding bar closes it) anchors the series. `ledger_stress_2x.jsonl` re-simulates the **same recorded action trace** under the pack's `stress_2x` multipliers — stored data so MATH-5/MATH-6 checks run on bytes.

## `metrics/v1`

Both profiles in one document with **schema-identical key sets** (`additionalProperties:false` + shared `$defs` = "no headline exists only under the optimistic profile" by construction, MATH-5). Survival-native vocabulary: returns, max drawdown, Sortino, CVaR(5%), funding/fees/fill costs, turnover, distance-to-liquidation min/p05/p25/median. Ratio metrics are integer 1e-8 fixed point, floor rounding, estimators pinned in `spec/metrics.md` + golden fixture. `claim_label` propagates from the pack (LABEL-1). The equity curve is referenced (`equity_curve_ref`), never duplicated.

## Chain construction

Two linear chains: `events` (each `events.jsonl` line's bytes, newline excluded) and `decisions` (each `decisions/NNNN.json` file's bytes).

```
run_config_sha256 = SHA256(JCS(manifest.run_config))
genesis(stream)   = SHA256(JCS({schema:"chain_genesis/v1", stream, run_id,
                                pack_content_hash, agent_manifest_sha256, run_config_sha256}))
link_i            = SHA256(JCS({schema:"chain_link/v1", stream, seq: i,
                                record_sha256: h_i, prev: link_{i-1}}))   // prev = genesis for i=0
```

- The chain is **born bound** to run identity, pack, agent, and config — a chain cannot be transplanted between runs.
- `chain.jsonl`: one `chain_link/v1` line per chained record, appended and fsynced **together with its record** — crash safety is a property of the write protocol, not a recovery heuristic.
- `chain.json` (the seal): per-stream genesis/head/count, whole-file hashes of every non-chained file, blob counts, and `root` = SHA-256 of the seal's JCS bytes with `root` removed — the bundle's single publishable identity.
- **Linear chains + full per-record hash links, not Merkle:** V1 verification is always whole-prefix; the first diverging seq NAMES the record (SCH-3) with a ~30-line third-party verifier (ENG-4). The protocol doc's Merkle commitments are Phase-2 multi-party machinery; they can wrap this root later without changing any record format.

## Crash / truncation behavior (three-valued verdict, never a boolean)

| Verdict | Condition |
|---|---|
| **COMPLETE** | `chain.json` exists and validates; every recomputed stream head **and record count** matches the seal's `stream_head` (`verify_chain(..., expected_head, expected_count)` — prefix consistency alone cannot see a rollback where the last N records and links are dropped together; the seal binding is what catches it, DET-2/SCH-3) and every file hash matches; final event is `EpisodeEnd` |
| **TRUNCATED** | `chain.json` absent, but `chain.jsonl` verifies as a contiguous prefix from seq 0 (all record hashes and links match). Reported with last good seq and per-turn artifact inventory — and reported **as a prefix, never as the complete stream** (without a seal, prefix verification cannot exclude rollback) |
| **CORRUPT** | anything else — verifier names the first offending record (`stream`, `seq`, `path`) |

Completeness has two independent markers: `chain.json` presence and the `EpisodeEnd` sentinel. The bundle manifest is immutable (no mutating `status` field — see decisions).

## Redaction (`share` profile)

Raw model text is the only redactable material and lives exclusively in `raw/` blobs referenced by hash from chained records. Redaction = delete listed blob files + write `redaction.json` (path, original hash, bytes, reason, `parent_root`). Every chained hash, link, and root remains valid; the verifier reports listed absences as **PASS-with-disclosure**; an unlisted missing blob = CORRUPT. Verdicts, fills, ledgers, and observations can never be silently dropped (the removals grammar restricts paths to `raw/`). Secrets are never present by construction, so redaction exists only for model text (SEC-1).

## Design decisions (synthesis resolutions)

1. **Events canonical / decisions derived-and-verified** (all three drafts): the redundancy becomes a free determinism check instead of a consistency liability (resolves open Q7).
2. **Incremental `chain.jsonl` + finalize-only `chain.json`** (determinism draft) over recompute-prefix-at-verify (trader) or periodic checkpoints (commerce): per-record fsynced links give the strongest kill-9 story with the simplest verifier.
3. **Run config inlined into `bundle_manifest.run_config`** (synthesis judgment; drafts had a separate `run_config.json`): one hashed document pins the whole replay input, one fewer file/schema, and the genesis still binds it via `run_config_sha256` over the JCS sub-object. Fields preserved verbatim from the drafts.
4. **Immutable manifest, no `status` field** (determinism draft over trader/commerce's `running→complete` mutation): mutating a hashed file is ugly; `chain.json` presence + `EpisodeEnd` are two independent, non-mutating completeness signals.
5. **File-level redaction of `raw/` blobs** (trader + determinism) over commerce's in-record field nulling: no special verification rules for modified records; deletion is the only redaction operation.
6. **`ledger_stress_2x.jsonl` stored** (determinism + commerce): MATH-5's dual reporting and MATH-6's independent recomputation must run on stored bytes.
7. **`account_after` with ΔNAV attribution in the decision record** (trader): "why did NAV drop at bar N" answered in the file an auditor actually opens; identical terms in the ledger keep one derivation, two projections, byte-equality CI-checked.
8. **`observations/` inline copies are not removable** under any V1 redaction profile; only `raw/`.
