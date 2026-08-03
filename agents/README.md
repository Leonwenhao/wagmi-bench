# Reference agents

This subtree is the complete Docker build context for the M3 HTTP reference
agents. It intentionally contains no pack data, bundle data, credentials, or
repo-root `.env` file.

Build and run the deterministic reckless policy:

```sh
docker build --pull=false --network=none \
  -f agents/Dockerfile -t tradeevolve-agent agents
docker run --rm -p 127.0.0.1:8000:8000 \
  -e TRADEVOLVE_AGENT_MODE=reckless \
  tradeevolve-agent
```

The Dockerfile pins the Python 3.12 base by manifest digest. Override
`PYTHON_BASE` only with another explicitly reviewed
`name@sha256:<manifest-digest>` reference.

Set `TRADEVOLVE_RECKLESS_EGRESS_URL` only inside the locked-down sandbox when
testing the deny-all egress path. The policy catches the expected refusal and
still returns its action.

Run the Fireworks/OpenAI-compatible baseline by injecting credentials only at
container runtime:

```sh
docker run --rm -p 127.0.0.1:8000:8000 \
  -e TRADEVOLVE_AGENT_MODE=llm \
  -e TRADEVOLVE_LLM_MODEL=accounts/example/models/reference \
  -e FIREWORKS_API_KEY \
  tradeevolve-agent
```

Optional public settings are `TRADEVOLVE_LLM_BASE_URL`,
`TRADEVOLVE_LLM_TEMPERATURE`, `TRADEVOLVE_LLM_MAX_TOKENS`, and
`TRADEVOLVE_LLM_TIMEOUT_SECONDS`. The known Fireworks and OpenAI origins are
bound to `FIREWORKS_API_KEY` and `OPENAI_API_KEY`, respectively. A custom
HTTPS origin must explicitly name its own distinct credential variable with
`TRADEVOLVE_LLM_API_KEY_ENV`; known-provider keys cannot be reused for a
custom origin. Never provide an API key as a build argument, image environment
layer, URL, manifest field, or command-line value.

Each request uses provider-enforced `json_schema` output with the exact market
aliases from that blinded observation, then applies the complete local
`action/v1` validator. Only a natural `finish_reason: stop` and non-empty text
can become HTTP 200. The Fireworks request explicitly sets
`reasoning_effort: "none"` so Kimi spends its output budget on the action
rather than a separate reasoning trace; structured output alone does not
disable reasoning. Trusted prompt-cache usage is added under the frozen
schema's engine-ignored
`ext.x_tradeevolve_cached_input_tokens` field, while `action.usage` remains
the frozen two-field input/output object. A paid completion that fails the
local checks becomes a bounded HTTP 400 evidence envelope: it preserves the
exact model content when UTF-8 and within the IC-3 size cap, commits to its
hash and byte count otherwise, and carries only provider-reported token
usage. The harness content-addresses that 4xx response and follows the frozen
validator-retry path. Generic malformed runner requests remain the
content-free `invalid_contract` response.

The exact prompt commitment is available without a provider call:

```sh
python -m agents.prompt
```

`LLMBaselinePolicy.estimate_run(...)` computes the pre-run token and optional
decimal-USD estimate before any provider request or credential access.
