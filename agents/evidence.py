# SPDX-License-Identifier: Apache-2.0
"""Bounded paid-call evidence shared by every IC-6 agent boundary.

Both boundaries that host an :class:`~agents.llm.LLMBaselinePolicy` -- the
HTTP server in :mod:`agents.server` and the in-process adapter in
:mod:`agents.local` -- must hand the harness the *same* bytes for the same
invalid completion, so a run is identical whichever lane carried it.
"""

from __future__ import annotations

import hashlib
import json

from agents.common import AgentContractError, canonical_json_bytes
from agents.llm import InvalidProviderAction

MAX_ACTION_BYTES = 65_536


def invalid_provider_payload(error: InvalidProviderAction) -> bytes:
    """Encode bounded paid-call evidence for one invalid model completion."""

    evidence = error.evidence
    try:
        raw_output = evidence.provider_output.encode("utf-8")
        include_output = True
        output_encoding = "utf-8"
    except UnicodeEncodeError:
        raw_output = json.dumps(
            evidence.provider_output,
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")
        include_output = False
        output_encoding = "json_string_ascii"
    usage: dict[str, object] | None = None
    if (
        evidence.input_tokens is not None
        and evidence.output_tokens is not None
    ):
        usage = {
            "input_tokens": evidence.input_tokens,
            "output_tokens": evidence.output_tokens,
        }
        if evidence.cached_input_tokens is not None:
            usage["cached_input_tokens"] = (
                evidence.cached_input_tokens
            )
    base: dict[str, object] = {
        "error": "invalid_contract",
        "finish_reason": evidence.finish_reason,
        "provider_output_bytes": len(raw_output),
        "provider_output_encoding": output_encoding,
        "provider_output_sha256": (
            "sha256:" + hashlib.sha256(raw_output).hexdigest()
        ),
        "reason": evidence.reason,
        "usage": usage,
    }
    if include_output:
        base["provider_output"] = evidence.provider_output
    else:
        base["provider_output_omitted"] = "non_utf8"
    body = canonical_json_bytes(base)
    if len(body) <= MAX_ACTION_BYTES:
        return body
    base.pop("provider_output", None)
    base["provider_output_omitted"] = "oversize"
    body = canonical_json_bytes(base)
    if len(body) > MAX_ACTION_BYTES:
        raise AgentContractError("invalid evidence exceeds the IC-3 size limit")
    return body
