# Scenario Pack Catalog

Every catalog entry carries the claim label `survival-stress`. A named
historical pack measures risk behavior on one recorded path: liquidation
survival, drawdown control, funding drag, turnover, and discipline. It is not
evidence of predictive ability or future performance.

## Memorization and era recognition

Models may recognize these events from pretrained knowledge and price action.
Removing dates and names from the observation is not a guarantee of anonymity.
Venue-constant era fingerprinting remains possible through the funding
cap/floor, fee tier, tick size, qty step, and min notional.

Those constants are **not** era-calibrated. All 13 packs share one disclosed,
uniform V1 venue-parameter baseline; the manifests state it plainly in each
market's `calibration_note` ("historical exchangeInfo parameters remain pending
primary-source verification before public release"). The baseline is a
conservative scope-pinned descriptor, not a reconstruction of what each window's
venue actually published. Primary-source verification of historical
`exchangeInfo` is deferred, and no era-accuracy claim is made until it is done.
Results are therefore comparable across packs by construction, and the
fingerprinting caveat above still holds: a uniform baseline is disclosed
information about the harness, not anonymity.

Pack names, descriptions, source URLs, real timestamps, and venue identity stay
outside the agent-facing observation. The observation contains an opaque
episode id, rebased clocks, aliased market identity, and only records whose
`available_at` is no later than the virtual clock.

## V1 catalog

Windows use UTC calendar boundaries. `End (exclusive)` is the first instant
not included in the scenario.

| Pack manifest | Regime | Start | End (exclusive) | Bar | Survival-stress focus |
|---|---|---:|---:|---:|---|
| [`covid-black-thursday`](../packs/covid-black-thursday/manifest.json) | crash | 2020-03-05 | 2020-03-21 | 1h | Cascade, wick liquidation, gap handling, funding, kill-switch discipline |
| [`china-mining-ban`](../packs/china-mining-ban/manifest.json) | crash | 2021-05-12 | 2021-05-25 | 1h | Controlled de-levering during a sharp multi-day decline |
| [`luna-collapse`](../packs/luna-collapse/manifest.json) | crash | 2022-05-05 | 2022-05-17 | 1h | Sustained drawdown management through contagion and volatility |
| [`ftx-2022`](../packs/ftx-2022/manifest.json) | crash | 2022-11-05 | 2022-11-16 | 1h | Position sizing through a multi-day insolvency-driven decline |
| [`yen-carry-unwind`](../packs/yen-carry-unwind/manifest.json) | crash | 2024-07-29 | 2024-08-09 | 1h | Weekend gap risk and discipline during a sharp recovery |
| [`10-10-cascade`](../packs/10-10-cascade/manifest.json) | crash | 2025-10-09 | 2025-10-14 | 1h | Extreme-wick liquidation discipline |
| [`spot-etf-approval`](../packs/spot-etf-approval/manifest.json) | advance/whipsaw | 2024-01-08 | 2024-01-26 | 4h | Momentum traps across a pop, reversal, and recovery |
| [`etf-rumor-whipsaw`](../packs/etf-rumor-whipsaw/manifest.json) | advance/whipsaw | 2023-10-13 | 2023-11-01 | 4h | Reversal discipline around unconfirmed market moves |
| [`election-run`](../packs/election-run/manifest.json) | advance | 2024-11-04 | 2024-12-07 | 4h | Holding discipline and the funding cost of a crowded long |
| [`q4-2020-institutional-run`](../packs/q4-2020-institutional-run/manifest.json) | advance | 2020-10-01 | 2021-01-01 | 4h | Patience through an extended trend |
| [`jan-2021-squeeze`](../packs/jan-2021-squeeze/manifest.json) | advance | 2021-01-01 | 2021-02-22 | 4h | Deep retracements inside a continuing trend |
| [`summer-2024-range`](../packs/summer-2024-range/manifest.json) | range | 2024-06-01 | 2024-07-29 | 4h | Funding bleed, overtrading, and range-trap discipline |
| [`2023-dead-zone`](../packs/2023-dead-zone/manifest.json) | range | 2023-06-01 | 2023-10-01 | 4h | Inactivity discipline in a low-volatility market |

The Summer-2024 window ends exactly where the yen-carry window begins, so the
two packs do not overlap.

## Data source and local build

V1 uses Binance USDT-M `BTCUSDT` perpetual-futures bulk archives hosted at
`data.binance.vision`. Each build recipe names exact monthly archive URLs for
trade bars, mark bars, index bars, and funding. The fetcher requires and checks
the upstream sibling SHA-256 file before using an archive. It does not call
Binance REST APIs.

Hydrate a committed manifest-only directory with ignored local series:

```sh
uv run wagmibench fetch-data \
  --pack covid-black-thursday
```

The command stages a full build, requires the generated manifest to be
byte-identical to committed `packs/<id>/manifest.json`, and then installs only
the missing series. Downloaded archives and built `bars_*.jsonl`,
`mark_*.jsonl`, `index_*.jsonl`, and `funding.jsonl` files are local-only and
must not be committed or redistributed with this project.

Before any public release, maintainers must re-read the current upstream terms
and run the repository and distribution-artifact data scanners. Source and
venue names identify provenance and mechanics only; they do not imply
affiliation, sponsorship, or endorsement.

## Pack invariants

A compatible pack reader must enforce the frozen
[IC-1 pack contract](contracts/IC-1.md), including:

- safe manifest-relative paths;
- exact file byte length, record count, and SHA-256;
- monotonic bar and funding timestamps;
- `available_at` not earlier than source time;
- complete trade, mark, and index series;
- funding settlement at the manifest-declared offsets and interval;
- funding cap, mark/trade divergence, and margin-ratio checks;
- JCS-based content hashing over the sorted file projection.

The exhaustive field definitions live in the generated
[schema field reference](contracts/field-reference.md).
