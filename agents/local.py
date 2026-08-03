# SPDX-License-Identifier: Apache-2.0
"""In-process IC-6 adapter around one decision policy.

The runner may host a model policy directly instead of talking to a sandboxed
HTTP adapter. The reply this wrapper returns is deliberately identical to what
:mod:`agents.server` would have put on the wire for the same policy outcome --
same canonical bytes, same status, same bounded paid-call evidence -- so the
engine's retry and missed-decision semantics do not depend on the lane.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from agents.common import (
    AgentContractError,
    DecisionPolicy,
    canonical_json_bytes,
)
from agents.evidence import MAX_ACTION_BYTES, invalid_provider_payload
from agents.llm import InvalidProviderAction
from harness.protocol import AgentReply

_HTTP_OK = 200
_HTTP_BAD_REQUEST = 400


def _elapsed_ms(started_ns: int) -> int:
    return (time.monotonic_ns() - started_ns) // 1_000_000


@dataclass(frozen=True, slots=True)
class LocalLLMAgent:
    """Host a policy in the runner process behind the frozen reply shape.

    A valid action becomes a 200 reply carrying canonical bytes. An invalid
    paid completion becomes a 400 reply whose body is the server's bounded
    evidence document, which the engine treats as ``schema_invalid`` and
    answers with exactly one validator-informed retry. Every other failure
    propagates so the engine records it as ``agent_error`` without retrying,
    matching the HTTP lane's 5xx handling.
    """

    policy: DecisionPolicy

    def decide(self, request: dict[str, object]) -> AgentReply:
        """Return one verbatim reply, never leaking provider or key detail."""

        started_ns = time.monotonic_ns()
        try:
            action = self.policy.decide(request)
            body = canonical_json_bytes(action)
            if len(body) > MAX_ACTION_BYTES:
                raise AgentContractError("action exceeds the IC-3 size limit")
        except InvalidProviderAction as exc:
            return AgentReply(
                body=invalid_provider_payload(exc),
                latency_ms=_elapsed_ms(started_ns),
                http_status=_HTTP_BAD_REQUEST,
            )
        return AgentReply(
            body=body,
            latency_ms=_elapsed_ms(started_ns),
            http_status=_HTTP_OK,
        )
