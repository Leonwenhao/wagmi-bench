# EvoSkill skills as WAGMI Bench contestants

This example runs an [EvoSkill](https://github.com/sentient-agi/EvoSkill)
(Sentient, Apache-2.0) skill artifact as the policy of a WAGMI Bench
contestant. EvoSkill evolves Agent-Skills-format artifacts —
`.claude/skills/<name>/SKILL.md` with YAML frontmatter and markdown
instructions — and its deployment guidance is to copy `.claude/skills/`
into your deployment. WAGMI Bench is such a deployment: the skill bodies
are compiled deterministically into the system prompt of the LLM policy
behind the IC-6 `/decide` endpoint, and the compiled prompt is committed
into the sealed evidence bundle by SHA-256.

**What this is:** a consumer of EvoSkill-format skill artifacts, so an
evolved skill folder drops in unchanged as a benchmark contestant.

**What this is not:** a run of EvoSkill's evolution loop. Evolving skills
against WAGMI Bench packs (frozen-twin runs, Harbor task bridging) is a
separate, larger integration.

## Run it

```sh
export TRADEVOLVE_AGENT_MODE=evoskill
export TRADEVOLVE_EVOSKILL_SKILLS_DIR=examples/evoskill/.claude/skills
export TRADEVOLVE_LLM_MODEL=accounts/fireworks/models/kimi-k3
python -m agents.server  # serves /healthz and /decide
```

Then, from another shell, run any pack against it with
`wagmibench run --agent llm --agent-url http://127.0.0.1:<port> …` and
compare the sealed bundle against the keyless baselines with
`wagmibench compare`.

To use a real evolved skill, point `TRADEVOLVE_EVOSKILL_SKILLS_DIR` at the
`.claude/skills/` folder an EvoSkill run produced. Multiple skills load in
sorted-name order; total skill text is bounded (64 KiB) and malformed
frontmatter fails loudly.

## Fidelity notes

- `SKILL.md` bodies and `references/*.md` are injected into the system
  prompt unconditionally (a `/decide` call is single-shot, so there is no
  progressive disclosure). `scripts/` helpers are **never executed**.
- The example skill here (`perps-leverage-discipline`) is hand-written in
  the exact format `mode="skill_only"` runs emit, so it doubles as a format
  fixture.

## Citation

EvoSkill: Automated Skill Discovery for Multi-Agent Systems.
Alzubi et al., Sentient + Virginia Tech, 2026. arXiv:2603.02766.
