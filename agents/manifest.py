# SPDX-License-Identifier: Apache-2.0
"""Secret-free IC-5 agent-manifest builders."""

from __future__ import annotations

import re
from pathlib import Path

from agents.common import JsonObject
from agents.llm import (
    PROVIDER_REASONING_EFFORT,
    LLMConfig,
    endpoint_domain,
)
from agents.prompt import DEFAULT_PROMPT_PATH, prompt_sha256

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_DOMAIN = re.compile(
    r"(?=.{1,253}\Z)"
    r"([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)"
    r"(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+\Z"
)


def _image_digest(value: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError("image_digest must be a sha256:<hex> digest")
    return value


def _domain(value: str) -> str:
    if _DOMAIN.fullmatch(value) is None:
        raise ValueError("provider hostname is not manifest-safe")
    return value


def build_reckless_manifest(
    *,
    image_digest: str,
    agent_version: str = "m3-v1",
) -> JsonObject:
    """Build the deny-all manifest for the egress-attempting policy."""

    return {
        "schema": "agent_manifest/v1",
        "name": "reckless-reference",
        "adapter": "http",
        "model_id": "none",
        "endpoint_domains": [],
        "inference_params": {
            "policy": "reckless-v1",
            "overleverage_target": "999.9999",
        },
        "prompt_sha256": None,
        "agent_version": agent_version,
        "image_sha256": _image_digest(image_digest),
    }


def build_llm_manifest(
    config: LLMConfig,
    *,
    image_digest: str,
    prompt_path: str | Path = DEFAULT_PROMPT_PATH,
    agent_version: str = "m3-v1",
) -> JsonObject:
    """Build a provider manifest with hostname and prompt commitment only."""

    hostname = _domain(endpoint_domain(config.base_url))
    if hostname == "api.anthropic.com":
        # The native Messages provider transmits no sampling or reasoning
        # controls (current Anthropic models reject them); the sealed set
        # mirrors exactly what goes on the wire.
        inference_params: JsonObject = {
            "max_tokens": config.max_tokens,
            "response_format": "json_schema",
            "provider_api": "anthropic-messages",
        }
    else:
        inference_params = {
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
            "reasoning_effort": PROVIDER_REASONING_EFFORT,
            "response_format": "json_schema",
            "provider_api": "openai-chat-completions",
        }
    return {
        "schema": "agent_manifest/v1",
        "name": "llm-baseline",
        "adapter": "http",
        "model_id": config.model,
        "endpoint_domains": [hostname],
        "inference_params": inference_params,
        "prompt_sha256": prompt_sha256(prompt_path),
        "agent_version": agent_version,
        "image_sha256": _image_digest(image_digest),
    }
