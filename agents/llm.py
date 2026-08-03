# SPDX-License-Identifier: Apache-2.0
"""Clean Fireworks/OpenAI-compatible reference-agent baseline."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Protocol, cast
from urllib.parse import urlsplit

from agents.common import (
    AgentContractError,
    JsonObject,
    canonical_json_bytes,
    decode_action_object,
    is_exact_int,
    market_aliases,
    observation_json,
    runner_retry_feedback,
    validate_action_document,
    validate_runner_request,
)
from agents.cost import ModelPricing, RunCostEstimate, estimate_run_cost
from agents.prompt import DEFAULT_PROMPT_PATH, load_prompt

_DEFAULT_BASE_URL = "https://api.fireworks.ai/inference/v1"
_MAX_PROVIDER_RESPONSE_BYTES = 1_048_576
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_PROVIDER_KEY_NAMES = {
    "api.fireworks.ai": "FIREWORKS_API_KEY",
    "api.openai.com": "OPENAI_API_KEY",
    "openrouter.ai": "OPENROUTER_API_KEY",
    "api.anthropic.com": "ANTHROPIC_API_KEY",
}
PROVIDER_BASE_URLS = {
    "fireworks": "https://api.fireworks.ai/inference/v1",
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "anthropic": "https://api.anthropic.com",
}
_ANTHROPIC_DOMAIN = "api.anthropic.com"
_ANTHROPIC_VERSION = "2023-06-01"
CACHED_INPUT_EXTENSION_KEY = "x_tradeevolve_cached_input_tokens"
MAX_RETRY_CORRECTION_BYTES = 292
MAX_IJSON_INTEGER = 2**53 - 1
PROVIDER_REASONING_EFFORT = "none"
_RETRY_CORRECTION_PREFIX = (
    "Correct the prior invalid action using this exact "
    "validator feedback, then return one bare action/v1: "
)
_FINISH_REASONS = frozenset(
    {"stop", "length", "content_filter", "tool_calls"}
)


class ProviderError(RuntimeError):
    """A provider failed without exposing credentials or response bodies."""


@dataclass(frozen=True, slots=True, repr=False)
class InvalidActionEvidence:
    """Sanitized evidence from one paid completion that was not action/v1."""

    provider_output: str = field(repr=False)
    input_tokens: int | None
    output_tokens: int | None
    finish_reason: str | None
    reason: str
    cached_input_tokens: int | None = None


class InvalidProviderAction(AgentContractError):
    """A paid completion failed the local action contract.

    The exception text and representation are deliberately content-free. The
    HTTP boundary may inspect :attr:`evidence` to create a bounded, protected
    4xx response that the harness content-addresses.
    """

    __slots__ = ("evidence",)

    def __init__(self, evidence: InvalidActionEvidence) -> None:
        super().__init__("provider completion violates action/v1")
        self.evidence = evidence

    def __repr__(self) -> str:
        return "InvalidProviderAction()"


def _decimal_string(value: str, field: str) -> str:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{field} must be a decimal string") from exc
    if not parsed.is_finite() or not Decimal("0") <= parsed <= Decimal("2"):
        raise ValueError(f"{field} must be between 0 and 2")
    return value


def _positive_int(value: str, field: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an integer") from exc
    if parsed <= 0:
        raise ValueError(f"{field} must be positive")
    return parsed


def endpoint_domain(base_url: str) -> str:
    """Extract a manifest-safe hostname from an API base URL."""

    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        raise ValueError("LLM base URL must be an HTTP(S) origin")
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("LLM base URL must not contain credentials or a query")
    hostname = parsed.hostname.lower()
    if parsed.scheme != "https" and hostname not in {
        "127.0.0.1",
        "::1",
        "localhost",
    }:
        raise ValueError("LLM base URL must use HTTPS outside loopback tests")
    return hostname


def provider_key_name(
    base_url: str,
    *,
    configured: str | None = None,
) -> str:
    """Bind one credential name to the configured provider origin.

    Known provider origins have fixed key names. A custom origin must opt into
    a distinct environment variable explicitly; it can never inherit a
    Fireworks or OpenAI credential by fallback.
    """

    hostname = endpoint_domain(base_url)
    expected = _PROVIDER_KEY_NAMES.get(hostname)
    if expected is not None:
        if configured is not None and configured != expected:
            raise ValueError("credential environment name mismatches provider origin")
        return expected
    if configured is None or _ENV_NAME.fullmatch(configured) is None:
        raise ValueError(
            "custom LLM origins require TRADEVOLVE_LLM_API_KEY_ENV"
        )
    if configured in set(_PROVIDER_KEY_NAMES.values()):
        raise ValueError("custom LLM origins require a provider-specific key name")
    return configured


@dataclass(frozen=True, slots=True)
class LLMConfig:
    """Public, secret-free inference configuration."""

    base_url: str
    model: str
    temperature: str = "0"
    max_tokens: int = 128
    timeout_seconds: int = 100
    api_key_env_name: str = "FIREWORKS_API_KEY"

    def __post_init__(self) -> None:
        endpoint_domain(self.base_url)
        provider_key_name(
            self.base_url,
            configured=self.api_key_env_name,
        )
        if not self.model or any(character.isspace() for character in self.model):
            raise ValueError("LLM model must be a non-empty slug")
        _decimal_string(self.temperature, "temperature")
        if self.max_tokens <= 0 or isinstance(self.max_tokens, bool):
            raise ValueError("max_tokens must be positive")
        if self.timeout_seconds <= 0 or isinstance(self.timeout_seconds, bool):
            raise ValueError("timeout_seconds must be positive")

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> LLMConfig:
        """Read non-secret settings at runtime.

        The model is deliberately required rather than silently selecting a
        potentially expensive or retired provider SKU. A provider shorthand
        (``TRADEVOLVE_LLM_PROVIDER``) selects a known base URL; an explicit
        ``TRADEVOLVE_LLM_BASE_URL`` always wins.
        """

        source = os.environ if environ is None else environ
        model = source.get("TRADEVOLVE_LLM_MODEL", "")
        provider = source.get("TRADEVOLVE_LLM_PROVIDER")
        if provider is not None and provider not in PROVIDER_BASE_URLS:
            known = ", ".join(sorted(PROVIDER_BASE_URLS))
            raise ValueError(
                f"unknown TRADEVOLVE_LLM_PROVIDER {provider!r}; known "
                f"providers: {known}"
            )
        default_base_url = (
            PROVIDER_BASE_URLS[provider]
            if provider is not None
            else _DEFAULT_BASE_URL
        )
        base_url = source.get(
            "TRADEVOLVE_LLM_BASE_URL",
            default_base_url,
        )
        configured_key_name = source.get("TRADEVOLVE_LLM_API_KEY_ENV")
        return cls(
            base_url=base_url,
            model=model,
            temperature=source.get("TRADEVOLVE_LLM_TEMPERATURE", "0"),
            max_tokens=_positive_int(
                source.get("TRADEVOLVE_LLM_MAX_TOKENS", "128"),
                "TRADEVOLVE_LLM_MAX_TOKENS",
            ),
            timeout_seconds=_positive_int(
                source.get("TRADEVOLVE_LLM_TIMEOUT_SECONDS", "100"),
                "TRADEVOLVE_LLM_TIMEOUT_SECONDS",
            ),
            api_key_env_name=provider_key_name(
                base_url,
                configured=configured_key_name,
            ),
        )


@dataclass(frozen=True, slots=True)
class ProviderResult:
    """Model content plus provider-reported usage."""

    content: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    finish_reason: str | None = "stop"
    content_is_text: bool = True
    cached_input_tokens: int | None = None


class CompletionProvider(Protocol):
    """Provider seam used by the paid-call-free unit tests."""

    def complete(
        self,
        *,
        system_prompt: str,
        observation: Mapping[str, object],
        retry_feedback: Mapping[str, object] | None,
    ) -> ProviderResult:
        """Return one model completion."""


def _api_key(
    config: LLMConfig,
    environ: Mapping[str, str] | None = None,
) -> str:
    source = os.environ if environ is None else environ
    value = source.get(config.api_key_env_name)
    if not value:
        raise ProviderError(
            "LLM credential is missing from the runtime environment"
        )
    return value


def _object(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) for key in value
    ):
        raise ProviderError(f"provider response {field} is not an object")
    return cast(Mapping[str, object], value)


def provider_action_schema(
    observation: Mapping[str, object],
) -> JsonObject:
    """Build the provider-only generation schema for observed market aliases.

    Fireworks supports the structural keywords used here. Numeric bounds stay
    in the frozen local validator because the provider does not document those
    keywords as enforced structured-output constraints.
    """

    aliases = market_aliases(observation)
    target_properties: JsonObject = {}
    for alias in aliases:
        target_properties[alias] = {
            "anyOf": [
                {
                    "type": "string",
                    "pattern": (
                        r"^-?(0|[1-9][0-9]{0,2})(\.[0-9]{1,4})?$"
                    ),
                },
                {"type": "integer"},
            ]
        }
    return {
        "type": "object",
        "properties": {
            "schema": {
                "type": "string",
                "enum": ["action/v1"],
            },
            "intent_kind": {
                "type": "string",
                "enum": ["leverage_target"],
            },
            "target": {
                "type": "object",
                "properties": target_properties,
                "required": list(aliases),
                "additionalProperties": False,
            },
            "max_slippage_bps": {"type": "integer"},
        },
        "required": [
            "schema",
            "intent_kind",
            "target",
            "max_slippage_bps",
        ],
        "additionalProperties": False,
    }


def provider_response_format(
    observation: Mapping[str, object],
) -> JsonObject:
    """Return the exact structured-output control placed in the request."""

    return {
        "type": "json_schema",
        "json_schema": {
            "name": "trade_action",
            "schema": provider_action_schema(observation),
        },
    }


def provider_response_format_json(
    observation: Mapping[str, object],
) -> str:
    """Return deterministic text for request-overhead cost estimation."""

    return canonical_json_bytes(
        provider_response_format(observation)
    ).decode("utf-8")


def provider_request_controls(
    observation: Mapping[str, object],
) -> JsonObject:
    """Return every generation control added to the provider request."""

    return {
        "reasoning_effort": PROVIDER_REASONING_EFFORT,
        "response_format": provider_response_format(observation),
    }


def provider_request_controls_json(
    observation: Mapping[str, object],
) -> str:
    """Return canonical control text for request-overhead estimation."""

    return canonical_json_bytes(
        provider_request_controls(observation)
    ).decode("utf-8")


def provider_retry_message(
    retry_feedback: Mapping[str, object],
) -> str:
    """Serialize the exact validator correction sent on IC-6 attempt two."""

    return _RETRY_CORRECTION_PREFIX + json.dumps(
        retry_feedback,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


@dataclass(frozen=True, slots=True)
class OpenAICompatibleProvider:
    """Minimal chat-completions client with no logging or SDK dependency."""

    config: LLMConfig
    api_key: str = field(repr=False)

    @classmethod
    def from_env(
        cls,
        config: LLMConfig,
        environ: Mapping[str, str] | None = None,
    ) -> OpenAICompatibleProvider:
        return cls(config=config, api_key=_api_key(config, environ))

    def _temperature_value(self) -> int | float:
        parsed = Decimal(self.config.temperature)
        if parsed == parsed.to_integral_value():
            return int(parsed)
        return float(parsed)

    def complete(
        self,
        *,
        system_prompt: str,
        observation: Mapping[str, object],
        retry_feedback: Mapping[str, object] | None,
    ) -> ProviderResult:
        """Call only ``/chat/completions`` with prompt + observation.

        On attempt 2, the provider also receives only the frozen retry
        correction fields. The rest of the IC-6 wrapper, manifests,
        credentials, and pack-side data are never placed in the request.
        """

        messages: list[JsonObject] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": observation_json(observation)},
        ]
        if retry_feedback is not None:
            messages.append(
                {
                    "role": "user",
                    "content": provider_retry_message(retry_feedback),
                }
            )
        payload: JsonObject = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self._temperature_value(),
            "max_tokens": self.config.max_tokens,
            **provider_request_controls(observation),
        }
        request = urllib.request.Request(
            self.config.base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8"),
            method="POST",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "tradeevolve-reference-agent/1",
            },
        )
        try:
            with urllib.request.urlopen(  # noqa: S310 - configured provider
                request,
                timeout=self.config.timeout_seconds,
            ) as response:
                raw = response.read(_MAX_PROVIDER_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise ProviderError(
                f"provider returned HTTP status {exc.code}"
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ProviderError("provider request failed") from None
        if len(raw) > _MAX_PROVIDER_RESPONSE_BYTES:
            raise ProviderError("provider response exceeded the size limit")
        try:
            decoded = cast(object, json.loads(raw.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ProviderError("provider response is not valid JSON") from None
        root = _object(decoded, "root")
        if root.get("object") != "chat.completion":
            raise ProviderError("provider response object is not chat.completion")
        if root.get("model") != self.config.model:
            raise ProviderError("provider response model does not match the request")
        choices = root.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderError("provider response has no choices")
        choice = _object(cast(list[object], choices)[0], "choices[0]")
        message = _object(choice.get("message"), "choices[0].message")
        content_value = message.get("content")
        content_is_text = isinstance(content_value, str)
        content = content_value if isinstance(content_value, str) else ""
        finish_reason_value = choice.get("finish_reason")
        if not isinstance(finish_reason_value, str):
            finish_reason = None
        elif finish_reason_value in _FINISH_REASONS:
            finish_reason = finish_reason_value
        else:
            finish_reason = "other"
        input_tokens: int | None = None
        output_tokens: int | None = None
        cached_input_tokens: int | None = None
        usage_value = root.get("usage")
        if usage_value is not None:
            usage = _object(usage_value, "usage")
            prompt_tokens = usage.get("prompt_tokens")
            completion_tokens = usage.get("completion_tokens")
            if (
                is_exact_int(prompt_tokens)
                and 0 <= prompt_tokens <= MAX_IJSON_INTEGER
                and is_exact_int(completion_tokens)
                and 0 <= completion_tokens <= MAX_IJSON_INTEGER
                and prompt_tokens + completion_tokens <= MAX_IJSON_INTEGER
            ):
                input_tokens = prompt_tokens
                output_tokens = completion_tokens
                details_value = usage.get("prompt_tokens_details")
                if details_value is not None:
                    details = _object(
                        details_value,
                        "usage.prompt_tokens_details",
                    )
                    cached_tokens = details.get("cached_tokens")
                    if (
                        not is_exact_int(cached_tokens)
                        or cached_tokens < 0
                        or cached_tokens > prompt_tokens
                        or cached_tokens > MAX_IJSON_INTEGER
                    ):
                        raise ProviderError(
                            "provider cached token usage is invalid"
                        )
                    cached_input_tokens = cached_tokens
                total_tokens = usage.get("total_tokens")
                if total_tokens is not None and (
                    not is_exact_int(total_tokens)
                    or total_tokens != prompt_tokens + completion_tokens
                ):
                    raise ProviderError(
                        "provider total token usage is inconsistent"
                    )
        return ProviderResult(
            content=content,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            finish_reason=finish_reason,
            content_is_text=content_is_text,
            cached_input_tokens=cached_input_tokens,
        )


_ANTHROPIC_STOP_REASONS = {
    "end_turn": "stop",
    "max_tokens": "length",
    "refusal": "content_filter",
}


def anthropic_output_config(
    observation: Mapping[str, object],
) -> JsonObject:
    """Return the native structured-output control for /v1/messages."""

    return {
        "format": {
            "type": "json_schema",
            "schema": provider_action_schema(observation),
        }
    }


@dataclass(frozen=True, slots=True)
class AnthropicMessagesProvider:
    """Native Anthropic Messages client with no logging or SDK dependency.

    Current Anthropic models reject sampling parameters, so nothing beyond
    ``model``, ``max_tokens``, ``system``, ``messages``, and the structured
    output control is transmitted; the ``thinking`` parameter is omitted so
    each model runs its own default. The sealed manifest must mirror exactly
    this transmitted set (``provider_api: anthropic-messages``).
    """

    config: LLMConfig
    api_key: str = field(repr=False)

    @classmethod
    def from_env(
        cls,
        config: LLMConfig,
        environ: Mapping[str, str] | None = None,
    ) -> AnthropicMessagesProvider:
        if Decimal(config.temperature) != 0:
            raise ProviderError(
                "current Anthropic models reject sampling parameters; leave "
                "temperature at its default '0'"
            )
        return cls(config=config, api_key=_api_key(config, environ))

    def complete(
        self,
        *,
        system_prompt: str,
        observation: Mapping[str, object],
        retry_feedback: Mapping[str, object] | None,
    ) -> ProviderResult:
        messages: list[JsonObject] = [
            {"role": "user", "content": observation_json(observation)},
        ]
        if retry_feedback is not None:
            messages.append(
                {
                    "role": "user",
                    "content": provider_retry_message(retry_feedback),
                }
            )
        payload: JsonObject = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "system": system_prompt,
            "messages": messages,
            "output_config": anthropic_output_config(observation),
        }
        request = urllib.request.Request(
            self.config.base_url.rstrip("/") + "/v1/messages",
            data=json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8"),
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "tradeevolve-reference-agent/1",
                "x-api-key": self.api_key,
                "anthropic-version": _ANTHROPIC_VERSION,
            },
        )
        try:
            with urllib.request.urlopen(  # noqa: S310 - configured provider
                request,
                timeout=self.config.timeout_seconds,
            ) as response:
                raw = response.read(_MAX_PROVIDER_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise ProviderError(
                f"provider returned HTTP status {exc.code}"
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ProviderError("provider request failed") from None
        if len(raw) > _MAX_PROVIDER_RESPONSE_BYTES:
            raise ProviderError("provider response exceeded the size limit")
        try:
            decoded = cast(object, json.loads(raw.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ProviderError("provider response is not valid JSON") from None
        root = _object(decoded, "root")
        if root.get("type") != "message":
            raise ProviderError("provider response type is not message")
        if root.get("model") != self.config.model:
            raise ProviderError("provider response model does not match the request")
        content_blocks = root.get("content")
        if not isinstance(content_blocks, list):
            raise ProviderError("provider response has no content array")
        text_parts: list[str] = []
        content_is_text = True
        for index, block_value in enumerate(cast(list[object], content_blocks)):
            block = _object(block_value, f"content[{index}]")
            block_type = block.get("type")
            if block_type == "thinking":
                continue
            if block_type != "text":
                content_is_text = False
                continue
            text_value = block.get("text")
            if not isinstance(text_value, str):
                raise ProviderError("provider text block has no text string")
            text_parts.append(text_value)
        content = "".join(text_parts)
        if not text_parts:
            content_is_text = False
        stop_reason_value = root.get("stop_reason")
        if not isinstance(stop_reason_value, str):
            finish_reason = None
        else:
            finish_reason = _ANTHROPIC_STOP_REASONS.get(
                stop_reason_value, "other"
            )
        input_tokens: int | None = None
        output_tokens: int | None = None
        cached_input_tokens: int | None = None
        usage_value = root.get("usage")
        if usage_value is not None:
            usage = _object(usage_value, "usage")
            prompt_tokens = usage.get("input_tokens")
            completion_tokens = usage.get("output_tokens")
            if (
                is_exact_int(prompt_tokens)
                and 0 <= prompt_tokens <= MAX_IJSON_INTEGER
                and is_exact_int(completion_tokens)
                and 0 <= completion_tokens <= MAX_IJSON_INTEGER
                and prompt_tokens + completion_tokens <= MAX_IJSON_INTEGER
            ):
                input_tokens = prompt_tokens
                output_tokens = completion_tokens
                cached_value = usage.get("cache_read_input_tokens")
                if cached_value is not None:
                    if (
                        not is_exact_int(cached_value)
                        or cached_value < 0
                        or cached_value > MAX_IJSON_INTEGER
                    ):
                        raise ProviderError(
                            "provider cached token usage is invalid"
                        )
                    cached_input_tokens = cached_value
        return ProviderResult(
            content=content,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            finish_reason=finish_reason,
            content_is_text=content_is_text,
            cached_input_tokens=cached_input_tokens,
        )


def build_provider(
    config: LLMConfig,
    environ: Mapping[str, str] | None = None,
) -> CompletionProvider:
    """Select the wire protocol for the configured provider origin."""

    if endpoint_domain(config.base_url) == _ANTHROPIC_DOMAIN:
        return AnthropicMessagesProvider.from_env(config, environ)
    return OpenAICompatibleProvider.from_env(config, environ)


@dataclass(frozen=True, slots=True)
class LLMBaselinePolicy:
    """Strict action-producing policy over an injected completion provider."""

    provider: CompletionProvider
    config: LLMConfig
    system_prompt: str

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> LLMBaselinePolicy:
        config = LLMConfig.from_env(environ)
        return cls(
            provider=build_provider(config, environ),
            config=config,
            system_prompt=load_prompt(DEFAULT_PROMPT_PATH),
        )

    def decide(self, request: Mapping[str, object]) -> JsonObject:
        observation = validate_runner_request(request)
        feedback = runner_retry_feedback(request)
        aliases = market_aliases(observation)
        result = self.provider.complete(
            system_prompt=self.system_prompt,
            observation=observation,
            retry_feedback=feedback,
        )
        invalid_reason: str | None = None
        if result.finish_reason != "stop":
            invalid_reason = "finish_reason_not_stop"
        elif not result.content_is_text:
            invalid_reason = "content_not_text"
        elif not result.content:
            invalid_reason = "empty_content"
        if invalid_reason is not None:
            raise InvalidProviderAction(
                InvalidActionEvidence(
                    provider_output=result.content,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    finish_reason=result.finish_reason,
                    reason=invalid_reason,
                    cached_input_tokens=result.cached_input_tokens,
                )
            )
        try:
            provider_counts = (
                result.input_tokens,
                result.output_tokens,
                result.cached_input_tokens,
            )
            if any(
                value is not None
                and (
                    not is_exact_int(value)
                    or not 0 <= value <= MAX_IJSON_INTEGER
                )
                for value in provider_counts
            ):
                raise AgentContractError(
                    "provider token usage exceeds the JCS-safe range"
                )
            if (
                result.cached_input_tokens is not None
                and result.input_tokens is not None
                and result.cached_input_tokens > result.input_tokens
            ):
                raise AgentContractError(
                    "provider cached input exceeds total input"
                )
            action = decode_action_object(result.content)
            # Provider telemetry, when trustworthy integers, replaces any
            # self-authored usage claim in model text.
            action.pop("usage", None)
            if "ext" in action:
                raise AgentContractError(
                    "provider-authored action.ext is forbidden"
                )
            if (
                result.input_tokens is not None
                and result.output_tokens is not None
            ):
                action["usage"] = {
                    "input_tokens": result.input_tokens,
                    "output_tokens": result.output_tokens,
                }
            validated = validate_action_document(
                action,
                aliases,
                require_all_markets=True,
            )
            if result.cached_input_tokens is not None:
                validated["ext"] = {
                    CACHED_INPUT_EXTENSION_KEY: (
                        result.cached_input_tokens
                    )
                }
            return validate_action_document(
                validated,
                aliases,
                require_all_markets=True,
            )
        except AgentContractError:
            raise InvalidProviderAction(
                InvalidActionEvidence(
                    provider_output=result.content,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    finish_reason=result.finish_reason,
                    reason="action_contract_invalid",
                    cached_input_tokens=result.cached_input_tokens,
                )
            ) from None

    def estimate_run(
        self,
        request: Mapping[str, object],
        *,
        turns: int,
        pricing: ModelPricing | None = None,
        attempts_per_turn: int = 1,
    ) -> RunCostEstimate:
        """Pre-run hook used before credentials or provider calls are needed."""

        observation = validate_runner_request(request)
        return estimate_run_cost(
            system_prompt=self.system_prompt,
            sample_observation_json=observation_json(observation),
            turns=turns,
            max_output_tokens=self.config.max_tokens,
            attempts_per_turn=attempts_per_turn,
            pricing=pricing,
            request_overhead_text=provider_request_controls_json(observation),
            retry_overhead_text=(
                ""
                if attempts_per_turn == 1
                else "x" * MAX_RETRY_CORRECTION_BYTES
            ),
        )
