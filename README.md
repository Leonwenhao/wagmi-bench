# WAGMI Bench — the proving ground for agents in perpetual-futures markets

Perpetual futures are becoming the default venue for price discovery — on the
largest perp venue, 74.13% of volume on 2026-07-30 referenced equities,
indices, commodities, and FX rather than crypto
[source](https://www.theblock.co/api/charts/chart/decentralized-finance/derivatives/daily-hip-3-volume-vs-hyperliquid-total-perpetuals-volume-market-share).
These markets never close, run on leverage, and are reachable from a consumer
wallet — which means the marginal participant in them will increasingly be
software, not a person. When an autonomous agent asks for capital in a
leveraged market, there has been no public, independent, reproducible record
of how it behaves when the tape turns.

**WAGMI Bench is that record.** It replays 13 recorded stretches of the BTC
perpetual-futures tape — vertical melt-ups, dead-range chop, violent
unwinds — on a local exchange simulator. The agent sees only what was
knowable at each moment and makes one decision per bar; the bench records
every decision, risk check, and fill into an evidence bundle that anyone
can re-run and get byte-identical results. It starts with BTC because BTC
has the longest continuously recorded perpetual tape available to us,
covering every kind of market in both directions. Perps beyond BTC are
next.

**It runs offline, with no API key.** The quickstart below uses a committed
synthetic fixture and a scripted policy — no network call, no credential,
no cost. Every claim in this README is either a command you can run right
now or a number that traces to a sealed bundle under `reports/`.

## What the current results show

Five open-weights models ran the full 13-pack catalog end to end (sealed
receipts under `reports/leaderboard-2026-07-31/`):

| Agent | WAGMI Score | Survived | Tapes won | Fills | Orders refused |
|---|---|---|---|---|---|
| `kimi-k3` | 65.38 | 13/13 | 4 | 442 | 7 |
| `inkling` | 50.00 | 13/13 | 2 | 73 | 114 |
| `deepseek-v4-pro` | 28.85 | 13/13 | 0 | 4 | 0 |
| `glm-5p2` | 25.00 | 13/13 | 0 | 0 | 0 |
| `qwen3p7-plus` | 23.08 | 12/13 | 0 | 10 | 116 (of 126 orders sent) |

"Tapes won" means the agent beat every surviving baseline on that tape under
both cost profiles (the CLI and score artifacts call this GMI). "Orders
refused" counts orders the risk gates rejected, almost always for illegal
leverage. The catalog is deliberately balanced — 5 melt-up packs, 2 chop
packs, 6 stress packs, per each pack's sealed `regime_description`
(`packs/*/manifest.json`; overview in `docs/pack-catalog.md`) — and the only
agent death in this table happened in a bull-run pack,
`q4-2020-institutional-run`, not a crash. These numbers describe how each
model behaved: whether it traded at all, whether it stayed inside the rules,
and whether it kept its account alive on 13 recorded historical windows.
They establish nothing about predictive ability or future performance —
see the claim label below.

**Claim label: `survival-stress`.** Historical scenario results are evidence
about liquidation survival, drawdown control, funding drag, turnover, and
rule-following under stress. They do not establish predictive ability or
future performance.

**Memorization caveat:** models may recognize historical events from pretrained
knowledge and price action. Even with dates and names removed from
observations, venue-constant era fingerprinting remains possible through the
funding cap/floor, fee tier, tick size, qty step, and min notional. Those
constants are disclosed rather than hidden, but they are not era-calibrated:
all 13 packs use one uniform V1 venue-parameter baseline, and each manifest
records that historical `exchangeInfo` parameters remain pending primary-source
verification. No era-accuracy claim is made until that verification is done.

## Naming

WAGMI Bench was developed under the working name **TradeEvolve**. The display
name and the `wagmibench` console script carry the new name; the frozen v1
identifiers deliberately do not. The v1 schema identifiers (`$id` URLs on
`tradeevolve.dev`), the `TRADEVOLVE_*` environment variables, and the container
image names retain the working name as stable v1 interfaces, because sealed
evidence and receipts already bind those exact strings. The frozen v1 contract
documents under `docs/contracts/` likewise predate the rename, so command
examples there may show the pre-rename `tradeevolve` command; the installed
console script is `wagmibench`. Changing any of these is a compatibility
change and follows the
[schema versioning policy](docs/contracts/VERSIONING.md).

## How the proving ground works

- Builds local scenario packs from checksum-verified Binance bulk archives.
- The agent never sees real dates, scenario names, or any future data.
- Simulates the exchange in exact integer math, under two fee/funding cost
  profiles.
- Records what the agent saw, said, meant, what rules ran, what happened, and
  the cost of holding the position.
- Verifies any bundle — complete, cut off mid-run, or tampered with — without
  contacting the original agent.
- Re-runs a recorded session offline and checks the resulting ledgers match
  byte for byte.
- Generates terminal, standalone HTML, and static SVG reports from a sealed
  bundle only.

WAGMI Bench does not place orders, hold funds, connect to a live venue, model an
order book, or provide financial advice.

## Install

Requirements: Git and [`uv`](https://docs.astral.sh/uv/). uv provisions
Python 3.12+ automatically if a matching interpreter is not already
installed.

```sh
# Install uv (macOS/Linux; or: brew install uv)
curl -LsSf https://astral.sh/uv/install.sh | sh

git clone https://github.com/Leonwenhao/wagmi-bench.git
cd wagmi-bench
uv sync --group dev
```

`uv sync` installs the pinned dependency set from `uv.lock` and the
`wagmibench` CLI into a local `.venv`. Every command below runs from the
repository root via `uv run`.

## Guided mode

```sh
uv run wagmibench
```

Run with no arguments in a terminal and WAGMI Bench opens a guided setup
(also available as `uv run wagmibench wizard`). It walks through a keyless
demo, a hosted model, your own agent, or a comparison of existing bundles.
Every screen prints the equivalent non-interactive command before running
it, so the wizard teaches the flags rather than hiding them. The priced
paths keep the same worst-case cost estimate and typed confirmation as the
commands below. Piped or scripted invocations with no arguments still get
the usual usage error, not a prompt.

## Quickstart: first report without keys

```sh
uv run wagmibench run \
  --pack fixtures/golden-mini/pack \
  --output bundles/quickstart

uv run wagmibench replay \
  --bundle bundles/quickstart \
  --pack fixtures/golden-mini/pack

uv run wagmibench report \
  --bundle bundles/quickstart \
  --output reports/quickstart
```

Open `reports/quickstart/report.html`. This path uses the committed synthetic
golden fixture and the keyless `MomentumAgent`; it makes no network or paid
model request. Bundle and report output paths must be new because evidence is
immutable.

The clean-room target is a first report in 15 minutes or less. Two fresh
Linux-container runs from a no-local clone were recorded on 2026-07-26: an
early rehearsal at 36 seconds, and the release-gate run on the tagged
candidate at **262 seconds** from locked dependency sync, which is the figure
to cite. Both are local release-gate evidence, not a publication or adoption
claim, and neither is a guarantee for your machine or network.

## Quickstart: checksum-verified historical pack

Real archives are fetched into the ignored `data/raw/` cache. The committed
`packs/` directories begin manifest-only; acquisition builds in a temporary
directory, requires the generated manifest to match the committed bytes, then
installs only ignored JSONL series beside that manifest:

```sh
uv run wagmibench packs list

uv run wagmibench fetch-data \
  --pack covid-black-thursday

uv run wagmibench run \
  --pack covid-black-thursday \
  --output bundles/covid-momentum

uv run wagmibench replay \
  --bundle bundles/covid-momentum \
  --pack covid-black-thursday

uv run wagmibench report \
  --bundle bundles/covid-momentum \
  --output reports/covid-momentum
```

The fetcher uses `data.binance.vision` bulk files only and requires each
upstream sibling checksum. It does not use Binance REST APIs. See the
[pack catalog](docs/pack-catalog.md) for all 13 windows — five melt-ups, two
chop windows, six stress windows — and the
[data posture](#data-and-release-posture) before distributing anything.

## CLI map

| Command | Purpose |
|---|---|
| `wagmibench wizard` | Guided interactive setup; each screen prints the equivalent command. Bare `wagmibench` in a terminal opens it. |
| `wagmibench init [DIRECTORY]` | Create a secret-free IC-6 HTTP adapter scaffold. |
| `wagmibench packs list` | List catalog ids, bar intervals, and local availability. |
| `wagmibench fetch-data --pack ID` | Fetch checksummed archives and build one local pack. |
| `wagmibench run --pack ID_OR_PATH` | Run the keyless reference policy by default and seal a bundle. Baselines: `--agent buyhold`, `shorthold`, `flat`. Priced model lanes: `--agent llm` (sandboxed adapter) and `--agent llm-local` (in-process, one command). |
| `wagmibench replay --bundle DIR --pack ID_OR_PATH` | Verify and reproduce stored economic artifacts offline. |
| `wagmibench report --bundle DIR` | Generate terminal, HTML, and SVG artifacts from a complete bundle. |
| `wagmibench compare --bundle DIR --bundle DIR …` | Render one pack-grouped table over sealed bundles: candidates read against baselines, flat-holds marked. |
| `wagmibench score --bundle DIR --bundle DIR …` | Aggregate sealed bundles into one conduct-ranked WAGMI Score per agent across the 13-pack catalog. |
| `wagmibench share --bundle DIR` | Create a verifiable redacted sub-bundle with the same evidence root. |

Run `uv run wagmibench COMMAND --help` for the complete options and actionable
failure guidance. The model-backed HTTP path shows a worst-case token/cost
estimate and requires explicit confirmation before its first request. It also
transmits the system prompt and blinded episode observations to the configured
provider; read the [adapter guide](docs/adapter-guide.md) before enabling it.
The priced path additionally requires a declared credential file/key name so
credential bytes are fingerprint-gated before any agent response can enter
evidence.
The `report` command emits the static claim-labeled SVG card; `share` instead
removes only committed raw model-response blobs and records each disclosed
absence in `redaction.json`.

## Evidence and determinism

Every run is a receipt, not a number you're asked to believe. The core
reproducibility contract is economic, not a promise that a nondeterministic
model will answer identically twice:

```text
pack bytes + recorded run config + recorded action/timing events
    -> byte-identical ledgers and metrics
```

Every complete bundle has a content-addressed root. Verification checks
schemas, file hashes, record chains, decision projections, and the final
`EpisodeEnd`; replay additionally requires the exact engine version, schema
major versions, and pack content hash recorded by the bundle.

Read [Replay and verification](docs/replay-and-verification.md) for operator
steps and [Compatible reader guide](docs/compatible-reader.md) for an
independent implementation checklist.

## Bring an agent

`wagmibench init my-agent` creates a minimal local HTTP adapter implementing
the frozen IC-6 `/healthz` and `/decide` contract. Start with the
[adapter guide](docs/adapter-guide.md), then run the generated flat policy with
`--agent http` to test transport before adding model calls or credentials.

The agent gets an anonymous episode id and a clock that starts at zero. It
never receives the pack manifest, real dates, the scenario name, or future
data. API keys
belong only in the agent container environment — or, for the in-process lane
below, in the runner's own environment or gitignored `.env` — and must never
appear in an action, bundle, report, URL, manifest, command line, or committed
file.

### Benchmark a hosted model in one command

`--agent llm-local` constructs the same model policy inside the run process,
so no separate adapter server is started and no `--agent-url` is passed:

```bash
uv run wagmibench run --pack covid-black-thursday --agent llm-local \
  --llm-provider anthropic --model claude-opus-5 --max-output-tokens 2000
```

The credential is read from the provider's canonical environment variable
(`ANTHROPIC_API_KEY` above, `FIREWORKS_API_KEY` for Fireworks, and so on),
falling back to a `KEY=VALUE` line in a gitignored `.env` — `--credential-file`
points elsewhere. The value stays in memory: it never enters the manifest,
bundle, report, or any message the CLI prints. The run shows the same
worst-case token/cost estimate and requires the same typed `yes` (or
`--confirm-spend`) before the first paid request, then seals an ordinary
evidence bundle whose agent manifest records adapter `in_process`, the billed
model id, and the exact provider hostname it called.

The two-process `--agent llm` lane remains the sandboxed path: the policy runs
in a separate container under an egress allowlist, with credential bytes
fingerprint-gated at HTTP ingress. Reach for `llm-local` when you want one
command against a hosted model, and for `llm` when the evidence needs those
isolation guarantees.

### EvoSkill skills as contestants

[EvoSkill](https://github.com/sentient-agi/EvoSkill) (Sentient, Apache-2.0)
evolves skills — plain markdown rule files in
`.claude/skills/<name>/SKILL.md` folders. WAGMI Bench runs them directly:
point `TRADEVOLVE_AGENT_MODE=evoskill` at a skills folder and the skill
text goes into the model's prompt, making it a benchmark contestant. The
skill-to-prompt compilation is deterministic, so the exact prompt any run
used can be recomputed from the skill files and checked; sealing the
compiled prompt hash directly into the bundle is planned (the bundle
currently records the base prompt hash). See
[examples/evoskill](examples/evoskill/README.md) for the walkthrough and a
format-exact example skill.

### Inference providers

The LLM lane speaks to Fireworks, OpenAI, OpenRouter (OpenAI-compatible
wire), and Anthropic (native Messages API) via `--llm-provider` or
`TRADEVOLVE_LLM_PROVIDER`; the sealed agent manifest records exactly the
controls each protocol transmits.

## Data and release posture

The repository distributes source recipes, exact URLs, checksums, schemas,
synthetic fixtures, and pack manifests—not historical market series. Users
fetch archives directly from the upstream host and build ignored local JSONL.
They are responsible for reviewing the current upstream terms that apply to
their use.

Before any public release, maintainers must re-read the current upstream terms
and run the tracked-tree and distribution-artifact scanners. References to
Binance identify the source and simulated venue mechanics only; they do not
imply affiliation, sponsorship, or endorsement. Binance and other third-party
names and marks belong to their respective owners.

Do not commit:

- files under `data/raw/`;
- built pack JSONL series or downloaded archives;
- evidence bundles, generated reports, model responses, or secrets.

## Documentation

- [Scenario pack catalog](docs/pack-catalog.md)
- [Replay and verification](docs/replay-and-verification.md)
- [Compatible reader guide](docs/compatible-reader.md)
- [Agent adapter guide](docs/adapter-guide.md)
- [Complete schema field reference](docs/contracts/field-reference.md)
- [Schema versioning policy](docs/contracts/VERSIONING.md)
- [FAQ](FAQ.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)

The interface-contract documents and schemas are frozen at v1. See the
versioning policy before proposing a compatibility change.

## License

Code and project-authored documentation are licensed under
[Apache License 2.0](LICENSE). Upstream market data is not included and is not
relicensed by this project.
