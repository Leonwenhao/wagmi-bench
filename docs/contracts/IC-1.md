# IC-1 — Pack Format (data pipeline → engine)

**Schemas:** [`pack_manifest/v1`](../../spec/schemas/pack_manifest.v1.schema.json) · [`bar_row/v1`](../../spec/schemas/bar_row.v1.schema.json) · [`funding_row/v1`](../../spec/schemas/funding_row.v1.schema.json)
**Status:** FROZEN v1 (founder sign-off 2026-07-26, DECISIONS.md #7). Changes now follow the VERSIONING.md migration procedure with written impact assessment.
**Complete field reference:** [`field-reference.md`](field-reference.md) — generated from the schema descriptions (`spec/tools/gen_field_reference.py`), CI-gated for 100% description coverage, render currency, and reverse-drift (SCH-2). This doc is the curated narrative; the generated reference is the exhaustive one.

## Purpose

A pack is one scenario's complete, content-addressed market dataset plus every venue parameter the engine needs to simulate it. The repo ships **manifests and fetch scripts only** (DATA-5); series files are built locally from pinned `data.binance.vision` archives (DATA-1/DATA-4) and verified against the hashes recorded in the manifest. A pack is identified everywhere by its `content_hash` — content addressing beats mutable version strings for evidence.

## Directory layout

```
packs/<pack_id>/
  manifest.json      pack_manifest/v1   (the only file committed to the repo)
  bars_4h.jsonl      bar_row/v1         trade-price bars   (built locally)
  mark_4h.jsonl      bar_row/v1         mark-price bars
  index_4h.jsonl     bar_row/v1         index-price bars
  funding.jsonl      funding_row/v1     settled funding prints
```

The three price series are **never conflated** (Protocol §1): index prices funding notional, mark drives margin/liquidation/uPnL, trade drives fills. Role lives in `manifest.files[].role`, keeping one row shape (`bar_row/v1`) and one parser for all three streams.

## Cross-cutting conventions (normative for all six ICs)

| Rule | Statement |
|---|---|
| Units | Field-name suffixes are load-bearing: `_ticks` = price in integer ticks (× `tick_size_micro` = micro-quote) · `_micro` = micro-USDT (1e-6) · `_1e8` = dimensionless rate/fraction ×1e8 · `_lev_1e4` = leverage ×1e4 · `_base_1e8` = base-asset qty ×1e8 · `_ms` = millisecond duration · `_ts`/`ts`/`available_at` = epoch-ms instant. Every field description restates its unit (SCH-2). |
| Numbers | No JSON number with a fractional part may appear in any TradeEvolve document. All integers lie within ±(2^53−1), the JCS-exact range. `"type": "number"` appears in no schema (DET-2/DET-4). |
| Serialization | Anything hashed is RFC 8785 (JCS) canonical JSON, UTF-8. JSONL = one JCS record per line, `\n`-terminated. Line hash excludes the terminating newline; file hashes are over complete uncompressed bytes. Hash wire format: `sha256:<64 lowercase hex>`. |
| Verification | Always over **stored bytes**, never over re-encoded parsed objects. |
| Rounding | Money divisions are agent-adverse by default: costs round up, credits round down; every case pinned by the golden fixture (C0.3). |
| Time | All persisted timestamps are real UTC epoch-ms **except inside observations**, which are rebased (see IC-2). The engine reads no wall clock. |
| Self-description | Every standalone document and every individually chained record carries `"schema": "<name>/v<major>"`. Exception: bulk series rows (bar/funding), whose schema is declared file-level in `manifest.files[].row_schema`. |

## The `available_at` rule (ISO-1)

Every observable row carries `available_at` — the instant it becomes knowable:

- bar rows: `available_at` = bar **close** time (`ts` + interval)
- funding rows: `available_at` = `ts` (a settlement is knowable the instant it occurs)

An observation at virtual clock T may include only rows with `available_at ≤ T`. The field is **stored per row, not derived**, so the ISO-1 property test and the C0.4 poison-row fixture operate on stored bytes.

## Key manifest semantics

- **`content_hash`** = SHA-256 of `JCS({"schema":"pack_content/v1","files":[{path,sha256,bytes,records}…]})`, entries sorted by path. It *is* the pack version, referenced by bundle manifests and chain genesis.
- **`markets`** — map: agent-facing alias (`"BTC"`) → full venue descriptor (`instrument: "binance-um:BTCUSDT"`, units, margin tiers, funding, fees, execution model). The alias keeps LLM payloads small and venue identity out of observations (ISO-3). **Commerce-extensibility scope (corrected at the M0 audit):** only the *container* generalizes — the alias→registry-entry map and the free-string `venue` are the merchant/product-registry pattern and carry over unchanged; the *descriptor contents* are required perpetual-futures microstructure (margin tiers, funding intervals, half-spread, participation cap, fees) that a merchant/product does not have. A commerce vertical ships its own descriptor under a new major, reusing the container; `ext` can add commerce fields but cannot remove the required trade blocks.
- **`funding` block is per-market data, never a constant** (MATH-4): `interval_ms`, `settlement_offsets_ms` (integer offsets from UTC midnight — no cron strings), era-accurate `cap_1e8`/`floor_1e8` (2020-era: ±300000).
- **`margin.tiers`** — explicit stored integers; validator asserts *maintenance = half initial at the max-leverage tier* (Protocol §2). The rule lives in data, not engine formulas.
- **`execution`** — half-spread (evolution-period-calibrated, window in `calibration_note`, never observable), impact model/coefficient, participation cap (excess is **cancelled, never filled**, MATH-7), and `cost_profile_multipliers_1e4` (`primary`=10000, `stress_2x`=20000) so the 2× re-simulation is fully determined by the pack (MATH-5).
- **`warmup_bars`** — history reserved before turn 0; "what did turn 0 legitimately see" is a stored fact.
- **`claim_label`** — `const "survival-stress"`; LABEL-1 CI is a mechanical equality check; `skill` is not a member by design.
- **`regime_description`** — human documentation only; never serialized into observations; its vocabulary seeds the IC-2 CI scan denylist.

## Pack validators (C1.4)

Gap detection (bars and funding), monotonic `ts`, `available_at` consistency (`= ts + interval` for bars, `= ts` for funding), funding stamps exactly on declared settlement offsets, `|rate_1e8| ≤ cap`, mark-vs-trade divergence sanity, maintenance-half-initial invariant, build-twice byte-identity (DET-5).

## Design decisions (synthesis resolutions)

1. **`funding_row` gains `available_at`** (all three drafts) vs the dev-plan `{ts, rate_1e8}` sketch — one uniform point-in-time rule over every observable row type; no per-type special case in ISO-1 tests.
2. **Alias → descriptor map** for markets (trader + commerce drafts) vs a flat symbol: multi-asset from day one, venue identity quarantined bundle-side. The commerce registry seam is the *container only* (see Key manifest semantics above); descriptor contents are trade-specific by construction and a commerce descriptor is a planned major, not a zero-change ride-along.
3. **Content hash as the pack version** with the hash computed over a sorted `pack_content/v1` file list (determinism draft's construction — the most precisely specified of the three).
4. **Cost profiles live in the pack** (commerce draft) not the bundle: replay of either profile is determined by pack + actions alone.
5. **`bytes` + `records` per file** (determinism draft): truncation detectable before hashing.
6. **Row-level `schema` omitted in bulk series files**, declared file-level in the manifest (determinism draft): ~15–20% byte savings on bulk data for zero evidence value; the manifest binds `path → row_schema → sha256` losslessly.
7. **One `bar_row` shape for all three series** with `v_base_1e8 = 0` permitted on mark/index (determinism + commerce): one parser, three streams; role is manifest provenance.
8. **`settlement_offsets_ms` from UTC midnight** (trader + commerce): integer math, era-exact, no parser.
9. `quote_asset` is `const "USDT"` in v1 (venue-accurate; the protocol doc's USDC is corrected per Scope). Future quote assets are a minor enum extension.
