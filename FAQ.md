# WAGMI Bench FAQ

## What does a historical scenario result mean?

It is `survival-stress` evidence. It shows how the recorded action trace
interacted with liquidation rules, drawdown limits, funding, fees, turnover,
and safety gates in one historical market path. It does not establish
predictive ability, a repeatable edge, or future performance.

## Why is there a memorization caveat?

Models may recognize historical events from pretrained knowledge and price action.
Dates and scenario names are removed from agent observations, but that does
not make the episode unrecognizable.

Venue-constant era fingerprinting is an independent signal: the funding
cap/floor, fee tier, tick size, qty step, and min notional can identify a market
era even when price levels are normalized or clocks are rebased. WAGMI Bench
discloses those constants in every pack manifest rather than hiding them.

## Are the venue constants era-accurate?

No, and V1 does not claim they are. All 13 packs share a single uniform
venue-parameter baseline: the same funding cap/floor and interval, fee tier,
tick size, qty step, min notional, leverage cap, margin tiers, and execution
costs. Each manifest's `calibration_note` says so directly — "historical
exchangeInfo parameters remain pending primary-source verification before public
release." The baseline is deliberately conservative and identical across eras so
packs stay comparable; verifying it against the venue's historical
`exchangeInfo` is a prerequisite for any era-accuracy claim and has not been
done.

## Are the scenarios hidden from the operator?

No. The operator chooses a named pack and can inspect its manifest. Isolation
is an in-band boundary: the agent receives an opaque episode id, rebased time,
and only rows available by the virtual clock. The agent does not receive the
pack manifest, real dates, event name, or future rows.

## What is deterministic?

Given the exact pack bytes, recorded run configuration, and recorded
action/timing events, replay must regenerate the ledgers, metrics, and derived
decision records byte for byte. Canonical serialization and record-chain hashes
are deterministic across supported platforms.

The original agent is not necessarily rerunnable. A hosted model may change,
sampling may vary, and network timing may differ. WAGMI Bench records those
decisions and replays their economic consequences; it does not pretend a model
will produce the same text later.

## Why does replay require the original pack?

The evidence bundle records the pack content hash and manifest hash, not the
historical series themselves. Replay refuses a different pack, engine version,
or schema-major set instead of silently approximating compatibility.

## What happens if a bundle is interrupted?

Verification has three verdicts:

- `COMPLETE`: the final seal, record chains, files, schemas, and ending event
  agree.
- `TRUNCATED`: no final seal exists, but the committed prefix verifies.
- `CORRUPT`: a byte, link, schema, path, count, or seal check failed.

A truncated bundle remains useful crash evidence, but it cannot be reported,
replayed as complete, or shared as a finished result.

## Does the repository contain historical market data?

No. It contains source recipes, URLs, checksums, schemas, synthetic fixtures,
and pack manifests. `wagmibench fetch-data` downloads archives directly from
the upstream bulk host, verifies sibling checksums, and builds local ignored
series.

Do not commit, upload, or attach raw archives or built pack JSONL. Before a
public release, maintainers must re-read the current upstream terms and scan
both the tracked tree and built distribution artifacts.

## Why are there two cost profiles?

Every run emits primary and `stress_2x` ledgers and the same metric vocabulary
for both. The second profile applies doubled fee/spread/slippage assumptions to
the same action trace. Showing both makes cost sensitivity visible and prevents
one optimistic profile from carrying the scenario summary alone.

## Does the quickstart need an API key or Docker?

No. The synthetic-fixture and `MomentumAgent` path is in-process, keyless, and
offline after dependencies are installed. Docker is used for the untrusted
HTTP-agent isolation path, not for the first report.

## What leaves my machine on the model-backed path?

When you explicitly configure and authorize a model-backed HTTP adapter, the
adapter may send its system prompt and blinded episode observations to the
declared provider. The CLI shows a worst-case token/cost estimate and requires
explicit confirmation before the first paid decision request.

Review the provider, endpoint domain, model, pricing, retention policy, and
data-handling terms before confirming. Credentials stay in the agent container
environment and must never enter evidence.

## Is WAGMI Bench a live trading system?

No. V1 is Binance-only, historic-only BTC perpetual-futures simulation. It
does not place orders, custody assets, connect to a live venue API, or include
news, order-book, hosted verification, or forward-season functionality.

## Is this financial advice?

No. WAGMI Bench is evaluation and evidence infrastructure. Simulated results
and reports are not investment advice, a recommendation, or a promise of
future results.

## Is there a public release or a published reference matrix?

Not in this checkout. The current state is local-only. Public repository,
package, reference-result, and launch claims require separate founder approval
and release receipts.
