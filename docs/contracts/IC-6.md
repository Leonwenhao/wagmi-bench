# IC-6 — Agent Runner API (harness ↔ user's agent)

**Schemas:** [`runner_request/v1`](../../spec/schemas/runner_request.v1.schema.json) · [`runner_response/v1`](../../spec/schemas/runner_response.v1.schema.json)
**Status:** FROZEN v1 (founder sign-off 2026-07-26, DECISIONS.md #7). Changes now follow the VERSIONING.md migration procedure with written impact assessment.
**Complete field reference:** [`field-reference.md`](field-reference.md) — generated from the schema descriptions (`spec/tools/gen_field_reference.py`), CI-gated for 100% description coverage, render currency, and reverse-drift (SCH-2). This doc is the curated narrative; the generated reference is the exhaustive one.

## Transport

HTTP/1.1 over the sandbox-internal network is V1-canonical. The agent container exposes:

- `GET /healthz` — optional; harness polls before turn 0; 200 = ready. Failure to become healthy aborts the run **before any economic activity** (`EpisodeEnd{reason: aborted}`).
- `POST /decide` — the decision round trip.

In-process scripted reference agents implement `decide(request_dict) -> response_dict` with byte-identical envelope and validation semantics. MCP adapter is post-V1 (reserved `adapter` enum member). No auth headers; nothing secret ever transits this interface (local, sandboxed, SEC-1).

## Request — `runner_request/v1`

```json
{"schema": "runner_request/v1",
 "attempt": 1,
 "observation": { ...observation/v1... },
 "retry": null}
```

On the single retry (attempt 2), `retry` carries `{reason, detail, prior_raw_sha256}` — the exact attempt-1 validator error, verbatim, so the agent can self-correct in one round trip. The observation inside attempt 2 is **byte-identical** to attempt 1; `ObservationEmitted` is logged once per turn.

## Response — `runner_response/v1`

HTTP 200 with body = a single **bare `action/v1` document** (no response envelope; optionality lives inside IC-3). `Content-Type: application/json`, ≤ 65536 bytes, exactly one JSON document. The harness content-addresses the **verbatim response bytes** into `raw/NNNN-aK.txt` *before any parsing* — evidence first, interpretation second: the raw text exists even (especially) when parsing fails (SCH-5).

## Timeout / retry semantics (exact)

| Situation | Harness behavior | Recorded as |
|---|---|---|
| Valid `action/v1` within deadline | proceed to gates | `AgentResponded(1)` → `ActionParsed(from_attempt: 1)` |
| Malformed/invalid (IC-3 V1–V8) within deadline, attempt 1 | **ONE retry**: new POST, same observation, `retry` populated, fresh full deadline | `AgentResponded(1)` → `AgentResponded(2)` → `ActionParsed(2)` or `ActionRejected{…, attempts: 2}` |
| Malformed again on attempt 2 | give up this turn; position unchanged | `ActionRejected{reason of final failure, validator_error, attempts: 2}` |
| **Timeout** (no bytes within `response_deadline_ms`, either attempt) | **NO retry** — missed decision, position unchanged (SAFE-4) | `ActionRejected{timeout, attempts: 1}` |
| Connection refused / agent crash / 5xx | NO retry — missed decision; the run continues (a dead agent is data, not a run failure) | `ActionRejected{agent_error}` |
| HTTP 4xx from the agent | treated as malformed (retry path) | as malformed |
| Gate block (parsed but over-limit) | **never retried, never clamped** | `RiskCheck{verdict: block}`; position unchanged |

- `response_deadline_ms` lives in `bundle_manifest.run_config` (default **120000**; per-attempt) and is echoed to the agent in `observation.episode.response_deadline_ms`. It is an operator budget decision, not an agent attribute — hence run config, not agent manifest.
- Latency is measured harness-side (request-sent → last-byte-received), recorded per attempt in `AgentResponded.latency_ms` — recorded, never consumed (outside the determinism domain).
- `token_usage` is copied from the action's optional `usage` field; `null` when absent (explicit null, never missing).

## Sandbox context (normative cross-reference)

Agent container: pinned image (digest in `agent_manifest.image_sha256`), **deny-all egress** except `agent_manifest.endpoint_domains`; every refused attempt is emitted by the harness proxy as `EgressBlocked` into the same event stream (ISO-2, SAFE-1). The container holds no pack manifest, no scenario labels, no filesystem access to pack data beyond served observations (ISO-3), and never sees `time_rebase_offset_ms`.

## Design decisions (synthesis resolutions)

1. **Versioned wrapper request** (all three drafts) vs the dev-plan's bare `{observation}` POST: the retry must carry the validator error without perturbing the observation, whose hash must remain the hash of exactly what the agent saw; a wrapper is the only shape that does both (resolves open Q15).
2. **Bare `action/v1` response** (all three drafts, honoring the dev-plan): keeps the agent-side contract minimal; `runner_response/v1` is formally a `$ref` to `action/v1` so the response has its own versioned name without a second shape.
3. **Retry only on malformed output, never on timeout** (all three drafts; reading of the dev-plan's ambiguous "second failure → ActionRejected(timeout|invalid)"): a malformed answer is a correctable protocol error; retrying a timeout doubles worst-case wall time and blurs latency evidence (SAFE-4 treats timeouts as missed decisions).
4. **Top-level `attempt` field alongside `retry`** (trader shape merged with determinism/commerce's nested attempt): the discriminator is visible without descending into the retry object; `retry` is null exactly when `attempt` is 1.
5. **Health check before turn 0** (determinism + commerce): distinguishes "never came up" (aborted run, no economics) from "died mid-run" (missed decisions, run continues).
