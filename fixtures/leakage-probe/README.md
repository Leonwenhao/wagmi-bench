# Leakage-Probe Fixture (C0.4 — ISO-1)

A tiny **synthetic** scenario pack whose only job is to make look-ahead leakage loud.
Schema-valid per IC-1 (`pack_manifest/v1`, `bar_row/v1`, `funding_row/v1`), but it is a
**test trap, not a benchmark pack** — never run a real episode from it.

- Pack `content_hash`: `sha256:4ecd617ee13ce9e226306a6742dde06d18528a26b6d19a778de10d7c2cdd4e8b`
- Window: `1590969600000` → `1591084800000` (real UTC epoch-ms; 8 × 4h decision bars)
- One market alias `BTC` → `binance-um:BTCUSDT`, full synthetic venue descriptor
  (tick 100000 micro = 0.10 USDT, 3x cap, 8h funding at offsets `[0, 28800000, 57600000]`,
  cap/floor ±300000, maintenance = ceil(half initial) at the max-leverage tier).

## Purpose (ISO-1)

Four **poison rows** — one per series file — carry:

1. `available_at = 1591689600000`, which is one full week **after** the pack window ends
   (`window.end_ts = 1591084800000`), while their `ts` sits normally inside the window; and
2. distinctive **sentinel values** in every price/volume/rate field (registry below).

A correct engine filters every observable row by `available_at <= virtual_clock` on **stored
bytes** (IC-1 §"The available_at rule"). Since the poison rows' `available_at` lies beyond any
clock value the episode can ever reach, a correct engine can **never** emit them. An engine that
derives observability from `ts`, from row order, or from `ts + interval` arithmetic instead of
the stored `available_at` field will surface a sentinel value — an immediate, unambiguous
ISO-1 **BLOCKER** failure.

**What this fixture proves, and what it does not (normative framing, M0 audit):**

- The **primary, load-bearing ISO-1 acceptance check** is the property from the Review Spec:
  every emitted observation satisfies max(`available_at`) ≤ `clock_ts`, cross-checked against
  the pack's stored bytes, property-tested across random configs, plus the [ADV] extraction
  attempts. That property — not this fixture — is the ISO-1 guarantee.
- The sentinel value-equality scan below is a **secondary tripwire** for one specific bug class:
  observability derived from `ts`/row order/arithmetic instead of stored `available_at`. It is
  **necessary but NOT sufficient, and it is gameable**: it fires only when one of the 4 poison
  rows actually surfaces. An adversarial engine can leak *clean* future rows (plausible values,
  no sentinel), leak *derived aggregates* of future data (min/max/mean of future closes), or
  special-case the poison rows themselves — they are self-identifying (`available_at` exactly one
  week past `window.end_ts`, `4242…` value pattern) — skip those 4 rows, leak everything else,
  and pass this fixture. **Passing the sentinel scan therefore does not prove leak-freedom**; it
  proves the naive-observability bug class is absent.

**The tripwire rule: any observation (or any other agent-visible surface) ever emitted that
contains any sentinel value from `sentinels.json` = leakage failure.** Detection is by integer
value equality over stored observation bytes; the scan itself needs no knowledge of field paths
or rebasing — but see "Coverage limits" below for the surfaces where field→source-row provenance
IS required for full ISO-1 coverage.

## Poison-row map

| File | Row index | `ts` (real UTC ms) | Poisoned fields |
|---|---|---|---|
| `bars_4h.jsonl` (trade) | 5 | 1591041600000 | `o,h,l,c,v_base_1e8` = 424242000101/103/100/102/104 |
| `mark_4h.jsonl` (mark) | 3 | 1591012800000 | `o,h,l,c` = 424242000201/203/200/202 (`v_base_1e8` = 0, per mark convention) |
| `index_4h.jsonl` (index) | 6 | 1591056000000 | `o,h,l,c` = 424242000301/303/300/302 (`v_base_1e8` = 0) |
| `funding.jsonl` | 2 | 1591027200000 (16:00 UTC) | `rate_1e8` = 42424242 |

All poison rows share `available_at = 1591689600000`. All other rows are clean:
bars have `available_at = ts + 14400000`, funding prints `available_at = ts`, and no clean
field value collides with any sentinel.

Poison rows sit in **different bar indexes per series** (5/3/6) so a bug in any one of the three
never-conflated streams (trade→fills, mark→margin/liquidation, index→funding notional) trips
independently, and mid-window placement catches both "off the end" and warmup-edge mistakes.

## Sentinel registry

`sentinels.json` is the machine-readable registry (fixture-local shape
`leakage_sentinel_registry/v1`; a test asset, not a `spec/schemas` contract document). It lists:

- `sentinel_values` — the flat set of 14 integers whose appearance anywhere agent-visible is a failure;
- `poison_rows` — file / row index / `ts` / `available_at` / field→sentinel map for each trap;
- `poison_available_at` — the shared future instant;
- the pack `content_hash` it belongs to.

Sentinels use the `4242420001xx/2xx/3xx` (per-series prefix) and `42424242` patterns: far outside
any plausible tick price (~9.5×10⁴ here) yet well inside the ±(2⁵³−1) JCS-exact range.

**Poison bar price/volume rows are caught by value-equality against `sentinels.json` ONLY** —
there is no secondary catch (a claim to the contrary was removed at the M0 audit): the
observation schema's `maximum: 99999999999` guard exists solely on the timestamp-shaped fields
(`clock_ts`, `bars[].ts`, `bars[].available_at`, `funding.next_settlement_ts`,
`funding.prints[].ts`), so a ~4.2×10¹¹ sentinel in `bars[].c` is fully schema-VALID, and the
IC-2 magnitude scan is scoped to those same timestamp fields precisely because legitimate
volumes (clean fixture `v_base_1e8` = 2.5×10¹¹; real data ~1.2×10¹²) exceed 10¹¹ — a blanket
magnitude scan would flag clean data and distinguishes nothing. A poison **timestamp** leaking
un-rebased *would* additionally violate the schema maximum, but prices and volumes are not
timestamps.

## Coverage limits (surfaces without in-band `available_at`)

The in-band `available_at ≤ clock_ts` property covers `bars[]` (bars carry `available_at` in the
observation). Agent-visible surfaces that do **not** carry one fall into two groups (IC-2 CI
scan item 4 / decision 7):

1. **Surfaced pack values:** funding `prints[]` (carry `ts` only, IC-2 decision 7) and the
   derived scalars `last_mark_px_ticks` / `last_index_px_ticks` (no timestamp at all). For
   these the sentinel scan gives only single-turn coverage — it fires only at the turn where
   the mark poison row (index 3), index poison row (index 6), or funding poison row (index 2)
   would align with what is surfaced. Full ISO-1 coverage requires the **provenance mapping
   step**: map each surfaced value back to its source pack row and assert that row's stored
   `available_at` ≤ the un-rebased clock.
2. **Mark-derived scalars (M0 audit round 2):** `position.<alias>.upnl_micro`,
   `position.<alias>.dist_to_liq_1e8`, and `account.nav_micro` (NAV includes uPnL) are
   functions of the current mark. The sentinel scan can **never** catch a leak here: a uPnL
   computed off the poisoned future mark is `qty × (424242000202 − entry)` — an ordinary large
   integer, not any value in `sentinels.json`. Concrete extraction the check must close: an
   agent that knows its own `qty_base_1e8` and `entry_px_ticks` reads `upnl_micro` and solves
   `mark = entry + upnl·1e8/(qty·tick)` — if the engine used a not-yet-available mark, the
   agent has recovered a future price. Coverage comes from the **single-mark invariant** (IC-2
   scan item 4): these scalars MUST be computed from the same provenance-checked mark row that
   populates `last_mark_px_ticks`, CI-asserted by exact arithmetic recomputation from in-band
   fields, plus the [ADV] solve-for-mark extraction case run against this pack (the solved
   mark must always be an `available_at`-legal row, and must never equal the poisoned mark
   close 424242000202).

The field-path-agnostic value scan alone does not fully cover either group.

## How the engine test uses it (C2.2 acceptance)

1. Load this pack (its `content_hash` verifies like any real pack; per-file `sha256`/`bytes`/`records` match).
2. Build observations for **every** turn across a matrix of configs (lookback depths, warmup,
   funding-print counts, both cost profiles — ISO-1 is property-tested across random configs).
3. **Primary assertion (load-bearing):** for each emitted observation, max(`available_at`) ≤
   `clock_ts`, cross-checked against the pack's stored bytes; for the surfaces without in-band
   `available_at` (funding prints, mark/index scalars), apply the provenance mapping — trace
   each surfaced value to its source pack row and check that row's stored `available_at`. In
   particular, the poisoned funding print (16:00 of day 1) must be absent from every
   observation's `prints[]` at every clock (its `available_at` is beyond the window). For the
   mark-derived scalars (`upnl_micro`, `dist_to_liq_1e8`, `nav_micro`), assert the single-mark
   invariant: recompute each from the in-band `last_mark_px_ticks` and position fields and
   require exact equality (pinned rounding), and run the [ADV] solve-for-mark extraction
   (`mark = entry + upnl·1e8/(qty·tick)` on a known position) — the solved mark must be an
   `available_at`-legal row and never the poisoned mark close.
4. **Secondary tripwire:** scan the stored JCS bytes: no integer field equals any
   `sentinel_values` member.
5. Any failure of either check → ISO-1 BLOCKER failure. Passing both does NOT prove
   leak-freedom (see "What this fixture proves" above); the property test + [ADV] extraction
   passes carry the guarantee.

## Intentional pack-validator violations

The C1.4 pack validators (`available_at = ts + interval` for bars, `available_at = ts` for
funding, `|rate_1e8| ≤ cap_1e8`) **must reject this pack** — the poison rows deliberately break
exactly those invariants (42424242 > 300000 cap, future `available_at`). That is a feature:
this fixture doubles as a negative test for C1.4. The engine leakage test loads it with pack
validation bypassed (fixture mode); everything is schema-valid, so parsers need no special cases.

## Regeneration

Deterministic (no clock, no randomness); rebuilding is byte-identical (DET-5 style) for **all
six pack files** — the 4 series files, `manifest.json`, AND `sentinels.json`.
`tools/gen_fixture.py [output_dir]` regenerates every pack file (default output: this
directory); `tools/validate_fixture.py` re-runs the full schema + hash + poison/sentinel
consistency check (run both with `.venv/bin/python`). All files are JCS (RFC 8785) canonical
JSON except `sentinels.json` (pretty-printed, sorted keys — a human-first registry); series
files are `\n`-terminated JSONL.

**CI locks (M0 audit round 3, ISO1-GEN-DRIFT):** `spec/tests/test_leakage_fixture.py`
regenerates the pack into a temp dir and asserts byte-identity against every committed file,
and `tools/validate_fixture.py` pins the sha256 of the two files *outside* `content_hash`
coverage (`sentinels.json`, `manifest.json`). The generator is the source of truth: an edit to
a committed file that bypasses the generator — or a generator edit that silently changes
committed bytes (the audit-hardened `sentinels.json` `rule` wording once regressed exactly this
way) — now fails CI loudly. Recorded hashes:

| File | Records | Bytes | SHA-256 |
|---|---|---|---|
| `bars_4h.jsonl` | 8 | 956 | `sha256:55acefe52d2f6f99e2c6a9c2cf522e8623885a38ba0cde38579b9ed83f7ba0d5` |
| `funding.jsonl` | 4 | 271 | `sha256:ec2e09e4953433f615d1ce8fadc910a2cac05fc38decdecb55f581ebc77a6a0b` |
| `index_4h.jsonl` | 8 | 868 | `sha256:c4c3b1ce0e7d5deee36a45fe84462b8b6a909a8002a3c0ed55aef9627a468863` |
| `mark_4h.jsonl` | 8 | 868 | `sha256:1743694874294777169c8c951d0701f45a590a4f185dc3b8fbef6653aa6dc185` |
| `manifest.json` | — | 2538 | `sha256:0c9f68e397ae4da2f1737a2d6fcb3249a007a263f1d09ae5e9c0bcf1bd5a8962` |
| `sentinels.json` | — | 2127 | `sha256:de3cd5b0eda9a9444d10194fdbc9c5dc3cf9be13ce6d9b3ff9da74185eafed2b` |

`manifest.json` is also written in JCS canonical form (one line); pretty-print locally to read it.
Unlike real packs, all five files (including series files) are committed: the fixture has no
upstream (`files[].upstream` is empty), and CI must not depend on network fetches.
