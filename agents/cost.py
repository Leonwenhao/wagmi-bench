# SPDX-License-Identifier: Apache-2.0
"""Deterministic token and cost estimates for pre-run confirmation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, InvalidOperation


class CostEstimateError(ValueError):
    """Pricing or workload inputs are not usable."""


def _non_negative_decimal(value: str, field: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise CostEstimateError(f"{field} must be a decimal string") from exc
    if not parsed.is_finite() or parsed < 0:
        raise CostEstimateError(f"{field} must be finite and non-negative")
    return parsed


@dataclass(frozen=True, slots=True)
class ModelPricing:
    """Provider prices in USD per one million tokens."""

    input_usd_per_million: Decimal
    output_usd_per_million: Decimal
    cached_input_usd_per_million: Decimal | None = None

    def __post_init__(self) -> None:
        values = {
            "input_usd_per_million": self.input_usd_per_million,
            "output_usd_per_million": self.output_usd_per_million,
        }
        if self.cached_input_usd_per_million is not None:
            values["cached_input_usd_per_million"] = (
                self.cached_input_usd_per_million
            )
        for field, value in values.items():
            if (
                not isinstance(value, Decimal)
                or not value.is_finite()
                or value < 0
            ):
                raise CostEstimateError(
                    f"{field} must be finite and non-negative"
                )
        cached = self.cached_input_usd_per_million
        if cached is not None and cached > self.input_usd_per_million:
            raise CostEstimateError(
                "cached_input_usd_per_million may not exceed "
                "input_usd_per_million"
            )

    @classmethod
    def from_strings(
        cls,
        *,
        input_usd_per_million: str,
        output_usd_per_million: str,
        cached_input_usd_per_million: str | None = None,
    ) -> ModelPricing:
        return cls(
            input_usd_per_million=_non_negative_decimal(
                input_usd_per_million,
                "input_usd_per_million",
            ),
            output_usd_per_million=_non_negative_decimal(
                output_usd_per_million,
                "output_usd_per_million",
            ),
            cached_input_usd_per_million=(
                None
                if cached_input_usd_per_million is None
                else _non_negative_decimal(
                    cached_input_usd_per_million,
                    "cached_input_usd_per_million",
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class RunCostEstimate:
    """Conservative pre-run workload estimate."""

    turns: int
    attempts_per_turn: int
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: Decimal | None
    request_overhead_tokens_per_attempt: int = 0
    retry_overhead_tokens_per_turn: int = 0
    input_estimator_bytes_per_token: int = 4
    framing_tokens_per_attempt: int = 32

    def to_mapping(self) -> dict[str, object]:
        return {
            "turns": self.turns,
            "attempts_per_turn": self.attempts_per_turn,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "request_overhead_tokens_per_attempt": (
                self.request_overhead_tokens_per_attempt
            ),
            "retry_overhead_tokens_per_turn": (
                self.retry_overhead_tokens_per_turn
            ),
            "input_estimator_bytes_per_token": (
                self.input_estimator_bytes_per_token
            ),
            "framing_tokens_per_attempt": self.framing_tokens_per_attempt,
            "estimated_cost_usd": (
                None
                if self.estimated_cost_usd is None
                else format(self.estimated_cost_usd, "f")
            ),
        }


def estimate_text_tokens(
    text: str,
    *,
    bytes_per_token: int = 4,
) -> int:
    """Estimate tokens from UTF-8 bytes, never returning zero."""

    if (
        isinstance(bytes_per_token, bool)
        or not isinstance(bytes_per_token, int)
        or bytes_per_token <= 0
    ):
        raise CostEstimateError("bytes_per_token must be a positive integer")
    byte_count = len(text.encode("utf-8"))
    return max(
        1,
        (byte_count + bytes_per_token - 1) // bytes_per_token,
    )


def estimate_run_cost(
    *,
    system_prompt: str,
    sample_observation_json: str,
    turns: int,
    max_output_tokens: int,
    attempts_per_turn: int = 1,
    pricing: ModelPricing | None = None,
    request_overhead_text: str = "",
    retry_overhead_text: str = "",
    input_bytes_per_token: int = 4,
    framing_tokens_per_attempt: int = 32,
) -> RunCostEstimate:
    """Estimate a full run before any provider request is sent."""

    for field, value, minimum in (
        ("turns", turns, 1),
        ("max_output_tokens", max_output_tokens, 1),
        ("attempts_per_turn", attempts_per_turn, 1),
        ("input_bytes_per_token", input_bytes_per_token, 1),
        (
            "framing_tokens_per_attempt",
            framing_tokens_per_attempt,
            0,
        ),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise CostEstimateError(f"{field} must be an integer >= {minimum}")
    if attempts_per_turn > 2:
        raise CostEstimateError("IC-6 permits at most two attempts per turn")
    prompt_tokens = estimate_text_tokens(
        system_prompt,
        bytes_per_token=input_bytes_per_token,
    )
    observation_tokens = estimate_text_tokens(
        sample_observation_json,
        bytes_per_token=input_bytes_per_token,
    )
    request_overhead_tokens = (
        0
        if not request_overhead_text
        else estimate_text_tokens(
            request_overhead_text,
            bytes_per_token=input_bytes_per_token,
        )
    )
    retry_overhead_tokens = (
        0
        if attempts_per_turn == 1 or not retry_overhead_text
        else estimate_text_tokens(
            retry_overhead_text,
            bytes_per_token=input_bytes_per_token,
        )
    )
    # A small fixed allowance covers role/message framing in compatible APIs.
    per_attempt_input = (
        prompt_tokens
        + observation_tokens
        + request_overhead_tokens
        + framing_tokens_per_attempt
    )
    calls = turns * attempts_per_turn
    input_tokens = (
        per_attempt_input * calls
        + retry_overhead_tokens * turns
    )
    output_tokens = max_output_tokens * calls
    estimated_cost: Decimal | None = None
    if pricing is not None:
        numerator = (
            Decimal(input_tokens) * pricing.input_usd_per_million
            + Decimal(output_tokens) * pricing.output_usd_per_million
        )
        estimated_cost = (numerator / Decimal(1_000_000)).quantize(
            Decimal("0.000001"),
            rounding=ROUND_CEILING,
        )
    return RunCostEstimate(
        turns=turns,
        attempts_per_turn=attempts_per_turn,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        estimated_cost_usd=estimated_cost,
        request_overhead_tokens_per_attempt=request_overhead_tokens,
        retry_overhead_tokens_per_turn=retry_overhead_tokens,
        input_estimator_bytes_per_token=input_bytes_per_token,
        framing_tokens_per_attempt=framing_tokens_per_attempt,
    )
