# AGENTS.md — operating WAGMI Bench

WAGMI Bench is a local, deterministic BTC perpetual-futures survival benchmark
for trading agents: it replays recorded market windows, feeds an agent
point-in-time observations, and seals decisions, risk checks, fills, ledgers,
and metrics into a verifiable evidence bundle. **Claim discipline:** every
result is `survival-stress` evidence — liquidation survival, drawdown control,
funding drag, turnover, rule-following under stress. When writing up a result,
never state or imply predictive ability, expected future returns, or adoption;
report what survived, what was liquidated, and how it read against fixed
baselines on frozen data. **Evidence rule:** bundles and reports are immutable.
Every `--output` must be a path that does not exist yet, and files inside a
bundle are never edited or hand-assembled. If a run was wrong, run again into a
new directory.

## Setup

Requires Git and `uv`. Run everything from the repository root.

```sh
uv sync --group dev
uv run wagmibench --help
```

Never activate the venv; every command is `uv run wagmibench ...`, and the only
bare-Python step is the agent server via `uv run python ...`.

## Keyless demo (no network, no keys)

```sh
uv run wagmibench run --pack fixtures/golden-mini/pack --output bundles/demo
uv run wagmibench report --bundle bundles/demo --output reports/demo
open reports/demo/report.html   # Linux: xdg-open
```

Committed synthetic fixture, keyless `momentum` policy — prove the toolchain
works here before spending anything.

## Real pack + baselines + compare

```sh
uv run wagmibench packs list
uv run wagmibench fetch-data --pack covid-black-thursday

for a in buyhold shorthold flat momentum; do
  uv run wagmibench run --pack covid-black-thursday --agent "$a" \
    --output "bundles/covid-$a"
done

uv run wagmibench compare \
  --bundle bundles/covid-buyhold --bundle bundles/covid-shorthold \
  --bundle bundles/covid-flat --bundle bundles/covid-momentum \
  --output reports/covid-compare
```

`packs list` prints the 13 catalog ids (crash/squeeze/range windows such as
`covid-black-thursday`, `luna-collapse`, `ftx-2022`, `10-10-cascade`,
`summer-2024-range`) and local availability. `fetch-data` downloads
checksum-verified `data.binance.vision` bulk archives into the ignored
`data/raw/` cache and builds ignored JSONL beside the committed manifest — once
per pack; later runs need no network. Any single bundle renders with
`report --bundle DIR --output DIR`.

## Benchmark a hosted model with the user's key

`--agent llm-local` runs a hosted model in-process — no container, no separate
adapter. This is the path for user-driven model runs.

```sh
uv run wagmibench run --pack covid-black-thursday --agent llm-local \
  --llm-provider anthropic --model MODEL_ID --max-output-tokens 128 \
  --output bundles/covid-model
```

- `--llm-provider`: `anthropic`, `openai`, `openrouter`, or `fireworks`.
- The key is read from the environment or a root `.env` under the provider's
  canonical name — `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`,
  `FIREWORKS_API_KEY`. Never pass a key as a flag.
- Before the first paid request the CLI prints a **worst-case** estimate (every
  turn, every retry, full output budget) and asks for confirmation. Real spend
  is normally well below it; one pack with a small model is typically well under
  a few dollars. Show the CLI's own numbers rather than quoting prices.
- `--confirm-spend` accepts non-interactively; use it only after the user has
  seen and accepted an estimate.

`--agent llm` is the separate cost-gated **sandboxed HTTP** path: it also needs
`--agent-url`, `--provider-domain`, `--credential-file`,
`--credential-env-name`, and explicit `--input-usd-per-million` /
`--output-usd-per-million`. Use it when an external adapter makes the priced
calls (see EvoSkill below) or for the sandbox lane.

## Bring the user's own agent (HTTP)

```sh
uv run wagmibench init my-agent
uv run python my-agent/agent_adapter.py          # terminal 1
```

Edit `Policy.decide` in `my-agent/agent_adapter.py`; leave the transport
envelope alone. `init` refuses to overwrite an existing directory. Then, from a
second terminal:

```sh
uv run wagmibench run --pack fixtures/golden-mini/pack \
  --agent http --agent-url http://127.0.0.1:8000 \
  --agent-name my-agent --output bundles/my-agent-demo
```

Contract summary — read `docs/adapter-guide.md` before writing `decide()`:

- `GET /healthz` returns 200 when ready. `POST /decide` receives one
  `runner_request/v1` and returns HTTP 200 with exactly one bare `action/v1`
  JSON object, `Content-Type: application/json`, ≤ 65,536 bytes, no Markdown
  fences or surrounding prose.
- `target` values are **signed decimal strings**, at most 4 decimal places
  (`"-1.25"` short, `"0"` flat). No fractional JSON numbers, NaN/Infinity, or
  duplicate keys.
- A malformed attempt 1 gets exactly one retry with the same observation plus
  validator feedback. A second malformed reply is a recorded rejected action;
  timeouts and crashes are recorded missed decisions; risk-gate violations are
  recorded blocked attempts, never clamped, never retried. Position is unchanged
  in all three cases and every attempt is evidence.
- The adapter never sees pack files, the manifest, real dates, the scenario
  name, or future rows. Optional attribution flags: `--agent-version`,
  `--prompt-sha256`, `--image-digest`, repeated `--endpoint-domain`. If the
  adapter calls a priced model, use `--agent llm` so the cost gate holds.

## EvoSkill skill folder as contestant

WAGMI Bench consumes EvoSkill-format artifacts —
`.claude/skills/<name>/SKILL.md` with YAML frontmatter — as the policy behind
`/decide`. A plain markdown strategy write-up is **not** a supported input yet;
convert it to SKILL.md format first.

```sh
export TRADEVOLVE_AGENT_MODE=evoskill
export TRADEVOLVE_EVOSKILL_SKILLS_DIR=examples/evoskill/.claude/skills
export TRADEVOLVE_LLM_PROVIDER=fireworks
export TRADEVOLVE_LLM_MODEL=PROVIDER_MODEL_ID
uv run python -m agents.server                   # terminal 1, port 8000
```

```sh
uv run wagmibench run --pack covid-black-thursday \
  --agent llm --agent-url http://127.0.0.1:8000 \
  --model PROVIDER_MODEL_ID --provider-domain api.fireworks.ai \
  --credential-file .env --credential-env-name FIREWORKS_API_KEY \
  --input-usd-per-million PRICE --output-usd-per-million PRICE \
  --output bundles/evoskill-covid
```

`SKILL.md` bodies and `references/*.md` are compiled deterministically into the
system prompt and committed by SHA-256 into the bundle; `scripts/` helpers are
never executed. Multiple folders load in sorted-name order, total text is capped
at 64 KiB, and malformed frontmatter fails loudly. Walkthrough plus a
format-exact example skill: `examples/evoskill/README.md`.

## Compare bundles and read the tier

`compare` renders one pack-grouped, survival-first table over two or more
COMPLETE bundles, always showing both cost profiles. Tiers are render-time
descriptions, never sealed into a bundle:

- **GMI** — survived, executed at least one fill, and finished with a net return
  above every **surviving** baseline in the same pack group under **both**
  cost profiles (a killed baseline's paper return does not set the bar).
- **NGMI** — the episode ended liquidated or killed flat.
- **—** — untiered: baselines themselves, zero-fill flat-holds, survivors that
  did not clear every baseline, and packs with no baseline bundles.

A tier is a comparison against fixed reference policies on identical frozen
data; it says nothing about future behavior. Run the baselines on the same pack
or the candidate is untiered by construction.

## Verify / replay a bundle

```sh
uv run wagmibench replay --bundle bundles/covid-momentum --pack covid-black-thursday
```

Replay re-drives the recorded action and timing events offline against the exact
pack and compares ledgers and metrics byte for byte; it requires the engine
version, schema major versions, and pack content hash the bundle recorded.
`COMPLETE` means the final seal, schemas, file hashes, both record chains,
record counts, decision projections, and `EpisodeEnd` all agree — only COMPLETE
bundles can be replayed, reported, or shared. `TRUNCATED` means no final seal
(crash evidence: preserve, never present as a finished run). `CORRUPT` means a
named check failed — preserve the original and investigate the first failure.
`share --bundle DIR --output DIR` makes a redacted sub-bundle with the same
evidence root, disclosing each removal in `redaction.json`.

## Gotchas

- **Output paths must be new.** `run`, `report`, `compare`, and `share` refuse
  to write into an existing directory; use a fresh name per attempt.
- **Fetch before running.** Committed `packs/` directories are manifest-only, so
  `run`/`replay` on an unfetched catalog id fails until `fetch-data` succeeds.
- **Network only for fetch**, plus any model provider you explicitly opt into.
- **Keys never enter files.** `.env` is gitignored (`.env.example` is the
  committed-safe template). Never write a key into a committed file, command
  line, URL, action, bundle, or report, and never echo one back to the user.
- **Never commit** `data/raw/`, built pack JSONL, archives, bundles, reports, or
  model responses. `bundles/` and `reports/` are already gitignored.
- **Sandbox lane is for reference results.** The Docker-isolated agent boundary
  (`sandbox/README.md`) exists for maintainer-run reference evidence. Default to
  `--agent llm-local` for user-driven model runs, `--agent http` for a local
  adapter.
- **No era-accuracy claims.** All 13 packs share one uniform V1 venue-parameter
  baseline pending primary-source verification; no reference results have been
  published.
