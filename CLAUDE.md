# CLAUDE.md — WAGMI Bench

WAGMI Bench is a local, deterministic BTC perpetual-futures survival benchmark:
it replays recorded market windows, feeds an agent point-in-time observations,
and seals decisions, risk checks, fills, ledgers, and metrics into a verifiable
evidence bundle. Results are `survival-stress` evidence — liquidation survival,
drawdown control, funding drag, turnover, rule-following. When reporting a
result, never state or imply predictive ability, expected future returns, or
adoption; say what survived, what was liquidated, and how it read against the
baselines on frozen data. Bundles and reports are immutable: every `--output`
must be a path that does not exist yet, and nothing inside a bundle is ever
edited. `AGENTS.md` has the fuller reference; this file is the short version.

## When the user asks X, do Y

| User asks | Do |
|---|---|
| "does this thing work / show me a report" | Keyless demo below (fixture, no keys). |
| "run the crash packs" / names a scenario | `packs list` → `fetch-data --pack ID` → run baselines + candidate → `compare`. |
| "benchmark GPT/Claude/a model on this" | `--agent llm-local` with the user's key; surface the cost estimate before confirming. |
| "wire in my strategy/bot" | `wagmibench init` → implement `decide()` → `--agent http --agent-url`. |
| "wire my EvoSkill skills in" | `TRADEVOLVE_AGENT_MODE=evoskill` + `agents.server`, then `--agent llm --agent-url`. |
| "here's my strategy in markdown" | Not a supported input; it must be SKILL.md format first. |
| "how did my agent do vs baselines" | `compare` over the candidate + baseline bundles on the same pack; read the tier. |
| "is this result real / can you verify it" | `replay --bundle --pack`; require verdict `COMPLETE`. |
| "share this result" | `wagmibench share --bundle DIR --output DIR` (redacted sub-bundle). |

## Setup

```sh
uv sync --group dev
```

Run everything from the repository root as `uv run wagmibench ...`.

## Keyless demo

```sh
uv run wagmibench run --pack fixtures/golden-mini/pack --output bundles/demo
uv run wagmibench report --bundle bundles/demo --output reports/demo
open reports/demo/report.html   # Linux: xdg-open
```

## Real pack, baselines, compare

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

`fetch-data` needs network once per pack; runs, replays, and reports do not.

## Hosted model with the user's key

```sh
uv run wagmibench run \
  --pack covid-black-thursday \
  --agent llm-local \
  --llm-provider anthropic \
  --model MODEL_ID \
  --max-output-tokens 128 \
  --output bundles/covid-model
```

Providers: `anthropic`, `openai`, `openrouter`, `fireworks`. The key is read
from the environment or root `.env` under the canonical name
(`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`,
`FIREWORKS_API_KEY`) — never as a flag. The CLI prints a worst-case cost
estimate (all turns, all retries, full output budget) and asks for confirmation;
real spend is normally far lower, and one pack with a small model is typically
well under a few dollars. Show the CLI's numbers; do not quote prices.
`--confirm-spend` accepts non-interactively — only after the user has seen an
estimate. `--agent llm` is the different sandboxed HTTP path (needs
`--agent-url`, `--provider-domain`, `--credential-env-name`, and explicit
per-million prices).

## The user's own agent

```sh
uv run wagmibench init my-agent
uv run python my-agent/agent_adapter.py          # terminal 1
```

Implement `Policy.decide` without touching the transport envelope, then:

```sh
uv run wagmibench run --pack fixtures/golden-mini/pack \
  --agent http --agent-url http://127.0.0.1:8000 \
  --agent-name my-agent --output bundles/my-agent-demo
```

`/decide` must return one bare `action/v1` JSON object (≤ 64 KiB, no fences),
with `target` as signed decimal strings of at most 4 decimal places. One retry
on a malformed first attempt; timeouts are recorded missed decisions and
risk-gate violations are recorded blocked attempts. Full grammar:
`docs/adapter-guide.md`.

## EvoSkill skills as contestant

```sh
export TRADEVOLVE_AGENT_MODE=evoskill
export TRADEVOLVE_EVOSKILL_SKILLS_DIR=examples/evoskill/.claude/skills
export TRADEVOLVE_LLM_PROVIDER=fireworks
export TRADEVOLVE_LLM_MODEL=PROVIDER_MODEL_ID
uv run python -m agents.server                   # terminal 1
```

```sh
uv run wagmibench run --pack covid-black-thursday \
  --agent llm --agent-url http://127.0.0.1:8000 \
  --model PROVIDER_MODEL_ID --provider-domain api.fireworks.ai \
  --credential-file .env --credential-env-name FIREWORKS_API_KEY \
  --input-usd-per-million PRICE --output-usd-per-million PRICE \
  --output bundles/evoskill-covid
```

`SKILL.md` bodies and `references/*.md` compile into the system prompt and are
committed by SHA-256 into the bundle; `scripts/` is never executed. Skill inputs
must be SKILL.md format — a plain markdown strategy file is not yet supported.
Details: `examples/evoskill/README.md`.

## Tiers

`compare` groups by pack and always shows both cost profiles. GMI: survived,
executed at least one fill, and net return above every surviving baseline in the pack
group under both cost profiles. NGMI: the episode ended liquidated or killed
flat. `—`: untiered — baselines, zero-fill flat-holds, survivors that did not
clear every baseline, and packs with no baselines. Tiers are render-time
comparisons on frozen data, not statements about future behavior.

## Replay

```sh
uv run wagmibench replay --bundle bundles/covid-momentum --pack covid-black-thursday
```

`COMPLETE` means seal, schemas, file hashes, both record chains, record counts,
decision projections, and `EpisodeEnd` all agree; only COMPLETE bundles can be
replayed, reported, or shared. `TRUNCATED` is crash evidence (never present as a
finished run); `CORRUPT` means a named check failed.

## Gotchas

- Output paths must be new — `run`, `report`, `compare`, `share` refuse to write
  into an existing directory.
- Fetch a pack before running it; committed `packs/` dirs are manifest-only.
- Network is needed for `fetch-data` only (plus any provider you opt into).
- `.env` is gitignored; never commit, paste into a file, or echo an API key.
- Never commit `data/raw/`, pack JSONL, archives, bundles, reports, or model
  responses.
- The Docker sandbox lane (`sandbox/README.md`) is for maintainer reference
  results; default to `--agent llm-local` for user-driven model runs.
