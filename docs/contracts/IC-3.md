# IC-3 — Action (agent → engine)

**Schema:** [`action/v1`](../../spec/schemas/action.v1.schema.json)
**Status:** FROZEN v1 (founder sign-off 2026-07-26, DECISIONS.md #7). Changes now follow the VERSIONING.md migration procedure with written impact assessment.
**Complete field reference:** [`field-reference.md`](field-reference.md) — generated from the schema descriptions (`spec/tools/gen_field_reference.py`), CI-gated for 100% description coverage, render currency, and reverse-drift (SCH-2). This doc is the curated narrative; the generated reference is the exhaustive one.

## Purpose

The agent's decision for one turn: a signed **target-position vector** (target leverage per market — no raw orders, reduce-only implicit), plus optional slippage tolerance, free-text rationale, and token telemetry. The wire form is LLM-ergonomic; the engine canonicalizes it to integers before anything is hashed.

## Wire format

```json
{"schema": "action/v1",
 "intent_kind": "leverage_target",
 "target": {"BTC": "-1.5"},
 "max_slippage_bps": 30,
 "comment": "optional rationale, recorded verbatim in raw/, never interpreted",
 "usage": {"input_tokens": 5200, "output_tokens": 180}}
```

| Field | Type / unit | Semantics |
|---|---|---|
| `intent_kind` | enum, v1 sole member `"leverage_target"`, optional | Intent discriminator (the extensibility seam). Absence = `"leverage_target"`; the enum is **additive-tolerant** (VERSIONING.md), so a future intent kind is a minor bump. `target` is conditionally required exactly when the intent is `leverage_target` (i.e. always, in v1). |
| `target` | map alias → decimal **string** (≤4 fraction digits) or bare JSON integer | Sign = direction, `"0"` = flat, magnitude = leverage on NAV. Fractional JSON numbers are **rejected** (`float_target`) — no float ever touches a money path. Required for `leverage_target` intents (schema-conditional; every v1 action). |
| `max_slippage_bps` | integer bps, 0–10000, optional | Modeled slippage above this ⇒ order cancelled (`max_slippage_exceeded`). Agent-facing bps; stored engine-side as `slippage_1e8` (1 bp = 10000, exact). |
| `comment` | string ≤2000, optional | Intent memo. Lives in the verbatim response bytes (content-addressed `raw/`), **not** copied into the canonical parsed action — keeps rationale redactable under the share profile without touching chained records. |
| `usage` | optional | Self-reported tokens; copied to `AgentResponded.token_usage`; telemetry, not intent. |
| `ext` | optional object | Sanctioned extension point; engine-ignored. |

**Canonical parsed form** (the only action representation ever hashed, in `ActionParsed` / decision records):
`{"intent_kind": "leverage_target", "target_lev_1e4": {"BTC": -15000}, "max_slippage_bps": 30, "from_attempt": 1}` — decimal strings parsed exactly (decimal re-parse of raw text, never via binary float) to 1e-4 integers; wire absence of `max_slippage_bps` canonicalizes to `null`; wire absence of `intent_kind` canonicalizes to `"leverage_target"` (the token is constant across every v1 trading action, so determinism is unaffected). On the canonical schemas (`event/v1 → ActionParsed`, `decision_record/v1 → meant.action`) `intent_kind` and `from_attempt` are unconditionally required while `target_lev_1e4`/`max_slippage_bps` are **conditionally required** behind `intent_kind = "leverage_target"` (`allOf/if/then` — the same seam as the wire form, completed at M0 audit round 3); since `"leverage_target"` is the sole v1 member, every v1 canonical action still carries the full leverage vector byte-for-byte.

## Validation rules (exhaustive, evaluated in this exact order — first failure wins, so the recorded reason is replay-stable)

| # | Stage | Rule | `reason` code |
|---|---|---|---|
| V1 | parse | Body ≤ 65536 bytes, valid UTF-8 | `oversize` |
| V2 | parse | Exactly one JSON document, parseable (trailing garbage fails) | `invalid_json` |
| V3 | parse | `schema` present and equals `action/v1` | `unknown_schema` |
| V4 | parse | Validates against the schema; no unknown top-level members | `schema_invalid` / `unknown_field` |
| V5 | parse | Every `target` key is a market declared in this pack | `unknown_market` |
| V6 | parse | Every `target` value is a decimal string per grammar or a JSON integer; fractional JSON numbers rejected | `invalid_target_format` / `float_target` |
| V7 | parse | Parsed magnitude within the structural sanity bound (\|lev\| ≤ 999.9999) | `target_out_of_range` |
| V8 | parse | `max_slippage_bps` integer in [0, 10000] if present | `invalid_slippage` |
| G1 | gate | \|per-market target\| ≤ market cap → `RiskCheck{lev-<alias>}` | gate block, not rejection |
| G2 | gate | gross Σ\|target\| ≤ account cap (3x) → `RiskCheck{lev-gross}` | gate block |
| G3 | gate | projected turnover ≤ cap (if configured) → `RiskCheck{turnover}` | gate block |
| G4 | gate | kill switch inactive, or target flat → `RiskCheck{drawdown-ks}` (+ `PostKillSwitchAttempt` on block) | gate block |
| — | harness | timeout / transport failure / crash / 5xx | `timeout` / `agent_error` (missed decision, SAFE-4) |

- An unrecognized `intent_kind` value fails V4 (`schema_invalid`): a v1 engine refuses intent kinds it does not know rather than guessing (same posture as unknown schema majors, VERSIONING.md).
- **Parse failures** (V1–V8) on attempt 1 trigger the single IC-6 retry with the validator error echoed; a second failure ⇒ `ActionRejected{reason of final failure, attempts: 2}`. Position unchanged — a rejection is data, not a crash (SAFE-2). Fuzz gate: 10k malformed inputs ⇒ zero crashes, all recorded.
- **Gate blocks are never retried and never clamped**: the action was understood and refused; the whole action is blocked, position unchanged. Retrying a refusal would blur "couldn't speak" vs "asked for too much" — the exact distinction the audit needs.
- `RiskCheck` events are emitted for **every active constraint, pass and block alike**, whenever an action is parsed (SAFE-1). Missed/rejected turns have no gate events (`rules: []` in the decision record).
- ≤ 1 action per turn is enforced by the transport shape: the single HTTP response body is the action; later deliveries are ignored and recorded.

## Design decisions (synthesis resolutions)

1. **Decimal-string targets** (all three drafts) vs the dev-plan `1.5` JSON number: no-floats in hashed payloads is a blocker; strings parse exactly and are at least as easy for an LLM. Bare JSON **integers** are additionally accepted (determinism draft) — integers are exact; only fractional numbers are banned. A fractional number is rejected (not leniently re-parsed, trader draft's tolerance dropped): the one-round-trip retry with the exact error teaches the rule, and leniency would create two wire forms for one intent.
2. **Leverage-cap enforcement moved from parse validation to the gate layer** (all three drafts, flagged deviation from the dev-plan sketch): a cap breach is a well-formed attempt exceeding authority; `RiskCheck{observed, limit, verdict: block}` is strictly richer SAFE-1 evidence than a flat rejection. Net behavior identical (position unchanged).
3. **`comment` and `usage` added** (trader + commerce), both optional and non-breaking; engine behavior identical with or without.
4. **`comment` not duplicated into the canonical action** (judgment call between trader's inline-comment and commerce's comment-hash): the verbatim response bytes already contain it and are content-addressed; duplicating it into chained records would defeat share-profile redaction. Canonical action = intent numbers only.
5. Deterministic short-circuit validation order (determinism draft): two implementations must record the same reason for the same bytes.
6. **`intent_kind` canonical discriminator — the commerce seam, APPLIED at M0 audit round 2 (clears the round-1 breaking-risk finding on this contract's own terms).** Round 1 documented "commerce purchase-intent = a future `action/v2` major"; round 2 held that open as a blocker (a documented-but-unratified future major is exactly the post-freeze cascade DECISIONS #5 exists to prevent) and mandated either the seam or explicit founder ratification. The seam is now in force: the wire form carries an optional `intent_kind` (enum, sole v1 member `"leverage_target"`, absence ≡ that member), `target` is **conditionally required** behind it (JSON-Schema `if/then`; the condition is vacuously true when the member is absent, so every v1 action still requires `target`), and the canonical hashed forms (`ActionParsed`, `decision_record.meant.action`) carry `intent_kind` as a **required constant** — a token that never varies within v1, so replay determinism and canonical byte-stability of trading actions are unaffected (the golden fixture pins the bytes). **Completed at M0 audit round 3:** the round-2 pass added the discriminator to the canonical forms but left `target_lev_1e4`/`max_slippage_bps` *unconditionally* required there (the seam existed only on the wire form — necessary but not sufficient, since a commerce intent cannot carry a leverage vector it does not have and `additionalProperties: false` blocks its payload fields). Both canonical forms now mirror the wire seam exactly: `intent_kind` + `from_attempt` unconditionally required, the leverage pair conditionally required via `allOf/if/then` on `intent_kind = "leverage_target"`. Because `intent_kind` is required on these forms and `"leverage_target"` is its sole v1 member, the conditional always fires in v1 — the set of valid v1 instances and their canonical bytes are unchanged, and the golden fixtures needed no re-derivation. A future commerce `purchase_intent` is then a **minor** bump on every surface, wire and canonical alike: new enum member + its own conditionally-required payload branch + new optional properties (the pattern is on the VERSIONING.md additive-tolerant list) — no `action/v2`, no `decision_record`/event/chain/replay ripple. `action.ext` remains **non-canonical** (recorded verbatim in `raw/` only) and remains *insufficient* for a verifiable signed-mandate purchase — an AP2 cart placed in `ext` never enters the hashed intent; that is what the `intent_kind` branch is for. This also keeps corrected the earlier IC-2 impression: the "zero schema change" mandate→verdict→conformance triple never covered the purchase-decision leg; with the seam, that leg is a planned *minor*, not a major. The founder may still simplify the seam away at the pre-freeze review (DECISIONS #5); until then the contract is freeze-READY with the seam in place.
