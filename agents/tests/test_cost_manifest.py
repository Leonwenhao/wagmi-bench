# SPDX-License-Identifier: Apache-2.0
"""Prompt, manifest, and pre-run cost-estimate tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator

from agents.cost import CostEstimateError, ModelPricing, estimate_run_cost
from agents.llm import LLMConfig
from agents.manifest import build_llm_manifest, build_reckless_manifest
from agents.prompt import DEFAULT_PROMPT_PATH, prompt_sha256

ROOT = Path(__file__).resolve().parents[2]
AGENT_ROOT = ROOT / "agents"
MANIFEST_SCHEMA = json.loads(
    (ROOT / "spec" / "schemas" / "agent_manifest.v1.schema.json").read_text(
        encoding="utf-8"
    )
)


def _assert_manifest_valid(manifest: dict[str, object]) -> None:
    errors = sorted(
        Draft202012Validator(MANIFEST_SCHEMA).iter_errors(manifest),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    assert not errors, [error.message for error in errors]


def test_manifests_are_schema_valid_and_secret_free() -> None:
    secret = "secret-value-must-not-appear"
    config = LLMConfig(
        base_url="https://api.fireworks.ai/inference/v1",
        model="accounts/example/models/reference",
    )
    image_digest = "sha256:" + ("a" * 64)

    llm = build_llm_manifest(config, image_digest=image_digest)
    reckless = build_reckless_manifest(image_digest=image_digest)

    _assert_manifest_valid(llm)
    _assert_manifest_valid(reckless)
    assert llm["endpoint_domains"] == ["api.fireworks.ai"]
    assert llm["prompt_sha256"] == prompt_sha256(DEFAULT_PROMPT_PATH)
    assert llm["image_sha256"] == image_digest
    assert cast(dict[str, object], llm["inference_params"])[
        "response_format"
    ] == "json_schema"
    assert cast(dict[str, object], llm["inference_params"])[
        "reasoning_effort"
    ] == "none"
    assert secret not in json.dumps(llm)
    assert reckless["endpoint_domains"] == []

    with pytest.raises(ValueError, match="image_digest"):
        build_reckless_manifest(image_digest=cast(Any, None))


def test_pre_run_estimate_is_deterministic_and_decimal_exact() -> None:
    pricing = ModelPricing.from_strings(
        input_usd_per_million="0.2",
        output_usd_per_million="0.8",
    )

    estimate = estimate_run_cost(
        system_prompt="system",
        sample_observation_json='{"schema":"observation/v1"}',
        turns=10,
        max_output_tokens=100,
        attempts_per_turn=2,
        pricing=pricing,
    )

    assert estimate.input_tokens == 820
    assert estimate.output_tokens == 2_000
    assert estimate.request_overhead_tokens_per_attempt == 0
    assert estimate.to_mapping()["estimated_cost_usd"] == "0.001764"


def test_cached_input_price_cannot_exceed_uncached_input_price() -> None:
    with pytest.raises(CostEstimateError, match="may not exceed"):
        ModelPricing.from_strings(
            input_usd_per_million="0.95",
            cached_input_usd_per_million="0.96",
            output_usd_per_million="4",
        )


def test_pre_run_estimate_counts_structured_request_overhead_per_attempt() -> None:
    without = estimate_run_cost(
        system_prompt="system",
        sample_observation_json='{"schema":"observation/v1"}',
        turns=3,
        max_output_tokens=10,
        attempts_per_turn=2,
    )
    with_overhead = estimate_run_cost(
        system_prompt="system",
        sample_observation_json='{"schema":"observation/v1"}',
        turns=3,
        max_output_tokens=10,
        attempts_per_turn=2,
        request_overhead_text="x" * 41,
    )

    assert with_overhead.request_overhead_tokens_per_attempt == 11
    assert with_overhead.input_tokens - without.input_tokens == 66


def test_authorization_estimate_counts_retry_and_byte_bound_exactly() -> None:
    estimate = estimate_run_cost(
        system_prompt="abc",
        sample_observation_json="defgh",
        turns=2,
        max_output_tokens=10,
        attempts_per_turn=2,
        request_overhead_text="ijklmnopq",
        retry_overhead_text="x" * 292,
        input_bytes_per_token=1,
        framing_tokens_per_attempt=128,
    )

    assert estimate.input_tokens == 1_164
    assert estimate.output_tokens == 40
    assert estimate.request_overhead_tokens_per_attempt == 9
    assert estimate.retry_overhead_tokens_per_turn == 292
    assert estimate.input_estimator_bytes_per_token == 1
    assert estimate.framing_tokens_per_attempt == 128


def test_agent_image_context_is_exact_pinned_and_secret_free() -> None:
    dockerfile = (AGENT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (AGENT_ROOT / ".dockerignore").read_text(encoding="utf-8")

    assert dockerignore.startswith("**\n")
    assert "!.env" not in dockerignore
    assert "!tests" not in dockerignore
    assert "COPY ." not in dockerfile
    assert "ADD " not in dockerfile
    assert (
        "python:3.12-slim@sha256:"
        "57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de"
    ) in dockerfile
    assert "USER 65532:65532" in dockerfile
