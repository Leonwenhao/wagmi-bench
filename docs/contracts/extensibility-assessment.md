# Commerce-Extensibility Assessment (IC-1 … IC-6)

**Mandate:** DECISIONS.md #5 — "contracts designed with commerce-extensibility explicitly assessed at M0." This is that assessment, written by an adversarial auditor who did not author the contracts. The question for each contract is narrow and concrete:

> Could this contract serve a **spending-agent** use — an **AP2-style signed intent mandate** as the *constraint object*, a **purchase decision** as the *action*, and **mandate-conformance verdicts** as the *risk checks* — **without a breaking (major) schema change?**

**Status:** freeze-READY draft review. Assessed **before** freeze so that any *breaking-risk* can be reserved-away with a cheap pre-freeze edit instead of an expensive post-freeze major. Anything rated **breaking-risk is a blocker** by the terms of the mandate.

**AP2 vocabulary used below** (Agent Payments Protocol): an *IntentMandate* is the user's signed authorization defining spending constraints (spend cap, allowed merchants/categories, currency, expiry); a *CartMandate* is a specific proposed purchase (items/SKUs, quantities, unit prices, total, merchant identity); *conformance* is the verdict that a cart satisfies the mandate. The natural mapping onto TradeEvolve:

| AP2 object | TradeEvolve contract | TradeEvolve surface |
|---|---|---|
| Mandate (constraint object) | IC-2 | `observation.risk.constraints[]` |
| Purchase decision | IC-3 | `action/v1` |
| Conformance verdict | IC-4 | `RiskCheck` event (+ mirrored `decision_record.rules`) |
| Merchant / product registry | IC-1 | `pack_manifest.markets` (alias → descriptor) |
| Transaction evidence | IC-5 | bundle / `decision_record` / `ledger_row` |
| Agent transport | IC-6 | runner request/response |

**Verdict legend:** `clean` (serves commerce with at most a minor/additive bump the policy already blesses) · `extension-point-needed` (serves commerce only after a deliberate additive seam is added; no existing V1 field is wrong, but the story is oversold) · `breaking-risk` (the commerce use is impossible without a **major** bump given the schema/policy as written — a **blocker**).

---

## Summary

| Contract | Verdict | One-line reason |
|---|---|---|
| IC-1 Pack / registry | **extension-point-needed** (major) | The alias→descriptor *container* generalizes; the descriptor *contents* are 8 required trade-microstructure blocks — a merchant/product cannot be registered without garbage-filling them or a descriptor major. |
| IC-2 Observation / mandate | **extension-point-needed** (major) | Spend-cap mandates fit (scalar). Allowlist/categorical/equality mandates do **not**: `limit`/`used` are integer-only, the constraint row has no `ext`, and the observation has no `ext` by design. The schema's own claim "allowlists arrive as new rows, not new schema" is **false**. |
| IC-3 Action / purchase decision | **breaking-risk** (BLOCKER) | The canonical hashed intent is leverage-only (`target` required, `ActionParsed.target_lev_1e4` required); `action.ext` is explicitly **non-canonical**. A purchase decision cannot become a first-class verifiable intent without an `action/v2` major. |
| IC-4 Event / conformance verdict | **breaking-risk** (BLOCKER) | `RiskCheck.verdict` is a closed `pass\|block` enum **not** on the additive-tolerant list → any third outcome (step-up / deferred-to-user) is a **major** by the versioning policy; `observed`/`limit` are integer-only in the chained, replayed spine. |
| IC-5 Bundle / ledger | **minor / info** | Evidence spine (chain, `decision_record` skeleton, `ext`) is domain-neutral and reusable. The accounting layer (`ledger_row`, `account_after`) hard-codes the trade MATH-2 identity; a commerce ledger legitimately forks — inherent, not a defect, but not covered by any zero-change claim. |
| IC-6 Runner API | **clean** | Observation-in / action-out, no auth, no trade vocabulary in transport. Genuinely domain-neutral. |

**Net:** two blockers (IC-3, IC-4). Both are cheap to neutralize **before** freeze; both become expensive majors if discovered after. The commerce-readiness that *is* present (RiskCheck→`constraint_id` linkage, per-row `unit`, free-string `scope`, `venue` as a string, `ext` on most schemas, `decision_record.ext`) is real and worth crediting — the gaps below are the places where the "zero schema change" narrative outruns the actual field types.

### Resolution status (M0 audit round 1, 2026-07-25)

| Item | Disposition |
|---|---|
| IC-3 blocker | **Addressed by documentation** (option 2 of the menu): commerce purchase-intent is a *planned `action/v2` major*, stated plainly in IC-3 design decision 6; the IC-2 "zero schema change" impression corrected in IC-2 design decision 2. The `intent_kind` reservation (option 1) is explicitly left to the founder's final contract review. |
| IC-4 blocker | **Fixed**: `verdict` added to the VERSIONING.md additive-tolerant list (a third outcome is now a minor bump); `observed`/`limit` documented as numeric-only with the minor-additive path for non-scalar conformance values (IC-4 design decision 9, mirrored in both schemas' `verdict` descriptions). |
| IC-2 major | **Fixed (text) + decided**: scalar-vs-set distinction now normative in `observation.risk` descriptions and IC-2 design decision 2; no speculative value-set field reserved (the additive path is already a minor). |
| IC-1 major | **Fixed**: "commerce hook" comment reframed in `pack_manifest.market_descriptor` and IC-1 to claim the container only. |
| IC-5 minor | Ledger/metrics layer already documented here as a deliberate per-vertical fork; no zero-change claim exists in IC-5 to correct. |

### Re-audit (M0 audit round 2, 2026-07-26) — adversarial verification against the frozen-draft schemas

Every round-1 disposition above was re-checked against the actual schema bytes by an auditor who did not author the fixes. Confirmed genuinely fixed: **IC-4** (`verdict` is on the additive-tolerant list in both `VERSIONING.md` line 17 and the `RiskCheck.verdict` description — a future `step_up`/`requires_user_authorization` is now a *minor*, not a major); **IC-1** (`market_descriptor.description` now claims only the container generalizes); **IC-2** (the `observation.risk` and constraint-`type` descriptions now state plainly that set/equality mandates need an optional value-set *field*, a minor additive schema change — the false "new rows, not new schema" claim is gone); **IC-5** and **IC-6** unchanged and correct.

**IC-3 is NOT cleared.** The round-1 disposition ("addressed by documentation") does not satisfy the mandate. The mandate for this assessment is explicit: *anything rated breaking-risk is a blocker that must be **fixed** before freeze.* Option 2 (documenting that commerce purchase-intent is a planned `action/v2` major) does not *fix* the breaking risk — it accurately labels it and leaves it live. Verified in the schema bytes: `action/v1` still declares `required: ["schema", "target"]` with `target.minProperties: 1`, and the only canonical, hashed, chained representation of intent — `event/v1 → ActionParsed.target_lev_1e4` **and** `decision_record.meant.action.target_lev_1e4` — is leverage-only and **required** in both. A signed-mandate purchase decision therefore cannot become a first-class verifiable (hashed, chained, replay-covered) intent without an `action/v2` major that ripples into `decision_record`, the event stream, `chain`, and replay byte-equality. That is precisely the breaking change DECISIONS #5 exists to surface *now*, while the edit is cheap.

**IC-3 required disposition before freeze (one of):**
1. **Apply the seam (recommended, the actual fix):** relax `target` to conditionally-required behind an optional canonical `intent_kind` discriminator (trading always emits `intent_kind: "leverage_target"` + `target`; a commerce `purchase_intent` branch is then a future *minor*, not a major). Cost to the trading path: the canonical form gains one always-present token; determinism is unaffected because the token is constant for every trading action. This converts the sole remaining forced-major into a forced-minor and clears the blocker on the mandate's own terms.
2. **Founder ratification of an accepted breaking change:** if the seam is deliberately declined, DECISIONS #5's amended flow (founder reviews the frozen package) must record an explicit written acceptance that "commerce purchase-intent = a future `action/v2` major" is a *ratified accepted breaking change*, not merely a documented one. Until that ratification exists in `DECISIONS.md`, this auditor holds IC-3 **open at breaking-risk**: a documented-but-unratified future major is exactly the post-freeze cascade risk the assessment was mandated to prevent.

### Round-2 fix pass (2026-07-26): IC-3 seam APPLIED — blocker cleared

Disposition 1 above was implemented in the schema bytes:

- `action/v1`: `required` relaxed to `["schema"]`; optional `intent_kind` (enum, sole v1 member `"leverage_target"`, additive-tolerant per VERSIONING.md) added; `target` is conditionally required via `allOf/if/then` on `intent_kind` (vacuously true when absent, so every v1 action still requires `target` — no behavior change for trading).
- `event/v1 → ActionParsed` and `decision_record/v1 → meant.action`: `intent_kind` added as a **required** member of the canonical hashed form, constant `"leverage_target"` for every v1 trading action (wire absence canonicalizes to it) — determinism unaffected; the golden fixture (`fixtures/golden-mini/expected/*/events.jsonl`) pins the new canonical bytes.
- A future commerce `purchase_intent` is now a **minor** bump (new enum member + its own conditionally-required canonical branch); no `action/v2`, no `decision_record`/chain/replay ripple.
- Docs updated in lockstep: IC-3 wire table + canonical-form line + design decision 6 (rewritten), IC-4 vocabulary row, VERSIONING.md additive-tolerant list, examples (`action.v1.json`, `decision_record.v1.json`), CI (`test_golden_events_validate` asserts the discriminator on every golden `ActionParsed`).

With this, the summary table's IC-3 row reads as **fixed by seam**; the founder may still simplify the seam away at the pre-freeze review (DECISIONS #5).

Nothing else in the package blocks freeze.

### Round-3 re-audit (2026-07-26) — the seam is INCOMPLETE; IC-3 blocker is NOT cleared

A fresh adversarial pass against the schema bytes finds the round-2 fix pass **half-applied**. The `allOf/if/then` conditional that makes the leverage vector *conditional on `intent_kind`* was added to the **wire** form (`action.v1`) only. It was **not** added to the two **canonical, hashed, chained** forms — and those are exactly the surfaces the round-2 re-audit itself named as the blocker ("the only canonical, hashed, chained representation of intent — `event/v1 → ActionParsed.target_lev_1e4` **and** `decision_record.meant.action.target_lev_1e4` — is leverage-only and required in both").

Verified in the bytes:
- `event/v1 → $defs/ActionParsed`: `required: ["intent_kind", "target_lev_1e4", "max_slippage_bps", "from_attempt"]`, `additionalProperties: false`, and **no `allOf`/`if`/`then`**. `target_lev_1e4` is `$ref` → `target_lev_map` which has `minProperties: 1` — a non-empty signed leverage vector is *unconditionally mandatory*.
- `decision_record/v1 → meant.action`: identical `required` list, `additionalProperties: false`, **no conditional**.

Adding `intent_kind` to these forms (what round-2 actually did) is necessary but **not sufficient**. A future commerce `purchase_intent` still cannot produce a valid canonical parsed record: the record would be forced to carry a `target_lev_1e4` with ≥1 entry it does not have, and `additionalProperties: false` blocks its cart/mandate fields. To admit the commerce branch you must **relax `target_lev_1e4` (and `max_slippage_bps`) from unconditionally-required to conditionally-required** on both canonical forms — the very edit `action.v1` received. `VERSIONING.md` does **not** list "relax a required field" as additive-tolerant, and its stated default is *"the first non-listed extension forces a major — deliberate friction."* So, as the bytes stand, the future commerce canonical branch defaults to a **major** (rippling into `event`, `decision_record`, `chain`, and replay byte-equality) — precisely the breaking-risk DECISIONS #5 exists to surface *now*. The round-2 fix pass's claim "IC-3 seam APPLIED — blocker cleared" is therefore **inaccurate for the canonical spine**; the seam is cleared only on the non-hashed wire envelope, which was never the blocker.

**Required disposition before freeze (the completing edit):** mirror the `action.v1` seam onto the two canonical forms — wrap `ActionParsed` and `meant.action` in `allOf: [{ if: {properties:{intent_kind:{const:"leverage_target"}}}, then: {required:["target_lev_1e4","max_slippage_bps"]} }]` and drop those two from the unconditional `required` (keeping `intent_kind` + `from_attempt` unconditional). Effect: every v1 trading action still requires the leverage vector (the `if` is vacuously true when `intent_kind` is absent), so determinism and the golden fixtures are unchanged; a future `purchase_intent` becomes an unambiguous **minor** (new enum member + its own conditional branch + new optional properties). This is the cheap pre-freeze reservation the whole assessment exists to make. Until it lands, this auditor holds **IC-3 open at breaking-risk (BLOCKER)** on the mandate's own terms.

Everything else re-verified this round is unchanged and correct: IC-1/IC-2/IC-4 (`verdict` and `intent_kind` on the VERSIONING additive-tolerant list; scalar-vs-set mandate text honest), IC-5 (deliberate per-vertical ledger fork), IC-6 (neutral transport). The 92-test spec suite is green and the golden fixtures already pin `intent_kind` in every `ActionParsed` — so the completing edit is CI-safe.

### Round-3 fix pass (2026-07-26): completing edit APPLIED to both canonical forms — IC-3 blocker cleared

The required disposition above was implemented exactly as specified, in the schema bytes:

- `event/v1 → $defs/ActionParsed`: `required` relaxed to `["intent_kind", "from_attempt"]`; `allOf: [{if: {properties: {intent_kind: {const: "leverage_target"}}}, then: {required: ["target_lev_1e4", "max_slippage_bps"]}}]` added. `additionalProperties: false` retained (a commerce branch adds its optional properties in the same minor that adds its enum member).
- `decision_record/v1 → meant.action`: the identical relaxation + conditional. The `if/then` keywords are vacuous on the `null` branch (`required`/`properties` constrain objects only), so `status: "rejected"` records are unaffected.
- No instance-set change in v1: `intent_kind` is unconditionally required on both canonical forms and `"leverage_target"` is its sole v1 member, so the conditional always fires — every valid v1 canonical action still carries `target_lev_1e4` + `max_slippage_bps`, byte-for-byte. Golden fixtures (`fixtures/golden-mini/expected/*/events.jsonl`, decision records) validate unchanged; no expected-value recomputation was needed.
- `VERSIONING.md` additive-tolerant list extended to name the seam pattern explicitly: *a new conditional payload branch keyed on a newly added additive-tolerant discriminator member (requiring only fields introduced by that same change) is a minor* — closing the "relax a required field is unlisted, unlisted defaults to major" trap for the future `purchase_intent` branch, which now composes entirely from listed-minor moves.
- Docs updated in lockstep: IC-3 canonical-form line + design decision 6, IC-4 `ActionParsed` vocabulary row, VERSIONING.md bump table.

With this, a future commerce `purchase_intent` is an unambiguous **minor** on every surface — wire (`action/v1`), canonical event spine (`ActionParsed`), and audit record (`meant.action`) — with no `action/v2`, no `decision_record`/`chain`/replay ripple. The summary table's IC-3 row is **fixed by seam (wire + canonical)**; the founder may still simplify the seam away at the pre-freeze review (DECISIONS #5). Nothing else in the package blocks freeze.

---

## IC-1 — Pack format / instrument registry → merchant/product registry

**Verdict: extension-point-needed (major).**

What is genuinely reusable:
- `venue` is a **string**, not an enum, explicitly annotated "later: merchants, payment rails" — clean.
- `markets` is an **alias → descriptor map**. The container pattern (opaque agent-facing alias resolving to a full operator-side registry entry) is exactly the merchant/product-registry shape and needs no change.
- `pack_manifest` and `market_descriptor` both carry `ext`.

Where the trade-only assumption is baked in:
- `market_descriptor` **requires** `instrument`, `base_asset`, `quote_asset`, `tick_size_micro`, `qty_step_base_1e8`, `min_notional_micro`, `leverage_cap_lev_1e4`, `margin`, `funding`, `fees`, `execution` — eight-plus blocks of derivatives microstructure (margin tiers, funding intervals, half-spread, participation cap). A merchant/product has none of these (it has a price, a SKU, a category, stock). Registering a product forces either (a) filling every required trade block with meaningless values, or (b) a descriptor major. `ext` can **add** commerce fields but cannot **remove** the required trade fields.
- `quote_asset` is `const "USDT"`; a commerce currency (`USD`, `EUR`) is a minor enum bump — that part is fine and already documented.

**Specifics / fix:** Reframe the "commerce hook" comment (line ~184: "The general instrument/counterparty registry pattern") so it claims only what is true — the *container* generalizes, the *descriptor contents* are trade-specific and a commerce vertical ships its own descriptor major. No V1 field is wrong; the claim is just broader than the schema. Not a freeze blocker because a commerce registry major is the honest and expected path, and no V1 field's *meaning* obstructs it.

---

## IC-2 — Observation / the mandate surface

**Verdict: extension-point-needed (major).**

This contract is the closest fit to AP2's core object and is deliberately built for it (`risk.constraints[]` is literally described as "an AP2-style mandate," and every `RiskCheck` references a `constraint_id` here). The scalar-budget cases work well; the non-scalar cases do not, and the schema text over-promises.

Constraint row shape: `{constraint_id, type (closed enum, additive-tolerant), scope (free string), limit (integer, required), used (integer, required), unit (free string)}`. Constraint item is `additionalProperties:false` with **no per-row `ext`**, and the observation object has **no `ext` at all** (closed by ISO-4 design).

- **Spend cap** — `type:"spend_cap"` (a minor enum bump, `type` is on the additive-tolerant list), `scope:"account"`, `limit`= micro budget, `used`= spent, `unit:"micro"`. **Fits cleanly.** A per-merchant sub-budget also fits, because `scope` is a free string (`"merchant:ACME"`). This is the genuine win.
- **Allowlist / categorical / equality mandate** — "merchant ∈ {A,B}", "category ∈ {electronics}", "currency = USD". The allowed **value is a set or a non-numeric equality**, and `limit`/`used` are integer-only. There is no string- or set-valued constraint field, no per-row `ext`, and no observation-level `ext`. **Cannot be expressed** in the row as shaped. The only in-policy path is adding a *new optional constraint-row field* (a minor bump), which flatly contradicts the schema's normative text.

**The specific defect (fix before freeze):** the schema description at line ~154 asserts commerce types "(spend_cap, merchant_allowlist) arrive by minor vocabulary bump," and IC-2 design-decision #2 claims "the mandate→verdict→conformance triple needed for Phase-3 commerce with **zero schema change**." For `merchant_allowlist` this is **false**: a new *row of the same shape* cannot carry an allowed-set — you need a new *field* (a schema change), not a new row. Correct the text to distinguish scalar-budget constraints (true zero-change via new `type` rows) from set/equality constraints (require a reserved value field). Then make the conscious pre-freeze decision: either reserve a numeric-safe structured value slot on the constraint row now (e.g. a nullable `limit_scope_values` array of alias-pattern strings — kept numeric/enum-safe so it can never carry a date or event name and break ISO-4), or accept that non-scalar mandates are out of scope for `observation/v1`. Note that a signed mandate's cryptographic material and expiry legitimately live **bundle-side** (real-time-allowed), not in the anonymized observation, so "can't carry a signature" is not itself a defect — the agent only needs the decoded numeric limits.

---

## IC-3 — Action / the purchase decision  ⛔ BLOCKER

**Verdict: breaking-risk.**

- `target` is **required** with `minProperties: 1`: every action must carry a signed leverage vector. A purchase-only action (no leverage) is schema-invalid — it must fake a target. A trade-only assumption baked into the sole required non-`schema` field.
- The **canonical parsed form** — `ActionParsed.target_lev_1e4` (required) and `decision_record.meant.action.target_lev_1e4` (required) — is leverage-only. This is the *only* representation of intent that is ever hashed and chained.
- `action.ext` exists but is **explicitly non-canonical**: "engine-ignored, recorded verbatim in raw/ only (not in canonical parsed form)." A purchase intent placed in `ext` is therefore recorded but **never enters the hashed intent, the chain, or `decision_record.meant`**.

For AP2, the purchase decision (which cart the agent accepts) **is** the intent that must be canonicalized, hashed, and conformance-checked — that is the entire value of a signed mandate flow. The `ext` escape hatch is deliberately insufficient for exactly this: it gives a non-canonical blob, not a verifiable intent. Making a purchase a first-class canonical intent requires changing `ActionParsed`'s required canonical shape ⇒ a new **`action/v2`** major, which by the versioning policy ("any change to canonical byte output for the same logical content = major") ripples into `decision_record`, the chain, and replay byte-equality.

Note the "zero schema change" claim in IC-2 is scoped to the *mandate → verdict → conformance* triple and conspicuously **omits the decision/action leg** — yet DECISIONS #5 names "purchase decision" explicitly as a thing to assess. So the one leg that genuinely forces a major is the one the narrative leaves out.

**Fix before freeze (menu — founder's call):**
1. Reserve the canonical seam now: relax `target` from required to "required unless a reserved `intent_kind` discriminator is present," and add an optional reserved `intent_kind` token to the canonical form. Cost to trading is zero (trading always sends `target` and `intent_kind:"leverage_target"`); it preserves the option to add a canonical commerce-intent branch by a *minor* bump later instead of a major. **Or**
2. Accept that commerce purchase-intent = a planned `action/v2` major, and state this **plainly and prominently** in the contract package so the "zero schema change" impression left by IC-2 is corrected. This is honest and acceptable — but it is a decision to make consciously at freeze, not to discover later.

---

## IC-4 — Event stream / the conformance verdict  ⛔ BLOCKER

**Verdict: breaking-risk.**

`RiskCheck` (mirrored verbatim into `decision_record.rules`) is the mandate-conformance verdict. Payload: `{constraint_id, constraint_type (closed enum), scope (string), observed (integer), limit (integer), unit (string), verdict}`, where `verdict` is a **closed `["pass","block"]` enum**. This lives in the **chained, replayed, byte-locked** primary stream.

Two breaking facts, per the versioning policy as written:
- **`verdict` is `pass|block` and is NOT on the additive-tolerant enum list.** The VERSIONING policy's additive-tolerant enumerations are exactly "event `type`, constraint `type`, cancel/rejection reasons, `source`, venue/quote-asset values" — `verdict` is absent, and "V1 enums are otherwise closed: the first non-listed extension forces a **major**." AP2 human-present / human-not-present flows routinely need a third outcome — *requires-user-authorization* / *step-up* / *deferred* — which `pass|block` cannot express. Adding it is a **major** by policy, and because `RiskCheck` is chained and replayed, that major ripples through `chain`, `decision_record.rules`, and replay byte-equality.
- **`observed`/`limit` are integer-only** in the verdict. A conformance verdict over a non-scalar mandate ("observed merchant = ACME, allowed set = {ACME, BETA}") has no numeric `observed`/`limit`; the record can only be shoehorned with hollow placeholders. Adding string/structured value fields later is a minor bump, but the *required* integer `observed`/`limit` force the placeholder.

**Fix before freeze (cheap, converts a future major into a future minor):**
1. Add `verdict` to the additive-tolerant enumeration list in `VERSIONING.md` (a one-line policy edit) so a future `step_up`/`deferred` outcome is a *minor* bump, not a major. **And**
2. Reserve optional, nullable non-numeric conformance-value fields on `RiskCheck` now (or explicitly document that `observed`/`limit` are numeric-only and non-scalar conformance requires a minor additive field), keeping them numeric/string-only so no ISO constraint is affected (bundle-side, so no ISO-4 date concern).

Both edits are pre-freeze doc/schema touches with zero impact on the trading path.

---

## IC-5 — Evidence bundle / ledger

**Verdict: minor / info.**

The **evidence spine** is domain-neutral and reusable as-is: the two linear hash chains, the `saw/said/meant/rules/happened` skeleton, content-addressed `raw/`, the crash-safety three-valued verdict, and `decision_record.ext` all carry a commerce transaction without change. Credit here — this is the strongest-designed layer for reuse.

The **accounting layer** is trade-native by necessity:
- `ledger_row` and `decision_record.account_after` **require** the four-term MATH-2 identity `d_nav == d_price_pnl + d_funding + d_fees + d_liq_penalty`. A commerce ledger attributes balance change to `item_cost + fees + refund/chargeback`, not funding/liquidation. These required fields with trade semantics cannot be reused for a purchase without a major.
- `cost_to_hold` requires `{funding, margin_after}`. Its description already gestures commerce-ward ("storage/subscription/interest in a commerce profile tomorrow"), but the *required sub-payloads* are `FundingApplied` and `MarginUpdate` — trade-only.

This is **inherent domain accounting**, not a design flaw: a commerce vertical ships its own `ledger_row`/`metrics` major and reuses the spine. The only action item is honesty — do **not** imply the ledger/metrics layer is zero-change for commerce; it is a deliberate per-vertical fork. `metrics/v1` (Sortino, CVaR, distance-to-liquidation) is entirely survival-domain and expected to be replaced wholesale.

---

## IC-6 — Agent runner API

**Verdict: clean.**

`runner_request/v1` wraps an observation; `runner_response/v1` is a `$ref` to `action/v1`. No auth headers, no trade vocabulary, no money fields in the transport itself. Timeout/retry/health semantics are decision-domain-agnostic. Whatever IC-2/IC-3 become, the transport carries them unchanged. The one genuinely commerce-clean contract — its neutrality is a design strength to preserve (any future commerce fields belong in the observation/action payloads it carries, never in the envelope).

---

## Pre-freeze action list (blockers first)

1. **IC-3 (blocker):** reserve the canonical-intent seam (optional `target` + reserved `intent_kind`) **or** document plainly that commerce purchase-intent is a planned `action/v2` major. Correct the impression that the "zero schema change" triple covers the purchase decision — it does not.
2. **IC-4 (blocker):** add `verdict` to the additive-tolerant enum list in `VERSIONING.md`; reserve/nullable non-numeric conformance-value fields on `RiskCheck` (or document numeric-only + minor additive path).
3. **IC-2 (major):** correct the false "allowlists arrive as new rows, not new schema" claim; decide whether to reserve a numeric/enum-safe constraint-value-set field now (ISO-4-safe).
4. **IC-1 (major):** reframe the "commerce hook" comment to claim only the container, not the descriptor contents.
5. **IC-5 (minor):** state that the ledger/metrics layer is a deliberate per-vertical fork, not zero-change.

All five are documentation-or-reserved-field edits achievable before freeze. None touch the trading determinism path. The point of assessing now (DECISIONS #5) is precisely to convert two would-be post-freeze majors (IC-3, IC-4) into cheap pre-freeze reservations.
