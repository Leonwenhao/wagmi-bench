# Agent Adapter Guide

V1 agents implement a small HTTP/1.1 interface inside the local sandbox. The
engine and recorder remain outside that container; no pack files, manifests,
scenario labels, or real timestamps are mounted into it.

The frozen wire contract is [IC-6](contracts/IC-6.md). Field-level definitions
for requests, observations, and actions are in the generated
[schema reference](contracts/field-reference.md).

## Start from the scaffold

```sh
uv run wagmibench init wagmibench-agent
uv run python wagmibench-agent/agent_adapter.py
```

The generated adapter is a secret-free flat policy. It is deliberately small:
edit the `Policy.decide` method without changing the transport envelope.
`wagmibench init` refuses to overwrite an existing directory.

Test the health surface from another terminal:

```sh
curl --fail http://127.0.0.1:8000/healthz
```

Run the scaffold against the synthetic pack from that second terminal:

```sh
uv run wagmibench run \
  --pack fixtures/golden-mini/pack \
  --agent http \
  --agent-url http://127.0.0.1:8000 \
  --agent-name flat-scaffold \
  --output bundles/http-quickstart
```

The generic HTTP path is keyless and does not trigger a paid-model
confirmation. It records `model_id: "none"` and accepts optional
`--agent-version`, `--prompt-sha256`, `--image-digest`, and repeated
`--endpoint-domain` attribution fields. If the adapter invokes a priced model,
use `--agent llm` instead so the cost gate cannot be bypassed.

## Endpoints

### `GET /healthz`

This optional readiness endpoint returns HTTP 200 when the policy is ready.
The sandbox polls it before turn 0. Failure to become healthy aborts before
economic activity begins.

### `POST /decide`

The request is one `runner_request/v1` JSON object:

```json
{
  "schema": "runner_request/v1",
  "attempt": 1,
  "observation": {
    "schema": "observation/v1",
    "episode": {
      "episode_id": "ep_opaque_example",
      "turn": 0,
      "clock_ms": 230400000,
      "response_deadline_ms": 120000
    }
  },
  "retry": null
}
```

The abbreviated observation above illustrates the envelope, not a
schema-valid complete observation. Treat the received observation as opaque
input and use the full field reference when implementing a reader.

Return HTTP 200 with exactly one bare `action/v1` object:

```json
{
  "schema": "action/v1",
  "intent_kind": "leverage_target",
  "target": {
    "BTC": "0"
  },
  "comment": "remain flat"
}
```

Target values are signed leverage strings with at most four decimal places.
Negative means short, positive means long, and zero means flat. Do not emit
fractional JSON numbers. `max_slippage_bps` is an optional integer.

Response requirements:

- `Content-Type: application/json`;
- one JSON document, no Markdown fences or surrounding prose;
- at most 65,536 bytes;
- no `NaN`, infinity, duplicate keys, or fractional JSON-number lexemes;
- no credentials or sensitive data in `comment`, `usage`, or extension fields.

## Retry and failure behavior

Malformed attempt 1 receives one retry with the identical observation and:

```json
{
  "attempt": 2,
  "retry": {
    "reason": "schema_invalid",
    "detail": "validator detail",
    "prior_raw_sha256": "sha256:..."
  }
}
```

The exact `reason` vocabulary is defined by the schema. A second malformed
response becomes a recorded rejected action. A timeout, connection refusal,
agent crash, or server error is a recorded missed decision and is not retried.
A parsed action that violates a risk gate is recorded as a blocked attempt; it
is never clamped and never retried.

Position is unchanged on a missed, rejected, or blocked decision. Every attempt
is evidence, including invalid raw bytes.

## Point-in-time and identity boundary

An adapter must not infer that rebased time is wall time. The observation
contains:

- an opaque episode id;
- a rebased virtual clock;
- aliased market ids such as `BTC`;
- only bars/funding whose `available_at` is no later than that clock;
- current account and declared risk limits.

It does not contain the named scenario, real date, venue name, pack manifest,
or future rows. Historical recognition can still occur through price action
and venue-constant era fingerprinting (funding cap/floor, fee tier, tick size,
qty step, min notional); reports disclose that limitation.

## Model-backed HTTP path

The implemented CLI model path is explicit:

```sh
uv run wagmibench run \
  --pack covid-black-thursday \
  --agent llm \
  --agent-url http://127.0.0.1:8000 \
  --model PROVIDER_MODEL_ID \
  --provider-domain api.provider.example \
  --credential-file .env \
  --credential-env-name PROVIDER_API_KEY \
  --input-usd-per-million CURRENT_INPUT_PRICE \
  --output-usd-per-million CURRENT_OUTPUT_PRICE \
  --output bundles/model-run
```

Use decimal USD prices copied from the provider's current official pricing
page. The CLI first runs a keyless dry estimate against the largest observed
payload, prints the worst-case attempts and token cost, and asks for the exact
confirmation `yes`. `--confirm-spend` is the non-interactive equivalent and
must be used only after the operator has reviewed the same information.

Confirmation authorizes paid requests and transmission of the system prompt
and blinded episode observations to the configured provider. It is not
authorization to transmit pack files, real dates, manifests, credentials, or
unrelated repository data.

After confirmation and before the first request, the CLI reads only the
declared key from the plain credential file and reduces it to private
length/SHA-256 fingerprints. Exact key bytes and common reversible encodings
(hex, Base64, Base64url, and URL encoding) are rejected at the trusted HTTP
ingress before an `AgentReply` exists. A rejected response becomes a generic
missed decision; its bytes cannot enter raw blobs, events, reports, exception
bodies, or replay artifacts. The adapter retains no credential plaintext.
This is an exact/common-encoding disclosure gate, not a claim that arbitrary
semantic transformations by malicious code are recognizable. Network
exfiltration remains separately constrained by the ISO-2 sandbox.

The `--agent llm` selector is specifically the cost-gated model-backed HTTP
path. The generic local HTTP path is `--agent http`, and the in-process keyless
reference run remains `--agent momentum`.

## Credentials and egress

- Inject credentials only as agent-container environment variables.
- Declare the exact provider hostname in the agent manifest.
- Permit no unrelated domain, raw IP, or direct DNS egress.
- Never put a credential in a URL, command argument, image layer, config file,
  action, bundle, or report.
- Keep the key in a plain gitignored `.env`; identify it with
  `--credential-env-name` so the trusted response-ingress gate is mandatory.
- Keep provider calls inside the sandboxed adapter, not in the deterministic
  engine process.

The sandbox records blocked attempts as `EgressBlocked` events. See the
[sandbox boundary](../sandbox/README.md) for the enforcement model and known
limitations.

## Adapter acceptance checklist

- `/healthz` becomes ready before the configured timeout.
- `/decide` accepts a complete `runner_request/v1`.
- A valid action is returned as bare JSON within the response deadline.
- The same observation is used on attempt 2.
- Malformed output and timeouts do not crash the episode.
- The adapter never receives or reads pack files.
- No future `available_at` appears in served observations.
- Container filesystem and egress checks pass.
- A complete run verifies and replays with the exact pack.
- An exact and commonly encoded secret canary is rejected before evidence
  capture and is absent from bundle, logs, and report.
