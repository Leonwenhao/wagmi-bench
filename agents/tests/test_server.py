# SPDX-License-Identifier: Apache-2.0
"""Local IC-6 server integration tests."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from agents.common import JsonObject
from agents.llm import (
    InvalidActionEvidence,
    InvalidProviderAction,
    ProviderError,
)
from agents.reckless import RecklessPolicy
from agents.server import make_server
from agents.tests.helpers import runner_request
from core.action import parse_action
from core.config import EpisodeConfig
from core.engine import run_episode
from harness.http import HTTPAgent, HTTPAgentSecretError


def test_health_and_decide_routes_are_container_contract_compatible() -> None:
    server = make_server(RecklessPolicy(probe_turns=()))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = cast(tuple[str, int], server.server_address)
    origin = f"http://{host}:{port}"
    try:
        with urllib.request.urlopen(origin + "/healthz", timeout=2) as response:
            health = json.loads(response.read().decode("utf-8"))
        reply = HTTPAgent(origin).decide(runner_request(turn=0))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert health == {"status": "ok"}
    assert reply.http_status == 200
    parsed = parse_action(reply.body, ("BTC",))
    assert parsed.accepted


def test_invalid_wrapper_gets_generic_400_without_echo() -> None:
    server = make_server(RecklessPolicy(probe_turns=()))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = cast(tuple[str, int], server.server_address)
    request = urllib.request.Request(
        f"http://{host}:{port}/decide",
        data=b'{"secret":"must-not-echo"}',
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        try:
            urllib.request.urlopen(request, timeout=2)
        except urllib.error.HTTPError as exc:
            status = exc.code
            body = exc.read()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert status == 400
    assert json.loads(body) == {"error": "invalid_contract"}
    assert b"must-not-echo" not in body


class _InvalidProviderPolicy:
    def __init__(self, content: str) -> None:
        self.content = content

    def decide(self, request: Mapping[str, object]) -> JsonObject:
        del request
        raise InvalidProviderAction(
            InvalidActionEvidence(
                provider_output=self.content,
                input_tokens=123,
                output_tokens=17,
                finish_reason="stop",
                reason="action_contract_invalid",
                cached_input_tokens=23,
            )
        )


def test_invalid_provider_action_gets_bounded_usage_bearing_400() -> None:
    content = '```json\\n{"not":"an action"}\\n```'
    server = make_server(_InvalidProviderPolicy(content))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = cast(tuple[str, int], server.server_address)
    try:
        reply = HTTPAgent(f"http://{host}:{port}").decide(
            runner_request(turn=0)
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    payload = cast(dict[str, object], json.loads(reply.body))
    assert reply.http_status == 400
    assert payload["error"] == "invalid_contract"
    assert payload["provider_output"] == content
    assert payload["provider_output_bytes"] == len(content.encode("utf-8"))
    assert payload["reason"] == "action_contract_invalid"
    assert payload["usage"] == {
        "cached_input_tokens": 23,
        "input_tokens": 123,
        "output_tokens": 17,
    }


def test_oversize_invalid_provider_output_is_hashed_not_truncated() -> None:
    content = "x" * 70_000
    server = make_server(_InvalidProviderPolicy(content))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = cast(tuple[str, int], server.server_address)
    try:
        reply = HTTPAgent(f"http://{host}:{port}").decide(
            runner_request(turn=0)
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    payload = cast(dict[str, object], json.loads(reply.body))
    assert reply.http_status == 400
    assert len(reply.body) <= 65_536
    assert "provider_output" not in payload
    assert payload["provider_output_omitted"] == "oversize"
    assert payload["provider_output_bytes"] == 70_000
    assert payload["usage"] == {
        "cached_input_tokens": 23,
        "input_tokens": 123,
        "output_tokens": 17,
    }


def test_non_utf8_scalar_provider_output_fails_closed_with_usage() -> None:
    server = make_server(_InvalidProviderPolicy("\ud800"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = cast(tuple[str, int], server.server_address)
    try:
        reply = HTTPAgent(f"http://{host}:{port}").decide(
            runner_request(turn=0)
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    payload = cast(dict[str, object], json.loads(reply.body))
    assert reply.http_status == 400
    assert payload["provider_output_omitted"] == "non_utf8"
    assert payload["provider_output_encoding"] == "json_string_ascii"
    assert payload["usage"] == {
        "cached_input_tokens": 23,
        "input_tokens": 123,
        "output_tokens": 17,
    }


def test_invalid_provider_usage_survives_http_retry_and_event_capture() -> None:
    server = make_server(_InvalidProviderPolicy('{"not":"action/v1"}'))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = cast(tuple[str, int], server.server_address)
    try:
        result = run_episode(
            pack_dir=Path(__file__).resolve().parents[2]
            / "fixtures/golden-mini/pack",
            agent=HTTPAgent(f"http://{host}:{port}"),
            config=EpisodeConfig(response_deadline_ms=1_000),
            run_id="run_7171717171717171",
            episode_id="ep_7171717171717171",
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    responded = [
        cast(dict[str, object], event["payload"])
        for event in result.events
        if event["type"] == "AgentResponded"
    ]
    rejected = [
        cast(dict[str, object], event["payload"])
        for event in result.events
        if event["type"] == "ActionRejected"
    ]
    assert len(responded) == 26
    assert all(payload["http_status"] == 400 for payload in responded)
    assert all(
        payload["token_usage"]
        == {"input_tokens": 123, "output_tokens": 17}
        for payload in responded
    )
    assert len(rejected) == 13
    assert {payload["reason"] for payload in rejected} == {"schema_invalid"}


def test_invalid_provider_envelope_with_credential_is_never_evidence() -> None:
    canary = "TE_SYNTHETIC_SECRET_CANARY_2b12d8fb"
    server = make_server(_InvalidProviderPolicy(canary))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = cast(tuple[str, int], server.server_address)
    try:
        result = run_episode(
            pack_dir=Path(__file__).resolve().parents[2]
            / "fixtures/golden-mini/pack",
            agent=HTTPAgent(
                f"http://{host}:{port}",
                protected_response_values=(canary,),
            ),
            config=EpisodeConfig(response_deadline_ms=1_000),
            run_id="run_7272727272727272",
            episode_id="ep_7272727272727272",
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert result.raw_blobs == {}
    assert not any(
        event["type"] == "AgentResponded"
        for event in result.events
    )
    rejected = [
        cast(dict[str, object], event["payload"])
        for event in result.events
        if event["type"] == "ActionRejected"
    ]
    assert len(rejected) == 13
    assert all(payload["reason"] == "agent_error" for payload in rejected)
    assert all(
        payload["detail"]
        == "agent transport failed: HTTPAgentSecretError"
        for payload in rejected
    )
    assert HTTPAgentSecretError.__name__ not in json.dumps(result.raw_blobs)


class _ProviderFailurePolicy:
    def decide(self, request: Mapping[str, object]) -> JsonObject:
        del request
        raise ProviderError("upstream private detail")


def test_provider_failure_remains_generic_503_without_echo() -> None:
    server = make_server(_ProviderFailurePolicy())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = cast(tuple[str, int], server.server_address)
    request = urllib.request.Request(
        f"http://{host}:{port}/decide",
        data=json.dumps(runner_request(turn=0)).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        try:
            urllib.request.urlopen(request, timeout=2)
        except urllib.error.HTTPError as exc:
            status = exc.code
            body = exc.read()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert status == 503
    assert json.loads(body) == {"error": "decision_unavailable"}
    assert b"private" not in body
