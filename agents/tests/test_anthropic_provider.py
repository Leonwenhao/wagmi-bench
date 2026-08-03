# SPDX-License-Identifier: Apache-2.0
"""Paid-call-free tests for the native Anthropic Messages provider."""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar, cast

import pytest

from agents.llm import (
    PROVIDER_BASE_URLS,
    AnthropicMessagesProvider,
    LLMConfig,
    OpenAICompatibleProvider,
    ProviderError,
    build_provider,
)
from agents.tests.helpers import runner_request

_MODEL = "claude-opus-5"


def _config(base_url: str) -> LLMConfig:
    return LLMConfig(
        base_url=base_url,
        model=_MODEL,
        temperature="0",
        max_tokens=256,
        timeout_seconds=5,
        api_key_env_name="LOOPBACK_TEST_KEY",
    )


class FakeMessagesHandler(BaseHTTPRequestHandler):
    payload: ClassVar[dict[str, object] | None] = None
    path_seen: ClassVar[str | None] = None
    api_key_header: ClassVar[str | None] = None
    version_header: ClassVar[str | None] = None
    stop_reason: ClassVar[str] = "end_turn"
    content_blocks: ClassVar[list[dict[str, object]]] = [
        {
            "type": "text",
            "text": '{"schema":"action/v1","target":{"BTC":"0"}}',
        }
    ]

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers["Content-Length"])
        raw = self.rfile.read(length)
        cls = type(self)
        cls.path_seen = self.path
        cls.api_key_header = self.headers.get("x-api-key")
        cls.version_header = self.headers.get("anthropic-version")
        cls.payload = cast(
            dict[str, object],
            json.loads(raw.decode("utf-8")),
        )
        response = json.dumps(
            {
                "type": "message",
                "model": _MODEL,
                "content": cls.content_blocks,
                "stop_reason": cls.stop_reason,
                "usage": {
                    "input_tokens": 42,
                    "output_tokens": 5,
                    "cache_read_input_tokens": 12,
                },
            },
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)


def _run_loopback(
    handler: type[FakeMessagesHandler],
    *,
    retry_feedback: Mapping[str, object] | None = None,
) -> tuple[object, type[FakeMessagesHandler]]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    secret = "test-secret-never-log"
    try:
        host, port = cast(tuple[str, int], server.server_address)
        provider = AnthropicMessagesProvider(
            config=_config(f"http://{host}:{port}"),
            api_key=secret,
        )
        assert secret not in repr(provider)
        result = provider.complete(
            system_prompt="strict prompt",
            observation=cast(
                Mapping[str, object],
                runner_request()["observation"],
            ),
            retry_feedback=retry_feedback,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    return result, handler


def test_native_messages_request_transmits_only_the_sealed_controls() -> None:
    result, handler = _run_loopback(FakeMessagesHandler)

    assert handler.path_seen == "/v1/messages"
    assert handler.api_key_header == "test-secret-never-log"
    assert handler.version_header == "2023-06-01"
    payload = handler.payload
    assert payload is not None
    assert set(payload) == {
        "model",
        "max_tokens",
        "system",
        "messages",
        "output_config",
    }
    assert "temperature" not in payload
    assert "thinking" not in payload
    output_config = cast(dict[str, object], payload["output_config"])
    format_control = cast(dict[str, object], output_config["format"])
    assert format_control["type"] == "json_schema"

    assert getattr(result, "finish_reason") == "stop"
    assert getattr(result, "content_is_text") is True
    assert getattr(result, "input_tokens") == 42
    assert getattr(result, "output_tokens") == 5
    assert getattr(result, "cached_input_tokens") == 12


def test_refusal_stop_reason_maps_to_content_filter() -> None:
    class RefusalHandler(FakeMessagesHandler):
        payload: ClassVar[dict[str, object] | None] = None
        stop_reason: ClassVar[str] = "refusal"
        content_blocks: ClassVar[list[dict[str, object]]] = []

    result, _handler = _run_loopback(RefusalHandler)
    assert getattr(result, "finish_reason") == "content_filter"
    assert getattr(result, "content_is_text") is False
    assert getattr(result, "content") == ""


def test_max_tokens_stop_reason_maps_to_length() -> None:
    class LengthHandler(FakeMessagesHandler):
        payload: ClassVar[dict[str, object] | None] = None
        stop_reason: ClassVar[str] = "max_tokens"

    result, _handler = _run_loopback(LengthHandler)
    assert getattr(result, "finish_reason") == "length"


def test_build_provider_selects_wire_protocol_by_domain() -> None:
    environ = {
        "ANTHROPIC_API_KEY": "test-key-not-real",
        "FIREWORKS_API_KEY": "test-key-not-real",
    }
    anthropic_config = LLMConfig(
        base_url=PROVIDER_BASE_URLS["anthropic"],
        model=_MODEL,
        api_key_env_name="ANTHROPIC_API_KEY",
    )
    assert isinstance(
        build_provider(anthropic_config, environ),
        AnthropicMessagesProvider,
    )
    fireworks_config = LLMConfig(
        base_url=PROVIDER_BASE_URLS["fireworks"],
        model="accounts/fireworks/models/kimi-k3",
    )
    assert isinstance(
        build_provider(fireworks_config, environ),
        OpenAICompatibleProvider,
    )


def test_anthropic_provider_refuses_nonzero_temperature() -> None:
    config = LLMConfig(
        base_url=PROVIDER_BASE_URLS["anthropic"],
        model=_MODEL,
        temperature="0.2",
        api_key_env_name="ANTHROPIC_API_KEY",
    )
    with pytest.raises(ProviderError, match="sampling parameters"):
        AnthropicMessagesProvider.from_env(
            config,
            {"ANTHROPIC_API_KEY": "test-key-not-real"},
        )


def test_config_from_env_provider_presets() -> None:
    config = LLMConfig.from_env(
        {
            "TRADEVOLVE_LLM_PROVIDER": "anthropic",
            "TRADEVOLVE_LLM_MODEL": _MODEL,
        }
    )
    assert config.base_url == PROVIDER_BASE_URLS["anthropic"]
    assert config.api_key_env_name == "ANTHROPIC_API_KEY"

    openrouter = LLMConfig.from_env(
        {
            "TRADEVOLVE_LLM_PROVIDER": "openrouter",
            "TRADEVOLVE_LLM_MODEL": "moonshotai/kimi-k3",
        }
    )
    assert openrouter.api_key_env_name == "OPENROUTER_API_KEY"

    with pytest.raises(ValueError, match="unknown TRADEVOLVE_LLM_PROVIDER"):
        LLMConfig.from_env(
            {
                "TRADEVOLVE_LLM_PROVIDER": "totally-unknown",
                "TRADEVOLVE_LLM_MODEL": _MODEL,
            }
        )
