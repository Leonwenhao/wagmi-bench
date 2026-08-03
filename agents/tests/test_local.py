# SPDX-License-Identifier: Apache-2.0
"""The in-process adapter must mirror the HTTP boundary byte-for-byte."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field

import pytest

from agents.common import canonical_json_bytes
from agents.evidence import invalid_provider_payload
from agents.llm import (
    InvalidProviderAction,
    LLMBaselinePolicy,
    LLMConfig,
    ProviderError,
    ProviderResult,
)
from agents.local import LocalLLMAgent
from agents.prompt import load_prompt
from agents.tests.helpers import runner_request

_ACTION = (
    '{"schema":"action/v1","intent_kind":"leverage_target",'
    '"target":{"BTC":"1.5"},"max_slippage_bps":25}'
)


@dataclass(slots=True)
class FakeProvider:
    """Paid-call-free provider seam returning one scripted completion."""

    content: str = _ACTION
    finish_reason: str | None = "stop"
    input_tokens: int | None = 123
    output_tokens: int | None = 7
    error: Exception | None = None
    calls: list[Mapping[str, object]] = field(default_factory=list)

    def complete(
        self,
        *,
        system_prompt: str,
        observation: Mapping[str, object],
        retry_feedback: Mapping[str, object] | None,
    ) -> ProviderResult:
        assert system_prompt
        del retry_feedback
        self.calls.append(observation)
        if self.error is not None:
            raise self.error
        return ProviderResult(
            content=self.content,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            finish_reason=self.finish_reason,
        )


def _agent(provider: FakeProvider) -> LocalLLMAgent:
    config = LLMConfig(
        base_url="https://api.anthropic.com",
        model="claude-opus-5",
        max_tokens=2_000,
        api_key_env_name="ANTHROPIC_API_KEY",
    )
    return LocalLLMAgent(
        policy=LLMBaselinePolicy(
            provider=provider,
            config=config,
            system_prompt=load_prompt(),
        )
    )


def test_valid_action_becomes_a_canonical_200_in_process_reply() -> None:
    provider = FakeProvider()

    reply = _agent(provider).decide(runner_request())

    assert reply.http_status == 200
    assert reply.transport == "in_process"
    assert reply.latency_ms >= 0
    decoded = json.loads(reply.body.decode("utf-8"))
    assert decoded["target"] == {"BTC": "1.5"}
    assert decoded["usage"] == {"input_tokens": 123, "output_tokens": 7}
    assert reply.body == canonical_json_bytes(decoded)
    assert len(provider.calls) == 1


def test_invalid_completion_reproduces_the_server_evidence_payload() -> None:
    agent = _agent(FakeProvider(content="not an action at all"))

    with pytest.raises(InvalidProviderAction) as raised:
        agent.policy.decide(runner_request())
    expected = invalid_provider_payload(raised.value)

    reply = agent.decide(runner_request())

    assert reply.http_status == 400
    assert reply.transport == "in_process"
    assert reply.body == expected
    assert b"invalid_contract" in reply.body


def test_truncated_completion_is_reported_as_a_400_finish_reason() -> None:
    reply = _agent(
        FakeProvider(content="", finish_reason="length")
    ).decide(runner_request())

    payload = json.loads(reply.body.decode("utf-8"))

    assert reply.http_status == 400
    assert payload["reason"] == "finish_reason_not_stop"
    assert payload["finish_reason"] == "length"


def test_provider_failure_propagates_for_the_engine_to_record() -> None:
    agent = _agent(FakeProvider(error=ProviderError("provider request failed")))

    with pytest.raises(ProviderError):
        agent.decide(runner_request())
