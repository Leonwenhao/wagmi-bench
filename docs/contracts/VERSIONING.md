# Schema Versioning & Evolution Policy

**Status:** FROZEN v1 (founder sign-off 2026-07-26, DECISIONS.md #7). Changes now follow the VERSIONING.md migration procedure with written impact assessment.
Applies to every schema in `spec/schemas/` (IC-1..IC-6) and every document/record they govern.

## Naming

- Every standalone document and every individually chained record self-describes with `"schema": "<name>/v<major>"` (e.g. `"observation/v1"`). Readers **reject unknown majors; they never guess**.
- Exception (bulk-data rule): rows inside pack series files (`bar_row`, `funding_row`) omit the per-row schema field; the containing file's row schema is declared once in `pack_manifest.files[].row_schema`. Rationale: per-row schema strings are ~15–20% of bulk bytes for zero information; the manifest binds `path → row_schema → sha256` losslessly. Individually chained records (event lines, decision files) always self-describe, because they may be extracted standalone.
- Schema files are named `<name>.v<major>.schema.json`; full versions (major.minor.patch) live in the registry file `spec/schemas/VERSIONS.json` (its `versions` member is the flat map `{"observation/v1": "1.0.0", ...}`). Every bundle records that map verbatim in `bundle_manifest.spec_versions`.

## What forces which bump

| Change | Bump |
|---|---|
| Documentation/description wording only | patch |
| New **optional** field; new enum member explicitly marked additive-tolerant in that schema's docs (event `type`, constraint `type`, cancel/rejection reasons, `source`, venue/quote-asset values, RiskCheck/rules `verdict`, action/ActionParsed `intent_kind`); a new conditional (`allOf/if/then`) payload branch keyed on a newly added additive-tolerant discriminator member, provided the branch only requires fields introduced by that same change and existing branches' required sets are untouched (the `intent_kind` seam pattern — present on the wire form `action/v1` AND both canonical hashed forms, `event/v1 → ActionParsed` and `decision_record/v1 → meant.action`, M0 audit round 3) | minor |
| Field removed or renamed; unit or semantic change; enum member removed or repurposed; any change to canonical byte output for the same logical content; widening `NNNN` filename width | **major** (= new schema name `<name>/v<n+1>`) |

V1 enums are otherwise **closed**: the first non-listed extension forces a major — deliberate friction while the vocabulary is young.

## Extension points (`ext`)

Most object schemas carry one sanctioned `ext` property: an object with open keys (reverse-DNS or `x_`-prefixed recommended), values restricted to JCS-safe types with **no fractional numbers**, ignored by the engine, hashed as-is. This restriction is **schema-enforced**, not a convention: every `ext` uses a recursive `$defs/ext_value` value schema (null / boolean / string / integer within ±(2^53−1) / arrays and objects thereof), so `{"x_weight": 1.5}` is schema-invalid. One lexical gap remains at the JSON Schema layer — a mathematically-integral literal such as `1.0` satisfies the `integer` type — and is closed by the CI instance scan below (float-rejecting parse of every hashed document). This is how experiments and Phase-3 commerce fields ride along without a schema fork. Deliberate exclusion: **`observation/v1` has no `ext`** — the isolation surface is closed because any extension slot could carry dates or scenario names (ISO-4).

## Immutability of history

Hashed bytes are immutable. Verification always operates on **stored bytes** (never re-encoded parsed objects), so no schema evolution can invalidate an existing bundle. New engine versions must still *read* every schema major they ever wrote, or say they cannot — loudly.

## Replay compatibility rule (DET-6)

`tradeevolve replay <bundle>` refuses unless **all** of:

1. recomputed pack content hash == `bundle_manifest.pack.content_hash`;
2. engine semver **exactly equals** `bundle_manifest.engine_version`;
3. the engine knows every schema major listed in `bundle_manifest.spec_versions`.

Any mismatch is a refusal naming the field, the expected value, and the found value, plus the pinned-install command. **No override flag exists in V1.** Byte-identical replay is only honest under exact pinning; "best-effort compatible replay" would be a separate, clearly labeled future command, never the default.

## How a v2 happens (migration notes requirement)

Any post-M0 schema change ships as one PR containing all of:

1. the new schema file (`<name>.v2.schema.json`) — the v1 file is never edited beyond descriptions;
2. a `VERSIONS.json` registry update;
3. **migration notes** in `docs/contracts/migrations/<name>-v1-to-v2.md`: what changed, why, the mechanical field mapping (old → new, unit conversions spelled out in integer arithmetic), and whether a converter tool is provided;
4. a **written impact assessment** enumerating every component that reads the schema (grep-enumerable because every reader names schemas explicitly) — the M0 exit rule and DECISIONS #5 mitigation;
5. updated examples that validate in CI, and golden-fixture updates if canonical bytes are affected.

Founder-requested changes at the M0 contract review follow the same procedure (the review happens **before** freeze, so pre-freeze edits amend v1 in place; the procedure above governs everything after freeze).

## CI enforcement (mechanical)

- Every schema file validates as draft 2020-12; every field has a `description`; `additionalProperties: false` everywhere except documented `ext` points (whose values are constrained by `$defs/ext_value`).
- **SCH-2 field-reference completeness gate** (`spec/tests/test_field_reference.py`, M0 audit round 3): the schema-embedded descriptions ARE the normative complete field reference, rendered to `docs/contracts/field-reference.md` by `spec/tools/gen_field_reference.py`. CI asserts (a) every field definition in every schema carries a non-empty description, (b) the committed render is byte-identical to regeneration and mentions every schema property (a schema edit without `gen_field_reference.py` re-run fails), and (c) reverse-drift: every backticked field path in the curated IC-1..IC-6 `Field`-headed tables resolves to real schema vocabulary. The curated IC docs are the architecture narrative and are NOT required to enumerate every field; the generated reference is the exhaustive one.
- Every example in `spec/schemas/examples/` validates against its schema.
- No schema contains `"type": "number"`; grep-level float ban.
- **Instance-level float ban**: every hashed/committed JSON document (examples, fixtures, and at runtime every bundle artifact) is parsed with a float-rejecting hook (`parse_float` raises); any JSON number with a fractional-part or exponent lexeme — including `1.0` inside an `ext` block, which the schema's `integer` type cannot distinguish — fails CI. This closes the gap between the schema-level `ext_value` constraint and DET-2.
- `spec/schemas/VERSIONS.json` exists, parses, and its `versions` map covers exactly the published `<name>.v<major>.schema.json` files.
- `metrics/v1` profile key-set equality (MATH-5); `claim_label` equality on pack/metrics/report surfaces (LABEL-1).
- Observation scan per IC-2 (ISO-4) and available-at property test (ISO-1) run against emitted observations, not just schemas.
