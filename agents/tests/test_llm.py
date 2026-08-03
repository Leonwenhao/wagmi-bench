# SPDX-License-Identifier: Apache-2.0
"""Paid-call-free LLM baseline and compatible-provider tests."""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast

import pytest

from agents.cost import ModelPricing
from agents.llm import (
    CACHED_INPUT_EXTENSION_KEY,
    MAX_IJSON_INTEGER,
    MAX_RETRY_CORRECTION_BYTES,
    InvalidProviderAction,
    LLMBaselinePolicy,
    LLMConfig,
    OpenAICompatibleProvider,
    ProviderError,
    ProviderResult,
    provider_retry_message,
)
from agents.prompt import load_prompt
from agents.tests.helpers import runner_request
from core.engine import _parse_detail


@dataclass(slots=True)
class FakeProvider:
    calls: list[Mapping[str, object]] = field(default_factory=list)
    feedbacks: list[Mapping[str, object] | None] = field(default_factory=list)

    def complete(
        self,
        *,
        system_prompt: str,
        observation: Mapping[str, object],
        retry_feedback: Mapping[str, object] | None,
    ) -> ProviderResult:
        assert system_prompt
        self.calls.append(observation)
        self.feedbacks.append(retry_feedback)
        return ProviderResult(
            content=(
                '{"schema":"action/v1","target":{"BTC":"0"},'
                '"usage":{"input_tokens":999,"output_tokens":999}}'
            ),
            input_tokens=123,
            output_tokens=7,
        )


def _config(base_url: str = "https://api.fireworks.ai/inference/v1") -> LLMConfig:
    return LLMConfig(
        base_url=base_url,
        model="accounts/example/models/reference",
        max_tokens=100,
        api_key_env_name=(
            "FIREWORKS_API_KEY"
            if base_url.startswith("https://api.fireworks.ai/")
            else "TEST_LOOPBACK_PROVIDER_KEY"
        ),
    )


def test_baseline_preserves_observation_and_uses_retry_feedback() -> None:
    provider = FakeProvider()
    policy = LLMBaselinePolicy(
        provider=provider,
        config=_config(),
        system_prompt=load_prompt(),
    )
    first_request = runner_request(attempt=1)
    retry_request = runner_request(attempt=2)

    policy.decide(first_request)
    action = policy.decide(retry_request)

    assert len(provider.calls) == 2
    assert provider.calls[0] == provider.calls[1]
    seen = provider.calls[1]
    assert seen.get("schema") == "observation/v1"
    assert "retry" not in seen
    assert "attempt" not in seen
    assert provider.feedbacks[0] is None
    assert provider.feedbacks[1] == retry_request["retry"]
    assert action["usage"] == {"input_tokens": 123, "output_tokens": 7}
    estimate = policy.estimate_run(
        retry_request,
        turns=3,
        pricing=ModelPricing.from_strings(
            input_usd_per_million="1",
            output_usd_per_million="2",
        ),
    )
    assert estimate.turns == 3
    assert estimate.estimated_cost_usd is not None


def test_baseline_injects_trusted_cached_usage_extension() -> None:
    class CachedProvider(FakeProvider):
        def complete(
            self,
            *,
            system_prompt: str,
            observation: Mapping[str, object],
            retry_feedback: Mapping[str, object] | None,
        ) -> ProviderResult:
            result = super().complete(
                system_prompt=system_prompt,
                observation=observation,
                retry_feedback=retry_feedback,
            )
            return ProviderResult(
                content=result.content,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                cached_input_tokens=41,
            )

    policy = LLMBaselinePolicy(
        provider=CachedProvider(),
        config=_config(),
        system_prompt="strict",
    )
    action = policy.decide(runner_request())

    assert action["usage"] == {"input_tokens": 123, "output_tokens": 7}
    assert action["ext"] == {CACHED_INPUT_EXTENSION_KEY: 41}


@pytest.mark.parametrize(
    ("cached_tokens", "accepted"),
    (
        (MAX_IJSON_INTEGER, True),
        (MAX_IJSON_INTEGER + 1, False),
    ),
)
def test_trusted_cached_usage_stays_within_frozen_ext_integer_range(
    cached_tokens: int,
    accepted: bool,
) -> None:
    class BoundaryProvider:
        def complete(
            self,
            *,
            system_prompt: str,
            observation: Mapping[str, object],
            retry_feedback: Mapping[str, object] | None,
        ) -> ProviderResult:
            del system_prompt, observation, retry_feedback
            return ProviderResult(
                content='{"schema":"action/v1","target":{"BTC":"0"}}',
                input_tokens=cached_tokens,
                output_tokens=0,
                cached_input_tokens=cached_tokens,
            )

    policy = LLMBaselinePolicy(
        provider=BoundaryProvider(),
        config=_config(),
        system_prompt="strict",
    )
    if not accepted:
        with pytest.raises(InvalidProviderAction):
            policy.decide(runner_request())
        return

    action = policy.decide(runner_request())
    assert action["ext"] == {
        CACHED_INPUT_EXTENSION_KEY: MAX_IJSON_INTEGER
    }


def test_baseline_rejects_provider_authored_extension() -> None:
    class SpoofingProvider:
        def complete(
            self,
            *,
            system_prompt: str,
            observation: Mapping[str, object],
            retry_feedback: Mapping[str, object] | None,
        ) -> ProviderResult:
            del system_prompt, observation, retry_feedback
            return ProviderResult(
                content=(
                    '{"ext":{"x_tradeevolve_cached_input_tokens":99},'
                    '"schema":"action/v1","target":{"BTC":"0"}}'
                ),
                input_tokens=123,
                output_tokens=7,
                cached_input_tokens=0,
            )

    policy = LLMBaselinePolicy(
        provider=SpoofingProvider(),
        config=_config(),
        system_prompt="strict",
    )
    with pytest.raises(InvalidProviderAction):
        policy.decide(runner_request())


def test_retry_correction_bound_covers_every_frozen_parser_reason() -> None:
    reasons = (
        "oversize",
        "invalid_json",
        "unknown_schema",
        "schema_invalid",
        "unknown_field",
        "unknown_market",
        "invalid_target_format",
        "float_target",
        "target_out_of_range",
        "invalid_slippage",
    )
    sizes = [
        len(
            provider_retry_message(
                {
                    "reason": reason,
                    "detail": _parse_detail(reason),
                    "prior_raw_sha256": "sha256:" + "a" * 64,
                }
            ).encode("utf-8")
        )
        for reason in reasons
    ]

    assert max(sizes) == MAX_RETRY_CORRECTION_BYTES == 292


def test_baseline_preserves_invalid_provider_evidence_without_exposing_it() -> None:
    content = "```json\\n{}\\n``` canary-provider-output"

    class BadProvider:
        def complete(
            self,
            *,
            system_prompt: str,
            observation: Mapping[str, object],
            retry_feedback: Mapping[str, object] | None,
        ) -> ProviderResult:
            del system_prompt, observation, retry_feedback
            return ProviderResult(
                content=content,
                input_tokens=77,
                output_tokens=11,
            )

    policy = LLMBaselinePolicy(
        provider=BadProvider(),
        config=_config(),
        system_prompt="strict",
    )
    with pytest.raises(InvalidProviderAction) as caught:
        policy.decide(runner_request())
    assert caught.value.evidence.provider_output == content
    assert caught.value.evidence.input_tokens == 77
    assert caught.value.evidence.output_tokens == 11
    assert caught.value.evidence.reason == "action_contract_invalid"
    assert content not in str(caught.value)
    assert content not in repr(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.parametrize(
    ("result", "reason"),
    [
        (
            ProviderResult(content="", input_tokens=10, output_tokens=2),
            "empty_content",
        ),
        (
            ProviderResult(
                content="{}",
                input_tokens=10,
                output_tokens=2,
                finish_reason="length",
            ),
            "finish_reason_not_stop",
        ),
        (
            ProviderResult(
                content="{}",
                input_tokens=10,
                output_tokens=2,
            ),
            "action_contract_invalid",
        ),
    ],
)
def test_baseline_rejects_incomplete_provider_completions_with_usage(
    result: ProviderResult,
    reason: str,
) -> None:
    class IncompleteProvider:
        def complete(
            self,
            *,
            system_prompt: str,
            observation: Mapping[str, object],
            retry_feedback: Mapping[str, object] | None,
        ) -> ProviderResult:
            del system_prompt, observation, retry_feedback
            return result

    policy = LLMBaselinePolicy(
        provider=IncompleteProvider(),
        config=_config(),
        system_prompt="strict",
    )
    with pytest.raises(InvalidProviderAction) as caught:
        policy.decide(runner_request())
    assert caught.value.evidence.reason == reason
    assert caught.value.evidence.input_tokens == 10
    assert caught.value.evidence.output_tokens == 2


def test_provider_origin_is_https_and_bound_to_one_key_name() -> None:
    fireworks = {
        "TRADEVOLVE_LLM_MODEL": "accounts/example/models/reference",
        "FIREWORKS_API_KEY": "fireworks-only",
        "OPENAI_API_KEY": "must-not-be-selected",
    }
    policy = LLMBaselinePolicy.from_env(fireworks)
    provider = cast(OpenAICompatibleProvider, policy.provider)
    assert provider.api_key == "fireworks-only"

    with pytest.raises(ProviderError):
        LLMBaselinePolicy.from_env(
            {
                "TRADEVOLVE_LLM_MODEL": "accounts/example/models/reference",
                "OPENAI_API_KEY": "wrong-provider-key",
            }
        )
    with pytest.raises(ValueError, match="HTTPS"):
        LLMConfig.from_env(
            {
                "TRADEVOLVE_LLM_BASE_URL": "http://api.fireworks.ai/v1",
                "TRADEVOLVE_LLM_MODEL": "accounts/example/models/reference",
            }
        )
    with pytest.raises(ValueError, match="provider-specific"):
        LLMConfig.from_env(
            {
                "TRADEVOLVE_LLM_BASE_URL": "https://custom.example/v1",
                "TRADEVOLVE_LLM_API_KEY_ENV": "FIREWORKS_API_KEY",
                "TRADEVOLVE_LLM_MODEL": "custom/model",
            }
        )


@dataclass(slots=True)
class CapturedRequest:
    authorization: str | None = None
    payload: dict[str, object] | None = None


class FakeCompletionHandler(BaseHTTPRequestHandler):
    captured = CapturedRequest()
    response_object = "chat.completion"
    response_model = "accounts/example/models/reference"
    cached_tokens: object = 12
    total_tokens: object = 47

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers["Content-Length"])
        raw = self.rfile.read(length)
        type(self).captured.authorization = self.headers.get("Authorization")
        type(self).captured.payload = cast(
            dict[str, object],
            json.loads(raw.decode("utf-8")),
        )
        response = json.dumps(
            {
                "object": type(self).response_object,
                "model": type(self).response_model,
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": (
                                '{"schema":"action/v1",'
                                '"target":{"BTC":"0"}}'
                            )
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 42,
                    "completion_tokens": 5,
                    "prompt_tokens_details": {
                        "cached_tokens": type(self).cached_tokens,
                    },
                    "total_tokens": type(self).total_tokens,
                },
            },
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)


def test_openai_compatible_client_against_loopback_fake_provider(
    capsys: pytest.CaptureFixture[str],
) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeCompletionHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    secret = "test-secret-never-log"
    try:
        host, port = cast(tuple[str, int], server.server_address)
        config = _config(f"http://{host}:{port}/v1")
        provider = OpenAICompatibleProvider(config=config, api_key=secret)
        assert secret not in repr(provider)
        result = provider.complete(
            system_prompt="strict prompt",
            observation=cast(
                Mapping[str, object],
                runner_request()["observation"],
            ),
            retry_feedback={
                "reason": "float_target",
                "detail": "send a decimal string",
                "prior_raw_sha256": "sha256:" + ("a" * 64),
            },
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert result.input_tokens == 42
    assert result.output_tokens == 5
    assert result.cached_input_tokens == 12
    captured = FakeCompletionHandler.captured
    assert captured.authorization == f"Bearer {secret}"
    assert captured.payload is not None
    assert captured.payload["reasoning_effort"] == "none"
    assert "reasoning_history" not in captured.payload
    assert "thinking" not in captured.payload
    messages = cast(list[dict[str, object]], captured.payload["messages"])
    user_content = cast(str, messages[1]["content"])
    user = cast(dict[str, object], json.loads(user_content))
    assert user["schema"] == "observation/v1"
    assert "retry" not in user
    assert len(messages) == 3
    correction = cast(str, messages[2]["content"])
    assert "float_target" in correction
    assert "send a decimal string" in correction
    response_format = cast(
        dict[str, object],
        captured.payload["response_format"],
    )
    assert response_format["type"] == "json_schema"
    json_schema = cast(
        dict[str, object],
        response_format["json_schema"],
    )
    assert json_schema["name"] == "trade_action"
    assert "strict" not in json_schema
    schema = cast(dict[str, object], json_schema["schema"])
    assert schema["required"] == [
        "schema",
        "intent_kind",
        "target",
        "max_slippage_bps",
    ]
    properties = cast(dict[str, object], schema["properties"])
    target = cast(dict[str, object], properties["target"])
    assert target["required"] == ["BTC"]
    assert target["additionalProperties"] is False
    target_properties = cast(dict[str, object], target["properties"])
    assert set(target_properties) == {"BTC"}
    assert "minimum" not in cast(
        dict[str, object],
        properties["max_slippage_bps"],
    )
    captured_output = capsys.readouterr()
    assert secret not in captured_output.out
    assert secret not in captured_output.err


@pytest.mark.parametrize(
    ("response_object", "response_model"),
    (
        ("wrong", "accounts/example/models/reference"),
        ("chat.completion", "accounts/example/models/wrong"),
    ),
)
def test_provider_rejects_response_provenance_mismatch(
    response_object: str,
    response_model: str,
) -> None:
    FakeCompletionHandler.response_object = response_object
    FakeCompletionHandler.response_model = response_model
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeCompletionHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = cast(tuple[str, int], server.server_address)
        provider = OpenAICompatibleProvider(
            config=_config(f"http://{host}:{port}/v1"),
            api_key="test-secret",
        )
        with pytest.raises(ProviderError):
            provider.complete(
                system_prompt="strict",
                observation=cast(
                    Mapping[str, object],
                    runner_request()["observation"],
                ),
                retry_feedback=None,
            )
    finally:
        FakeCompletionHandler.response_object = "chat.completion"
        FakeCompletionHandler.response_model = (
            "accounts/example/models/reference"
        )
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    ("cached_tokens", "total_tokens"),
    (
        (43, 47),
        (-1, 47),
        (True, 47),
        (12, 46),
    ),
)
def test_provider_rejects_inconsistent_usage_details(
    cached_tokens: object,
    total_tokens: object,
) -> None:
    FakeCompletionHandler.cached_tokens = cached_tokens
    FakeCompletionHandler.total_tokens = total_tokens
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeCompletionHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = cast(tuple[str, int], server.server_address)
        provider = OpenAICompatibleProvider(
            config=_config(f"http://{host}:{port}/v1"),
            api_key="test-secret",
        )
        with pytest.raises(ProviderError):
            provider.complete(
                system_prompt="strict",
                observation=cast(
                    Mapping[str, object],
                    runner_request()["observation"],
                ),
                retry_feedback=None,
            )
    finally:
        FakeCompletionHandler.cached_tokens = 12
        FakeCompletionHandler.total_tokens = 47
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
