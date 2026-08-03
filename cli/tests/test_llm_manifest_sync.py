# SPDX-License-Identifier: Apache-2.0
"""The CLI must seal the request contract the LLM adapter actually sends."""

from __future__ import annotations

from typing import Final

from agents.llm import (
    LLMConfig,
    provider_request_controls,
    provider_response_format,
)
from agents.manifest import build_llm_manifest
from cli.main import _llm_inference_params

_IMAGE_DIGEST: Final = "sha256:" + "ab" * 32
_OBSERVATION: Final[dict[str, object]] = {
    "markets": {"BTCUSDT": {}, "ETHUSDT": {}},
}


def _config() -> LLMConfig:
    return LLMConfig(
        base_url="https://api.fireworks.ai/inference/v1",
        model="accounts/fireworks/models/qwen3p7-plus",
        temperature="0.2",
        max_tokens=192,
    )


def test_cli_inference_params_match_agent_manifest_builder() -> None:
    config = _config()
    sealed = build_llm_manifest(config, image_digest=_IMAGE_DIGEST)
    assert _llm_inference_params(
        temperature=config.temperature,
        max_output_tokens=config.max_tokens,
    ) == sealed["inference_params"]


def test_cli_inference_params_key_set_matches_agent_manifest_builder() -> None:
    config = _config()
    sealed = build_llm_manifest(config, image_digest=_IMAGE_DIGEST)
    params = sealed["inference_params"]
    assert isinstance(params, dict)
    assert sorted(
        _llm_inference_params(
            temperature=config.temperature,
            max_output_tokens=config.max_tokens,
        )
    ) == sorted(params)


def test_cli_inference_params_match_transmitted_request_controls() -> None:
    config = _config()
    params = _llm_inference_params(
        temperature=config.temperature,
        max_output_tokens=config.max_tokens,
    )
    controls = provider_request_controls(_OBSERVATION)
    assert params["reasoning_effort"] == controls["reasoning_effort"]
    response_format = provider_response_format(_OBSERVATION)
    assert params["response_format"] == response_format["type"]
    assert params["temperature"] == config.temperature
    assert params["max_tokens"] == config.max_tokens


def test_cli_inference_params_are_manifest_safe_scalars() -> None:
    params = _llm_inference_params(temperature="0", max_output_tokens=128)
    for value in params.values():
        assert isinstance(value, str | int)
        assert not isinstance(value, bool)
